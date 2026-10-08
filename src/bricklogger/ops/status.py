"""Whether the daemon, the web interface and the MCP server run, and the
daemon's summary when it answers: the three lines ``status`` always has,
daemon or no daemon."""

from __future__ import annotations

import os
from datetime import UTC, datetime
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
    live = liveness(client.base_url)
    try:
        summary = client.get("/v1/status") if live is not None else None
    finally:
        client.close()
    daemon = _door(client.base_url, live)
    if summary and not daemon["started_at"]:
        daemon["started_at"] = summary.get("started_at")
    return {
        "daemon": daemon,
        "web": probe_web(settings),
        "mcp": probe_mcp(settings),
        "status": summary,
    }


def probe_web(settings: DaemonSettings) -> dict[str, Any]:
    """Whether `serve` answers at its binding in daemon.yaml."""
    url = binding(settings.web.host, settings.web.port)
    return _door(url, liveness(url))


def probe_mcp(settings: DaemonSettings) -> dict[str, Any]:
    """Whether `mcp serve --http` answers at its binding in daemon.yaml.

    An installation whose assistant speaks stdio has no server there, and
    nothing answering is the honest answer to that too.
    """
    url = binding(settings.mcp.host, settings.mcp.port)
    return _door(f"{url}/mcp", liveness(url))


def _door(url: str, live: dict[str, Any] | None) -> dict[str, Any]:
    """One of the three lines: whether it runs, where, and the process ID and
    start time it gave; a process that gives neither is running without."""
    answer = live or {}
    return {
        "running": live is not None,
        "url": url,
        "pid": answer.get("pid"),
        "started_at": answer.get("started_at"),
    }


def binding(host: str, port: int) -> str:
    """The URL a process bound to this host and port is asked at."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    return f"http://{probe_host}:{port}"


def answers(url: str) -> bool:
    """Whether something alive answers ``GET /health/live`` there."""
    return liveness(url) is not None


def liveness(url: str) -> dict[str, Any] | None:
    """What answers ``GET /health/live`` there, or ``None`` when nothing does.
    An answer without a body still counts as alive."""
    import httpx

    try:
        response = httpx.get(f"{url}/health/live", timeout=2.0)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def live_answer(started_at: datetime | None) -> dict[str, Any]:
    """What a process answers on ``GET /health/live``: that it lives, its own
    process ID and when it started. The endpoint needs no login, so it tells
    nothing more."""
    return {
        "live": "ok",
        "pid": os.getpid(),
        "started_at": stamp(started_at) if started_at is not None else None,
    }


def stamp(moment: datetime) -> str:
    """A time as ``status`` shows it: UTC to the second."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
