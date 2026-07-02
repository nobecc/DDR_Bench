#!/usr/bin/env python3
"""Generate ReAct-compatible insight and session artifacts from trajectories."""

from __future__ import annotations

import asyncio
import csv
import inspect
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from agent.prompt_manager import PromptManager


TextGenerator = Callable[..., Awaitable[str] | str]


@dataclass
class TrajectoryTurn:
    """One assistant tool call paired with its resulting tool message."""

    timestamp: str
    assistant_message: str
    tool_name: str
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tool_result: Any = None
    tool_call_id: str = ""
    is_error: bool = False

    def environment_message(self) -> str:
        return f"Tool execution result: {format_value(self.tool_result)}"


@dataclass(frozen=True)
class InsightGenerationSettings:
    """Generation settings shared by CubePI and D-Code."""

    insight_max_tokens: int = 512
    summary_max_tokens: int = 16384
    insight_temperature: float = 0.5


def utc_timestamp() -> str:
    """Return an ISO timestamp suitable for artifact rows."""

    return datetime.now().astimezone().isoformat()


def session_timestamp() -> str:
    """Return the filename timestamp format used by the ReAct runner."""

    return datetime.now().strftime("%Y%m%d_%H%M%S")


def format_value(value: Any) -> str:
    """Render a tool result deterministically while preserving plain strings."""

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


async def _call_generator(
    generator: TextGenerator,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
) -> str:
    result = generator(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if inspect.isawaitable(result):
        result = await result
    return str(result or "").strip()


async def generate_message_insight(
    turn: TrajectoryTurn,
    task: str,
    generator: TextGenerator,
    settings: InsightGenerationSettings,
) -> str:
    """Use the original ReAct prompt to summarize one tool execution."""

    prompt_manager = PromptManager(auto_finish=True)
    messages = [
        {"role": "system", "content": prompt_manager.get_insight_system_prompt()},
        {
            "role": "user",
            "content": prompt_manager.build_insight_prompt(
                turn.assistant_message,
                turn.environment_message(),
                task,
            ),
        },
    ]
    return await _call_generator(
        generator,
        messages,
        max_tokens=settings.insight_max_tokens,
        temperature=settings.insight_temperature,
    )


def trajectory_as_chat_messages(turns: Iterable[TrajectoryTurn]) -> list[dict[str, Any]]:
    """Convert trajectory turns to the ReAct final-summary conversation schema."""

    messages: list[dict[str, Any]] = []
    for turn in turns:
        messages.append(
            {
                "role": "agent",
                "content": turn.assistant_message,
                "tool_call": {
                    "tool": turn.tool_name,
                    "arguments": turn.tool_arguments,
                },
            }
        )
        messages.append(
            {
                "role": "environment",
                "content": turn.environment_message(),
                "tool_result": turn.tool_result,
            }
        )
    return messages


async def generate_final_summary(
    turns: list[TrajectoryTurn],
    generator: TextGenerator,
    settings: InsightGenerationSettings,
) -> str:
    """Generate the same chat-wise FINISH summary used by the ReAct runner."""

    prompt_manager = PromptManager(auto_finish=True)
    conversation = trajectory_as_chat_messages(turns)
    messages = [
        {
            "role": "system",
            "content": prompt_manager.get_final_summary_system_prompt(),
        },
        {
            "role": "user",
            "content": prompt_manager.build_final_summary_prompt(conversation),
        },
    ]
    response = await _call_generator(
        generator,
        messages,
        max_tokens=settings.summary_max_tokens,
        temperature=settings.insight_temperature,
    )
    if response.upper().startswith("FINISH:"):
        return response
    return f"FINISH: {response}"


async def generate_artifacts(
    *,
    turns: list[TrajectoryTurn],
    task: str,
    generator: TextGenerator,
    output_dir: Path,
    settings: InsightGenerationSettings,
    session_id: str | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    final_summary: str | None = None,
) -> dict[str, str]:
    """Generate all evaluator-compatible artifacts for one completed trajectory."""

    generation_started_at = time.time()
    session_id = session_id or session_timestamp()
    output_dir.mkdir(parents=True, exist_ok=True)
    insights: list[str] = []
    for turn in turns:
        try:
            insight = await generate_message_insight(turn, task, generator, settings)
        except Exception:
            insight = "NO INSIGHT"
        insights.append(insight or "NO INSIGHT")

    if not final_summary or not final_summary.upper().startswith("FINISH:"):
        final_summary = await generate_final_summary(turns, generator, settings)
    generation_ended_at = time.time()
    runtime_metadata = runtime_metadata or {}
    runtime_metadata["artifact_generation_started_at"] = generation_started_at
    runtime_metadata["artifact_generation_ended_at"] = generation_ended_at
    runtime_metadata["artifact_generation_duration_seconds"] = round(
        generation_ended_at - generation_started_at,
        2,
    )
    runtime_metadata["ended_at"] = generation_ended_at
    if runtime_metadata.get("started_at") is not None:
        runtime_metadata["duration_seconds"] = round(
            generation_ended_at - float(runtime_metadata["started_at"]),
            2,
        )
    paths = artifact_paths(output_dir, session_id)
    write_insights_csv(paths["insights"], turns, insights)
    write_chat_messages_csv(paths["chat_messages"], turns, final_summary)
    write_trajectory_jsonl(paths["trajectory"], turns)
    write_session_stats_json(
        paths["session_stats"],
        session_id=session_id,
        task=task,
        turns=turns,
        insight_count=len(insights),
        final_summary=final_summary,
        paths=paths,
        settings=settings,
        runtime_metadata=runtime_metadata,
    )
    return {name: str(path) for name, path in paths.items()}


def artifact_paths(output_dir: Path, session_id: str) -> dict[str, Path]:
    return {
        "insights": output_dir / f"insights_{session_id}.csv",
        "chat_messages": output_dir / f"chat_messages_{session_id}.csv",
        "session_stats": output_dir / f"session_stats_{session_id}.json",
        "trajectory": output_dir / f"trajectory_{session_id}.jsonl",
    }


def write_insights_csv(
    path: Path,
    turns: list[TrajectoryTurn],
    insights: list[str],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["timestamp", "assistant_message", "user_message", "insight"],
        )
        writer.writeheader()
        for turn, insight in zip(turns, insights, strict=True):
            writer.writerow(
                {
                    "timestamp": turn.timestamp,
                    "assistant_message": turn.assistant_message,
                    "user_message": turn.environment_message(),
                    "insight": insight,
                }
            )


def write_chat_messages_csv(
    path: Path,
    turns: list[TrajectoryTurn],
    final_summary: str,
) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["timestamp", "role", "content", "tool_call", "tool_result"],
        )
        writer.writeheader()
        for turn in turns:
            writer.writerow(
                {
                    "timestamp": turn.timestamp,
                    "role": "agent",
                    "content": turn.assistant_message,
                    "tool_call": json.dumps(
                        {"tool": turn.tool_name, "arguments": turn.tool_arguments},
                        ensure_ascii=False,
                        default=str,
                    ),
                    "tool_result": "",
                }
            )
            writer.writerow(
                {
                    "timestamp": turn.timestamp,
                    "role": "environment",
                    "content": turn.environment_message(),
                    "tool_call": "",
                    "tool_result": format_value(turn.tool_result),
                }
            )
        writer.writerow(
            {
                "timestamp": utc_timestamp(),
                "role": "agent",
                "content": final_summary,
                "tool_call": "",
                "tool_result": "",
            }
        )


