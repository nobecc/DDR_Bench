#!/usr/bin/env python3
"""Parse a DeepAnalyze Markdown/JSON report into DDR_Bench insight files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.output import build_task, write_outputs  # noqa: E402
from insights_discovery.deepanalyze.run_single import markdown_to_data  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a DeepAnalyze report to DDR_Bench insights.json/.csv")
    parser.add_argument("--input", required=True, help="Markdown or JSON report produced by DeepAnalyze")
    parser.add_argument("--output-file", required=True, help="Target insights.json path; CSV is written next to it")
    parser.add_argument("--cik", default="")
    parser.add_argument("--question", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    task = build_task(args.cik, args.question)
    report = Path(args.input).read_text(encoding="utf-8")
    data = markdown_to_data(report, task, args.cik)
    paths = write_outputs(data, Path(args.output_file))
    print(f"Parsed {len(data.get('insights', []))} insights to {paths['json']} and {paths['csv']}")


if __name__ == "__main__":
    main()
