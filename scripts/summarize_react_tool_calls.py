#!/usr/bin/env python3
"""Summarize ReAct MCP tool-call logs by company and by method."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


SERVER_GLOBS = {
    "sqlite-mcp": "sqlite-mcp_calls_*.csv",
    "code-mcp": "code-mcp_calls_*.csv",
}


def read_latest_session_stats(company_dir: Path) -> dict[str, Any]:
    files = sorted(company_dir.glob("session_stats_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {}
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def count_server_calls(company_dir: Path) -> tuple[Counter, Counter, Counter, Counter]:
    by_server: Counter[str] = Counter()
    by_tool: Counter[str] = Counter()
    success_by_server: Counter[str] = Counter()
    failed_by_server: Counter[str] = Counter()

    for server_name, pattern in SERVER_GLOBS.items():
        for log_path in sorted(company_dir.glob(pattern)):
            with log_path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    tool_name = row.get("tool_name") or "unknown"
                    by_server[server_name] += 1
                    by_tool[tool_name] += 1
                    if str(row.get("success", "")).lower() == "true":
                        success_by_server[server_name] += 1
                    else:
                        failed_by_server[server_name] += 1

    return by_server, by_tool, success_by_server, failed_by_server


def summarize_company(company_dir: Path, method: str) -> dict[str, Any]:
    cik = company_dir.name.removeprefix("company_")
    session_stats = read_latest_session_stats(company_dir)
    by_server, by_tool, success_by_server, failed_by_server = count_server_calls(company_dir)

    sqlite_calls = by_server["sqlite-mcp"]
    code_calls = by_server["code-mcp"]
    total_calls = sqlite_calls + code_calls
    session_total = session_stats.get("total_tool_calls")

    return {
        "method": method,
        "cik": cik,
        "total_tool_calls": total_calls,
        "session_total_tool_calls": session_total if session_total is not None else "",
        "successful_tool_calls": sum(success_by_server.values()),
        "failed_tool_calls": sum(failed_by_server.values()),
        "sqlite_tool_calls": sqlite_calls,
        "code_tool_calls": code_calls,
        "other_tool_calls": max((session_total or total_calls) - total_calls, 0),
        "tool_counts_json": json.dumps(dict(sorted(by_tool.items())), ensure_ascii=False),
        "server_counts_json": json.dumps(dict(sorted(by_server.items())), ensure_ascii=False),
        "source": company_dir.as_posix(),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "method",
        "cik",
        "total_tool_calls",
        "session_total_tool_calls",
        "successful_tool_calls",
        "failed_tool_calls",
        "sqlite_tool_calls",
        "code_tool_calls",
        "other_tool_calls",
        "tool_counts_json",
        "server_counts_json",
        "source",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_method_summary(path: Path, method: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    company_count = len(rows)
    total_tool_calls = sum(int(r["total_tool_calls"]) for r in rows)
    total_sqlite = sum(int(r["sqlite_tool_calls"]) for r in rows)
    total_code = sum(int(r["code_tool_calls"]) for r in rows)

    by_tool: Counter[str] = Counter()
    for row in rows:
        by_tool.update(json.loads(row["tool_counts_json"]))

    summary = {
        "method": method,
        "company_count": company_count,
        "total_tool_calls": total_tool_calls,
        "average_tool_calls": round(total_tool_calls / company_count, 2) if company_count else 0,
        "total_sqlite_mcp_calls": total_sqlite,
        "average_sqlite_mcp": round(total_sqlite / company_count, 2) if company_count else 0,
        "total_code_mcp_calls": total_code,
        "average_code_mcp": round(total_code / company_count, 2) if company_count else 0,
        "by_tool": dict(sorted(by_tool.items())),
    }
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def write_method_csv(path: Path, summary: dict[str, Any]) -> None:
    tool_names = sorted(summary["by_tool"])
    fieldnames = [
        "method",
        "company_count",
        "total_tool_calls",
        "average_tool_calls",
        "total_sqlite_mcp_calls",
        "average_sqlite_mcp",
        "total_code_mcp_calls",
        "average_code_mcp",
        *tool_names,
    ]
    row = {k: summary.get(k, "") for k in fieldnames}
    for tool_name in tool_names:
        row[tool_name] = summary["by_tool"][tool_name]

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def write_method_md(path: Path, summary: dict[str, Any]) -> None:
    tool_names = sorted(summary["by_tool"])
    headers = [
        "method",
        "company_count",
        "total_tool_calls",
        "average_tool_calls",
        "average_sqlite_mcp",
        "average_code_mcp",
        *tool_names,
    ]
    row = [
        summary["method"],
        summary["company_count"],
        summary["total_tool_calls"],
        summary["average_tool_calls"],
        summary["average_sqlite_mcp"],
        summary["average_code_mcp"],
        *[summary["by_tool"][tool_name] for tool_name in tool_names],
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
        "| " + " | ".join(str(item) for item in row) + " |",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize ReAct MCP tool-call logs.")
    parser.add_argument("--logs-dir", default="./outputs/react", help="Directory containing company_* ReAct logs.")
    parser.add_argument("--output-dir", default="./outputs/react", help="Output directory.")
    parser.add_argument("--method", default="react_sqlite_code", help="Method name to write in tables.")
    args = parser.parse_args()

    logs_dir = Path(args.logs_dir)
    company_dirs = sorted(p for p in logs_dir.glob("company_*") if p.is_dir())
    rows = [summarize_company(company_dir, args.method) for company_dir in company_dirs]

    output_dir = Path(args.output_dir)
    write_csv(output_dir / "tool_call_stats_by_company.csv", rows)
    summary = write_method_summary(output_dir / "tool_call_stats_summary.json", args.method, rows)
    write_method_csv(output_dir / "tool_call_stats_method_comparison.csv", summary)
    write_method_md(output_dir / "tool_call_stats_method_comparison.md", summary)

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
