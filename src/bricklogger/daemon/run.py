"""``bricklogger daemon run``: the daemon in the foreground, serving its API.

With ``log_to_file`` the daemon logs to ``log.file`` with rotation instead of
stdout; that is how ``daemon start`` runs it in the background.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn

from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon, DaemonError
from bricklogger.daemon.logging import configure_logging

log = logging.getLogger(__name__)

PID_FILE = "bricklogger.pid"


def run_daemon(config_dir: Path, *, log_to_file: bool = False) -> int:
    """Start the daemon, serve the API until stopped, then stop the daemon."""
    try:
        settings = load_daemon_settings(config_dir)
    except ConfigError as exc:
        print(f"error: daemon.yaml: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings.log, file=settings.log_file if log_to_file else None)
    daemon = Daemon(config_dir)
    try:
        daemon.start()
    except DaemonError as exc:
        log.error("the daemon cannot start: %s", exc)
        if not log_to_file:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    server: uvicorn.Server | None = None

    def request_stop() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(daemon, request_stop)
    config = uvicorn.Config(
        app,
        host=settings.api.host,
        port=settings.api.port,
        log_level="warning",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    log.info("serving the API on http://%s:%d", settings.api.host, settings.api.port)
    try:
        server.run()
    finally:
        daemon.stop()
    return 0
