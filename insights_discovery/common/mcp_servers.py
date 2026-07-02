#!/usr/bin/env python3
"""Helpers for starting DDR_Bench SSE MCP servers for external agents."""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
SQLITE_SERVER_NAME = "ddrbench_sqlite"
CODE_SERVER_NAME = "ddrbench_code"
SQLITE_MCP_URL = "http://127.0.0.1:8765/sse"
CODE_MCP_URL = "http://127.0.0.1:8766/sse"
SQLITE_PORT = 8765
CODE_PORT = 8766
SQLITE_MCP_CONFIG = {"type": "sse", "url": SQLITE_MCP_URL}
CODE_MCP_CONFIG = {"type": "sse", "url": CODE_MCP_URL}


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def find_available_port(host: str = "127.0.0.1") -> int:
    """Reserve an OS-selected free port number for an imminent server start."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return
        time.sleep(0.1)
    raise TimeoutError(f"MCP server port {port} did not become ready within {timeout}s")


def _python_executable() -> str:
    project_python = REPO_ROOT / ".venv/bin/python"
    if project_python.exists():
        return project_python.as_posix()
    return sys.executable


def _resolve_repo_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    return path if path.is_absolute() else REPO_ROOT / path


def _source_path(resolution: dict[str, Any], source_type: str) -> str:
    for source in resolution.get("sources", []):
        if source.get("available") and source.get("type") == source_type:
            return str(source.get("path") or "")
    return ""


def _scenario_code_root(config_path: Path, scenario: str) -> str:
    try:
        import yaml

        resolved = config_path if config_path.is_absolute() else REPO_ROOT / config_path
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        scenario_config = (data.get("scenarios") or {}).get(scenario) or {}
        return str(_resolve_repo_path(scenario_config.get("code_root")) or REPO_ROOT)
    except Exception:
        return str(REPO_ROOT)


def _path_has_csv(path: Path) -> bool:
    if path.is_file():
        return path.suffix.lower() == ".csv"
    if path.is_dir():
        return any(path.glob("*.csv"))
    return False


def load_scenario_data_source_availability(config_path: Path, scenario: str) -> dict[str, Any]:
    resolved = config_path if config_path.is_absolute() else REPO_ROOT / config_path
    if not resolved.exists():
        return {
            "config_path": resolved.as_posix(),
            "scenario": scenario,
            "sqlite_available": False,
            "csv_available": False,
            "sources": [],
            "error": "config_not_found",
        }

    try:
        import yaml

        config = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {
            "config_path": resolved.as_posix(),
            "scenario": scenario,
            "sqlite_available": False,
            "csv_available": False,
            "sources": [],
            "error": f"config_load_failed: {exc}",
        }

    scenario_config = (config.get("scenarios") or {}).get(scenario) or {}
    data_sources = list(scenario_config.get("data_sources") or [])
    if not data_sources:
        if scenario_config.get("db_path"):
            data_sources.append({"name": f"{scenario}_sqlite", "type": "sqlite", "path": scenario_config["db_path"]})
        if scenario_config.get("code_root"):
            data_sources.append({"name": f"{scenario}_code_root", "type": "csv_directory", "path": scenario_config["code_root"]})

    sources: list[dict[str, Any]] = []
    sqlite_available = False
    csv_available = False
    for source in data_sources:
        source_type = str(source.get("type", "")).lower()
        path = _resolve_repo_path(source.get("path"))
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
        "config_path": resolved.as_posix(),
        "scenario": scenario,
        "sqlite_available": sqlite_available,
        "csv_available": csv_available,
        "sources": sources,
        "error": None,
    }


def build_active_mcp_config(config_path: Path, scenario: str, mcp_mode: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    availability = load_scenario_data_source_availability(config_path, scenario)
    if mcp_mode == "none":
        return None, {**availability, "mcp_mode": mcp_mode, "active_servers": []}

    servers: dict[str, dict[str, str]] = {}
    if mcp_mode == "all":
        servers[SQLITE_SERVER_NAME] = SQLITE_MCP_CONFIG
        servers[CODE_SERVER_NAME] = CODE_MCP_CONFIG
    else:
        if availability["sqlite_available"]:
            servers[SQLITE_SERVER_NAME] = SQLITE_MCP_CONFIG
            servers[CODE_SERVER_NAME] = CODE_MCP_CONFIG
        if availability["csv_available"]:
            servers[CODE_SERVER_NAME] = CODE_MCP_CONFIG

    return ({"mcpServers": servers} if servers else None), {
        **availability,
        "mcp_mode": mcp_mode,
        "active_servers": sorted(servers),
    }


def server_names_from_resolution(resolution: dict[str, Any]) -> set[str]:
    return set(resolution.get("active_servers") or [])


@contextlib.contextmanager
def managed_mcp_servers(
    resolution: dict[str, Any],
    *,
    config_path: Path,
    scenario: str,
    log_dir: Path | None = None,
    enabled: bool = True,
    sqlite_port: int = SQLITE_PORT,
    code_port: int = CODE_PORT,
) -> Iterator[list[subprocess.Popen]]:
    """Start missing DDR_Bench SSE MCP servers and stop only the ones we own."""
    if not enabled:
        yield []
        return

    active = server_names_from_resolution(resolution)
    processes: list[subprocess.Popen] = []
    log_handles = []
    log_dir = log_dir or (REPO_ROOT / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    server_env = os.environ.copy()
    server_env["CUSTOM_LOG_DIR"] = log_dir.as_posix()

    try:
        if SQLITE_SERVER_NAME in active and not _port_open(sqlite_port):
            db_path = _source_path(resolution, "sqlite")
            if not db_path:
                db_path = str(REPO_ROOT / "data/10k/raw/10k_financial_data.db")
            log_file = (log_dir / "sqlite_mcp_auto.log").open("a", encoding="utf-8")
            log_handles.append(log_file)
            proc = subprocess.Popen(
                [
                    _python_executable(),
                    "tool_server/sqlite_mcp.py",
                    "--transport",
                    "sse",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(sqlite_port),
                    "--data-path",
                    db_path,
                ],
                cwd=REPO_ROOT,
                env=server_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            processes.append(proc)
            _wait_for_port(sqlite_port)

        if CODE_SERVER_NAME in active and not _port_open(code_port):
            code_root = _scenario_code_root(config_path, scenario)
            log_file = (log_dir / "code_mcp_auto.log").open("a", encoding="utf-8")
            log_handles.append(log_file)
            proc = subprocess.Popen(
                [
                    _python_executable(),
                    "tool_server/code_mcp.py",
                    "--transport",
                    "sse",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(code_port),
                    "--code-root",
                    code_root,
                ],
                cwd=REPO_ROOT,
                env=server_env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            processes.append(proc)
            _wait_for_port(code_port)

        yield processes
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for handle in log_handles:
            handle.close()
