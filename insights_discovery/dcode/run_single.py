#!/usr/bin/env python3
"""Run dcode insight discovery for one DDR_Bench 10-K company."""

from __future__ import annotations

import argparse
import contextlib
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

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from insights_discovery.common.mcp_servers import managed_mcp_servers


DEFAULT_OUTPUT_ROOT = Path("outputs/dcode/test")
DEFAULT_CONFIG = Path("config.yaml")
DEFAULT_SCENARIO = "10k"
PROJECT_MCP_CONFIG = Path(".deepagents/.mcp.json")
SQLITE_MCP_SERVER = {
    "type": "sse",
    "url": "http://127.0.0.1:8765/sse",
}
CODE_MCP_SERVER = {
    "type": "sse",
    "url": "http://127.0.0.1:8766/sse",
}
DEFAULT_PROMPT_TEMPLATE = (
    "Analyze company with CIK {cik}. "
    "Produce at least 20 high-value insights. "
    "Save final JSON to {output_path}."
)

MCP_COMPAT_SITE_CUSTOMIZE = '''"""DDR_Bench local compatibility shims for the project virtualenv."""

import sys
import types

try:
    import mcp.client.streamable_http as _streamable_http

    if (
        not hasattr(_streamable_http, "streamable_http_client")
        and hasattr(_streamable_http, "streamablehttp_client")
    ):
        _streamable_http.streamable_http_client = _streamable_http.streamablehttp_client
except Exception:
    pass

try:
    import mcp.client.auth as _auth

    if not hasattr(_auth, "__path__"):
        _auth.__path__ = []

    if "mcp.client.auth.utils" not in sys.modules:
        _utils = types.ModuleType("mcp.client.auth.utils")

        def _unsupported(*_args, **_kwargs):
            raise RuntimeError(
                "mcp.client.auth.utils is unavailable in this MCP SDK version. "
                "OAuth MCP auth is not supported by the DDR_Bench compatibility shim."
            )

        _utils.build_oauth_authorization_server_metadata_discovery_urls = _unsupported
        _utils.build_protected_resource_metadata_discovery_urls = _unsupported
        _utils.create_oauth_metadata_request = _unsupported
        _utils.handle_auth_metadata_response = _unsupported
        _utils.handle_protected_resource_response = _unsupported
        sys.modules["mcp.client.auth.utils"] = _utils
except Exception:
    pass
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run dcode insight discovery for one CIK."
    )
    parser.add_argument(
        "--cik",
        required=True,
        help="Target company CIK.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Directory for this run. If the basename is not company_<CIK>, "
            "outputs are written under output-dir/company_<CIK>."
        ),
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
        help=(
            "MCP exposure mode. auto exposes only MCP servers backed by "
            "available configured data sources; all preserves both DDR_Bench "
            "servers; none disables MCP."
        ),
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
    return REPO_ROOT


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


def ensure_mcp_streamable_http_compat(dcode_bin: str) -> None:
    """Install a venv-local shim for mcp/langchain-mcp-adapters name drift."""
    dcode_path = Path(dcode_bin).resolve()
    if ".venv" not in dcode_path.parts:
        return
    venv_index = dcode_path.parts.index(".venv")
    venv_root = Path(*dcode_path.parts[: venv_index + 1])
    site_packages = next((venv_root / "lib").glob("python*/site-packages"), None)
    if site_packages is None:
        return
    sitecustomize = site_packages / "sitecustomize.py"
    existing = sitecustomize.read_text(encoding="utf-8") if sitecustomize.exists() else ""
    if (
        "streamable_http_client" in existing
        and "streamablehttp_client" in existing
        and "mcp.client.auth.utils" in existing
    ):
        return
    sitecustomize.write_text(MCP_COMPAT_SITE_CUSTOMIZE, encoding="utf-8")


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
    root = repo_root().as_posix()
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root}{os.pathsep}{pythonpath}" if pythonpath else root
    return env


def ensure_local_no_proxy(env: dict[str, str]) -> None:
    required = ["localhost", "127.0.0.1", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        existing = [item.strip() for item in env.get(key, "").split(",") if item.strip()]
        lower_existing = {item.lower() for item in existing}
        for item in required:
            if item.lower() not in lower_existing:
                existing.append(item)
        env[key] = ",".join(existing)


def load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = repo_root() / env_path
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def make_dcode_env(debug_file: Path) -> dict[str, str]:
    env = make_env()
    env.setdefault("DEEPAGENTS_CODE_DEBUG", "1")
    env.setdefault("DEEPAGENTS_CODE_DEBUG_FILE", debug_file.as_posix())
    ensure_local_no_proxy(env)
    return env


def project_python() -> str:
    python = repo_root() / ".venv/bin/python"
    if python.exists():
        return python.as_posix()
    return sys.executable


def _resolve_repo_path(path_value: str, root: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else root / path


def _path_has_csv(path: Path) -> bool:
    if path.is_file():
        return path.suffix.lower() == ".csv"
    if path.is_dir():
        return any(path.glob("*.csv"))
    return False


def load_scenario_data_source_availability(
    config_path: Path,
    scenario: str,
) -> dict[str, Any]:
    root = repo_root()
    resolved_config = config_path if config_path.is_absolute() else root / config_path
    if not resolved_config.exists():
        return {
            "config_path": resolved_config.as_posix(),
            "scenario": scenario,
            "sqlite_available": False,
            "csv_available": False,
            "sources": [],
            "error": "config_not_found",
        }

    try:
        import yaml

        with resolved_config.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as exc:
        return {
            "config_path": resolved_config.as_posix(),
            "scenario": scenario,
            "sqlite_available": False,
            "csv_available": False,
            "sources": [],
            "error": f"config_load_failed: {exc}",
        }

    scenario_config = (config.get("scenarios") or {}).get(scenario) or {}
    data_sources = scenario_config.get("data_sources") or []

    # Backward-compatible fallback for older configs without data_sources.
    if not data_sources:
        if scenario_config.get("db_path"):
            data_sources.append(
                {
                    "name": f"{scenario}_sqlite",
                    "type": "sqlite",
                    "path": scenario_config["db_path"],
                }
            )
        if scenario_config.get("data_path"):
            data_sources.append(
                {
                    "name": f"{scenario}_data_path",
                    "type": "csv_directory",
                    "path": scenario_config["data_path"],
                }
            )

    sources: list[dict[str, Any]] = []
    sqlite_available = False
    csv_available = False
    for source in data_sources:
        source_type = str(source.get("type", "")).lower()
        path_value = source.get("path")
        path = _resolve_repo_path(path_value, root) if path_value else None
        exists = bool(path and path.exists())
        has_csv = bool(path and _path_has_csv(path))
        is_sqlite = source_type in {"sqlite", "db", "database"} and exists
        is_csv = source_type in {"csv", "csv_directory", "csv_file"} and has_csv
        sqlite_available = sqlite_available or is_sqlite
        csv_available = csv_available or is_csv
        sources.append(
            {
                "name": source.get("name", ""),
                "type": source_type,
                "path": path.as_posix() if path else "",
                "exists": exists,
                "has_csv": has_csv,
                "available": is_sqlite or is_csv,
            }
        )

    return {
        "config_path": resolved_config.as_posix(),
        "scenario": scenario,
        "sqlite_available": sqlite_available,
        "csv_available": csv_available,
        "sources": sources,
        "error": None,
    }


def build_active_mcp_config(
    *,
    config_path: Path,
    scenario: str,
    mcp_mode: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    availability = load_scenario_data_source_availability(config_path, scenario)
    if mcp_mode == "none":
        return None, {**availability, "mcp_mode": mcp_mode, "active_servers": []}

    servers: dict[str, dict[str, str]] = {}
    if mcp_mode == "all":
        servers["ddrbench_sqlite"] = SQLITE_MCP_SERVER
        servers["ddrbench_code"] = CODE_MCP_SERVER
    else:
        if availability["sqlite_available"]:
            servers["ddrbench_sqlite"] = SQLITE_MCP_SERVER
            servers["ddrbench_code"] = CODE_MCP_SERVER
        if availability["csv_available"]:
            servers["ddrbench_code"] = CODE_MCP_SERVER

    mcp_config = {"mcpServers": servers} if servers else None
    return mcp_config, {
        **availability,
        "mcp_mode": mcp_mode,
        "active_servers": sorted(servers),
    }


def load_scenario_config(config_path: Path, scenario: str) -> dict[str, Any]:
    resolved_config = config_path if config_path.is_absolute() else repo_root() / config_path
    if not resolved_config.exists():
        return {}
    try:
        import yaml

        config = yaml.safe_load(resolved_config.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    scenario_config = (config.get("scenarios") or {}).get(scenario) or {}
    return scenario_config if isinstance(scenario_config, dict) else {}


def available_source_path(mcp_resolution: dict[str, Any], source_type: str) -> str | None:
    for source in mcp_resolution.get("sources", []):
        if source.get("available") and source.get("type") == source_type and source.get("path"):
            return str(source["path"])
    return None


def build_stdio_mcp_config(
    *,
    mcp_resolution: dict[str, Any],
    config_path: Path,
    scenario: str,
    log_dir: Path | None = None,
) -> dict[str, Any] | None:
    active_servers = set(mcp_resolution.get("active_servers") or [])
    if not active_servers:
        return None

    scenario_config = load_scenario_config(config_path, scenario)
    root = repo_root()
    python = project_python()
    env = {"PYTHONPATH": root.as_posix()}
    if log_dir is not None:
        env["CUSTOM_LOG_DIR"] = log_dir.as_posix()
    servers: dict[str, dict[str, Any]] = {}

    if "ddrbench_sqlite" in active_servers:
        db_path_value = scenario_config.get("db_path") or "./data/10k/raw/10k_financial_data.db"
        db_path = (
            available_source_path(mcp_resolution, "sqlite")
            or _resolve_repo_path(str(db_path_value), root).as_posix()
        )
        servers["ddrbench_sqlite"] = {
            "command": python,
            "args": [
                (root / "tool_server/sqlite_mcp.py").as_posix(),
                "--transport",
                "stdio",
                "--data-path",
                db_path,
            ],
            "env": env,
        }

    if "ddrbench_code" in active_servers:
        servers["ddrbench_code"] = {
            "command": python,
            "args": [
                (root / "tool_server/code_mcp.py").as_posix(),
                "--transport",
                "stdio",
                "--data-path",
                root.as_posix(),
            ],
            "env": env,
        }

    return {"mcpServers": servers} if servers else None


@contextlib.contextmanager
def temporary_project_mcp_config(mcp_config: dict[str, Any] | None):
    path = repo_root() / PROJECT_MCP_CONFIG
    original: str | None = None
    existed = path.exists()
    if existed:
        original = path.read_text(encoding="utf-8")

    try:
        if mcp_config is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, mcp_config)
        yield
    finally:
        if existed and original is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(original, encoding="utf-8")
        elif path.exists():
            path.unlink()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def list_mcp_call_logs(extra_dir: Path | None = None) -> list[Path]:
    logs_dir = repo_root() / "logs"
    paths: list[Path] = []
    if logs_dir.exists():
        paths.extend(logs_dir.glob("*-mcp_calls_*.csv"))
    if extra_dir is not None and extra_dir.exists():
        paths.extend(extra_dir.glob("*-mcp_calls_*.csv"))
    return sorted(paths)


def parse_iso_timestamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def collect_tool_call_stats(start_time: float, end_time: float, extra_dir: Path | None = None) -> dict[str, Any]:
    """Count MCP tool calls logged between two wall-clock timestamps."""
    csv.field_size_limit(sys.maxsize)
    by_server: dict[str, dict[str, int]] = {}
    by_tool: dict[str, int] = {}
    total = 0
    success = 0
    errors = 0
    log_files: list[str] = []

    for path in list_mcp_call_logs(extra_dir):
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
    config_path: Path,
    scenario: str,
    mcp_mode: str,
    extra_args: list[str],
    dry_run: bool,
    quiet: bool,
    annotate_output: bool,
    auto_mcp: bool = True,
    mcp_transport: str = "stdio",
    env_file: str | Path = ".env",
) -> dict[str, Any]:
    ensure_mcp_streamable_http_compat(dcode_bin)
    load_env_file(env_file)

    company_dir = output_root / f"company_{cik}"
    output_path = company_dir / "insights.json"
    log_path = company_dir / "run.log"
    metadata_path = company_dir / "run_metadata.json"
    prompt_path = company_dir / "prompt.txt"
    debug_path = company_dir / "dcode_debug.log"
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

    mcp_config, mcp_resolution = build_active_mcp_config(
        config_path=config_path,
        scenario=scenario,
        mcp_mode=mcp_mode,
    )
    if mcp_config is not None and mcp_transport == "stdio":
        mcp_config = build_stdio_mcp_config(
            mcp_resolution=mcp_resolution,
            config_path=config_path,
            scenario=scenario,
            log_dir=company_dir,
        )
    if mcp_config is None:
        cmd.insert(1, "--no-mcp")

    started_at = time.time()
    record: dict[str, Any] = {
        "cik": cik,
        "output_path": output_path.as_posix(),
        "log_path": log_path.as_posix(),
        "metadata_path": metadata_path.as_posix(),
        "prompt_path": prompt_path.as_posix(),
        "debug_path": debug_path.as_posix(),
        "command": cmd,
        "started_at": started_at,
        "timeout_seconds": timeout,
        "dry_run": dry_run,
        "mcp_resolution": mcp_resolution,
        "mcp_transport": mcp_transport,
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

    if mcp_transport == "sse":
        mcp_server_context = managed_mcp_servers(
            mcp_resolution,
            config_path=config_path,
            scenario=scenario,
            log_dir=company_dir,
            enabled=auto_mcp and not dry_run,
        )
    else:
        mcp_server_context = contextlib.nullcontext([])

    with mcp_server_context, temporary_project_mcp_config(mcp_config):
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write("$ " + " ".join(cmd) + "\n")
            log_file.write("MCP resolution: " + json.dumps(mcp_resolution, ensure_ascii=False) + "\n\n")
            log_file.write("MCP transport: " + mcp_transport + "\n\n")
            log_file.flush()
            try:
                result = subprocess.run(
                    cmd,
                    cwd=repo_root(),
                    env=make_dcode_env(debug_path),
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
    tool_call_stats = collect_tool_call_stats(started_at, ended_at, company_dir)
    token_usage = parse_token_usage_from_log(log_path)
    metadata = {
        "cik": cik,
        "status": status,
        "returncode": returncode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(ended_at - started_at, 2),
        "mcp_resolution": mcp_resolution,
        "mcp_transport": mcp_transport,
        "tool_call_stats": tool_call_stats,
        "token_usage": token_usage,
        "debug_path": debug_path.as_posix(),
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


def resolve_single_output_root(output_dir: Path, cik: str) -> Path:
    root = repo_root()
    resolved = output_dir if output_dir.is_absolute() else root / output_dir
    if resolved.name == f"company_{cik}":
        return resolved.parent
    return resolved


def main() -> int:
    args = parse_args()
    root = repo_root()
    os.chdir(root)

    cik = str(args.cik)
    output_root = resolve_single_output_root(args.output_dir, cik)
    output_root.mkdir(parents=True, exist_ok=True)

    dcode_bin = resolve_dcode(args.dcode_bin)
    company_dir = output_root / f"company_{cik}"
    output_path = company_dir / "insights.json"

    if valid_json(output_path) and not args.overwrite:
        metadata = load_metadata_if_present(company_dir)
        record = {
            "cik": cik,
            "status": "skipped_existing",
            "output_path": output_path.as_posix(),
            "metadata_path": (company_dir / "run_metadata.json").as_posix(),
            "output_valid_json": True,
        }
        if metadata:
            record["tool_call_stats"] = metadata.get("tool_call_stats")
            record["token_usage"] = metadata.get("token_usage")
        print(f"CIK {cik}: skipped existing valid output")
    else:
        print(f"CIK {cik}: running")
        record = run_one(
            dcode_bin=dcode_bin,
            cik=cik,
            output_root=output_root,
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
        print(f"CIK {cik}: {record['status']} ({record.get('duration_seconds', 0)}s)")

    return 0 if record.get("status") in {"ok", "skipped_existing", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