def write_trajectory_jsonl(path: Path, turns: list[TrajectoryTurn]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for turn in turns:
            stream.write(json.dumps(asdict(turn), ensure_ascii=False, default=str))
            stream.write("\n")


def write_session_stats_json(
    path: Path,
    *,
    session_id: str,
    task: str,
    turns: list[TrajectoryTurn],
    insight_count: int,
    final_summary: str,
    paths: dict[str, Path],
    settings: InsightGenerationSettings,
    runtime_metadata: dict[str, Any],
) -> None:
    failed = sum(1 for turn in turns if turn.is_error)
    data = {
        "session_id": session_id,
        "task": task,
        "completed": True,
        "total_messages": len(turns) * 2 + 1,
        "total_agent_messages": len(turns) + 1,
        "total_user_messages": len(turns),
        "total_insight_messages": insight_count,
        "total_tool_calls": len(turns),
        "successful_tool_calls": len(turns) - failed,
        "failed_tool_calls": failed,
        "tool_success_rate": ((len(turns) - failed) / len(turns)) if turns else 0,
        "final_summary": final_summary,
        "insights_file": paths["insights"].name,
        "chat_messages_file": paths["chat_messages"].name,
        "trajectory_file": paths["trajectory"].name,
        "insight_generation": asdict(settings),
        "runtime": runtime_metadata,
    }
    if runtime_metadata.get("token_usage"):
        data["token_usage"] = runtime_metadata["token_usage"]
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def load_trajectory_jsonl(path: Path) -> list[TrajectoryTurn]:
    turns: list[TrajectoryTurn] = []
    with path.open(encoding="utf-8") as stream:
        for raw_line in stream:
            if raw_line.strip():
                turns.append(TrajectoryTurn(**json.loads(raw_line)))
    return turns


def run_async(coro: Awaitable[Any]) -> Any:
    """Run a coroutine from synchronous runners that do not own an event loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run_async cannot be called from an active event loop")
