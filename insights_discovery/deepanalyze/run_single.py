#!/usr/bin/env python3
"""Run one DDR_Bench 10-K insight-discovery task with DeepAnalyze."""

from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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
    build_task,
    default_output_file,
    extract_json_object,
    normalize_output,
    usage_to_dict,
    write_outputs,
)
from insights_discovery.common.data_package import (  # noqa: E402
    existing_10k_company_package,
    export_10k_company_package,
    resolve_package_dir,
)


PROMPT_TEMPLATE = """You are DeepAnalyze running inside the DDR_Bench insight-discovery benchmark.

Target task: {task}
Target CIK: {cik}
Minimum insight count: {min_insights}

You have uploaded files exported from the local DDR_Bench 10-K SQLite database for exactly this target CIK:
- schema/documentation JSON describing the source tables and columns;
- company metadata JSON;
- filings JSONL;
- financial_facts JSONL.
- summary JSON/CSV with row counts, coverage, top fact distributions, core FY trends, and candidate YoY changes.

Use your built-in file-analysis and code-execution tools to inspect these files directly. Do not use DDR_Bench MCP tools, web search, external websites, or memorized company facts as evidence. Treat the uploaded files as the only evidence source.

Exploration requirements:
1. Inspect the schema/documentation and row counts before writing the report.
2. Use code over the uploaded JSON/JSONL files to compute trends, distributions, comparisons, year-over-year changes, outliers, and cross-checks.
3. Cover identity, ticker/SIC/filing coverage, revenue, costs, margins, operating income, net income, EPS, assets, liabilities, cash, debt, liquidity, working capital, cash flow, capex, dividends, repurchases, financing, segments, geography, operations/KPIs, accounting policies, tax, controls, commitments, contingencies, litigation, regulation, cybersecurity, environmental topics, and risk topics when the data supports them.
4. Prioritize insights that link multiple facts, reveal volatility or anomalies, expose dependencies/constraints, or connect narrative/filing context to numeric evidence.
5. Every insight must be specific, factual, useful for later QA evaluation, and include important numbers, dates, periods, comparisons, and caveats.
6. Every insight must include at least one evidence item. Use source "sqlite" and cite a precise table/query/filter/file reference, such as `financial_facts where cik='{cik}' and fact_name='Revenues' and fiscal_period='FY'`.
7. Do not include unsupported guesses. If evidence is partial, state exactly what the uploaded data supports.
8. Make sure the company identity matches CIK {cik}; do not analyze another company.

Final answer format:
Return a concise Markdown report only. Do not wrap it in code fences. Use exactly this repeated structure so the benchmark parser can convert it:

# DDR_Bench Insights Report
Task: {task}
CIK: {cik}

## Insight 1: short topic label
Insight: one self-contained insight with key facts, numbers, dates, and caveats.
Evidence:
- source: sqlite
  reference: precise source reference

## Insight 2: short topic label
Insight: ...
Evidence:
- source: sqlite
  reference: ...

## Summary
Brief synthesis of the most important findings.

Produce at least {min_insights} distinct insights. More is better if they are not duplicates."""


class ApiHTTPError(RuntimeError):
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


def output_path(args: argparse.Namespace) -> Path:
    return default_output_file(args.output_dir, args.output_file or "", args.cik or "")


