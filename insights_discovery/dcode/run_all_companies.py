#!/usr/bin/env python3
"""Run dcode insight discovery for every DDR_Bench 10-K company."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ENTITY_IDS = Path("data/10k/entity_ids.json")
DEFAULT_OUTPUT_ROOT = Path("outputs/dcode")
DEFAULT_PROMPT_TEMPLATE = (
    "Analyze company with CIK {cik}. "
    "Produce at least 20 high-value insights. "
    "Save final JSON to {output_path}."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run dcode insight discovery for all CIKs in entity_ids.json."
    )
    parser.add_argument(
        "--entity-ids",
        type=Path,
        default=DEFAULT_ENTITY_IDS,
        help=f"Path to entity IDs JSON, relative to repo root by default: {DEFAULT_ENTITY_IDS}",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"Output root, relative to repo root by default: {DEFAULT_OUTPUT_ROOT}",
    )
    parser.add_argument(
        "--dcode-bin",
        default=None,
        help="Path to dcode executable. Defaults to PATH lookup, then uv tool install path.",
    )
    parser.add_argument(
        "-M",
        "--model",
        default=None,
        help="Model passed to dcode via -M/--model.",
    )
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
        help="Optional explicit CIK list to run instead of all entity IDs.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Run even when insights.json already exists and is valid JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help=(
            "Pass -q to dcode. This makes logs cleaner but usually prevents "
            "token usage from being printed by dcode."
        ),
    )
    parser.add_argument(
        "--annotate-output",
        action="store_true",
        help=(
            "Also add run_metadata to insights.json. Off by default to keep "
            "DDR evaluation input clean."
        ),
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument passed to dcode. Repeat for multiple args.",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_entity_ids(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    return [str(item) for item in data]


def resolve_dcode(explicit: str | None) -> str:
    if explicit:
        return explicit

    project_dcode = repo_root() / ".venv/bin/dcode"
    if project_dcode.exists():
        return str(project_dcode)

    found = shutil.which("dcode")
    if found:
        return found

    fallbacks = [
        Path.home() / ".local/share/uv/tools/deepagents-code/bin/dcode",
        Path("/home/chenbei/.local/share/uv/tools/deepagents-code/bin/dcode"),
    ]
    for fallback in fallbacks:
        if fallback.exists():
            return str(fallback)

    raise FileNotFoundError(
        "Could not find dcode. Pass --dcode-bin or add dcode to PATH."
    )


def valid_json(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with path.open("r", encoding="utf-8") as f:
            json.load(f)
        return True
    except Exception:
        return False


def make_env() -> dict[str, str]:
    env = os.environ.copy()
    no_proxy = env.get(
        "no_proxy",
        "localhost,127.0.0.1,::1,10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn",
    )
    for item in ["localhost", "127.0.0.1", "::1"]:
        if item not in no_proxy:
            no_proxy = f"{no_proxy},{item}" if no_proxy else item
    env["no_proxy"] = no_proxy
    env["NO_PROXY"] = no_proxy
    return env


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_mcp_call_logs() -> list[Path]:
    logs_dir = repo_root() / "logs"
    if not logs_dir.exists():
        return []
    return sorted(logs_dir.glob("*-mcp_calls_*.csv"))


def parse_iso_timestamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def collect_tool_call_stats(start_time: float, end_time: float) -> dict[str, Any]:
    """Count MCP tool calls logged between two wall-clock timestamps."""
    csv.field_size_limit(sys.maxsize)
    by_server: dict[str, dict[str, int]] = {}
    by_tool: dict[str, int] = {}
    total = 0
    success = 0
    errors = 0
    log_files: list[str] = []

    for path in list_mcp_call_logs():
        server_name = path.name.split("_calls_", 1)[0]
        file_used = False
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    ts = parse_iso_timestamp(row.get("timestamp", ""))
                    if ts is None or ts < start_time or ts > end_time:
                        continue
                    tool_name = row.get("tool_name", "unknown") or "unknown"
                    ok = row.get("success") == "True"
                    by_server.setdefault(server_name, {})
                    by_server[server_name][tool_name] = (
                        by_server[server_name].get(tool_name, 0) + 1
                    )
                    by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
                    total += 1
                    success += int(ok)
                    errors += int(not ok)
                    file_used = True
        except Exception:
            continue
        if file_used:
            log_files.append(path.as_posix())

    return {
        "total_tool_calls": total,
        "successful_tool_calls": success,
        "failed_tool_calls": errors,
        "by_tool": dict(sorted(by_tool.items())),
        "by_server": {
            server: dict(sorted(counts.items()))
            for server, counts in sorted(by_server.items())
        },
        "log_files": log_files,
    }


def parse_compact_token_count(value: str) -> int | None:
    text = value.strip().replace(",", "")
    if not text or text == "-":
        return None
    multiplier = 1
    if text[-1:].upper() == "K":
        multiplier = 1_000
        text = text[:-1]
    elif text[-1:].upper() == "M":
        multiplier = 1_000_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def parse_token_usage_from_log(log_path: Path) -> dict[str, Any]:
    """Best-effort parser for dcode's non-interactive Usage Stats table."""
    if not log_path.exists():
        return {"available": False, "reason": "log_missing"}

    text = log_path.read_text(encoding="utf-8", errors="ignore")
    if "Usage Stats" not in text:
        return {
            "available": False,
            "reason": "usage_stats_not_found",
            "request_count": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "per_model": {},
        }

    ansi_re = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
    clean = ansi_re.sub("", text)
    lines = clean.splitlines()
    usage_index = max(i for i, line in enumerate(lines) if "Usage Stats" in line)
    per_model: dict[str, dict[str, int]] = {}
    total_row: dict[str, int] | None = None

    row_re = re.compile(
        r"^(?P<model>\\S.*?)\\s{2,}(?P<reqs>\\d+)\\s+"
        r"(?P<input>[0-9.,]+[KM]?)\\s+"
        r"(?P<output>[0-9.,]+[KM]?)\\s*$",
        re.IGNORECASE,
    )
    for line in lines[usage_index + 1 : usage_index + 20]:
        stripped = line.strip()
        if not stripped or stripped.startswith("Model ") or stripped.startswith("Agent active"):
            continue
        match = row_re.match(stripped)
        if not match:
            continue
        input_tokens = parse_compact_token_count(match.group("input"))
        output_tokens = parse_compact_token_count(match.group("output"))
        if input_tokens is None or output_tokens is None:
            continue
        row = {
            "request_count": int(match.group("reqs")),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        model = match.group("model").strip()
        if model == "Total":
            total_row = row
        else:
            per_model[model] = row

    if total_row is None and len(per_model) == 1:
        total_row = next(iter(per_model.values()))

    if total_row is None:
        return {
            "available": False,
            "reason": "usage_stats_parse_failed",
            "request_count": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "per_model": per_model,
        }

    return {
        "available": True,
        **total_row,
        "per_model": per_model,
    }


def annotate_output_json(output_path: Path, metadata: dict[str, Any]) -> None:
    if not valid_json(output_path):
        return
    with output_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data["run_metadata"] = metadata
        write_json(output_path, data)


def load_metadata_if_present(company_dir: Path) -> dict[str, Any] | None:
    metadata_path = company_dir / "run_metadata.json"
    if not metadata_path.exists():
        return None
    try:
        with metadata_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def run_one(
    *,
    dcode_bin: str,
    cik: str,
    output_root: Path,
    timeout: int,
    model: str | None,
    extra_args: list[str],
    dry_run: bool,
    quiet: bool,
    annotate_output: bool,
) -> dict[str, Any]:
    company_dir = output_root / f"company_{cik}"
    output_path = company_dir / "insights.json"
    log_path = company_dir / "run.log"
    metadata_path = company_dir / "run_metadata.json"
    prompt_path = company_dir / "prompt.txt"
    company_dir.mkdir(parents=True, exist_ok=True)

    prompt = DEFAULT_PROMPT_TEMPLATE.format(
        cik=cik,
        output_path=output_path.as_posix(),
    )
    prompt_path.write_text(prompt + "\n", encoding="utf-8")

    cmd = [
        dcode_bin,
        "--trust-project-mcp",
        "-n",
        prompt,
        "--timeout",
        str(timeout),
        *extra_args,
    ]
    if model:
        cmd[1:1] = ["-M", model]
    if quiet:
        cmd.insert(-len(extra_args) if extra_args else len(cmd), "-q")

    started_at = time.time()
    record: dict[str, Any] = {
        "cik": cik,
        "output_path": output_path.as_posix(),
        "log_path": log_path.as_posix(),
        "metadata_path": metadata_path.as_posix(),
        "prompt_path": prompt_path.as_posix(),
        "command": cmd,
        "started_at": started_at,
        "timeout_seconds": timeout,
        "dry_run": dry_run,
    }

    if dry_run:
        record.update(
            {
                "status": "dry_run",
                "returncode": None,
                "duration_seconds": 0,
                "output_valid_json": valid_json(output_path),
                "tool_call_stats": {
                    "total_tool_calls": 0,
                    "successful_tool_calls": 0,
                    "failed_tool_calls": 0,
                    "by_tool": {},
                    "by_server": {},
                    "log_files": [],
                },
                "token_usage": {
                    "available": False,
                    "reason": "dry_run",
                    "request_count": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "per_model": {},
                },
            }
        )
        return record

    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write("$ " + " ".join(cmd) + "\n\n")
        log_file.flush()
        try:
            result = subprocess.run(
                cmd,
                cwd=repo_root(),
                env=make_env(),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout + 60,
                check=False,
            )
            returncode = result.returncode
            status = "ok" if returncode == 0 and valid_json(output_path) else "failed"
        except subprocess.TimeoutExpired:
            returncode = 124
            status = "timeout"
            log_file.write(f"\nTimed out after {timeout + 60} seconds.\n")

    ended_at = time.time()
    tool_call_stats = collect_tool_call_stats(started_at, ended_at)
    token_usage = parse_token_usage_from_log(log_path)
    metadata = {
        "cik": cik,
        "status": status,
        "returncode": returncode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(ended_at - started_at, 2),
        "tool_call_stats": tool_call_stats,
        "token_usage": token_usage,
    }
    write_json(metadata_path, metadata)
    if annotate_output:
        annotate_output_json(output_path, metadata)

    record.update(
        {
            "status": status,
            "returncode": returncode,
            "duration_seconds": metadata["duration_seconds"],
            "output_valid_json": valid_json(output_path),
            "tool_call_stats": tool_call_stats,
            "token_usage": token_usage,
        }
    )
    return record


def main() -> int:
    args = parse_args()
    root = repo_root()
    os.chdir(root)

    entity_ids_path = args.entity_ids if args.entity_ids.is_absolute() else root / args.entity_ids
    output_root = args.output_root if args.output_root.is_absolute() else root / args.output_root
    output_root.mkdir(parents=True, exist_ok=True)

    ciks = args.only if args.only else load_entity_ids(entity_ids_path)
    ciks = [str(cik) for cik in ciks][args.start_index :]
    if args.limit is not None:
        ciks = ciks[: args.limit]

    dcode_bin = resolve_dcode(args.dcode_bin)
    manifest_path = output_root / "batch_manifest.json"
    records: list[dict[str, Any]] = []

    for index, cik in enumerate(ciks, start=args.start_index):
        company_dir = output_root / f"company_{cik}"
        output_path = company_dir / "insights.json"
        if valid_json(output_path) and not args.overwrite:
            metadata = load_metadata_if_present(company_dir)
            record = {
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
                output_root=output_root,
                timeout=args.timeout,
                model=args.model,
                extra_args=args.extra_arg,
                dry_run=args.dry_run,
                quiet=args.quiet,
                annotate_output=args.annotate_output,
            )
            record["index"] = index
            print(
                f"[{index}] CIK {cik}: {record['status']} "
                f"({record.get('duration_seconds', 0)}s)"
            )

        records.append(record)
        write_json(
            manifest_path,
            {
                "entity_ids_path": entity_ids_path.as_posix(),
                "output_root": output_root.as_posix(),
                "dcode_bin": dcode_bin,
                "model": args.model,
                "count": len(records),
                "records": records,
            },
        )

    failures = [r for r in records if r.get("status") not in {"ok", "skipped_existing", "dry_run"}]
    print(f"Done. records={len(records)} failures={len(failures)}")
    print(f"Manifest: {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
