#!/usr/bin/env python3
"""Run dcode insight discovery for a batch of DDR_Bench 10-K companies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from run_single import (
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_SCENARIO,
    load_entity_ids,
    load_metadata_if_present,
    repo_root,
    resolve_dcode,
    run_one,
    valid_json,
    write_json,
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
        "--only",
        nargs="*",
        default=None,
        help="Optional explicit CIK list to run instead of entity_ids.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Run even when insights.json already exists and is valid JSON.",
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
        "--annotate-output",
        action="store_true",
        help="Also add run_metadata to insights.json.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to dcode. Repeat for multiple args.",
    )
    return parser.parse_args()


def run_batch(args: argparse.Namespace) -> tuple[list[dict[str, Any]], Path]:
    root = repo_root()
    os.chdir(root)

    entity_ids_path = args.entity_ids if args.entity_ids.is_absolute() else root / args.entity_ids
    output_dir = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    ciks = args.only if args.only else load_entity_ids(entity_ids_path)
    ciks = [str(cik) for cik in ciks][args.start_index :]
    if args.limit is not None:
        ciks = ciks[: args.limit]

    dcode_bin = resolve_dcode(args.dcode_bin)
    manifest_path = output_dir / "batch_manifest.json"
    records: list[dict[str, Any]] = []

    for index, cik in enumerate(ciks, start=args.start_index):
        company_dir = output_dir / f"company_{cik}"
        output_path = company_dir / "insights.json"
        if valid_json(output_path) and not args.overwrite:
            metadata = load_metadata_if_present(company_dir)
            record: dict[str, Any] = {
                "cik": cik,
                "index": index,
                "status": "skipped_existing",
                "output_path": output_path.as_posix(),
                "metadata_path": (company_dir / "run_metadata.json").as_posix(),
                "output_valid_json": True,
            }
            if metadata:
                record["tool_call_stats"] = metadata.get("tool_call_stats")
                record["token_usage"] = metadata.get("token_usage")
            print(f"[{index}] CIK {cik}: skipped existing valid output")
        else:
            print(f"[{index}] CIK {cik}: running")
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
                annotate_output=args.annotate_output,
                auto_mcp=not args.no_auto_mcp,
                mcp_transport=args.mcp_transport,
                env_file=args.env_file,
            )
            record["index"] = index
            print(f"[{index}] CIK {cik}: {record['status']} ({record.get('duration_seconds', 0)}s)")

        records.append(record)
        write_json(
            manifest_path,
            {
                "entity_ids_path": entity_ids_path.as_posix(),
                "output_dir": output_dir.as_posix(),
                "dcode_bin": dcode_bin,
                "model": args.model,
                "config": (args.config if args.config.is_absolute() else root / args.config).as_posix(),
                "scenario": args.scenario,
                "mcp_mode": args.mcp_mode,
                "mcp_transport": args.mcp_transport,
                "count": len(records),
                "records": records,
            },
        )

    return records, manifest_path


def main() -> int:
    args = parse_args()
    records, manifest_path = run_batch(args)
    failures = [r for r in records if r.get("status") not in {"ok", "skipped_existing", "dry_run"}]
    print(f"Done. records={len(records)} failures={len(failures)}")
    print(f"Manifest: {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
