#!/usr/bin/env python3
"""Shared subprocess batch runner for insight discovery agents."""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List


def load_entity_ids(path: Path) -> List[str]:
    data = json.load(open(path, "r", encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Expected {path} to contain a JSON list.")
    return [str(item) for item in data]


def parse_target_ids(value: str) -> set[str] | None:
    if not value:
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def read_json_field(output_file: Path, key: str, default):
    if not output_file.exists():
        return default
    try:
        return json.load(open(output_file, "r", encoding="utf-8")).get(key, default) or default
    except Exception:
        return default


def add_token_usage(total: Dict[str, int], usage: Dict[str, int]) -> None:
    total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    total["total_tokens"] += int(usage.get("total_tokens") or 0)


def empty_tool_usage() -> Dict[str, Dict[str, int]]:
    return {name: {"calls": 0, "successes": 0, "failures": 0} for name in ["search", "fetch", "code_interpreter"]}


def add_tool_usage(total: Dict[str, Dict[str, int]], usage: Dict[str, Dict[str, int]]) -> None:
    for name, stats in usage.items():
        total.setdefault(name, {"calls": 0, "successes": 0, "failures": 0})
        total[name]["calls"] += int(stats.get("calls") or 0)
        total[name]["successes"] += int(stats.get("successes") or 0)
        total[name]["failures"] += int(stats.get("failures") or 0)


def add_common_batch_args(parser: argparse.ArgumentParser, default_output_dir: str = "") -> None:
    parser.add_argument("--id-file", default="./data/10k/entity_ids.json")
    parser.add_argument("--output-dir", default=default_output_dir, required=not bool(default_output_dir))
    parser.add_argument("--target-ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--timeout", type=int, default=36000)
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[2]))


def run_subprocess_batch(args: argparse.Namespace, single_script: Path, extra_args: List[str]) -> Dict:
    entity_ids = load_entity_ids(Path(args.id_file))
    target_ids = parse_target_ids(args.target_ids)
    if target_ids is not None:
        entity_ids = [cik for cik in entity_ids if cik in target_ids]
    if args.limit:
        entity_ids = entity_ids[: args.limit]

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "started_at": datetime.now().isoformat(),
        "id_file": str(Path(args.id_file).resolve()),
        "output_dir": str(output_root.resolve()),
        "total_requested": len(entity_ids),
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "tool_usage": empty_tool_usage(),
        "results": [],
    }

    for index, cik in enumerate(entity_ids, 1):
        company_dir = output_root / f"company_{cik}"
        output_file = company_dir / "insights.json"
        if args.skip_existing and output_file.exists():
            token_usage = read_json_field(output_file, "token_usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
            tool_usage = read_json_field(output_file, "tool_usage", empty_tool_usage())
            add_token_usage(manifest["token_usage"], token_usage)
            add_tool_usage(manifest["tool_usage"], tool_usage)
            manifest["results"].append({"cik": cik, "status": "skipped", "output_file": str(output_file), "token_usage": token_usage, "tool_usage": tool_usage})
            print(f"[{index}/{len(entity_ids)}] skip existing company {cik}")
            continue

        company_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(single_script), "--cik", cik, "--output-file", str(output_file), *extra_args]
        print(f"[{index}/{len(entity_ids)}] run company {cik}")
        print(" ".join(cmd))
        process = subprocess.run(cmd, cwd=args.cwd, env=os.environ.copy(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=args.timeout)
        log_file = company_dir / "run.log"
        stdout_text = process.stdout or ""
        if log_file.exists() and log_file.stat().st_size > 0:
            with log_file.open("a", encoding="utf-8") as f:
                f.write("\n\n[subprocess stdout]\n")
                f.write(stdout_text)
        else:
            log_file.write_text(stdout_text, encoding="utf-8")

        status = "success" if process.returncode == 0 else "failed"
        token_usage = read_json_field(output_file, "token_usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        tool_usage = read_json_field(output_file, "tool_usage", empty_tool_usage())
        add_token_usage(manifest["token_usage"], token_usage)
        add_tool_usage(manifest["tool_usage"], tool_usage)
        manifest["results"].append({"cik": cik, "status": status, "returncode": process.returncode, "output_file": str(output_file), "log_file": str(log_file), "token_usage": token_usage, "tool_usage": tool_usage})
        print(f"[{index}/{len(entity_ids)}] {status} company {cik} tokens={token_usage.get('total_tokens', 0)}")
        if process.returncode != 0 and args.stop_on_error:
            break

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["success_count"] = sum(1 for item in manifest["results"] if item["status"] == "success")
    manifest["failed_count"] = sum(1 for item in manifest["results"] if item["status"] == "failed")
    manifest["skipped_count"] = sum(1 for item in manifest["results"] if item["status"] == "skipped")
    json.dump(manifest, open(output_root / "batch_manifest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Manifest saved to {output_root / 'batch_manifest.json'}")
    return manifest
