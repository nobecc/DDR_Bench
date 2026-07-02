#!/usr/bin/env python3
"""Run OpenAI o4-mini-deep-research insight discovery over entity_ids.json."""

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
    parser = argparse.ArgumentParser(description="Batch runner for OpenAI-compatible 10-K insight discovery")
    add_common_batch_args(parser, default_output_dir="./outputs/openai_o4_mini_deep_research")
    parser.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL", "http://35.220.164.252:3888/v1"))
    parser.add_argument("--api-key", default=os.getenv("MODEL_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("MODEL_NAME", "openai/o4-mini-deep-research"))
    parser.add_argument("--mcp-url", default=os.getenv("SQLITE_MCP_URL", "http://127.0.0.1:8765/sse"))
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--file-root", action="append")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--api-timeout", type=float, default=0.0)
    parser.add_argument("--request-timeout", type=float, default=0.0)
    parser.add_argument("--create-request-timeout", type=float, default=0.0)
    parser.add_argument("--request-retries", type=int, default=5)
    parser.add_argument("--retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--retry-max-delay", type=float, default=60.0)
    parser.add_argument("--background", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--background-fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--stream-status", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--poll-status", action="store_true")
    parser.add_argument("--allow-web-search", action="store_true")
    parser.add_argument("--use-code-interpreter", action="store_true")
    parser.add_argument("--use-data-package", action="store_true")
    parser.add_argument("--data-package-only", action="store_true")
    parser.add_argument("--data-package-profile", choices=["compact", "summary", "full"], default=os.getenv("OPENAI_DATA_PACKAGE_PROFILE", "compact"))
    parser.add_argument("--max-attached-file-bytes", type=int, default=int(os.getenv("OPENAI_MAX_ATTACHED_FILE_BYTES", "1000000")))
    parser.add_argument("--file-input-mode", choices=["base64", "upload", "none"], default=os.getenv("OPENAI_FILE_INPUT_MODE", "base64"))
    parser.add_argument("--file-upload-purpose", default=os.getenv("OPENAI_FILE_UPLOAD_PURPOSE", "user_data"))
    parser.add_argument("--file-upload-model-field", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--code-interpreter-memory-limit", default=os.getenv("OPENAI_CODE_INTERPRETER_MEMORY_LIMIT", "4g"))
    parser.add_argument("--dump-raw-response", action="store_true")
    parser.add_argument("--print-request-id", action="store_true")
    parser.add_argument("--print-response-headers", action="store_true")
    parser.add_argument("--min-data-tool-calls", type=int, default=1)
    return parser.parse_args()


def build_extra_args(args: argparse.Namespace) -> List[str]:
    extra = [
        "--base-url", args.base_url,
        "--model", args.model,
        "--mcp-url", args.mcp_url,
        "--db", args.db,
        "--env-file", args.env_file,
        "--max-steps", str(args.max_steps),
        "--temperature", str(args.temperature),
        "--min-insights", str(args.min_insights),
        "--api-timeout", str(args.api_timeout),
        "--request-timeout", str(args.request_timeout),
        "--create-request-timeout", str(args.create_request_timeout),
        "--request-retries", str(args.request_retries),
        "--retry-initial-delay", str(args.retry_initial_delay),
        "--retry-max-delay", str(args.retry_max_delay),
        "--poll-interval", str(args.poll_interval),
        "--min-data-tool-calls", str(args.min_data_tool_calls),
    ]
    if not args.background:
        extra.append("--no-background")
    if not args.background_fallback:
        extra.append("--no-background-fallback")
    if not args.stream:
        extra.append("--no-stream")
    if args.stream_status:
        extra.append("--stream-status")
    if args.api_key:
        extra.extend(["--api-key", args.api_key])
    if args.poll_status:
        extra.append("--poll-status")
    if args.allow_web_search:
        extra.append("--allow-web-search")
    if args.use_code_interpreter:
        extra.append("--use-code-interpreter")
    if args.use_data_package:
        extra.append("--use-data-package")
    if args.data_package_only:
        extra.append("--data-package-only")
    extra.extend(["--data-package-profile", args.data_package_profile])
    extra.extend(["--max-attached-file-bytes", str(args.max_attached_file_bytes)])
    extra.extend(["--file-input-mode", args.file_input_mode])
    if args.file_upload_purpose:
        extra.extend(["--file-upload-purpose", args.file_upload_purpose])
    if not args.file_upload_model_field:
        extra.append("--no-file-upload-model-field")
    if args.code_interpreter_memory_limit:
        extra.extend(["--code-interpreter-memory-limit", args.code_interpreter_memory_limit])
    if args.dump_raw_response:
        extra.append("--dump-raw-response")
    if args.print_request_id:
        extra.append("--print-request-id")
    if args.print_response_headers:
        extra.append("--print-response-headers")
    for file_root in args.file_root or ["./data/10k"]:
        extra.extend(["--file-root", file_root])
    return extra


if __name__ == "__main__":
    args = parse_args()
    run_subprocess_batch(args, Path(__file__).with_name("run_single.py"), build_extra_args(args))
