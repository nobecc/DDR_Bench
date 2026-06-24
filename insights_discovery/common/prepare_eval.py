#!/usr/bin/env python3
"""Prepare insight-discovery outputs for DDR_Bench message-wise evaluation."""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List


def load_json(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def convert_one(source_file: Path, output_root: Path) -> Dict:
    company_dir = source_file.parent
    cik = company_dir.name.removeprefix("company_")
    data = load_json(source_file)
    insights = data.get("insights", []) or []

    target_dir = output_root / f"company_{cik}"
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().isoformat()
    insights_csv = target_dir / "insights_research.csv"
    written = 0
    with open(insights_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "assistant_message", "user_message", "insight"])
        writer.writeheader()
        for item in insights:
            if isinstance(item, str):
                item = {"insight": item}
            if not isinstance(item, dict):
                continue
            insight = (item.get("insight") or "").strip()
            if not insight:
                continue
            writer.writerow({
                "timestamp": timestamp,
                "assistant_message": item.get("topic", ""),
                "user_message": json.dumps(item.get("evidence", []), ensure_ascii=False),
                "insight": insight,
            })
            written += 1

    removed_session_stats = []
    for session_json in target_dir.glob("session_stats*.json"):
        session_json.unlink()
        removed_session_stats.append(str(session_json))

    return {
        "cik": cik,
        "insight_count": written,
        "source_file": str(source_file),
        "insights_csv": str(insights_csv),
        "removed_session_stats": removed_session_stats,
    }


def convert_all(source_dir: Path, output_dir: Path) -> List[Dict]:
    return [convert_one(source_file, output_dir) for source_file in sorted(source_dir.glob("company_*/insights.json"))]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare insight outputs for DDR_Bench message-wise evaluation")
    parser.add_argument("--source-dir", default="./outputs/openai_o4_mini_deep_research")
    parser.add_argument("--output-dir", default="./logs/openai_o4_mini_deep_research_10k")
    parser.add_argument("--manifest", default="./logs/openai_o4_mini_deep_research_10k/prepare_manifest.json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    converted = convert_all(Path(args.source_dir), Path(args.output_dir))
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"converted": converted}, f, ensure_ascii=False, indent=2)
    print(f"Converted {len(converted)} companies. Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
