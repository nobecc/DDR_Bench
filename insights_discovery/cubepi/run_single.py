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
    normalize_output,
    write_outputs,
)


AGENT_RULES_PATH = REPO_ROOT / ".deepagents" / "AGENTS.md"
DEFAULT_SQLITE_MCP_URL = "http://127.0.0.1:8765/sse"
DEFAULT_CODE_MCP_URL = "http://127.0.0.1:8766/sse"


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def make_env() -> dict[str, str]:
    env = os.environ.copy()
    no_proxy = env.get(
        "no_proxy",
        "localhost,127.0.0.1,::1,10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn",
    )
    for item in ["localhost", "127.0.0.1", "::1"]:
        if item not in no_proxy:
            no_proxy = f"{no_proxy},{item}" if no_proxy else item
    os.environ["no_proxy"] = no_proxy
    os.environ["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    env["NO_PROXY"] = no_proxy
    return env


def output_path(args: argparse.Namespace) -> Path:
    return default_output_file(args.output_dir, args.output_file or "", args.cik or "")


def init_tool_usage() -> dict[str, dict[str, int]]:
    names = [
        "ddrbench_sqlite_get_database_info",
        "ddrbench_sqlite_describe_table",
        "ddrbench_sqlite_search",
        "ddrbench_sqlite_execute_query",
        "ddrbench_sqlite_fetch",
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


def build_system_prompt(args: argparse.Namespace, task: str, output_file: Path) -> str:
    base_rules = AGENT_RULES_PATH.read_text(encoding="utf-8")
    return (
        f"{base_rules}\n\n"
        "## CubePi Runtime Notes\n\n"
        "- Available tools use the same DDR_Bench-prefixed names described in the rules.\n"
        "- Return one final valid JSON object only; the runner will persist it to disk.\n"
        f"- Target task: {task}.\n"
        f"- Target output path: {output_file.as_posix()}.\n"
        f"- Required minimum insights for this run: {args.min_insights}.\n"
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


def build_tools(args: argparse.Namespace, tool_usage: dict[str, dict[str, int]]):
    async def call_and_record(url: str, tool_name: str, arguments: dict[str, Any], public_name: str) -> str:
        try:
            result = await call_mcp_tool(url, tool_name, arguments)
        except Exception as exc:
            result = {"error": str(exc)}
        record_tool_usage(tool_usage, public_name, result)
        return compact_json(result)

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
    async def ddrbench_sqlite_search(query: str, max_results: int = 20) -> str:
        """Search records across all DDR_Bench SQLite tables."""
        return await call_and_record(
            args.sqlite_mcp_url,
            "search",
            {"query": query, "max_results": max_results},
            "ddrbench_sqlite_search",
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

    @tool(execution_mode="sequential")
    async def ddrbench_sqlite_fetch(id: str) -> str:
        """Fetch one full SQLite record by ID returned from search."""
        return await call_and_record(
            args.sqlite_mcp_url,
            "fetch",
            {"id": id},
            "ddrbench_sqlite_fetch",
        )

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

    return [
        ddrbench_sqlite_get_database_info,
        ddrbench_sqlite_describe_table,
        ddrbench_sqlite_search,
        ddrbench_sqlite_execute_query,
        ddrbench_sqlite_fetch,
        ddrbench_code_execute_code,
        ddrbench_code_list_files,
        ddrbench_code_get_field_description,
    ]


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


def output_is_parseable(data: dict[str, Any], min_insights: int) -> tuple[bool, str]:
    insights = data.get("insights", []) or []
    if len(insights) < min_insights:
        return False, f"Only {len(insights)} insights were produced; expected at least {min_insights}."
    if len(insights) == 1 and insights[0].get("topic") == "unparsed_output":
        return False, "The model response was not parseable JSON."
    return True, ""


async def run_agent_async(args: argparse.Namespace) -> str:
    load_env_file(args.env_file)
    make_env()

    task = build_task(args.cik or "", args.question or "")
    output_file = output_path(args)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file = output_file.parent / "prompt.txt"
    prompt = (
        f"Analyze company with CIK {args.cik}. "
        f"Produce at least {args.min_insights} high-value insights. "
        f"Save final JSON to {output_file.as_posix()}."
        if args.cik
        else args.question or task
    )
    prompt_file.write_text(prompt + "\n", encoding="utf-8")

    tool_usage = init_tool_usage()
    provider = build_provider(args)
    model = provider.model(
        args.model,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        context_window=args.context_window,
    )
    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(args, task, output_file),
        tools=build_tools(args, tool_usage),
        tool_execution="sequential",
    )

    log_path = output_file.parent / "run.log"
    started_at = time.time()
    chunks: list[str] = []
    assistant_texts: list[str] = []

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ cubepi run_single provider={args.provider} model={args.model}\n\n")
        log.write(prompt + "\n\n")
        log.flush()

        def on_event(event, signal=None):
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
            error_data = {
                "task": task,
                "cik": args.cik or "",
                "insights": [],
                "summary": "",
                "error": {"type": exc.__class__.__name__, "message": str(exc)},
                "tool_usage": tool_usage,
                "model_calls": {"attempts": 1, "completed": 0, "failures": 1},
            }
            write_outputs(error_data, output_file)
            raise

    raw_text = assistant_texts[-1] if assistant_texts else "".join(chunks)
    data = normalize_output(raw_text, task, args.cik or "")
    data["tool_usage"] = tool_usage
    data["model_calls"] = {"attempts": 1, "completed": 1, "failures": 0}
    data["runtime"] = {
        "provider": args.provider,
        "model": args.model,
        "started_at": started_at,
        "ended_at": time.time(),
    }
    acceptable, reason = output_is_parseable(data, args.min_insights)
    if not acceptable:
        data["warning"] = reason
        data["raw_model_output"] = raw_text
    paths = write_outputs(data, output_file)
    if not acceptable:
        raise RuntimeError(f"{reason} Saved output to {paths['json']} and {paths['csv']}")
    return f"Saved {len(data.get('insights', []))} insights to {paths['json']} and {paths['csv']}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CubePi DDR_Bench 10-K insight discovery runner")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default=os.getenv("CUBEPI_PROVIDER", "openai"))
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("MODEL_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", "gpt-5.5"))
    parser.add_argument("--sqlite-mcp-url", default=os.getenv("SQLITE_MCP_URL", DEFAULT_SQLITE_MCP_URL))
    parser.add_argument("--code-mcp-url", default=os.getenv("CODE_MCP_URL", DEFAULT_CODE_MCP_URL))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--cik", help="10-K company CIK. Builds the task: Analyze company with CIK {cik}")
    parser.add_argument("--question", help="Optional custom task override. If omitted, --cik is required.")
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--output-dir", default="./outputs/cubepi")
    parser.add_argument("--output-file", help="Exact JSON output path. CSV is written next to it.")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--context-window", type=int, default=200000)
    args = parser.parse_args()
    if not args.cik and not args.question:
        parser.error("Either --cik or --question is required.")
    return args


def main() -> int:
    print(asyncio.run(run_agent_async(parse_args())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