def api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def request_json(
    args: argparse.Namespace,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if args.api_key:
        request_headers["Authorization"] = f"Bearer {args.api_key}"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    actual_timeout = args.request_timeout if timeout is None else (None if timeout <= 0 else timeout)
    try:
        with urllib.request.urlopen(request, timeout=actual_timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise ApiHTTPError(method, url, exc.code, error_body) from exc


def request_json_with_retries(
    args: argparse.Namespace,
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, args.request_retries + 2):
        try:
            return request_json(args, method, url, payload, headers=headers, timeout=timeout)
        except RETRY_EXCEPTIONS as exc:
            if isinstance(exc, ApiHTTPError) and exc.status_code < 500 and exc.status_code not in {408, 409, 429}:
                raise
            last_exc = exc
            if attempt > args.request_retries:
                break
            delay = min(args.retry_max_delay, args.retry_initial_delay * (2 ** (attempt - 1)))
            print(f"{method} {url} failed ({exc}); retrying in {delay:g}s [{attempt}/{args.request_retries}]")
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def encode_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----ddrbench-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])
    for name, path in files.items():
        filename = path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def upload_file(args: argparse.Namespace, path: Path) -> str:
    url = api_url(args.deepanalyze_url, "/files")
    last_exc: Exception | None = None
    for attempt in range(1, args.request_retries + 2):
        body, boundary = encode_multipart({"purpose": "file-extract"}, {"file": path})
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if args.api_key:
            headers["Authorization"] = f"Bearer {args.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=None if args.request_timeout <= 0 else args.request_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            file_id = data.get("id")
            if not file_id:
                raise RuntimeError(f"DeepAnalyze file upload response did not include id: {data}")
            return str(file_id)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            api_error = ApiHTTPError("POST", url, exc.code, error_body)
            if api_error.status_code < 500 and api_error.status_code not in {408, 409, 429}:
                raise api_error from exc
            last_exc = api_error
        except TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
        if attempt > args.request_retries:
            break
        delay = min(args.retry_max_delay, args.retry_initial_delay * (2 ** (attempt - 1)))
        print(f"POST {url} upload failed for {path.name} ({last_exc}); retrying in {delay:g}s [{attempt}/{args.request_retries}]")
        time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def sqlite_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(query, params)]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def export_company_package(args: argparse.Namespace, package_dir: Path) -> list[Path]:
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    package_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cik = args.cik or ""

    tables = [
        row["name"]
        for row in conn.execute(
            "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name"
        )
    ]
    schema: dict[str, Any] = {"source_database": str(db_path), "tables": {}}
    for table in tables:
        schema["tables"][table] = {
            "columns": [dict(row) for row in conn.execute(f"pragma table_info({table})")],
            "comment": sqlite_rows(conn, "select comment from table_comments where table_name=?", (table,)),
            "documentation": sqlite_rows(conn, "select documentation from table_documentation where table_name=?", (table,)),
            "column_comments": sqlite_rows(conn, "select column_name, comment from column_comments where table_name=?", (table,)),
            "column_documentation": sqlite_rows(conn, "select column_name, documentation from column_documentation where table_name=?", (table,)),
        }

    metadata = {
        "cik": cik,
        "companies": sqlite_rows(conn, "select * from companies where cik=?", (cik,)),
        "company_addresses": sqlite_rows(conn, "select * from company_addresses where cik=?", (cik,)),
        "company_tickers": sqlite_rows(conn, "select * from company_tickers where cik=?", (cik,)),
        "row_counts": {},
    }
    for table in ["companies", "company_addresses", "company_tickers", "filings", "financial_facts"]:
        metadata["row_counts"][table] = conn.execute(f"select count(*) from {table} where cik=?", (cik,)).fetchone()[0]

    filings = sqlite_rows(conn, "select * from filings where cik=? order by filing_date, id", (cik,))
    financial_facts = sqlite_rows(
        conn,
        """
        select * from financial_facts
        where cik=?
        order by fiscal_year, fiscal_period, fact_name, end_date, id
        """,
        (cik,),
    )
    if not metadata["companies"]:
        raise ValueError(f"No company row found for CIK {cik}")
    if not financial_facts:
        raise ValueError(f"No financial_facts rows found for CIK {cik}")

    paths = [
        package_dir / f"company_{cik}_schema.json",
        package_dir / f"company_{cik}_metadata.json",
        package_dir / f"company_{cik}_filings.jsonl",
        package_dir / f"company_{cik}_financial_facts.jsonl",
    ]
    write_json(paths[0], schema)
    write_json(paths[1], metadata)
    write_jsonl(paths[2], filings)
    write_jsonl(paths[3], financial_facts)
    return paths


def chat_completion(args: argparse.Namespace, prompt: str, file_ids: list[str]) -> dict[str, Any]:
    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "file_ids": file_ids,
            }
        ],
    }
    if args.api_key:
        payload["api_key"] = args.api_key
    return request_json_with_retries(
        args,
        "POST",
        api_url(args.deepanalyze_url, "/chat/completions"),
        payload,
        timeout=args.api_timeout,
    )


def response_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    return json.dumps(response, ensure_ascii=False)


def clean_deepanalyze_text(text: str) -> str:
    text = re.sub(r"<Code>.*?</Code>", "", text, flags=re.S)
    text = re.sub(r"<Execute>.*?</Execute>", "", text, flags=re.S)
    answer_match = re.search(r"<Answer>(.*?)</Answer>", text, flags=re.S)
    if answer_match:
        return answer_match.group(1).strip()
    return text.strip()


def parse_evidence(block: str) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    pattern = re.compile(
        r"-\s*source:\s*(?P<source>[^\n]+)\n\s*reference:\s*(?P<reference>.*?)(?=\n-\s*source:|\n##\s+Insight|\n##\s+Summary|\Z)",
        re.S | re.I,
    )
    for match in pattern.finditer(block):
        evidence.append({
            "source": match.group("source").strip().strip("`"),
            "reference": re.sub(r"\s+", " ", match.group("reference").strip()).strip("`"),
        })
    if not evidence:
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("-"):
                evidence.append({"source": "sqlite", "reference": line.lstrip("- ").strip()})
    return evidence


