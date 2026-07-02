#!/usr/bin/env python3
"""Run one 10-K insight-discovery task with Gemini Deep Research."""

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urlparse

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(path: str) -> None:
        env_path = Path(path)
        if not env_path.exists():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.output import (  # noqa: E402
    add_usage,
    build_task,
    default_output_file,
    init_tool_usage,
    normalize_output,
    output_is_acceptable,
    usage_to_dict,
    write_outputs,
)
from insights_discovery.common.data_package import (  # noqa: E402
    export_10k_company_package,
)
from insights_discovery.common.tools import build_gemini_mcp_tools  # noqa: E402


class ApiHTTPError(RuntimeError):
    """HTTP error from the Gemini Interactions endpoint."""

    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {body[:2000]}")


INSIGHT_MINING_SYSTEM_PROMPT = """You are an autonomous financial data exploration agent. Your job is to deeply explore the available data for the given 10-K company task and produce as many concrete, evidence-grounded insights as possible.

Use the provided MCP server silently as needed. If code execution is available, use it only for calculations, comparisons, aggregation, or consistency checks over data retrieved from MCP. Then return one final plain-text JSON object.

Exploration requirements:
1. First inspect what data is available through the MCP `search` and `fetch` tools.
2. Explore broadly across business description, risk factors, MD&A, financial statements, notes, audit matters, segment data, liquidity, debt, revenue, costs, operations, customers, suppliers, regulation, litigation, taxes, accounting estimates, and recent trends.
3. Look for findings that go beyond restating section headings: cross-link facts across tables or filing sections, surface unusual changes, hidden dependencies, accounting judgments, risk transmission paths, mismatches between narrative and numbers, and details that would be easy to miss in a shallow reading.
4. Base the insights on company-specific evidence from MCP `search` and `fetch`. Do not rely on built-in browsing, external websites, generic knowledge, or memorized facts as the primary source.
5. {web_policy}
6. Every insight must include at least one evidence item whose source is sqlite or mcp.
7. Make sure the company identity matches CIK {cik}. Do not analyze another company.
8. Produce at least {min_insights} distinct insights. More is better if they are not duplicates.
9. Each insight must be specific, factual, and useful for later QA evaluation. Preserve important numbers, dates, trends, comparisons, and caveats.
10. Do not include unsupported guesses. If evidence is partial, say exactly what the evidence supports and why the pattern matters.

Final output requirements:
Return only plain text containing valid JSON. Do not wrap it in markdown. This runner parses your text response as JSON after the call completes. Use this exact object shape:
{{
  "task": "{task}",
  "cik": "{cik}",
  "insights": [
    {{
      "id": 1,
      "topic": "short topic label",
      "insight": "one self-contained insight with key facts, numbers, dates, and caveats",
      "evidence": [
        {{
          "source": "{source_schema}",
          "reference": "table/path/url/record id if available"
        }}
      ]
    }}
  ],
  "summary": "brief synthesis of the most important findings"
}}

The insights array must contain at least {min_insights} items."""


DATA_PACKAGE_PROMPT = """

Additional uploaded-data-package mode:
- A per-CIK DDR_Bench data package has been uploaded as document inputs.
- The package contains schema/documentation JSON, company metadata JSON, filings JSONL, financial_facts JSONL, and summary JSON/CSV exported from `data/10k/raw/10k_financial_data.db` for CIK {cik}.
- First inspect the small summary JSON/CSV to understand row counts, coverage, top facts, core FY trends, and candidate YoY changes.
- Then use the raw JSON/JSONL document inputs as needed to verify numbers and build evidence-grounded insights.
- You may cite uploaded-file evidence with source "sqlite" because the files are direct SQLite exports. References should name the table/file and a precise filter/query.
"""


