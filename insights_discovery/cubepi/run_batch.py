#!/usr/bin/env python3
"""Run CubePi insight discovery over DDR_Bench entity_ids.json."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.batch import add_common_batch_args, run_subprocess_batch  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch runner for CubePi DDR_Bench insight discovery")
    add_common_batch_args(parser, default_output_dir="./outputs/cubepi")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default=os.getenv("CUBEPI_PROVIDER", "openai"))
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("MODEL_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", "gpt-5.5"))
    parser.add_argument("--sqlite-mcp-url", default=os.getenv("SQLITE_MCP_URL", "http://127.0.0.1:8765/sse"))
    parser.add_argument("--code-mcp-url", default=os.getenv("CODE_MCP_URL", "http://127.0.0.1:8766/sse"))
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--scenario", default="10k")
    parser.add_argument("--mcp-mode", choices=["auto", "all", "none"], default="auto")
    parser.add_argument("--no-auto-mcp", action="store_true")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--context-window", type=int, default=200000)
    return parser.parse_args()


def build_extra_args(args: argparse.Namespace) -> List[str]:
    extra = [
        "--provider", args.provider,
        "--model", args.model,
        "--sqlite-mcp-url", args.sqlite_mcp_url,
        "--code-mcp-url", args.code_mcp_url,
        "--config", str(args.config),
        "--scenario", args.scenario,
        "--mcp-mode", args.mcp_mode,
        "--env-file", args.env_file,
        "--min-insights", str(args.min_insights),
        "--timeout", str(args.timeout),
        "--temperature", str(args.temperature),
        "--max-tokens", str(args.max_tokens),
        "--context-window", str(args.context_window),
    ]
    if args.base_url:
        extra.extend(["--base-url", args.base_url])
    if args.api_key:
        extra.extend(["--api-key", args.api_key])
    if args.no_auto_mcp:
        extra.append("--no-auto-mcp")
    return extra


if __name__ == "__main__":
    args = parse_args()
    run_subprocess_batch(args, Path(__file__).with_name("run_single.py"), build_extra_args(args))
