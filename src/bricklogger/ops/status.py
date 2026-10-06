"""Whether the daemon, the web interface and the MCP server run, and the
daemon's summary when it answers: the three lines ``status`` always has,
daemon or no daemon."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.config.schema import DaemonSettings
from bricklogger.ops.client import DaemonApi
from bricklogger.ops.context import Operations
from bricklogger.ops.errors import OperationError

PID_FILE = "bricklogger.pid"


def read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def process_alive(pid: int) -> bool:
    """Whether the process exists and has not exited; a zombie counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    state = stat.rsplit(")", 1)[-1].split()
    return not state or state[0] != "Z"


def probe(ops: Operations) -> dict[str, Any]:
    """``daemon``, ``web`` and ``mcp`` with whether they run and where, and
    ``status``, the daemon summary, ``None`` when the daemon does not answer."""
    try:
        settings = load_daemon_settings(ops.config_dir, ops.env)
    except ConfigError as exc:
        raise OperationError(f"daemon.yaml: {exc}") from exc
    client = DaemonApi(
        ops.api_url or f"http://{settings.api.host}:{settings.api.port}",
        ops.token or settings.api.token,
    )
    try:
        running = client.reachable()
        summary = client.get("/v1/status") if running else None
    finally:
        client.close()
    pid = read_pid(settings.data_dir / PID_FILE)
    daemon = {
        "running": running,
        "url": client.base_url,
        "pid": pid if pid is not None and process_alive(pid) else None,
        "started_at": summary.get("started_at") if summary else None,
    }
    return {
        "daemon": daemon,
        "web": probe_web(settings),
        "mcp": probe_mcp(settings),
        "status": summary,
    }


def probe_web(settings: DaemonSettings) -> dict[str, Any]:
    """Whether `serve` answers at its binding in daemon.yaml."""
    url = binding(settings.web.host, settings.web.port)
    return {"running": answers(url), "url": url}


def probe_mcp(settings: DaemonSettings) -> dict[str, Any]:
    """Whether `mcp serve --http` answers at its binding in daemon.yaml.

    An installation whose assistant speaks stdio has no server there, and
    nothing answering is the honest answer to that too.
    """
    url = binding(settings.mcp.host, settings.mcp.port)
    return {"running": answers(url), "url": f"{url}/mcp"}


def binding(host: str, port: int) -> str:
    """The URL a process bound to this host and port is asked at."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{probe_host}:{port}"


def answers(url: str) -> bool:
    """Whether something alive answers ``GET /health/live`` there."""
    import httpx

    try:
        return httpx.get(f"{url}/health/live", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False
