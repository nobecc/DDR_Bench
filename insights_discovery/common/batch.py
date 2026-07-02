#!/usr/bin/env python3
"""Shared subprocess batch runner for insight discovery agents."""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
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


def read_company_token_usage(company_dir: Path, legacy_output_file: Path) -> Dict[str, int]:
    """Read native session token usage, falling back to legacy insights.json."""

    session_stats_files = sorted(
        company_dir.glob("session_stats_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in session_stats_files:
        usage = read_json_field(path, "token_usage", {})
        if usage:
            return usage
    return read_json_field(
        legacy_output_file,
        "token_usage",
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )


def add_token_usage(total: Dict[str, int], usage: Dict[str, int]) -> None:
    total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    total["total_tokens"] += int(usage.get("total_tokens") or 0)
    total["model_calls"] += int(usage.get("model_calls") or 0)


def empty_tool_usage() -> Dict[str, Dict[str, int]]:
    return {}


def add_tool_usage(total: Dict[str, Dict[str, int]], usage: Dict[str, Dict[str, int]]) -> None:
    for name, stats in usage.items():
        total.setdefault(name, {"calls": 0, "successes": 0, "failures": 0})
        total[name]["calls"] += int(stats.get("calls") or 0)
        total[name]["successes"] += int(stats.get("successes") or 0)
        total[name]["failures"] += int(stats.get("failures") or 0)


def collect_mcp_tool_call_stats(company_dir: Path) -> Dict:
    """Collect real MCP calls from the per-company CSV logs."""
    csv.field_size_limit(sys.maxsize)
    tool_usage: Dict[str, Dict[str, int]] = {}
    by_server: Dict[str, Dict[str, int]] = {}
    log_files: List[str] = []

    for path in sorted(company_dir.glob("*-mcp_calls_*.csv")):
        server_name = path.name.split("_calls_", 1)[0]
        file_has_calls = False
        try:
            with path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    tool_name = (row.get("tool_name") or "unknown").strip()
                    succeeded = (row.get("success") or "").strip().lower() == "true"
                    stats = tool_usage.setdefault(
                        tool_name,
                        {"calls": 0, "successes": 0, "failures": 0},
                    )
                    stats["calls"] += 1
                    stats["successes"] += int(succeeded)
                    stats["failures"] += int(not succeeded)
                    server_tools = by_server.setdefault(server_name, {})
                    server_tools[tool_name] = server_tools.get(tool_name, 0) + 1
                    file_has_calls = True
        except (OSError, csv.Error):
            continue
        if file_has_calls:
            log_files.append(str(path))

    total_calls = sum(stats["calls"] for stats in tool_usage.values())
    successful_calls = sum(stats["successes"] for stats in tool_usage.values())
    return {
        "total_tool_calls": total_calls,
        "successful_tool_calls": successful_calls,
        "failed_tool_calls": total_calls - successful_calls,
        "by_tool": {
            name: stats["calls"] for name, stats in sorted(tool_usage.items())
        },
        "by_server": {
            server: dict(sorted(tools.items()))
            for server, tools in sorted(by_server.items())
        },
        "log_files": log_files,
        "tool_usage": dict(sorted(tool_usage.items())),
    }


def add_tool_call_stats(total: Dict, stats: Dict) -> None:
    total["total_tool_calls"] += int(stats.get("total_tool_calls") or 0)
    total["successful_tool_calls"] += int(stats.get("successful_tool_calls") or 0)
    total["failed_tool_calls"] += int(stats.get("failed_tool_calls") or 0)
    for name, count in (stats.get("by_tool") or {}).items():
        total["by_tool"][name] = total["by_tool"].get(name, 0) + int(count or 0)
    for server, tools in (stats.get("by_server") or {}).items():
        server_total = total["by_server"].setdefault(server, {})
        for name, count in tools.items():
            server_total[name] = server_total.get(name, 0) + int(count or 0)


def empty_tool_call_stats() -> Dict:
    return {
        "total_tool_calls": 0,
        "successful_tool_calls": 0,
        "failed_tool_calls": 0,
        "by_tool": {},
        "by_server": {},
    }


def add_common_batch_args(parser: argparse.ArgumentParser, default_output_dir: str = "") -> None:
    parser.add_argument("--id-file", default="./data/10k/entity_ids.json")
    parser.add_argument("--output-dir", default=default_output_dir, required=not bool(default_output_dir))
    parser.add_argument("--target-ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--timeout", type=int, default=36000)
    parser.add_argument("--cwd", default=str(Path(__file__).resolve().parents[2]))


def run_subprocess_batch(
    args: argparse.Namespace,
    single_script: Path,
    extra_args: List[str],
    *,
    single_uses_output_dir: bool = False,
) -> Dict:
    batch_started = time.monotonic()
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
        "token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_calls": 0,
        },
        "tool_usage": empty_tool_usage(),
        "tool_call_stats": empty_tool_call_stats(),
        "model": getattr(args, "model", None),
        "provider": getattr(args, "provider", None),
        "config": str(getattr(args, "config", "")),
        "scenario": getattr(args, "scenario", None),
        "mcp_mode": getattr(args, "mcp_mode", None),
        "insight_generation": {
            "insight_max_tokens": getattr(args, "insight_max_tokens", None),
            "summary_max_tokens": getattr(args, "summary_max_tokens", None),
            "insight_temperature": getattr(args, "insight_temperature", None),
        },
        "results": [],
    }

    for index, cik in enumerate(entity_ids, 1):
        company_started = time.monotonic()
        company_dir = output_root / f"company_{cik}"
        output_file = company_dir / "insights.json"
        native_complete = bool(
            list(company_dir.glob("session_stats_*.json"))
            and list(company_dir.glob("insights_*.csv"))
        )
        if args.skip_existing and (output_file.exists() or native_complete):
            token_usage = read_company_token_usage(company_dir, output_file)
            tool_call_stats = collect_mcp_tool_call_stats(company_dir)
            tool_usage = tool_call_stats.pop("tool_usage")
            if not tool_usage:
                tool_usage = read_json_field(output_file, "tool_usage", empty_tool_usage())
            add_token_usage(manifest["token_usage"], token_usage)
            add_tool_usage(manifest["tool_usage"], tool_usage)
            add_tool_call_stats(manifest["tool_call_stats"], tool_call_stats)
            manifest["results"].append({
                "cik": cik,
                "status": "skipped",
                "company_dir": str(company_dir),
                "duration_seconds": 0.0,
                "token_usage": token_usage,
                "tool_usage": tool_usage,
                "tool_call_stats": tool_call_stats,
            })
            print(f"[{index}/{len(entity_ids)}] skip existing company {cik}")
            continue

        company_dir.mkdir(parents=True, exist_ok=True)
        output_args = (
            ["--output-dir", str(output_root)]
            if single_uses_output_dir
            else ["--output-file", str(output_file)]
        )
        cmd = [
            sys.executable,
            str(single_script),
            "--cik",
            cik,
            *output_args,
            *extra_args,
        ]
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
        token_usage = read_company_token_usage(company_dir, output_file)
        tool_call_stats = collect_mcp_tool_call_stats(company_dir)
        tool_usage = tool_call_stats.pop("tool_usage")
        if not tool_usage:
            tool_usage = read_json_field(output_file, "tool_usage", empty_tool_usage())
        add_token_usage(manifest["token_usage"], token_usage)
        add_tool_usage(manifest["tool_usage"], tool_usage)
        add_tool_call_stats(manifest["tool_call_stats"], tool_call_stats)
        result = {
            "cik": cik,
            "status": status,
            "returncode": process.returncode,
            "company_dir": str(company_dir),
            "log_file": str(log_file),
            "duration_seconds": round(time.monotonic() - company_started, 2),
            "token_usage": token_usage,
            "tool_usage": tool_usage,
            "tool_call_stats": tool_call_stats,
        }
        if not single_uses_output_dir:
            result["output_file"] = str(output_file)
        manifest["results"].append(result)
        print(
            f"[{index}/{len(entity_ids)}] {status} company {cik} "
            f"tokens={token_usage.get('total_tokens', 0)} "
            f"tool_calls={tool_call_stats['total_tool_calls']} "
            f"duration={result['duration_seconds']}s"
        )
        if process.returncode != 0 and args.stop_on_error:
            break

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["duration_seconds"] = round(time.monotonic() - batch_started, 2)
    manifest["success_count"] = sum(1 for item in manifest["results"] if item["status"] == "success")
    manifest["failed_count"] = sum(1 for item in manifest["results"] if item["status"] == "failed")
    manifest["skipped_count"] = sum(1 for item in manifest["results"] if item["status"] == "skipped")
    json.dump(manifest, open(output_root / "batch_manifest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Manifest saved to {output_root / 'batch_manifest.json'}")
    return manifest
