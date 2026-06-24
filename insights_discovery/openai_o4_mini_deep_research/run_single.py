#!/usr/bin/env python3
"""Run one 10-K insight-discovery task with an OpenAI-compatible chat model."""

import argparse
import base64
import http.client
import json
import mimetypes
import os
import sys
import ipaddress
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlparse
from pathlib import Path
from typing import Any, Dict, List

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
    existing_10k_company_package,
    export_10k_company_package,
    resolve_package_dir,
)
from insights_discovery.common.tools import build_openai_mcp_tools  # noqa: E402


class ApiHTTPError(RuntimeError):
    """HTTP error from the OpenAI-compatible Responses endpoint."""

    def __init__(self, method: str, url: str, status_code: int, body: str):
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {body[:2000]}")


TRANSIENT_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    BrokenPipeError,
    http.client.RemoteDisconnected,
    urllib.error.URLError,
    json.JSONDecodeError,
)
RETRY_EXCEPTIONS = TRANSIENT_EXCEPTIONS + (ApiHTTPError,)


INSIGHT_MINING_SYSTEM_PROMPT = """You are an autonomous financial data exploration agent. Your job is to deeply explore the available data for the given 10-K company task and produce as many concrete, evidence-grounded insights as possible.

Use the provided MCP server silently as needed. If code interpreter is available, use it only for calculations, comparisons, aggregation, or consistency checks over data retrieved from MCP. Then return one final plain-text JSON object.

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
Return only plain text containing valid JSON. Do not wrap it in markdown. This runner does not use the Responses API structured-output feature; it parses your text response as JSON after the call completes. Use this exact object shape:
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
- A per-CIK DDR_Bench data package has been attached to the Responses input as `input_file` items.
- The package contains schema/documentation JSON, company metadata JSON, filings JSONL, financial_facts JSONL, and summary JSON/CSV exported from `data/10k/raw/10k_financial_data.db` for CIK {cik}.
- Inspect the attached files directly. Use code interpreter only for calculations if the attached data is available to it; otherwise reason from the file contents provided in the input.
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
        web_policy = "Use only the uploaded SQLite-export files and python-tool calculations. Do not cite web sources or MCP sources."
    elif use_data_package:
        web_policy = (
            "Use MCP search/fetch evidence and the uploaded SQLite-export files. "
            "Do not cite web sources in the final insights."
        )
    elif allow_web_search:
        web_policy = "External web context is not exposed as a tool in this runner; use only MCP search/fetch evidence."
    else:
        web_policy = "Use only MCP search/fetch evidence. Do not cite web sources in the final insights."
    source_schema = "sqlite|mcp"
    prompt = INSIGHT_MINING_SYSTEM_PROMPT.format(
        task=task,
        cik=cik or "",
        min_insights=min_insights,
        web_policy=web_policy,
        source_schema=source_schema,
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


def response_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def response_items(response: Any) -> List[Any]:
    return response_field(response, "output", []) or []


def item_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def response_output_text(response: Any) -> str:
    text = response_field(response, "output_text")
    if text:
        return text
    chunks = []
    for item in response_items(response):
        for content in item_field(item, "content", []) or []:
            content_text = item_field(content, "text")
            if content_text:
                chunks.append(content_text)
    return "\n".join(chunks)


def raise_for_response_failure(response: Dict[str, Any]) -> None:
    status = str(response_field(response, "status", "") or "").lower()
    if status not in {"failed", "cancelled", "incomplete"}:
        return
    error = response_field(response, "error") or {}
    incomplete_details = response_field(response, "incomplete_details") or {}
    if isinstance(error, dict) and error:
        code = error.get("code", "unknown_error")
        message = error.get("message", "")
        raise RuntimeError(f"/v1/responses returned status={status}, code={code}: {message}")
    raise RuntimeError(f"/v1/responses returned status={status}: {incomplete_details}")


def record_tool_usage_from_response(tool_usage: Dict[str, Dict[str, int]], response: Any) -> None:
    for item in response_items(response):
        item_type = item_field(item, "type", "")
        if "mcp" not in item_type and "tool" not in item_type and "code" not in item_type:
            continue
        name = item_field(item, "name") or item_field(item, "tool_name")
        if not name and "code" in item_type and "interpreter" in item_type:
            name = "code_interpreter"
        if name not in {"search", "fetch", "code_interpreter"}:
            continue
        stats = tool_usage.setdefault(name, {"calls": 0, "successes": 0, "failures": 0})
        stats["calls"] += 1
        status = str(item_field(item, "status", "") or "").lower()
        if status in {"failed", "error", "incomplete"}:
            stats["failures"] += 1
        else:
            stats["successes"] += 1


def responses_endpoint(base_url: str) -> str:
    normalized = (base_url or "https://api.openai.com/v1").rstrip("/")
    if normalized.endswith("/responses"):
        return normalized
    return f"{normalized}/responses"


def response_endpoint(base_url: str, response_id: str) -> str:
    return f"{responses_endpoint(base_url)}/{quote(response_id, safe='')}"


def validate_mcp_url_for_openai(mcp_url: str) -> None:
    parsed = urlparse(mcp_url or "")
    if parsed.scheme != "https":
        raise ValueError(
            "OpenAI remote MCP safety checks require a public HTTPS MCP URL. "
            f"Got {mcp_url!r}. Ask ops for an HTTPS URL like https://<public-domain>/sse."
        )
    host = (parsed.hostname or "").lower()
    if host in {"localhost"} or host.endswith(".localhost"):
        raise ValueError(f"MCP URL host is local-only and unsafe for OpenAI remote access: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip in cgnat:
        raise ValueError(
            "MCP URL uses a non-public IP address that OpenAI will reject or cannot reach: "
            f"{mcp_url!r}"
        )


def openai_headers(args: argparse.Namespace, idempotency_key: str = "") -> Dict[str, str]:
    api_key = args.api_key or os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY") or "EMPTY"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def files_endpoint(base_url: str) -> str:
    normalized = (base_url or "https://api.openai.com/v1").rstrip("/")
    if normalized.endswith("/files"):
        return normalized
    if normalized.endswith("/responses"):
        normalized = normalized[: -len("/responses")]
    return f"{normalized}/files"


def encode_multipart(fields: Dict[str, str], files: Dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----ddrbench-{uuid.uuid4().hex}"
    chunks: List[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    for name, path in files.items():
        filename = path.name
        content_type = supported_file_mime_type(path)
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def supported_file_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jsonl", ".ndjson", ".txt", ".log", ".md"}:
        return "text/plain"
    if suffix == ".json":
        return "application/json"
    if suffix == ".csv":
        return "text/csv"
    if suffix == ".tsv":
        return "text/tab-separated-values"
    return mimetypes.guess_type(path.name)[0] or "text/plain"


def upload_openai_file(args: argparse.Namespace, path: Path) -> str:
    fields = {"purpose": args.file_upload_purpose}
    if args.file_upload_model_field:
        fields["model"] = args.model
        fields["model_name"] = args.model
    body, boundary = encode_multipart(fields, {"file": path})
    headers = openai_headers(args)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    if args.file_upload_model_field:
        headers["X-Model"] = args.model
        headers["X-Model-Name"] = args.model
    upload_url = files_endpoint(args.base_url)
    if args.file_upload_model_field:
        separator = "&" if "?" in upload_url else "?"
        upload_url = f"{upload_url}{separator}{urlencode({'model': args.model, 'model_name': args.model})}"
    request = urllib.request.Request(
        upload_url,
        headers=headers,
        data=body,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.request_timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError("POST", upload_url, exc.code, error_body) from exc
    file_id = data.get("id")
    if not file_id:
        raise RuntimeError(f"File upload response did not include an id: {data}")
    return str(file_id)


def file_data_url(path: Path) -> str:
    content_type = supported_file_mime_type(path)
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def build_input(args: argparse.Namespace, input_text: str, data_package_paths: List[Path], file_ids: List[str]) -> Any:
    if not args.use_data_package:
        return input_text
    content: List[Dict[str, Any]] = []
    if args.file_input_mode == "upload":
        for file_id in file_ids:
            content.append({"type": "input_file", "file_id": file_id})
    elif args.file_input_mode == "base64":
        for path in data_package_paths:
            content.append({
                "type": "input_file",
                "filename": path.name,
                "file_data": file_data_url(path),
            })
    elif args.file_input_mode != "none":
        raise ValueError(f"Unsupported --file-input-mode: {args.file_input_mode}")
    content.append({"type": "input_text", "text": input_text})
    return [{"role": "user", "content": content}]


def build_tools(args: argparse.Namespace) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []
    if not args.data_package_only:
        tools.extend(build_openai_mcp_tools(mcp_url=args.mcp_url, include_code_interpreter=False))
    if args.use_code_interpreter:
        container: Dict[str, Any] = {"type": "auto"}
        if args.code_interpreter_memory_limit:
            container["memory_limit"] = args.code_interpreter_memory_limit
        tools.append({"type": "code_interpreter", "container": container})
    if not tools:
        raise ValueError("No tools configured. Disable --data-package-only or enable --use-code-interpreter/--use-data-package.")
    return tools


def request_json(
    args: argparse.Namespace,
    method: str,
    url: str,
    payload: Dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
    idempotency_key: str = "",
) -> Dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        headers=openai_headers(args, idempotency_key=idempotency_key),
        data=data,
        method=method,
    )
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
    idempotency_key: str = "",
) -> Dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, args.request_retries + 2):
        try:
            return request_json(
                args,
                method,
                url,
                payload,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        except RETRY_EXCEPTIONS as exc:
            if isinstance(exc, ApiHTTPError) and not is_retryable_http_error(exc):
                raise
            last_exc = exc
            if attempt > args.request_retries:
                break
            delay = min(args.retry_max_delay, args.retry_initial_delay * (2 ** (attempt - 1)))
            print(f"{method} {url} timed out or failed ({exc}); retrying in {delay:g}s [{attempt}/{args.request_retries}]")
            time.sleep(delay)
    assert last_exc is not None
    if isinstance(last_exc, http.client.RemoteDisconnected):
        raise RuntimeError(
            "The OpenAI-compatible gateway closed the Responses connection without a response. "
            "This is a gateway/proxy disconnect before the model response completed, not a local JSON parsing issue. "
            "Try --no-background --stream, the official OpenAI base URL, or increase the gateway upstream timeout."
        ) from last_exc
    raise last_exc


def stream_response_v1(args: argparse.Namespace, payload: Dict[str, Any], *, idempotency_key: str = "") -> Dict[str, Any]:
    stream_payload = {**payload, "stream": True}
    request = urllib.request.Request(
        responses_endpoint(args.base_url),
        headers={**openai_headers(args, idempotency_key=idempotency_key), "Accept": "text/event-stream"},
        data=json.dumps(stream_payload).encode("utf-8"),
        method="POST",
    )
    actual_timeout = None if args.api_timeout <= 0 else args.api_timeout
    text_chunks: List[str] = []
    completed_response: Dict[str, Any] | None = None
    event_name = ""
    data_lines: List[str] = []

    def handle_sse_event() -> None:
        nonlocal completed_response, event_name, data_lines
        if not data_lines:
            event_name = ""
            return
        data_text = "\n".join(data_lines)
        event_name = ""
        data_lines = []
        if data_text == "[DONE]":
            return
        event = json.loads(data_text)
        event_type = str(event.get("type") or event.get("event") or event_name or "").lower()
        if args.stream_status and event_type:
            print(f"stream event: {event_type}")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_chunks.append(delta)
            return
        if event_type in {
            "response.completed",
            "response.failed",
            "response.cancelled",
            "response.incomplete",
        }:
            response = event.get("response")
            completed_response = response if isinstance(response, dict) else event
            return
        if str(event.get("status", "")).lower() in {"completed", "failed", "cancelled", "incomplete"}:
            completed_response = event

    try:
        with urllib.request.urlopen(request, timeout=actual_timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    handle_sse_event()
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line.partition(":")[2].strip()
                elif line.startswith("data:"):
                    data_lines.append(line.partition(":")[2].lstrip())
            handle_sse_event()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError("POST", responses_endpoint(args.base_url), exc.code, error_body) from exc

    if completed_response is not None:
        return completed_response
    if text_chunks:
        return {"status": "completed", "output_text": "".join(text_chunks)}
    raise RuntimeError("Streaming Responses request ended without a completed response or output text.")


def stream_response_v1_with_retries(args: argparse.Namespace, payload: Dict[str, Any]) -> Dict[str, Any]:
    idempotency_key = f"ddrbench-{uuid.uuid4()}"
    last_exc: Exception | None = None
    for attempt in range(1, args.request_retries + 2):
        try:
            return stream_response_v1(args, payload, idempotency_key=idempotency_key)
        except RETRY_EXCEPTIONS as exc:
            if isinstance(exc, ApiHTTPError) and not is_retryable_http_error(exc):
                raise
            last_exc = exc
            if attempt > args.request_retries:
                break
            delay = min(args.retry_max_delay, args.retry_initial_delay * (2 ** (attempt - 1)))
            print(
                f"POST {responses_endpoint(args.base_url)} stream failed ({exc}); "
                f"retrying in {delay:g}s [{attempt}/{args.request_retries}]"
            )
            time.sleep(delay)
    assert last_exc is not None
    if isinstance(last_exc, http.client.RemoteDisconnected):
        raise RuntimeError(
            "The OpenAI-compatible gateway closed the streaming Responses connection without a response. "
            "This usually means the gateway/proxy does not support long Responses streaming for this model, "
            "or has an upstream idle/request timeout. Try the official OpenAI base URL, fix the gateway's "
            "streaming/timeout settings, or run with --use-data-package --data-package-only to avoid remote MCP."
        ) from last_exc
    raise last_exc


def response_status(response: Dict[str, Any]) -> str:
    return str(response_field(response, "status", "") or "").lower()


def create_response_v1(args: argparse.Namespace, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not args.background:
        if args.stream:
            return stream_response_v1_with_retries(args, payload)
        return request_json_with_retries(
            args,
            "POST",
            responses_endpoint(args.base_url),
            payload,
            timeout=args.api_timeout,
            idempotency_key=f"ddrbench-{uuid.uuid4()}",
        )

    background_payload = {**payload, "background": True}
    create_idempotency_key = f"ddrbench-{uuid.uuid4()}"
    try:
        response = request_json_with_retries(
            args,
            "POST",
            responses_endpoint(args.base_url),
            background_payload,
            timeout=args.create_request_timeout,
            idempotency_key=create_idempotency_key,
        )
    except RETRY_EXCEPTIONS as exc:
        if not args.background_fallback:
            raise
        print(
            "Background response creation failed after retries; "
            f"falling back to blocking Responses POST. Last error: {exc}"
        )
        if args.stream:
            return stream_response_v1_with_retries(args, payload)
        return request_json_with_retries(
            args,
            "POST",
            responses_endpoint(args.base_url),
            payload,
            timeout=args.api_timeout,
            idempotency_key=f"ddrbench-{uuid.uuid4()}",
        )
    response_id = response_field(response, "id")
    if not response_id:
        return response

    deadline = None if args.api_timeout <= 0 else time.monotonic() + args.api_timeout
    last_status = response_status(response)
    while last_status not in {"completed", "failed", "cancelled", "incomplete"}:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(
                f"Response {response_id} did not complete within {args.api_timeout:.0f}s "
                f"(last status: {last_status or 'unknown'})."
            )
        if args.poll_status:
            print(f"Response {response_id} status={last_status or 'unknown'}; polling again in {args.poll_interval:g}s")
        time.sleep(args.poll_interval)
        response = request_json_with_retries(args, "GET", response_endpoint(args.base_url, response_id))
        last_status = response_status(response)
    return response


def run_agent(args: argparse.Namespace) -> str:
    load_dotenv(args.env_file)
    if not args.data_package_only:
        validate_mcp_url_for_openai(args.mcp_url)
    task = build_task(args.cik or "", args.question or "")
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_by_step: List[Dict[str, int]] = []
    tool_usage = init_tool_usage()
    model_calls = {"attempts": 0, "completed": 0, "failures": 0}
    file_ids: List[str] = []
    data_package_paths: List[Path] = []

    if args.use_data_package:
        if args.data_package_dir:
            package_dir = resolve_package_dir(args.data_package_dir, args.cik or "")
            data_package_paths = existing_10k_company_package(args.cik or "", package_dir)
        else:
            package_dir = output_path(args).parent / "openai_input"
            data_package_paths = export_10k_company_package(args.db, args.cik or "", package_dir)
        if args.file_input_mode == "upload":
            file_ids = [upload_openai_file(args, path) for path in data_package_paths]

    instructions = build_insight_mining_prompt(
        task,
        args.cik or "",
        args.min_insights,
        allow_web_search=args.allow_web_search,
        use_data_package=args.use_data_package,
        data_package_only=args.data_package_only,
    )
    input_text = (
        f"Start exploring now. Final output must be valid JSON with at least "
        f"{args.min_insights} insights for this task: {task}"
    )
    if args.use_data_package:
        input_text += (
            " Use the attached SQLite-export files before finalizing insights."
        )
        if file_ids:
            input_text += f" Attached file ids: {', '.join(file_ids)}."
    input_payload = build_input(args, input_text, data_package_paths, file_ids)
    tools = build_tools(args)

    previous_response_id = ""
    for step in range(args.max_steps):
        model_calls["attempts"] += 1
        try:
            # o4-mini-deep-research does not support structured outputs here.
            # Ask for JSON in text and parse response_output_text() after completion.
            payload = {
                "model": args.model,
                "instructions": instructions,
                "input": input_payload,
                "tools": tools,
            }
            if previous_response_id:
                payload["previous_response_id"] = previous_response_id
            response = create_response_v1(args, payload)
            dump_raw_response(args, response, step + 1)
            raise_for_response_failure(response)
        except Exception as exc:
            model_calls["failures"] += 1
            paths = write_error_output(args, task, usage_total, usage_by_step, tool_usage, model_calls, step + 1, exc)
            print(f"Model call failed at step {step + 1}. Partial diagnostics saved to {paths['json']} and {paths['csv']}")
            raise

        model_calls["completed"] += 1
        previous_response_id = response_field(response, "id", "") or previous_response_id
        record_tool_usage_from_response(tool_usage, response)
        step_usage = usage_to_dict(response_field(response, "usage"))
        add_usage(usage_total, step_usage)
        usage_by_step.append({"step": step + 1, **step_usage})

        raw_text = response_output_text(response)
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
            input_payload = build_input(args, input_text, data_package_paths, file_ids)
            continue

        data["token_usage"] = usage_total
        data["token_usage_by_step"] = usage_by_step
        data["tool_usage"] = tool_usage
        data["model_calls"] = model_calls
        if args.use_data_package:
            data["data_package"] = {
                "input_files": [str(path) for path in data_package_paths],
                "file_ids": file_ids,
                "file_input_mode": args.file_input_mode,
                "mode": "data_package_only" if args.data_package_only else "mcp_plus_data_package",
            }
        if not acceptable:
            data["warning"] = reason
        paths = write_outputs(data, output_path(args))
        return f"Saved {len(data.get('insights', []))} insights to {paths['json']} and {paths['csv']}"

    raw_text = ""
    data = normalize_output(raw_text, task, args.cik or "")
    data["warning"] = "Reached max_steps before the model produced a final answer."
    data["token_usage"] = usage_total
    data["token_usage_by_step"] = usage_by_step
    data["tool_usage"] = tool_usage
    data["model_calls"] = model_calls
    paths = write_outputs(data, output_path(args))
    return f"Reached max_steps. Saved partial output to {paths['json']} and {paths['csv']}."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI-compatible 10-K insight discovery runner")
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", "http://35.220.164.252:3888/v1"))
    parser.add_argument("--api-key", default=os.getenv("MODEL_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", "openai/o4-mini-deep-research"))
    parser.add_argument("--mcp-url", default=os.getenv("SQLITE_MCP_URL", "http://127.0.0.1:8765/sse"))
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--data-package-dir", default="", help="Precomputed package root or company package dir. If set, files are reused instead of exported from --db.")
    parser.add_argument("--file-root", action="append", help="Local path to search; repeatable")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--cik", help="10-K company CIK. Builds the task: Analyze company with CIK {cik}")
    parser.add_argument("--question", help="Optional custom task override. If omitted, --cik is required.")
    parser.add_argument("--min-insights", type=int, default=10, help="Minimum number of insights requested.")
    parser.add_argument("--output-dir", default="./outputs/openai_o4_mini_deep_research")
    parser.add_argument("--output-file", help="Exact JSON output path. CSV is written next to it.")
    parser.add_argument("--api-timeout", type=float, default=14400.0, help="Overall wait time per model response. Use 0 to wait indefinitely.")
    parser.add_argument("--request-timeout", type=float, default=60.0, help="HTTP timeout for each retrieve/poll request.")
    parser.add_argument("--create-request-timeout", type=float, default=600.0, help="HTTP timeout for the initial response creation request.")
    parser.add_argument("--request-retries", type=int, default=5, help="Retries for transient HTTP timeouts or URL errors.")
    parser.add_argument("--retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--retry-max-delay", type=float, default=60.0)
    parser.add_argument("--background", action=argparse.BooleanOptionalAction, default=True, help="Use Responses background mode and poll.")
    parser.add_argument("--background-fallback", action=argparse.BooleanOptionalAction, default=True, help="Fall back to blocking POST if background creation repeatedly fails.")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True, help="Use streaming for blocking Responses POSTs to avoid idle proxy disconnects.")
    parser.add_argument("--stream-status", action="store_true", help="Print Responses streaming event types.")
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--poll-status", action="store_true", help="Print background response status while polling.")
    parser.add_argument("--allow-web-search", action="store_true")
    parser.add_argument("--use-code-interpreter", action="store_true")
    parser.add_argument("--use-data-package", action="store_true", help="Export target CIK rows from SQLite and attach them as Responses input_file items.")
    parser.add_argument("--data-package-only", action="store_true", help="Use only attached SQLite-export files plus code interpreter; do not expose the MCP server.")
    parser.add_argument("--file-input-mode", choices=["base64", "upload", "none"], default=os.getenv("OPENAI_FILE_INPUT_MODE", "base64"), help="How to attach data-package files to Responses input. base64 avoids /v1/files.")
    parser.add_argument("--file-upload-purpose", default=os.getenv("OPENAI_FILE_UPLOAD_PURPOSE", "user_data"))
    parser.add_argument("--file-upload-model-field", action=argparse.BooleanOptionalAction, default=True, help="Include model in /v1/files multipart fields for OpenAI-compatible gateways that require it.")
    parser.add_argument("--code-interpreter-memory-limit", default=os.getenv("OPENAI_CODE_INTERPRETER_MEMORY_LIMIT", "4g"))
    parser.add_argument("--dump-raw-response", action="store_true")
    parser.add_argument("--min-data-tool-calls", type=int, default=1)
    args = parser.parse_args()
    if args.data_package_only:
        args.use_data_package = True
        args.use_code_interpreter = True
    if args.use_data_package:
        args.use_code_interpreter = True
        if not args.cik:
            parser.error("--use-data-package requires --cik so the SQLite export can be scoped to one company.")
    return args


if __name__ == "__main__":
    print(run_agent(parse_args()))
