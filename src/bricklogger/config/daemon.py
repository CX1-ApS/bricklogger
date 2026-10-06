"""``daemon.yaml`` alone, for commands that need the data directory before
anything else is known."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from bricklogger.config.instances import block_span, indent_of, key_line
from bricklogger.config.issues import ConfigError, issues_from_validation_error
from bricklogger.config.loader import (
    config_file,
    interpolate,
    parse_text,
    read_env_file,
)
from bricklogger.config.schema import DaemonSettings


def load_daemon_settings(
    config_dir: Path, env: Mapping[str, str] | None = None
) -> DaemonSettings:
    """Read and validate ``daemon.yaml``; a missing file gives every default.

    Secrets come from the directory's ``env`` file and from the
    environment, where the environment wins, exactly as they do when the
    whole directory is loaded. ``daemon.yaml`` held no secret until
    notifications arrived, which is why this went unnoticed.
    """
    environment: Mapping[str, str] = {
        **read_env_file(config_dir),
        **(os.environ if env is None else env),
    }
    path = config_file(config_dir, "daemon")
    text = path.read_text(encoding="utf-8") if path.is_file() else None
    data, issues = parse_text("daemon", text)
    if not issues:
        data, issues = interpolate(data, environment, "daemon")
    if not issues:
        try:
            return DaemonSettings.model_validate(data)
        except ValidationError as exc:
            issues = issues_from_validation_error(exc, "daemon")
    raise ConfigError("; ".join(str(issue) for issue in issues))


def with_mcp_token(text: str | None, reference: str) -> str:
    """``daemon.yaml`` with ``mcp.token`` set to the reference, nothing else moved.

    The file is written by hand and carries comments, so the line is replaced
    where it stands, or spliced into the ``mcp`` block, or the block appended
    when there is none — the way an instance is spliced into its own file.
    """
    original = text or ""
    block = f"mcp:\n  token: {reference}\n"
    start = key_line(original, "mcp")
    if start is None:
        head = original.rstrip("\n")
        return f"{head}\n\n{block}" if head else block
    lines = original.splitlines(keepends=True)
    _, end = block_span(lines, start)
    inner = indent_of(lines[start]) + 2
    for index in range(start + 1, end):
        if not lines[index].strip():
            continue
        inner = indent_of(lines[index])
        if lines[index].lstrip().startswith("token:"):
            lines[index] = f"{' ' * inner}token: {reference}\n"
            return "".join(lines)
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, f"{' ' * inner}token: {reference}\n")
    return "".join(lines)
