#!/usr/bin/env python3
"""Run DDR_Bench evaluation against insight logs without editing config.yaml."""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import get_config
from evaluate.unified_evaluator import UnifiedEvaluator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate insight logs with DDR_Bench metrics")
    parser.add_argument("--scenario", default="10k", choices=["10k", "mimic", "globem"])
    parser.add_argument("--logs-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--context-mode",
        default="chat-wise",
        choices=["chat-wise", "message-wise", "both"],
        help=(
            "chat-wise scores the full insight set as one context; "
            "message-wise scores numbered individual insights; both runs both modes."
        ),
    )
    parser.add_argument("--test-mode", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = get_config(args.config)
    scenario_config = config.get_scenario(args.scenario)

    os.environ["DDR_LOG_LEVEL"] = config.agent.log_level or "INFO"
    vllm_url = f"http://localhost:{config.provider.vllm_port or 8000}/v1/chat/completions"

    evaluator = UnifiedEvaluator(
        scenario=args.scenario,
        vllm_url=vllm_url,
        provider=config.evaluation.provider or "azure",
        openai_model=config.evaluation.model or "gpt-5-mini",
        azure_model=config.evaluation.model or "gpt-5-mini",
        max_retries=config.evaluation.max_retries or 5,
        retry_delay=config.evaluation.retry_delay or 2.0,
    )

    evaluator.run_evaluation(
        qa_file=scenario_config.qa_file,
        logs_dir=args.logs_dir,
        output_file=args.output,
        test_mode=args.test_mode,
        context_mode=args.context_mode,
    )
