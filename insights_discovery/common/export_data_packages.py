#!/usr/bin/env python3
"""Precompute DDR_Bench 10-K per-CIK JSON/JSONL data packages."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.batch import load_entity_ids, parse_target_ids  # noqa: E402
from insights_discovery.common.data_package import (  # noqa: E402
    existing_10k_company_package,
    export_10k_company_package,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export reusable 10-K data packages for DDR_Bench insight discovery")
    parser.add_argument("--db", default="./data/10k/raw/10k_financial_data.db")
    parser.add_argument("--id-file", default="./data/10k/entity_ids.json")
    parser.add_argument("--output-dir", default="./data/10k/company_packages")
    parser.add_argument("--target-ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--manifest", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
        "db": str(Path(args.db).resolve()),
        "id_file": str(Path(args.id_file).resolve()),
        "output_dir": str(output_root.resolve()),
        "total_requested": len(entity_ids),
        "results": [],
    }

    for index, cik in enumerate(entity_ids, 1):
        package_dir = output_root / f"company_{cik}"
        print(f"[{index}/{len(entity_ids)}] export company {cik}")
        try:
            if args.skip_existing:
                paths = existing_10k_company_package(cik, package_dir)
                status = "skipped"
            else:
                paths = export_10k_company_package(args.db, cik, package_dir)
                status = "success"
            manifest["results"].append({
                "cik": cik,
                "status": status,
                "package_dir": str(package_dir),
                "files": [str(path) for path in paths],
            })
            print(f"[{index}/{len(entity_ids)}] {status} company {cik} files={len(paths)}")
        except Exception as exc:
            manifest["results"].append({
                "cik": cik,
                "status": "failed",
                "package_dir": str(package_dir),
                "error": {"type": exc.__class__.__name__, "message": str(exc)},
            })
            print(f"[{index}/{len(entity_ids)}] failed company {cik}: {exc}")
            if args.stop_on_error:
                break

    manifest["finished_at"] = datetime.now().isoformat()
    manifest["success_count"] = sum(1 for item in manifest["results"] if item["status"] == "success")
    manifest["failed_count"] = sum(1 for item in manifest["results"] if item["status"] == "failed")
    manifest["skipped_count"] = sum(1 for item in manifest["results"] if item["status"] == "skipped")
    manifest_path = Path(args.manifest) if args.manifest else output_root / "export_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Manifest saved to {manifest_path}")
    return 1 if manifest["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
