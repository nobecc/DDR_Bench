#!/usr/bin/env python3
"""Shared output, validation, and accounting helpers for insight discovery agents."""

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def build_task(cik: str = "", question: str = "") -> str:
    if question:
        return question
    if cik:
        return f"Analyze company with CIK {cik}"
    raise ValueError("Either cik or question is required.")


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def usage_to_dict(usage: Any) -> Dict[str, int]:
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if hasattr(usage, "model_dump"):
        usage_data = usage.model_dump()
    elif isinstance(usage, dict):
        usage_data = usage
    else:
        usage_data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
    prompt_tokens = usage_data.get("prompt_tokens", usage_data.get("input_tokens", 0))
    completion_tokens = usage_data.get("completion_tokens", usage_data.get("output_tokens", 0))
    total_tokens = usage_data.get("total_tokens", 0) or int(prompt_tokens or 0) + int(completion_tokens or 0)
    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
    }


def add_usage(total: Dict[str, int], usage: Dict[str, int]) -> None:
    total["prompt_tokens"] += usage.get("prompt_tokens", 0)
    total["completion_tokens"] += usage.get("completion_tokens", 0)
    total["total_tokens"] += usage.get("total_tokens", 0)


def init_tool_usage() -> Dict[str, Dict[str, int]]:
    return {
        name: {"calls": 0, "successes": 0, "failures": 0}
        for name in ["search", "fetch", "code_interpreter"]
    }


def record_tool_usage(tool_usage: Dict[str, Dict[str, int]], name: str, result: Dict[str, Any]) -> None:
    stats = tool_usage.setdefault(name, {"calls": 0, "successes": 0, "failures": 0})
    stats["calls"] += 1
    if isinstance(result, dict) and result.get("error"):
        stats["failures"] += 1
    else:
        stats["successes"] += 1


def extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def normalize_output(raw_text: str, task: str, cik: str) -> Dict[str, Any]:
    try:
        data = extract_json_object(raw_text)
    except Exception:
        data = {
            "task": task,
            "cik": cik or "",
            "insights": [{"id": 1, "topic": "unparsed_output", "insight": raw_text, "evidence": []}],
            "summary": "The model did not return parseable JSON; raw text was stored as one insight.",
        }

    data.setdefault("task", task)
    data.setdefault("cik", cik or "")
    data.setdefault("insights", [])
    data.setdefault("summary", "")

    normalized = []
    for index, item in enumerate(data.get("insights") or [], 1):
        if isinstance(item, str):
            item = {"insight": item}
        if not isinstance(item, dict):
            continue
        normalized.append({
            "id": item.get("id") or index,
            "topic": item.get("topic", ""),
            "insight": item.get("insight", ""),
            "evidence": item.get("evidence", []),
        })
    data["insights"] = normalized
    return data


def evidence_sources(item: Dict[str, Any]) -> List[str]:
    return [
        str(e.get("source", "")).strip().lower()
        for e in (item.get("evidence", []) or [])
        if isinstance(e, dict)
    ]


def data_supported_insight_count(data: Dict[str, Any]) -> int:
    return sum(
        1
        for item in data.get("insights", []) or []
        if {"sqlite", "mcp"} & set(evidence_sources(item))
    )


def web_evidence_count(data: Dict[str, Any]) -> int:
    return sum(evidence_sources(item).count("web") for item in data.get("insights", []) or [])


def tool_call_total(tool_usage: Dict[str, Dict[str, int]], tool_names: List[str]) -> int:
    return sum(tool_usage.get(name, {}).get("calls", 0) for name in tool_names)


def output_is_acceptable(data: Dict[str, Any], tool_usage: Dict[str, Dict[str, int]],
                         min_data_tool_calls: int, allow_web_search: bool) -> tuple[bool, str]:
    data_tool_calls = tool_call_total(tool_usage, ["search", "fetch"])
    if data_tool_calls < min_data_tool_calls:
        return False, f"Only {data_tool_calls} data-tool calls were executed; at least {min_data_tool_calls} are required."
    insights = data.get("insights", []) or []
    if not insights:
        return False, "The final JSON contains no insights."
    supported = data_supported_insight_count(data)
    if supported < len(insights):
        return False, f"Only {supported}/{len(insights)} insights include sqlite/file evidence."
    if not allow_web_search and web_evidence_count(data) > 0:
        return False, "Web evidence is present, but this MCP-only runner expects sqlite/mcp evidence."
    return True, ""


def default_output_file(output_dir: str, output_file: str = "", cik: str = "", suffix: str = "insights") -> Path:
    if output_file:
        return Path(output_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if cik:
        return Path(output_dir) / f"company_{cik}_{suffix}_{timestamp}.json"
    return Path(output_dir) / f"research_{suffix}_{timestamp}.json"


def write_outputs(data: Dict[str, Any], output_file: Path) -> Dict[str, str]:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    csv_file = output_file.with_suffix(".csv")
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "topic", "insight", "evidence"])
        writer.writeheader()
        for item in data.get("insights", []):
            writer.writerow({
                "id": item.get("id", ""),
                "topic": item.get("topic", ""),
                "insight": item.get("insight", ""),
                "evidence": compact_json(item.get("evidence", [])),
            })
    return {"json": str(output_file), "csv": str(csv_file)}