def markdown_to_data(markdown: str, task: str, cik: str) -> dict[str, Any]:
    text = clean_deepanalyze_text(markdown)
    try:
        return normalize_output(json.dumps(extract_json_object(text), ensure_ascii=False), task, cik)
    except Exception:
        pass

    insights: list[dict[str, Any]] = []
    insight_re = re.compile(
        r"^##\s+Insight\s+(?P<id>\d+)\s*:\s*(?P<topic>.*?)\s*$"
        r"(?P<body>.*?)(?=^##\s+Insight\s+\d+\s*:|^##\s+Summary\s*$|\Z)",
        re.M | re.S,
    )
    for fallback_id, match in enumerate(insight_re.finditer(text), 1):
        body = match.group("body").strip()
        insight_match = re.search(r"Insight:\s*(.*?)(?=\nEvidence:|\Z)", body, flags=re.S | re.I)
        insight_text = insight_match.group(1).strip() if insight_match else body
        evidence_match = re.search(r"Evidence:\s*(.*)", body, flags=re.S | re.I)
        evidence = parse_evidence(evidence_match.group(1)) if evidence_match else []
        insights.append({
            "id": int(match.group("id") or fallback_id),
            "topic": match.group("topic").strip(),
            "insight": re.sub(r"\s+", " ", insight_text),
            "evidence": evidence,
        })

    summary_match = re.search(r"^##\s+Summary\s*$(.*)\Z", text, flags=re.M | re.S)
    summary = summary_match.group(1).strip() if summary_match else ""
    if not insights:
        insights = [{"id": 1, "topic": "unparsed_output", "insight": text, "evidence": []}]
        summary = "DeepAnalyze output could not be parsed into insight sections."
    return {"task": task, "cik": cik, "insights": insights, "summary": summary}


def run_agent(args: argparse.Namespace) -> str:
    load_dotenv(args.env_file)
    task = build_task(args.cik or "", args.question or "")
    output_file = output_path(args)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if args.data_package_dir:
        package_dir = resolve_package_dir(args.data_package_dir, args.cik or "")
    else:
        package_dir = output_file.parent / "deepanalyze_input"

    prompt = PROMPT_TEMPLATE.format(task=task, cik=args.cik or "", min_insights=args.min_insights)
    (output_file.parent / "prompt.md").write_text(prompt, encoding="utf-8")

    started_at = time.time()
    if args.data_package_dir:
        file_paths = existing_10k_company_package(args.cik or "", package_dir)
    else:
        file_paths = export_10k_company_package(args.db, args.cik or "", package_dir)
    file_ids = [upload_file(args, path) for path in file_paths]
    response = chat_completion(args, prompt, file_ids)
    if args.dump_raw_response:
        (output_file.parent / "raw_response.json").write_text(
            json.dumps(response, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    raw_text = response_text(response)
    report_text = clean_deepanalyze_text(raw_text)
    report_path = output_file.with_suffix(".md")
    report_path.write_text(report_text, encoding="utf-8")

    data = markdown_to_data(report_text, task, args.cik or "")
    data["tool_usage"] = {"deepanalyze_file_upload": {"calls": len(file_ids), "successes": len(file_ids), "failures": 0}}
    data["model_calls"] = {"attempts": 1, "completed": 1, "failures": 0}
    data["token_usage"] = usage_to_dict(response.get("usage"))
    data["runtime"] = {
        "provider": "deepanalyze",
        "model": args.model,
        "deepanalyze_url": args.deepanalyze_url,
        "input_files": [str(path) for path in file_paths],
        "file_ids": file_ids,
        "started_at": started_at,
        "ended_at": time.time(),
    }
    if len(data.get("insights", [])) < args.min_insights:
        data["warning"] = f"Only {len(data.get('insights', []))} insights were parsed; expected at least {args.min_insights}."
        data["raw_model_output"] = raw_text

    paths = write_outputs(data, output_file)
    return f"Saved {len(data.get('insights', []))} insights to {paths['json']} and {paths['csv']}; raw report: {report_path}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeepAnalyze DDR_Bench 10-K insight discovery runner")
    parser.add_argument("--deepanalyze-url", default=os.getenv("DEEPANALYZE_URL", "http://localhost:8200/v1"))
    parser.add_argument("--api-key", default=os.getenv("DEEPANALYZE_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("DEEPANALYZE_MODEL", "DeepAnalyze-8B"))
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--data-package-dir", default="", help="Precomputed package root or company package dir. If set, files are reused instead of exported from --db.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--cik", help="10-K company CIK. Builds the task: Analyze company with CIK {cik}")
    parser.add_argument("--question", help="Optional custom task override. If omitted, --cik is required.")
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--output-dir", default="./outputs/deepanalyze")
    parser.add_argument("--output-file", help="Exact JSON output path. CSV is written next to it.")
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--api-timeout", type=float, default=14400.0)
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--retry-max-delay", type=float, default=60.0)
    parser.add_argument("--dump-raw-response", action="store_true")
    args = parser.parse_args()
    if not args.cik and not args.question:
        parser.error("Either --cik or --question is required.")
    return args


if __name__ == "__main__":
    print(run_agent(parse_args()))
