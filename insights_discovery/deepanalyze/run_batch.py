#!/usr/bin/env python3
"""Run DeepAnalyze insight discovery over DDR_Bench entity_ids.json."""

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
    parser = argparse.ArgumentParser(description="Batch runner for DeepAnalyze DDR_Bench 10-K insight discovery")
    add_common_batch_args(parser, default_output_dir="./outputs/deepanalyze")
    parser.add_argument("--deepanalyze-url", default=os.getenv("DEEPANALYZE_URL", "http://localhost:8200/v1"))
    parser.add_argument("--api-key", default=os.getenv("DEEPANALYZE_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("DEEPANALYZE_MODEL", "DeepAnalyze-8B"))
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--min-insights", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.4)
    parser.add_argument("--api-timeout", type=float, default=14400.0)
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--request-retries", type=int, default=2)
    parser.add_argument("--retry-initial-delay", type=float, default=5.0)
    parser.add_argument("--retry-max-delay", type=float, default=60.0)
    parser.add_argument("--dump-raw-response", action="store_true")
    return parser.parse_args()


def build_extra_args(args: argparse.Namespace) -> List[str]:
    extra = [
        "--deepanalyze-url", args.deepanalyze_url,
        "--model", args.model,
        "--db", args.db,
        "--env-file", args.env_file,
        "--min-insights", str(args.min_insights),
        "--temperature", str(args.temperature),
        "--api-timeout", str(args.api_timeout),
        "--request-timeout", str(args.request_timeout),
        "--request-retries", str(args.request_retries),
        "--retry-initial-delay", str(args.retry_initial_delay),
        "--retry-max-delay", str(args.retry_max_delay),
    ]
    if args.api_key:
        extra.extend(["--api-key", args.api_key])
    if args.dump_raw_response:
        extra.append("--dump-raw-response")
    return extra


if __name__ == "__main__":
    args = parse_args()
    run_subprocess_batch(args, Path(__file__).with_name("run_single.py"), build_extra_args(args))
