#!/usr/bin/env python3
"""Pairwise novelty evaluation for unused DDR_Bench insight outputs."""

import argparse
import csv
import itertools
import json
import logging
import math
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import get_config
from evaluate.base_evaluator import BaseEvaluator


LOGGER = logging.getLogger("novelty_eval")


TEN_K_NOVELTY_SYSTEM = """You are an expert financial analyst. You will compare two sets of novel financial insights about the same company.

Company Context: These insights were generated during 10-K analysis but were NOT used to answer specific financial questions correctly. They represent potentially valuable but unused observations.

Your task: Determine which set provides MORE valuable information for investment analysis and business understanding.

Consider:
- Investment value: Does it inform investment decisions?
- Business strategy implications: Does it reveal strategic directions or challenges?
- Financial health indicators: Does it highlight financial strengths or risks?
- Competitive positioning: Does it clarify market position or advantages?
- Depth of insight: Does it reveal meaningful patterns or trends?
- Do not be biased by the length, number of insights, fluency, etc. Just focus on the usefulness of the insights.

Respond in EXACTLY this format (two lines):
Line 1: One sentence explaining your reasoning (max 100 words)
Line 2: Your decision - ONLY one of: MODEL_A, MODEL_B, or TIE"""


TEN_K_NOVELTY_USER = """Insights from Model A:
{insights_a}

Insights from Model B:
{insights_b}

Your response:"""


@dataclass
class MethodInput:
    name: str
    eval_result: Path
    logs_dir: Path


def parse_method_arg(value: str) -> MethodInput:
    parts = value.split("=", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "--method must use name=eval_result_json=logs_dir, e.g. cubepi=outputs/cubepi/10k_evaluation_result.json=eval/cubepi_10k"
        )
    name, eval_result, logs_dir = parts
    return MethodInput(name=name.strip(), eval_result=Path(eval_result), logs_dir=Path(logs_dir))


def load_insights(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    insights = []
    for row in rows:
        insight = (row.get("insight") or "").strip()
        if not insight or "NO INSIGHT" in insight.upper():
            continue
        try:
            data = json.loads(insight)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("insights"), list):
            for item in data["insights"]:
                if isinstance(item, str):
                    item = {"insight": item}
                if not isinstance(item, dict):
                    continue
                nested_insight = (item.get("insight") or "").strip()
                if not nested_insight:
                    continue
                insights.append({
                    "index": len(insights),
                    "topic": (item.get("topic") or row.get("assistant_message") or "").strip(),
                    "insight": nested_insight,
                })
            continue
        if insight.lstrip().startswith("{") and '"insights"' in insight:
            matches = re.findall(
                r'"topic"\s*:\s*"((?:\\.|[^"\\])*)".*?"insight"\s*:\s*"((?:\\.|[^"\\])*)"',
                insight,
                flags=re.DOTALL,
            )
            if matches:
                for topic, nested_insight in matches:
                    try:
                        topic = json.loads(f'"{topic}"')
                    except json.JSONDecodeError:
                        topic = topic.replace('\\"', '"')
                    try:
                        nested_insight = json.loads(f'"{nested_insight}"')
                    except json.JSONDecodeError:
                        nested_insight = nested_insight.replace('\\"', '"')
                    nested_insight = nested_insight.strip()
                    if nested_insight:
                        insights.append({
                            "index": len(insights),
                            "topic": topic.strip(),
                            "insight": nested_insight,
                        })
                continue
        insights.append({
            "index": len(insights),
            "topic": (row.get("assistant_message") or "").strip(),
            "insight": insight,
        })
    return insights


def used_indices(entity_result: Dict[str, Any], unused_mode: str) -> set[int]:
    used: set[int] = set()
    for qa_result in entity_result.get("message_wise_context_results", []):
        if unused_mode == "unused_from_insufficient_qas" and qa_result.get("context_quality") != "INSUFFICIENT_INFO":
            continue
        for key in ("supporting_message_indices", "contradicting_message_indices"):
            for value in qa_result.get(key, []) or []:
                try:
                    used.add(int(value))
                except (TypeError, ValueError):
                    continue
    return used


def extract_method_novel_insights(method: MethodInput, unused_mode: str) -> Dict[str, Dict[str, Any]]:
    eval_data = json.load(method.eval_result.open(encoding="utf-8"))
    by_entity = {}
    for entity_result in eval_data.get("entity_results", []):
        entity_id = str(entity_result.get("entity_id", ""))
        if not entity_id:
            continue
        insights_file = method.logs_dir / f"company_{entity_id}" / "insights_research.csv"
        if not insights_file.exists():
            LOGGER.warning("Missing insights file for %s/%s: %s", method.name, entity_id, insights_file)
            continue
        insights = load_insights(insights_file)
        used = used_indices(entity_result, unused_mode)
        novel = [item for item in insights if int(item["index"]) not in used]
        valid_used = sorted(index for index in used if 0 <= index < len(insights))
        by_entity[entity_id] = {
            "method": method.name,
            "entity_id": entity_id,
            "insights_file": str(insights_file),
            "total_insights": len(insights),
            "used_indices": valid_used,
            "used_indices_all": sorted(used),
            "used_count": len(valid_used),
            "used_out_of_range_count": len(used) - len(valid_used),
            "novel_count": len(novel),
            "novel_insights": novel,
        }
    return by_entity