def build_insight_mining_prompt(
    task: str,
    cik: str,
    min_insights: int,
    allow_web_search: bool = False,
    use_data_package: bool = False,
    data_package_only: bool = False,
) -> str:
    if data_package_only:
        web_policy = "Use only the uploaded SQLite-export document inputs. Do not cite web sources or MCP sources."
    elif use_data_package:
        web_policy = (
            "Use MCP search/fetch evidence and the uploaded SQLite-export document inputs. "
            "Do not cite web sources in the final insights."
        )
    elif allow_web_search:
        web_policy = "External web context is available only as secondary context; final evidence must still come from MCP search/fetch."
    else:
        web_policy = "Use only MCP search/fetch evidence. Do not cite web sources in the final insights."
    prompt = INSIGHT_MINING_SYSTEM_PROMPT.format(
        task=task,
        cik=cik or "",
        min_insights=min_insights,
        web_policy=web_policy,
        source_schema="sqlite|mcp",
    )
    if use_data_package:
        prompt += DATA_PACKAGE_PROMPT.format(cik=cik or "")
    return prompt


def output_path(args: argparse.Namespace) -> Path:
    return default_output_file(args.output_dir, args.output_file or "", args.cik or "")


def dump_raw_response(args: argparse.Namespace, response: Dict[str, Any], step: int) -> None:
    if not args.dump_raw_response:
        return
    raw_path = output_path(args).with_name(f"{output_path(args).stem}_raw_step_{step}.json")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as f:
        json.dump(response, f, ensure_ascii=False, indent=2)


def write_error_output(
    args: argparse.Namespace,
    task: str,
    usage_total: Dict[str, int],
    usage_by_step: List[Dict[str, int]],
    tool_usage: Dict[str, Dict[str, int]],
    model_calls: Dict[str, int],
    step: int,
    error: Exception,
) -> Dict[str, str]:
    data = {
        "task": task,
        "cik": args.cik or "",
        "insights": [],
        "summary": "",
        "error": {"type": error.__class__.__name__, "message": str(error), "failed_at_step": step},
        "token_usage": usage_total,
        "token_usage_by_step": usage_by_step,
        "tool_usage": tool_usage,
        "model_calls": model_calls,
    }
    return write_outputs(data, output_path(args))


def field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def interaction_id(interaction: Any) -> str:
    return str(field(interaction, "id", "") or field(interaction, "name", "")).split("/")[-1]


def interaction_status(interaction: Dict[str, Any]) -> str:
    return str(field(interaction, "status", "") or "").lower()


def iter_text_content(value: Any) -> List[str]:
    chunks: List[str] = []
    if isinstance(value, dict):
        content_type = value.get("type")
        text = value.get("text")
        if isinstance(text, str) and (content_type in {None, "text"} or text.strip()):
            chunks.append(text)
        for child_key in ["content", "parts", "steps", "candidates", "message", "output"]:
            child = value.get(child_key)
            if child is not None:
                chunks.extend(iter_text_content(child))
    elif isinstance(value, list):
        for item in value:
            chunks.extend(iter_text_content(item))
    return chunks


def interaction_output_text(interaction: Dict[str, Any]) -> str:
    steps = field(interaction, "steps", []) or []
    if steps:
        for step in reversed(steps):
            text = "\n".join(iter_text_content(field(step, "content", []))).strip()
            if text:
                return text
    return "\n".join(iter_text_content(interaction)).strip()


def raise_for_interaction_failure(interaction: Dict[str, Any]) -> None:
    status = interaction_status(interaction)
    if status not in {"failed", "cancelled", "expired"}:
        return
    error = field(interaction, "error") or {}
    if isinstance(error, dict) and error:
        code = error.get("code", "unknown_error")
        message = error.get("message", "")
        raise RuntimeError(f"Gemini interaction returned status={status}, code={code}: {message}")
    raise RuntimeError(f"Gemini interaction returned status={status}: {interaction}")


