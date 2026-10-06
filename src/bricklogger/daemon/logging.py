"""The daemon's own logging: text or JSON lines, to stdout for ``daemon run``
and to a size-rotated file for ``daemon start``. See ``docs/features/daemon.md``,
"Logging".
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from bricklogger.config.schema import LogSettings

TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# The attributes every LogRecord carries; anything else was passed as ``extra``.
STANDARD_ATTRIBUTES = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, and the extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in STANDARD_ATTRIBUTES and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: LogSettings, *, file: Path | None) -> None:
    """Route the root logger to stdout, or to a file rotated by size."""
    root = logging.getLogger()
    root.setLevel(settings.level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    handler: logging.Handler
    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            file,
            maxBytes=settings.max_size,
            backupCount=settings.keep,
            encoding="utf-8",
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter() if settings.format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    root.addHandler(handler)
    # The libraries underneath report at info level about things the daemon
    # itself reports; their debug output stays available for troubleshooting.
    quiet = logging.WARNING if settings.level != "debug" else logging.DEBUG
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(quiet)