def format_insight_set(items: List[Dict[str, Any]], max_chars: int) -> str:
    lines = []
    for display_index, item in enumerate(items, 1):
        topic = item.get("topic") or f"Insight {item.get('index')}"
        text = item.get("insight", "")
        lines.append(f"{display_index}. {topic}: {text}")
    rendered = "\n".join(lines) if lines else "(No unused insights.)"
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars].rstrip() + "\n...[truncated]"


def build_judge(config_path: str, provider: str = "", model: str = "") -> BaseEvaluator:
    config = get_config(config_path)
    provider = provider or config.evaluation.provider or "openai"
    model = model or config.evaluation.model or "gpt-5-mini"
    return BaseEvaluator(
        scenario="10k",
        entity_prefix="company",
        provider=provider,
        openai_model=model,
        azure_model=model,
        max_retries=config.evaluation.max_retries or 5,
        retry_delay=config.evaluation.retry_delay or 2.0,
    )


def parse_decision(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        upper = line.upper()
        if upper in {"MODEL_A", "MODEL_B", "TIE"}:
            return upper
    match = re.search(r"\b(MODEL_A|MODEL_B|TIE)\b", (text or "").upper())
    return match.group(1) if match else "PARSE_FAILED"


def compare_pair(
    judge: BaseEvaluator,
    method_a: str,
    method_b: str,
    entity_id: str,
    insights_a: List[Dict[str, Any]],
    insights_b: List[Dict[str, Any]],
    rng: random.Random,
    max_chars: int,
) -> Dict[str, Any]:
    swapped = rng.random() < 0.5
    presented_a_method = method_b if swapped else method_a
    presented_b_method = method_a if swapped else method_b
    presented_a = insights_b if swapped else insights_a
    presented_b = insights_a if swapped else insights_b

    messages = [
        {"role": "system", "content": TEN_K_NOVELTY_SYSTEM},
        {
            "role": "user",
            "content": TEN_K_NOVELTY_USER.format(
                insights_a=format_insight_set(presented_a, max_chars),
                insights_b=format_insight_set(presented_b, max_chars),
            ),
        },
    ]
    raw = judge.call_llm_api(messages, max_tokens=512, temperature=0.0)
    decision = parse_decision(raw)

    if decision == "MODEL_A":
        winner = presented_a_method
    elif decision == "MODEL_B":
        winner = presented_b_method
    elif decision == "TIE":
        winner = "TIE"
    else:
        winner = "PARSE_FAILED"

    return {
        "entity_id": entity_id,
        "method_1": method_a,
        "method_2": method_b,
        "presented_a_method": presented_a_method,
        "presented_b_method": presented_b_method,
        "presented_order_swapped": swapped,
        "method_1_novel_count": len(insights_a),
        "method_2_novel_count": len(insights_b),
        "decision": decision,
        "winner": winner,
        "raw_response": raw,
    }


def bradley_terry_scores(methods: List[str], outcomes: List[Dict[str, Any]]) -> Dict[str, float]:
    wins = {name: 0.0 for name in methods}
    games = {(a, b): 0.0 for a in methods for b in methods if a != b}
    for item in outcomes:
        a = item["method_1"]
        b = item["method_2"]
        winner = item["winner"]
        if winner == "PARSE_FAILED":
            continue
        games[(a, b)] += 1
        games[(b, a)] += 1
        if winner == "TIE":
            wins[a] += 0.5
            wins[b] += 0.5
        elif winner in wins:
            wins[winner] += 1.0

    if not any(games.values()):
        return {name: 0.0 for name in methods}

    strengths = {name: 1.0 for name in methods}
    for _ in range(1000):
        updated = {}
        max_delta = 0.0
        for i in methods:
            denom = 0.0
            for j in methods:
                if i == j:
                    continue
                n_ij = games.get((i, j), 0.0)
                if n_ij:
                    denom += n_ij / (strengths[i] + strengths[j])
            updated[i] = wins[i] / denom if denom > 0 and wins[i] > 0 else 1e-9
        mean_strength = sum(updated.values()) / len(updated)
        if mean_strength > 0:
            updated = {name: value / mean_strength for name, value in updated.items()}
        max_delta = max(abs(updated[name] - strengths[name]) for name in methods)
        strengths = updated
        if max_delta < 1e-9:
            break

    log_scores = {name: math.log(max(value, 1e-12)) for name, value in strengths.items()}
    center = sum(log_scores.values()) / len(log_scores)
    return {name: round(score - center, 6) for name, score in log_scores.items()}


def write_novel_manifest(path: Path, novel_by_method: Dict[str, Dict[str, Dict[str, Any]]]) -> None:
    rows = []
    for method, by_entity in novel_by_method.items():
        for entity_id, item in by_entity.items():
            rows.append({
                "method": method,
                "entity_id": entity_id,
                "total_insights": item["total_insights"],
                "used_count": item["used_count"],
                "used_out_of_range_count": item["used_out_of_range_count"],
                "novel_count": item["novel_count"],
                "used_indices_json": json.dumps(item["used_indices"]),
                "used_indices_all_json": json.dumps(item["used_indices_all"]),
                "novel_insights_json": json.dumps(item["novel_insights"], ensure_ascii=False),
                "insights_file": item["insights_file"],
            })
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "method", "entity_id", "total_insights", "used_count", "novel_count",
            "used_out_of_range_count", "used_indices_json", "used_indices_all_json",
            "novel_insights_json", "insights_file",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pairwise novelty evaluation for unused insights")
    parser.add_argument(
        "--method",
        action="append",
        type=parse_method_arg,
        required=True,
        help="Repeatable: name=eval_result_json=logs_dir",
    )
    parser.add_argument("--output-dir", default="./outputs/novelty_eval")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--scenario", default="10k", choices=["10k"])
    parser.add_argument("--unused-mode", default="unused_from_insufficient_qas", choices=["all_unused", "unused_from_insufficient_qas"])
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-insights-chars", type=int, default=12000)
    parser.add_argument("--dry-run", action="store_true", help="Only extract unused insights; do not call the judge")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if len(args.method) < 2 and not args.dry_run:
        raise SystemExit("At least two --method values are required for pairwise judging.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    novel_by_method = {
        method.name: extract_method_novel_insights(method, args.unused_mode)
        for method in args.method
    }
    manifest_path = output_dir / "novel_insights_manifest.csv"
    write_novel_manifest(manifest_path, novel_by_method)
    LOGGER.info("Wrote novel insight manifest: %s", manifest_path)

    if args.dry_run:
        return

    rng = random.Random(args.seed)
    judge = build_judge(args.config, args.provider, args.model)
    method_names = [method.name for method in args.method]
    common_entities = sorted(set.intersection(*(set(novel_by_method[name]) for name in method_names)))

    outcomes = []
    for entity_id in common_entities:
        for method_a, method_b in itertools.combinations(method_names, 2):
            insights_a = novel_by_method[method_a][entity_id]["novel_insights"]
            insights_b = novel_by_method[method_b][entity_id]["novel_insights"]
            for repeat in range(args.repeats):
                LOGGER.info("Judging %s: %s vs %s repeat %d/%d", entity_id, method_a, method_b, repeat + 1, args.repeats)
                outcome = compare_pair(
                    judge,
                    method_a,
                    method_b,
                    entity_id,
                    insights_a,
                    insights_b,
                    rng,
                    args.max_insights_chars,
                )
                outcome["repeat"] = repeat
                outcomes.append(outcome)
                time.sleep(0.5)

    outcomes_path = output_dir / "pairwise_outcomes.jsonl"
    with outcomes_path.open("w", encoding="utf-8") as f:
        for item in outcomes:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    summary_counts = {name: {"wins": 0, "losses": 0, "ties": 0, "parse_failed": 0} for name in method_names}
    for item in outcomes:
        a, b, winner = item["method_1"], item["method_2"], item["winner"]
        if winner == "TIE":
            summary_counts[a]["ties"] += 1
            summary_counts[b]["ties"] += 1
        elif winner == "PARSE_FAILED":
            summary_counts[a]["parse_failed"] += 1
            summary_counts[b]["parse_failed"] += 1
        else:
            loser = b if winner == a else a
            summary_counts[winner]["wins"] += 1
            summary_counts[loser]["losses"] += 1

    bt_scores = bradley_terry_scores(method_names, outcomes)
    summary_rows = []
    for name in method_names:
        counts = summary_counts[name]
        decided = counts["wins"] + counts["losses"] + counts["ties"]
        summary_rows.append({
            "method": name,
            **counts,
            "decided_comparisons": decided,
            "win_rate_with_half_ties": round((counts["wins"] + 0.5 * counts["ties"]) / decided, 4) if decided else 0,
            "bradley_terry_score": bt_scores.get(name, 0.0),
        })

    summary_csv = output_dir / "novelty_pairwise_summary.csv"
    with summary_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        writer.writeheader()
        writer.writerows(summary_rows)

    summary_json = output_dir / "novelty_pairwise_summary.json"
    summary_json.write_text(
        json.dumps(
            {
                "methods": method_names,
                "entity_count": len(common_entities),
                "comparison_count": len(outcomes),
                "unused_mode": args.unused_mode,
                "summary": summary_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    LOGGER.info("Wrote pairwise outcomes: %s", outcomes_path)
    LOGGER.info("Wrote novelty summary: %s", summary_csv)


if __name__ == "__main__":
    main()