def record_tool_usage_from_interaction(tool_usage: Dict[str, Dict[str, int]], value: Any) -> None:
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("tool_name") or value.get("function_name") or "")
        item_type = str(value.get("type") or value.get("tool_type") or "")
        candidate = name or item_type
        if candidate in {"search", "fetch"} or item_type in {"code_execution", "code_interpreter"}:
            normalized = "code_interpreter" if item_type == "code_execution" else candidate
            stats = tool_usage.setdefault(normalized, {"calls": 0, "successes": 0, "failures": 0})
            stats["calls"] += 1
            status = str(value.get("status") or "").lower()
            if status in {"failed", "error", "cancelled"} or value.get("error"):
                stats["failures"] += 1
            else:
                stats["successes"] += 1
        for child in value.values():
            record_tool_usage_from_interaction(tool_usage, child)
    elif isinstance(value, list):
        for item in value:
            record_tool_usage_from_interaction(tool_usage, item)


def gemini_usage_to_dict(interaction: Dict[str, Any]) -> Dict[str, int]:
    usage = (
        field(interaction, "usage")
        or field(interaction, "usage_metadata")
        or field(interaction, "usageMetadata")
        or {}
    )
    if isinstance(usage, dict):
        prompt_tokens = usage.get("promptTokenCount", usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        completion_tokens = usage.get(
            "candidatesTokenCount",
            usage.get("completion_tokens", usage.get("output_tokens", 0)),
        )
        total_tokens = usage.get("totalTokenCount", usage.get("total_tokens", 0))
        if prompt_tokens or completion_tokens or total_tokens:
            return {
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or int(prompt_tokens or 0) + int(completion_tokens or 0)),
            }
    return usage_to_dict(usage)


def interactions_endpoint(base_url: str) -> str:
    normalized = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    if normalized.endswith("/interactions"):
        return normalized
    return f"{normalized}/interactions"


