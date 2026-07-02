#!/usr/bin/env python3
"""Run dcode insight discovery for a batch of DDR_Bench 10-K companies."""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from run_single import (
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_SCENARIO,
    has_completed_artifacts,
    load_entity_ids,
    load_metadata_if_present,
    repo_root,
    resolve_dcode,
    run_one,
    write_json,
)
from insights_discovery.common.run_directories import ensure_run_dir
from insights_discovery.common.batch import (
    add_token_usage,
    add_tool_call_stats,
    add_tool_usage,
    collect_mcp_tool_call_stats,
    empty_tool_call_stats,
    empty_tool_usage,
    parse_target_ids,
)


DEFAULT_ENTITY_IDS = Path("data/10k/entity_ids.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run dcode insight discovery for a batch of CIKs."
    )
    parser.add_argument(
        "--entity-ids",
        type=Path,
        default=DEFAULT_ENTITY_IDS,
        help=f"Path to entity IDs JSON. Default: {DEFAULT_ENTITY_IDS}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Batch output directory. Default: {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument(
        "--dcode-bin",
        default=None,
        help="Path to dcode executable. Defaults to repo .venv, PATH, then common uv tool paths.",
    )
    parser.add_argument("-M", "--model", default=None, help="Model passed to dcode.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Per-company dcode wall-clock timeout in seconds.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run only the first N CIKs after filtering.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start from this zero-based index in entity_ids.json.",
    )
    parser.add_argument(
        "--target-ids",
        help="Comma-separated CIKs to run, matching the CubePI batch interface.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Run even when completed trajectory artifacts already exist.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to DDR_Bench config YAML. Default: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        help=f"Scenario name used to select data sources. Default: {DEFAULT_SCENARIO}",
    )
    parser.add_argument(
        "--mcp-mode",
        choices=["auto", "all", "none"],
        default="auto",
        help="MCP mode: auto exposes servers backed by available data sources.",
    )
    parser.add_argument(
        "--no-auto-mcp",
        action="store_true",
        help="With --mcp-transport sse, do not auto-start missing DDR_Bench SSE MCP servers.",
    )
    parser.add_argument(
        "--mcp-transport",
        choices=["stdio", "sse"],
        default="stdio",
        help=(
            "Transport used in the temporary dcode MCP config. stdio lets dcode "
            "start the needed MCP servers itself; sse uses local HTTP servers."
        ),
    )
    parser.add_argument("--env-file", default=".env", help="Optional env file loaded before starting dcode.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Pass -q to dcode.")
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to dcode. Repeat for multiple args.",
    )
    parser.add_argument("--insight-max-tokens", type=int, default=512)
    parser.add_argument("--summary-max-tokens", type=int, default=16384)
    parser.add_argument("--insight-temperature", type=float, default=0.5)
    return parser.parse_args()


