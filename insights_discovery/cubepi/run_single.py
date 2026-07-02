#!/usr/bin/env python3
"""Run one DDR_Bench insight-discovery task with CubePi."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from cubepi import Agent, tool
from cubepi.providers.anthropic import AnthropicProvider
from cubepi.providers.base import TextContent, UserMessage
from cubepi.providers.openai import OpenAIProvider
from mcp import ClientSession
from mcp.client.sse import sse_client

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.output import (  # noqa: E402
    build_task,
    compact_json,
    default_output_file,
)
from insights_discovery.common.insight_generation_helper import (  # noqa: E402
    InsightGenerationSettings,
    TrajectoryTurn,
    generate_artifacts,
    session_timestamp,
    utc_timestamp,
)
from insights_discovery.common.mcp_servers import (  # noqa: E402
    CODE_MCP_URL,
    CODE_SERVER_NAME,
    SQLITE_MCP_URL,
    SQLITE_SERVER_NAME,
    build_active_mcp_config,
    find_available_port,
    managed_mcp_servers,
    server_names_from_resolution,
)
from insights_discovery.common.run_directories import (  # noqa: E402
    ensure_run_dir,
)


AGENT_RULES_PATH = REPO_ROOT / ".deepagents" / "AGENTS.md"
DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_SCENARIO = "10k"


class CubePiTrajectoryCollector:
    """Pair CubePI assistant tool calls and results by tool-call ID."""

    def __init__(self) -> None:
        self.assistant_by_call_id: dict[str, str] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.turns: list[TrajectoryTurn] = []

    def record_assistant_message(self, message: Any) -> None:
        text = message_text(message).strip()
        for item in getattr(message, "content", []) or []:
            if getattr(item, "type", None) != "tool_call":
                continue
            call_id = str(getattr(item, "id", "") or "")
            if call_id:
                self.assistant_by_call_id[call_id] = text

    def record_tool_start(self, event: Any) -> None:
        call_id = str(getattr(event, "tool_call_id", "") or "")
        self.pending[call_id] = {
            "timestamp": utc_timestamp(),
            "assistant_message": self.assistant_by_call_id.get(call_id, ""),
            "tool_name": str(getattr(event, "tool_name", "") or ""),
            "tool_arguments": dict(getattr(event, "args", {}) or {}),
            "tool_call_id": call_id,
        }

    def record_tool_end(self, event: Any) -> None:
        call_id = str(getattr(event, "tool_call_id", "") or "")
        pending = self.pending.pop(call_id, {})
        self.turns.append(
            TrajectoryTurn(
                timestamp=pending.get("timestamp", utc_timestamp()),
                assistant_message=pending.get(
                    "assistant_message",
                    self.assistant_by_call_id.get(call_id, ""),
                ),
                tool_name=pending.get(
                    "tool_name",
                    str(getattr(event, "tool_name", "") or ""),
                ),
                tool_arguments=pending.get("tool_arguments", {}),
                tool_result=getattr(event, "result", None),
                tool_call_id=call_id,
                is_error=bool(getattr(event, "is_error", False)),
            )
        )

    def handle(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        if event_type == "message_end":
            message = getattr(event, "message", None)
            if getattr(message, "role", None) == "assistant":
                self.record_assistant_message(message)
        elif event_type == "tool_execution_start":
            self.record_tool_start(event)
        elif event_type == "tool_execution_end":
            self.record_tool_end(event)


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    for key in ("NO_PROXY", "no_proxy"):
        values = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
        existing = {item.lower() for item in values}
        for item in ("localhost", "127.0.0.1", "::1"):
            if item.lower() not in existing:
                values.append(item)
        os.environ[key] = ",".join(values)


def output_path(args: argparse.Namespace) -> Path:
    run_dir = ensure_run_dir(Path(args.output_dir))
    if args.cik:
        return run_dir / f"company_{args.cik}" / ".artifact_anchor"
    return default_output_file(str(run_dir), "", "", suffix="insights")


def init_tool_usage() -> dict[str, dict[str, int]]:
    names = [
        "ddrbench_sqlite_get_database_info",
        "ddrbench_sqlite_describe_table",
        "ddrbench_sqlite_execute_query",
        "ddrbench_code_execute_code",
        "ddrbench_code_list_files",
        "ddrbench_code_get_field_description",
    ]
    return {name: {"calls": 0, "successes": 0, "failures": 0} for name in names}


def record_tool_usage(
    tool_usage: dict[str, dict[str, int]], name: str, result: dict[str, Any]
) -> None:
    stats = tool_usage.setdefault(name, {"calls": 0, "successes": 0, "failures": 0})
    stats["calls"] += 1
    if isinstance(result, dict) and result.get("error"):
        stats["failures"] += 1
    else:
        stats["successes"] += 1


async def call_mcp_tool(mcp_url: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with sse_client(mcp_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    if result.structuredContent is not None:
        return result.structuredContent
    if result.content:
        text = getattr(result.content[0], "text", "")
        try:
            return json.loads(text)
        except Exception:
            return {"text": text}
    return {}


def build_system_prompt(args: argparse.Namespace, task: str) -> str:
    base_rules = AGENT_RULES_PATH.read_text(encoding="utf-8")
    return (
        f"{base_rules}\n\n"
        "## CubePi Runtime Notes\n\n"
        "- Available tools use the same DDR_Bench-prefixed names described in the rules.\n"
        "- Preserve exploration depth: investigate enough evidence to support at least "
        f"{args.min_insights} distinct high-value findings.\n"
        f"- Target task: {task}.\n"
    )


def build_provider(args: argparse.Namespace):
    provider_name = args.provider.lower()
    if provider_name == "openai":
        api_key = args.api_key or os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY") or "EMPTY"
        return OpenAIProvider(
            provider_id="openai",
            api_key=api_key,
            base_url=args.base_url or os.getenv("MODEL_BASE_URL") or None,
        )
    if provider_name == "anthropic":
        api_key = args.api_key or os.getenv("ANTHROPIC_API_KEY")
        return AnthropicProvider(
            provider_id="anthropic",
            api_key=api_key,
            base_url=args.base_url or os.getenv("ANTHROPIC_BASE_URL") or None,
        )
    raise ValueError(f"Unsupported provider: {args.provider}")


def build_tools(args: argparse.Namespace, tool_usage: dict[str, dict[str, int]], active_servers: set[str]):
    async def call_and_record(url: str, tool_name: str, arguments: dict[str, Any], public_name: str) -> str:
        try:
            result = await call_mcp_tool(url, tool_name, arguments)
        except Exception as exc:
            result = {"error": str(exc)}
        record_tool_usage(tool_usage, public_name, result)
        return compact_json(result)

    tools = []

    if SQLITE_SERVER_NAME in active_servers:
        @tool(execution_mode="sequential")
        async def ddrbench_sqlite_get_database_info() -> str:
            """Get general information about the DDR_Bench 10-K SQLite database."""
            return await call_and_record(
                args.sqlite_mcp_url,
                "get_database_info",
                {},
                "ddrbench_sqlite_get_database_info",
            )

        @tool(execution_mode="sequential")
        async def ddrbench_sqlite_describe_table(table_name: str) -> str:
            """Get columns and schema details for a SQLite table."""
            return await call_and_record(
                args.sqlite_mcp_url,
                "describe_table",
                {"table_name": table_name},
                "ddrbench_sqlite_describe_table",
            )

        @tool(execution_mode="sequential")
        async def ddrbench_sqlite_execute_query(query: str, limit: int = 20) -> str:
            """Execute a read-only SQL query against the DDR_Bench 10-K database."""
            return await call_and_record(
                args.sqlite_mcp_url,
                "execute_query",
                {"query": query, "limit": limit},
                "ddrbench_sqlite_execute_query",
            )

        tools.extend([
            ddrbench_sqlite_get_database_info,
            ddrbench_sqlite_describe_table,
            ddrbench_sqlite_execute_query,
        ])

    if CODE_SERVER_NAME in active_servers:
        @tool(execution_mode="sequential")
        async def ddrbench_code_execute_code(code: str, timeout: int = 30) -> str:
            """Execute read-only Python analysis code in the DDR_Bench data root."""
            return await call_and_record(
                args.code_mcp_url,
                "execute_code",
                {"code": code, "timeout": timeout},
                "ddrbench_code_execute_code",
            )

        @tool(execution_mode="sequential")
        async def ddrbench_code_list_files(path: str = ".", pattern: str | None = None, recursive: bool = False) -> str:
            """List files available through the DDR_Bench code MCP server."""
            arguments: dict[str, Any] = {"path": path, "recursive": recursive}
            if pattern:
                arguments["pattern"] = pattern
            return await call_and_record(
                args.code_mcp_url,
                "list_files",
                arguments,
                "ddrbench_code_list_files",
            )

        @tool(execution_mode="sequential")
        async def ddrbench_code_get_field_description(data_file: str) -> str:
            """Get field descriptions for a data file exposed by the code MCP server."""
            return await call_and_record(
                args.code_mcp_url,
                "get_field_description",
                {"data_file": data_file},
                "ddrbench_code_get_field_description",
            )

        tools.extend([
            ddrbench_code_execute_code,
            ddrbench_code_list_files,
            ddrbench_code_get_field_description,
        ])

    return tools


def message_text(message: Any) -> str:
    parts: list[str] = []
    for item in getattr(message, "content", []) or []:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", "") or "")
    return "".join(parts)


def event_to_line(event: Any) -> str | None:
    event_type = getattr(event, "type", None)
    if event_type == "message_update":
        stream_event = getattr(event, "stream_event", None)
        if getattr(stream_event, "type", None) == "text_delta":
            return getattr(stream_event, "delta", "")
    if event_type == "message_end":
        message = getattr(event, "message", None)
        role = getattr(message, "role", "")
        text = message_text(message)
        suffix = f" role={role}"
        if text.strip():
            suffix += f" text_chars={len(text)}"
        return f"\n[event] {event_type}{suffix}\n"
    if event_type in {"tool_execution_start", "tool_execution_end", "agent_start", "agent_end", "turn_start", "turn_end"}:
        data = getattr(event, "__dict__", {})
        if hasattr(event, "model_dump"):
            data = event.model_dump()
        tool_name = data.get("tool_name") or data.get("name") or ""
        suffix = f": {tool_name}" if tool_name else ""
        if event_type == "tool_execution_end" and data.get("is_error"):
            suffix += " error=True"
        return f"\n[event] {event_type}{suffix}\n"
    return None


def add_message_token_usage(total: dict[str, int], message: Any) -> None:
    """Accumulate CubePI provider usage from one assistant message."""

    usage = getattr(message, "usage", None)
    if usage is None:
        return
    prompt_tokens = (
        int(getattr(usage, "input_tokens", 0) or 0)
        + int(getattr(usage, "cache_read_tokens", 0) or 0)
        + int(getattr(usage, "cache_write_tokens", 0) or 0)
    )
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    total["prompt_tokens"] += prompt_tokens
    total["completion_tokens"] += completion_tokens
    total["total_tokens"] += prompt_tokens + completion_tokens
    total["model_calls"] += 1


def make_cubepi_text_generator(model: Any, token_usage: dict[str, int]):
    """Adapt the exploration model to the shared text-generator interface."""

    async def generate(
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> str:
        system_prompt = "\n\n".join(
            message["content"] for message in messages if message["role"] == "system"
        )
        user_text = "\n\n".join(
            message["content"] for message in messages if message["role"] != "system"
        )
        response = await model.generate(
            [UserMessage(content=[TextContent(text=user_text)])],
            system_prompt=system_prompt,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
        add_message_token_usage(token_usage, response)
        return message_text(response)

    return generate


async def run_agent_async(args: argparse.Namespace) -> str:
    load_env_file(args.env_file)

    task = build_task(args.cik or "", args.question or "")
    output_file = output_path(args)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file = output_file.parent / "prompt.txt"
    prompt = (
        f"Analyze company with CIK {args.cik}. "
        f"Explore enough local evidence to support at least {args.min_insights} "
        "distinct high-value findings."
        if args.cik
        else args.question or task
    )
    prompt_file.write_text(prompt + "\n", encoding="utf-8")

    tool_usage = init_tool_usage()
    token_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "model_calls": 0,
    }
    _mcp_config, mcp_resolution = build_active_mcp_config(args.config, args.scenario, args.mcp_mode)
    active_servers = server_names_from_resolution(mcp_resolution)
    sqlite_port = find_available_port()
    code_port = find_available_port()
    if not args.no_auto_mcp:
        args.sqlite_mcp_url = f"http://127.0.0.1:{sqlite_port}/sse"
        args.code_mcp_url = f"http://127.0.0.1:{code_port}/sse"
    provider = build_provider(args)
    model = provider.model(
        args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        context_window=args.context_window,
    )
    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(args, task),
        tools=build_tools(args, tool_usage, active_servers),
        tool_execution="sequential",
    )

    log_path = output_file.parent / "run.log"
    started_at = time.time()
    artifact_session_id = session_timestamp()
    chunks: list[str] = []
    assistant_texts: list[str] = []
    trajectory = CubePiTrajectoryCollector()

    with managed_mcp_servers(
        mcp_resolution,
        config_path=args.config,
        scenario=args.scenario,
        log_dir=output_file.parent,
        enabled=not args.no_auto_mcp,
        sqlite_port=sqlite_port,
        code_port=code_port,
    ), log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cubepi run_single provider={args.provider} model={args.model}\n\n")
        log.write("MCP resolution: " + json.dumps(mcp_resolution, ensure_ascii=False) + "\n\n")
        log.write(prompt + "\n\n")
        log.flush()

        def on_event(event, signal=None):
            trajectory.handle(event)
            line = event_to_line(event)
            if line is None:
                return
            log.write(line)
            log.flush()
            if line and not line.startswith("\n[event]"):
                chunks.append(line)
            event_type = getattr(event, "type", None)
            if event_type == "message_end":
                message = getattr(event, "message", None)
                if getattr(message, "role", None) == "assistant":
                    add_message_token_usage(token_usage, message)
                    text = message_text(message).strip()
                    if text:
                        assistant_texts.append(text)
            elif event_type == "agent_end":
                for message in getattr(event, "messages", []) or []:
                    if getattr(message, "role", None) == "assistant":
                        text = message_text(message).strip()
                        if text:
                            assistant_texts.append(text)

        agent.subscribe(on_event)
        try:
            run_id = await asyncio.wait_for(agent.prompt(prompt), timeout=args.timeout)
            log.write(f"\n[event] prompt_return run_id={run_id}\n")
            log.flush()
        except Exception as exc:
            raise

    artifact_paths = await generate_artifacts(
        turns=trajectory.turns,
        task=task,
        generator=make_cubepi_text_generator(model, token_usage),
        output_dir=output_file.parent,
        settings=InsightGenerationSettings(
            insight_max_tokens=args.insight_max_tokens,
            summary_max_tokens=args.summary_max_tokens,
            insight_temperature=args.insight_temperature,
        ),
        session_id=artifact_session_id,
        runtime_metadata={
            "framework": "cubepi",
            "provider": args.provider,
            "model": args.model,
            "started_at": started_at,
            "ended_at": time.time(),
            "token_usage": token_usage,
        },
    )
    return (
        f"Captured {len(trajectory.turns)} tool turns and saved DDR artifacts: "
        f"{artifact_paths}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CubePi DDR_Bench 10-K insight discovery runner")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default=os.getenv("CUBEPI_PROVIDER", "openai"))
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("MODEL_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", "gpt-5.5"))
    parser.add_argument("--sqlite-mcp-url", default=os.getenv("SQLITE_MCP_URL", SQLITE_MCP_URL))
    parser.add_argument("--code-mcp-url", default=os.getenv("CODE_MCP_URL", CODE_MCP_URL))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--mcp-mode", choices=["auto", "all", "none"], default="auto")
    parser.add_argument("--no-auto-mcp", action="store_true")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--cik", help="10-K company CIK. Builds the task: Analyze company with CIK {cik}")
    parser.add_argument("--question", help="Optional custom task override. If omitted, --cik is required.")
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--output-dir", default="./outputs/cubepi")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--context-window", type=int, default=200000)
    parser.add_argument("--insight-max-tokens", type=int, default=512)
    parser.add_argument("--summary-max-tokens", type=int, default=16384)
    parser.add_argument("--insight-temperature", type=float, default=0.5)
    args = parser.parse_args()
    if not args.cik and not args.question:
        parser.error("Either --cik or --question is required.")
    return args


def main() -> int:
    print(asyncio.run(run_agent_async(parse_args())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