def files_upload_endpoint(base_url: str) -> str:
    parsed = urlparse((base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/"))
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or "generativelanguage.googleapis.com"
    path = parsed.path.rstrip("/")
    if path.endswith("/interactions"):
        path = path[: -len("/interactions")]
    if path.startswith("/upload/"):
        return f"{scheme}://{netloc}{path}/files"
    if path:
        return f"{scheme}://{netloc}/upload{path}/files"
    return f"{scheme}://{netloc}/upload/v1beta/files"


def interaction_endpoint(base_url: str, id_value: str) -> str:
    return f"{interactions_endpoint(base_url)}/{quote(id_value, safe='')}"


def validate_mcp_url_for_gemini(mcp_url: str) -> None:
    parsed = urlparse(mcp_url or "")
    if parsed.scheme != "https":
        raise ValueError(
            "Gemini remote MCP requires a public HTTPS MCP URL. "
            f"Got {mcp_url!r}. Use a URL like https://<public-domain>/sse."
        )
    host = (parsed.hostname or "").lower()
    if host in {"localhost"} or host.endswith(".localhost"):
        raise ValueError(f"MCP URL host is local-only and unsafe for Gemini remote access: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip in cgnat:
        raise ValueError(f"MCP URL uses a non-public IP address that Gemini cannot reach: {mcp_url!r}")


def gemini_headers(args: argparse.Namespace) -> Dict[str, str]:
    api_key = args.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("MODEL_API_KEY")
    if not api_key:
        raise ValueError("Gemini API key is required via --api-key, GEMINI_API_KEY, GOOGLE_API_KEY, or MODEL_API_KEY.")
    return {"Content-Type": "application/json", "x-goog-api-key": api_key}


def gemini_api_key(args: argparse.Namespace) -> str:
    api_key = args.api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("MODEL_API_KEY")
    if not api_key:
        raise ValueError("Gemini API key is required via --api-key, GEMINI_API_KEY, GOOGLE_API_KEY, or MODEL_API_KEY.")
    return api_key


def document_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "text/csv"
    if suffix in {".json", ".jsonl", ".txt", ".md"}:
        return "text/plain"
    return "text/plain"


def upload_gemini_file(args: argparse.Namespace, path: Path) -> Dict[str, str]:
    data = path.read_bytes()
    mime_type = document_mime_type(path)
    start_headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": gemini_api_key(args),
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
    }
    metadata = json.dumps({"file": {"display_name": path.name}}).encode("utf-8")
    start_request = urllib.request.Request(
        files_upload_endpoint(args.base_url),
        headers=start_headers,
        data=metadata,
        method="POST",
    )
    try:
        with urllib.request.urlopen(start_request, timeout=args.request_timeout) as response:
            upload_url = response.headers.get("x-goog-upload-url")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError("POST", files_upload_endpoint(args.base_url), exc.code, error_body) from exc
    if not upload_url:
        raise RuntimeError(f"Gemini upload start response did not include x-goog-upload-url for {path}")

    upload_headers = {
        "Content-Length": str(len(data)),
        "X-Goog-Upload-Offset": "0",
        "X-Goog-Upload-Command": "upload, finalize",
    }
    upload_request = urllib.request.Request(upload_url, headers=upload_headers, data=data, method="POST")
    try:
        with urllib.request.urlopen(upload_request, timeout=None if args.request_timeout <= 0 else args.request_timeout) as response:
            file_info = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError("POST", upload_url, exc.code, error_body) from exc
    file_obj = file_info.get("file", file_info)
    uri = file_obj.get("uri")
    name = file_obj.get("name")
    returned_mime_type = file_obj.get("mimeType") or file_obj.get("mime_type") or mime_type
    if not uri:
        raise RuntimeError(f"Gemini file upload response did not include uri for {path}: {file_info}")
    return {"name": str(name or ""), "uri": str(uri), "mime_type": str(returned_mime_type), "path": str(path)}


def build_tools(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.data_package_only:
        tools: List[Dict[str, Any]] = []
        if args.allow_web_search:
            tools.extend([{"type": "google_search"}, {"type": "url_context"}])
        if args.use_code_execution:
            tools.append({"type": "code_execution"})
        return tools
    return build_gemini_mcp_tools(
        mcp_url=args.mcp_url,
        include_code_execution=args.use_code_execution,
        include_web_search=args.allow_web_search,
    )


def build_interaction_input(input_text: str, uploaded_files: List[Dict[str, str]]) -> Any:
    if not uploaded_files:
        return input_text
    file_parts = [
        {"type": "document", "uri": item["uri"], "mime_type": item["mime_type"]}
        for item in uploaded_files
    ]
    return [*file_parts, {"type": "text", "text": input_text}]


def request_json(
    args: argparse.Namespace,
    method: str,
    url: str,
    payload: Dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, headers=gemini_headers(args), data=data, method=method)
    actual_timeout = args.request_timeout if timeout is None else (None if timeout <= 0 else timeout)
    try:
        with urllib.request.urlopen(request, timeout=actual_timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError(method, url, exc.code, error_body) from exc
    return json.loads(response_body)


def is_retryable_http_error(exc: ApiHTTPError) -> bool:
    if exc.status_code in {408, 409, 425, 429}:
        return True
    return 500 <= exc.status_code <= 599


def request_json_with_retries(
    args: argparse.Namespace,
    method: str,
    url: str,
    payload: Dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, args.request_retries + 2):
        try:
            return request_json(args, method, url, payload, timeout=timeout)
        except (TimeoutError, urllib.error.URLError, ApiHTTPError) as exc:
            if isinstance(exc, ApiHTTPError) and not is_retryable_http_error(exc):
                raise
            last_exc = exc
            if attempt > args.request_retries:
                break
            delay = min(args.retry_max_delay, args.retry_initial_delay * (2 ** (attempt - 1)))
            print(f"{method} {url} timed out or failed ({exc}); retrying in {delay:g}s [{attempt}/{args.request_retries}]")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def create_interaction(args: argparse.Namespace, payload: Dict[str, Any]) -> Dict[str, Any]:
    interaction = request_json_with_retries(
        args,
        "POST",
        interactions_endpoint(args.base_url),
        {**payload, "background": True},
        timeout=args.create_request_timeout,
    )
    id_value = interaction_id(interaction)
    if not id_value:
        return interaction

    deadline = None if args.api_timeout <= 0 else time.monotonic() + args.api_timeout
    status = interaction_status(interaction)
    while status not in {"completed", "failed", "cancelled", "expired"}:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                f"Interaction {id_value} did not complete within {args.api_timeout:.0f}s "
                f"(last status: {status or 'unknown'})."
            )
        if args.poll_status:
            print(f"Interaction {id_value} status={status or 'unknown'}; polling again in {args.poll_interval:g}s")
        time.sleep(args.poll_interval)
        interaction = request_json_with_retries(args, "GET", interaction_endpoint(args.base_url, id_value))
        status = interaction_status(interaction)
    return interaction


def run_agent(args: argparse.Namespace) -> str:
    load_dotenv(args.env_file)
    if not args.data_package_only:
        validate_mcp_url_for_gemini(args.mcp_url)
    task = build_task(args.cik or "", args.question or "")
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_by_step: List[Dict[str, int]] = []
    tool_usage = init_tool_usage()
    model_calls = {"attempts": 0, "completed": 0, "failures": 0}
    data_package_paths: List[Path] = []
    uploaded_files: List[Dict[str, str]] = []

    if args.use_data_package:
        package_dir = output_path(args).parent / "gemini_input"
        data_package_paths = export_10k_company_package(args.db, args.cik or "", package_dir)
        uploaded_files = [upload_gemini_file(args, path) for path in data_package_paths]

    instructions = build_insight_mining_prompt(
        task,
        args.cik or "",
        args.min_insights,
        allow_web_search=args.allow_web_search,
        use_data_package=args.use_data_package,
        data_package_only=args.data_package_only,
    )
    input_text = (
        f"{instructions}\n\nStart exploring now. Final output must be valid JSON with at least "
        f"{args.min_insights} insights for this task: {task}"
    )
    if args.use_data_package:
        input_text += (
            "\n\nThe uploaded SQLite-export document inputs are attached before this text. "
            "Use the summary JSON/CSV first, then verify details against the raw JSON/JSONL files. "
            "Uploaded file URIs: "
            + ", ".join(item["uri"] for item in uploaded_files)
        )
    tools = build_tools(args)

    previous_interaction_id = ""
    for step in range(args.max_steps):
        model_calls["attempts"] += 1
        try:
            agent_config = {"type": "deep-research"}
            if args.thinking_summaries != "none":
                agent_config["thinking_summaries"] = args.thinking_summaries
            payload = {
                "agent": args.model,
                "input": build_interaction_input(input_text, uploaded_files),
                "agent_config": agent_config,
            }
            if tools:
                payload["tools"] = tools
            if previous_interaction_id:
                payload["previous_interaction_id"] = previous_interaction_id
            interaction = create_interaction(args, payload)
            dump_raw_response(args, interaction, step + 1)
            raise_for_interaction_failure(interaction)
        except Exception as exc:
            model_calls["failures"] += 1
            paths = write_error_output(args, task, usage_total, usage_by_step, tool_usage, model_calls, step + 1, exc)
            print(f"Model call failed at step {step + 1}. Partial diagnostics saved to {paths['json']} and {paths['csv']}")
            raise

        model_calls["completed"] += 1
        previous_interaction_id = interaction_id(interaction) or previous_interaction_id
        record_tool_usage_from_interaction(tool_usage, interaction)
        step_usage = gemini_usage_to_dict(interaction)
        add_usage(usage_total, step_usage)
        usage_by_step.append({"step": step + 1, **step_usage})

        raw_text = interaction_output_text(interaction)
        data = normalize_output(raw_text, task, args.cik or "")
        acceptable, reason = output_is_acceptable(
            data,
            tool_usage,
            min_data_tool_calls=0 if args.data_package_only else args.min_data_tool_calls,
            allow_web_search=args.allow_web_search,
        )
        if not acceptable and step < args.max_steps - 1:
            input_text = (
                "The previous answer is not acceptable for this DDR_Bench run. "
                f"Reason: {reason} Use MCP search/fetch now, then return a revised JSON. "
                "Do not use web-only or memorized evidence."
            )
            continue

        data["token_usage"] = usage_total
        data["token_usage_by_step"] = usage_by_step
        data["tool_usage"] = tool_usage
        data["model_calls"] = model_calls
        if args.use_data_package:
            data["data_package"] = {
                "input_files": [str(path) for path in data_package_paths],
                "uploaded_files": uploaded_files,
                "mode": "data_package_only" if args.data_package_only else "mcp_plus_data_package",
            }
        if not acceptable:
            data["warning"] = reason
        paths = write_outputs(data, output_path(args))
        return f"Saved {len(data.get('insights', []))} insights to {paths['json']} and {paths['csv']}"

    data = normalize_output("", task, args.cik or "")
    data["warning"] = "Reached max_steps before the model produced a final answer."
    data["token_usage"] = usage_total
    data["token_usage_by_step"] = usage_by_step
    data["tool_usage"] = tool_usage
    data["model_calls"] = model_calls
    paths = write_outputs(data, output_path(args))
    return f"Reached max_steps. Saved partial output to {paths['json']} and {paths['csv']}."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini Deep Research 10-K insight discovery runner")
    parser.add_argument("--base-url", default=os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"))
    parser.add_argument("--api-key", default=os.getenv("GEMINI_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("GEMINI_DEEP_RESEARCH_AGENT", "deep-research-preview-04-2026"))
    parser.add_argument("--mcp-url", default=os.getenv("SQLITE_MCP_URL", "http://127.0.0.1:8765/sse"))
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--file-root", action="append", help="Accepted for CLI parity; Gemini remote MCP cannot read local paths directly.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.2, help="Accepted for CLI parity; Deep Research ignores this option.")
    parser.add_argument("--cik", help="10-K company CIK. Builds the task: Analyze company with CIK {cik}")
    parser.add_argument("--question", help="Optional custom task override. If omitted, --cik is required.")
    parser.add_argument("--min-insights", type=int, default=10, help="Minimum number of insights requested.")
    parser.add_argument("--output-dir", default="./outputs/gemini_deep_research")
    parser.add_argument("--output-file", help="Exact JSON output path. CSV is written next to it.")
    parser.add_argument("--api-timeout", type=float, default=14400.0, help="Overall wait time per interaction. Use 0 to wait indefinitely.")
    parser.add_argument("--request-timeout", type=float, default=60.0, help="HTTP timeout for each retrieve/poll request.")
    parser.add_argument("--create-request-timeout", type=float, default=600.0, help="HTTP timeout for the initial interaction creation request.")
    parser.add_argument("--request-retries", type=int, default=5, help="Retries for transient HTTP timeouts or URL errors.")
    parser.add_argument("--retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--retry-max-delay", type=float, default=60.0)
    parser.add_argument("--background", action=argparse.BooleanOptionalAction, default=True, help="Accepted for CLI parity; Gemini Deep Research always uses background mode.")
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--poll-status", action="store_true", help="Print background interaction status while polling.")
    parser.add_argument("--allow-web-search", action="store_true")
    parser.add_argument("--use-code-execution", action="store_true")
    parser.add_argument("--use-code-interpreter", dest="use_code_execution", action="store_true", help="Alias for OpenAI runner parity.")
    parser.add_argument("--use-data-package", action="store_true", help="Export target CIK rows, upload them via Gemini Files API, and attach them as document inputs.")
    parser.add_argument("--data-package-only", action="store_true", help="Use only uploaded SQLite-export document inputs; do not expose the MCP server.")
    parser.add_argument("--thinking-summaries", default="auto", choices=["auto", "none"])
    parser.add_argument("--dump-raw-response", action="store_true")
    parser.add_argument("--min-data-tool-calls", type=int, default=1)
    args = parser.parse_args()
    if args.data_package_only:
        args.use_data_package = True
    if args.use_data_package and not args.cik:
        parser.error("--use-data-package requires --cik so the SQLite export can be scoped to one company.")
    return args


if __name__ == "__main__":
    print(run_agent(parse_args()))