def run_batch(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Path]:
    batch_started = time.monotonic()
    started_at = datetime.now().isoformat()
    root = repo_root()
    os.chdir(root)

    entity_ids_path = args.entity_ids if args.entity_ids.is_absolute() else root / args.entity_ids
    output_root = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    output_dir = ensure_run_dir(output_root)
    print(f"Deep Agents Code run directory: {output_dir}")

    ciks = load_entity_ids(entity_ids_path)
    target_ids = parse_target_ids(args.target_ids)
    if target_ids is not None:
        ciks = [cik for cik in ciks if cik in target_ids]
    ciks = [str(cik) for cik in ciks][args.start_index :]
    if args.limit is not None:
        ciks = ciks[: args.limit]

    dcode_bin = resolve_dcode(args.dcode_bin)
    manifest_path = output_dir / "batch_manifest.json"
    records: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "started_at": started_at,
        "id_file": entity_ids_path.as_posix(),
        "output_dir": output_dir.as_posix(),
        "total_requested": len(ciks),
        "token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "model_calls": 0,
        },
        "tool_usage": empty_tool_usage(),
        "tool_call_stats": empty_tool_call_stats(),
        "results": records,
        "model": args.model,
        "dcode_bin": dcode_bin,
        "config": (
            args.config if args.config.is_absolute() else root / args.config
        ).as_posix(),
        "scenario": args.scenario,
        "mcp_mode": args.mcp_mode,
        "mcp_transport": args.mcp_transport,
        "insight_generation": {
            "insight_max_tokens": args.insight_max_tokens,
            "summary_max_tokens": args.summary_max_tokens,
            "insight_temperature": args.insight_temperature,
        },
    }

    for position, cik in enumerate(ciks, start=1):
        company_started = time.monotonic()
        company_dir = output_dir / f"company_{cik}"
        if has_completed_artifacts(company_dir) and not args.overwrite:
            metadata = load_metadata_if_present(company_dir)
            record: dict[str, Any] = {
                "cik": cik,
                "status": "skipped",
                "company_dir": company_dir.as_posix(),
                "metadata_path": (company_dir / "run_metadata.json").as_posix(),
                "artifacts_complete": True,
                "duration_seconds": 0.0,
            }
            if metadata:
                record["tool_call_stats"] = metadata.get("tool_call_stats")
                record["token_usage"] = metadata.get("token_usage")
            print(f"[{position}/{len(ciks)}] skip existing company {cik}")
        else:
            print(f"[{position}/{len(ciks)}] run company {cik}")
            record = run_one(
                dcode_bin=dcode_bin,
                cik=cik,
                output_root=output_dir,
                timeout=args.timeout,
                model=args.model,
                config_path=args.config,
                scenario=args.scenario,
                mcp_mode=args.mcp_mode,
                extra_args=args.extra_arg,
                dry_run=args.dry_run,
                quiet=args.quiet,
                insight_max_tokens=args.insight_max_tokens,
                summary_max_tokens=args.summary_max_tokens,
                insight_temperature=args.insight_temperature,
                auto_mcp=not args.no_auto_mcp,
                mcp_transport=args.mcp_transport,
                env_file=args.env_file,
            )
            record["status"] = "success" if record["status"] == "ok" else record["status"]
            record["company_dir"] = company_dir.as_posix()
            record["duration_seconds"] = round(time.monotonic() - company_started, 2)

        token_usage = record.get("token_usage") or {}
        mcp_stats = collect_mcp_tool_call_stats(company_dir)
        tool_usage = mcp_stats.pop("tool_usage")
        record["tool_usage"] = tool_usage
        record["tool_call_stats"] = mcp_stats
        add_token_usage(manifest["token_usage"], token_usage)
        add_tool_usage(manifest["tool_usage"], tool_usage)
        add_tool_call_stats(manifest["tool_call_stats"], mcp_stats)
        records.append(record)
        print(
            f"[{position}/{len(ciks)}] {record['status']} company {cik} "
            f"tokens={token_usage.get('total_tokens') or 0} "
            f"tool_calls={mcp_stats['total_tool_calls']} "
            f"duration={record['duration_seconds']}s"
        )
        if record["status"] not in {"success", "skipped", "dry_run"}:
            print(f"  error: inspect {company_dir / 'run.log'}")
        manifest["finished_at"] = datetime.now().isoformat()
        manifest["duration_seconds"] = round(time.monotonic() - batch_started, 2)
        manifest["success_count"] = sum(
            item["status"] == "success" for item in records
        )
        manifest["failed_count"] = sum(
            item["status"] not in {"success", "skipped", "dry_run"}
            for item in records
        )
        manifest["skipped_count"] = sum(
            item["status"] == "skipped" for item in records
        )
        write_json(manifest_path, manifest)

    if not records:
        manifest["finished_at"] = datetime.now().isoformat()
        manifest["duration_seconds"] = round(time.monotonic() - batch_started, 2)
        manifest["success_count"] = 0
        manifest["failed_count"] = 0
        manifest["skipped_count"] = 0
        write_json(manifest_path, manifest)

    return records, manifest_path


def main() -> int:
    args = parse_args()
    records, manifest_path = run_batch(args)
    failures = [
        r for r in records
        if r.get("status") not in {"success", "skipped", "dry_run"}
    ]
    print(f"Done. records={len(records)} failures={len(failures)}")
    print(f"Manifest: {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
