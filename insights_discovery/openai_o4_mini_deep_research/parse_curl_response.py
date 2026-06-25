#!/usr/bin/env python3
"""Parse a saved OpenAI Responses curl result into DDR_Bench insight outputs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.output import (  # noqa: E402
    build_task,
    normalize_output,
    usage_to_dict,
    write_outputs,
)


def field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def item_content_text(item: Any) -> str:
    chunks: list[str] = []
    for content in field(item, "content", []) or []:
        text = field(content, "text")
        if text:
            chunks.append(str(text))
    return "\n".join(chunks)


def response_output_text(response: dict[str, Any]) -> str:
    text = response.get("output_text")
    if text:
        return str(text)
    chunks: list[str] = []
    for item in response.get("output", []) or []:
        text = item_content_text(item)
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def normalize_response_text(raw_text: str, task: str, cik: str) -> dict[str, Any]:
    """Normalize model text, accepting either a JSON object or a bare insight array."""
    text = (raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        raw_text = json.dumps({"insights": parsed}, ensure_ascii=False)
    elif isinstance(parsed, dict):
        raw_text = json.dumps(parsed, ensure_ascii=False)
    return normalize_output(raw_text, task, cik)


def is_failed_item(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or "").lower()
    return bool(item.get("error")) or status in {"failed", "error", "incomplete", "cancelled"}


def tool_name_for_item(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "")
    name = item.get("name") or item.get("tool_name") or ""
    if name:
        return str(name)
    if item_type == "code_interpreter_call" or "code_interpreter" in item_type:
        return "code_interpreter"
    if item_type == "mcp_list_tools":
        return "mcp_list_tools"
    if "mcp" in item_type:
        return "mcp"
    return item_type or "unknown"


def collect_tool_call_stats(response: dict[str, Any]) -> dict[str, Any]:
    by_tool: dict[str, int] = {}
    by_server: dict[str, dict[str, int]] = {}
    successful = 0
    failed = 0
    calls: list[dict[str, Any]] = []

    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        is_tool_item = (
            "mcp" in item_type
            or "tool" in item_type
            or "code_interpreter" in item_type
        )
        if not is_tool_item:
            continue

        tool_name = tool_name_for_item(item)
        server = str(item.get("server_label") or ("code_interpreter" if tool_name == "code_interpreter" else "responses"))
        by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
        by_server.setdefault(server, {})
        by_server[server][tool_name] = by_server[server].get(tool_name, 0) + 1

        failed_item = is_failed_item(item)
        if failed_item:
            failed += 1
        else:
            successful += 1

        calls.append({
            "type": item_type,
            "tool": tool_name,
            "server": server,
            "status": item.get("status", ""),
            "id": item.get("id", ""),
            "error": item.get("error"),
        })

    return {
        "total_tool_calls": successful + failed,
        "successful_tool_calls": successful,
        "failed_tool_calls": failed,
        "by_tool": dict(sorted(by_tool.items())),
        "by_server": {server: dict(sorted(tools.items())) for server, tools in sorted(by_server.items())},
        "calls": calls,
    }


def collect_runner_tool_usage(stats: dict[str, Any]) -> dict[str, dict[str, int]]:
    usage = {
        "search": {"calls": 0, "successes": 0, "failures": 0},
        "fetch": {"calls": 0, "successes": 0, "failures": 0},
        "code_interpreter": {"calls": 0, "successes": 0, "failures": 0},
    }
    for call in stats.get("calls", []):
        name = call.get("tool")
        if name not in usage:
            continue
        usage[name]["calls"] += 1
        if call.get("error") or str(call.get("status") or "").lower() in {"failed", "error", "incomplete", "cancelled"}:
            usage[name]["failures"] += 1
        else:
            usage[name]["successes"] += 1
    return usage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse saved Responses curl JSON into DDR_Bench outputs.")
    parser.add_argument("--raw-response", required=True, type=Path, help="Path to raw JSON saved from curl.")
    parser.add_argument("--output-file", required=True, type=Path, help="Final insights JSON path. CSV is written next to it.")
    parser.add_argument("--metadata-file", type=Path, help="Optional run metadata JSON path.")
    parser.add_argument("--cik", default="")
    parser.add_argument("--task", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    response = json.loads(args.raw_response.read_text(encoding="utf-8"))
    task = args.task or build_task(args.cik, "")
    cik = args.cik or str(response.get("cik") or "")

    tool_call_stats = collect_tool_call_stats(response)
    tool_usage = collect_runner_tool_usage(tool_call_stats)
    token_usage = usage_to_dict(response.get("usage"))

    if response.get("error") or str(response.get("status") or "").lower() in {"failed", "cancelled", "incomplete"}:
        data = {
            "task": task,
            "cik": cik,
            "insights": [],
            "summary": "",
            "error": response.get("error") or {
                "status": response.get("status"),
                "incomplete_details": response.get("incomplete_details"),
            },
        }
    else:
        data = normalize_response_text(response_output_text(response), task, cik)

    model_calls = {
        "attempts": 1,
        "completed": 0 if data.get("error") else 1,
        "failures": 1 if data.get("error") else 0,
    }
    output_data = {
        "task": data.get("task", task),
        "cik": data.get("cik", cik),
        "insights": data.get("insights", []),
        "summary": data.get("summary", ""),
    }
    if data.get("error"):
        output_data["error"] = data["error"]

    paths = write_outputs(output_data, args.output_file)

    metadata_file = args.metadata_file or args.output_file.with_name("run_metadata.json")
    metadata = {
        "cik": cik,
        "status": "failed" if data.get("error") else "ok",
        "raw_response": str(args.raw_response),
        "output_file": paths["json"],
        "csv_file": paths["csv"],
        "response_id": response.get("id", ""),
        "response_status": response.get("status", ""),
        "model": response.get("model", ""),
        "tool_call_stats": tool_call_stats,
        "tool_usage": tool_usage,
        "token_usage": token_usage,
        "model_calls": model_calls,
    }
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved insights to {paths['json']} and {paths['csv']}")
    print(f"Saved metadata to {metadata_file}")


if __name__ == "__main__":
    main()
