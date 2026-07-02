#!/usr/bin/env python3
"""Runtime hook and parser for D-Code's structured LangGraph message stream."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from insights_discovery.common.insight_generation_helper import (
    TrajectoryTurn,
    utc_timestamp,
)


_LOCK = threading.Lock()
_INSTALLED = False


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _append_event(event: dict[str, Any]) -> None:
    destination = os.getenv("DDR_DCODE_TRAJECTORY_EVENTS")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str))
        stream.write("\n")


def install() -> None:
    """Patch D-Code's non-interactive message handler in this subprocess only."""

    global _INSTALLED
    if _INSTALLED or not os.getenv("DDR_DCODE_TRAJECTORY_EVENTS"):
        return

    import deepagents_code.non_interactive as non_interactive
    from langchain_core.messages import AIMessage, ToolMessage

    original = non_interactive._process_message_chunk

    def wrapped(data, state, console, file_op_tracker):
        if isinstance(data, tuple) and len(data) == 2:
            message, metadata = data
            if not (metadata and metadata.get("lc_source") == "summarization"):
                if isinstance(message, AIMessage):
                    _append_event(
                        {
                            "type": "assistant_chunk",
                            "timestamp": utc_timestamp(),
                            "message": _jsonable(message),
                            "metadata": _jsonable(metadata),
                        }
                    )
                elif isinstance(message, ToolMessage):
                    _append_event(
                        {
                            "type": "tool_result",
                            "timestamp": utc_timestamp(),
                            "message": _jsonable(message),
                            "metadata": _jsonable(metadata),
                        }
                    )
        return original(data, state, console, file_op_tracker)

    non_interactive._process_message_chunk = wrapped
    _INSTALLED = True


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text", "")))
    return "".join(parts)


def _tool_chunks(message: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = message.get("tool_call_chunks") or []
    if chunks:
        return [item for item in chunks if isinstance(item, dict)]
    calls = message.get("tool_calls") or []
    return [item for item in calls if isinstance(item, dict)]


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        return {"raw": str(raw)}


def parse_events(path: Path) -> list[TrajectoryTurn]:
    """Pair streamed assistant tool-call chunks with ToolMessages."""

    calls: dict[str, dict[str, Any]] = {}
    call_order: list[str] = []
    text_buffer = ""

    with path.open(encoding="utf-8") as stream:
        for raw_line in stream:
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            message = event.get("message") or {}
            if event.get("type") == "assistant_chunk":
                text_buffer += _text_from_content(message.get("content"))
                for chunk in _tool_chunks(message):
                    call_id = str(chunk.get("id") or "")
                    if not call_id:
                        continue
                    if call_id not in calls:
                        calls[call_id] = {
                            "timestamp": event.get("timestamp") or utc_timestamp(),
                            "assistant_message": text_buffer.strip(),
                            "tool_name": str(chunk.get("name") or ""),
                            "arguments_fragments": [],
                        }
                        call_order.append(call_id)
                    if chunk.get("name"):
                        calls[call_id]["tool_name"] = str(chunk["name"])
                    args = chunk.get("args", chunk.get("arguments"))
                    if isinstance(args, dict):
                        calls[call_id]["arguments_fragments"] = [args]
                    elif args:
                        calls[call_id]["arguments_fragments"].append(str(args))
            elif event.get("type") == "tool_result":
                call_id = str(message.get("tool_call_id") or "")
                call = calls.setdefault(
                    call_id,
                    {
                        "timestamp": event.get("timestamp") or utc_timestamp(),
                        "assistant_message": text_buffer.strip(),
                        "tool_name": str(message.get("name") or ""),
                        "arguments_fragments": [],
                    },
                )
                if call_id not in call_order:
                    call_order.append(call_id)
                call["tool_result"] = message.get("content")
                call["is_error"] = str(message.get("status", "")).lower() == "error"
                text_buffer = ""

    turns: list[TrajectoryTurn] = []
    for call_id in call_order:
        call = calls[call_id]
        fragments = call.pop("arguments_fragments", [])
        arguments: Any
        if len(fragments) == 1 and isinstance(fragments[0], dict):
            arguments = fragments[0]
        else:
            arguments = _parse_arguments("".join(str(item) for item in fragments))
        turns.append(
            TrajectoryTurn(
                timestamp=call["timestamp"],
                assistant_message=call.get("assistant_message", ""),
                tool_name=call.get("tool_name", ""),
                tool_arguments=arguments,
                tool_result=call.get("tool_result"),
                tool_call_id=call_id,
                is_error=bool(call.get("is_error", False)),
            )
        )
    return turns
