"""Output conventions of the CLI: tables for people, JSON unchanged for scripts,
errors on stderr."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from bricklogger.config.issues import ConfigIssue
from bricklogger.daemon.health import told

stdout = Console()
stderr = Console(stderr=True)


def print_json(data: Any) -> None:
    """Emit JSON exactly as the API would, one document on stdout."""
    typer.echo(json.dumps(data, indent=2, default=str))


def issues_table(title: str, issues: Iterable[ConfigIssue]) -> Table:
    table = Table(title=title, title_justify="left", show_lines=False, box=None)
    table.add_column("File")
    table.add_column("Subject")
    table.add_column("Key")
    table.add_column("Message")
    for issue in issues:
        table.add_row(
            f"{issue.file}.yaml", issue.subject or "", issue.key or "", issue.message
        )
    return table


def health_line(row: Mapping[str, Any], *, state: bool = True) -> str:
    """An instance's state and health on one line:
    ``running, degraded since T — reason, and 1 more``."""
    health = row.get("health")
    text = str(row.get("state", "")) if state else ""
    if not health:
        return text
    text = f"{text}, {health}" if text else str(health)
    if health == "ok":
        return text
    since = f" since {row['since']}" if row.get("since") else ""
    reason = told(row)
    return f"{text}{since} — {reason}" if reason else f"{text}{since}"


def fail(message: str, code: int = 1) -> typer.Exit:
    """Report an error on stderr and return the exit to raise."""
    stderr.print(f"error: {message}", highlight=False)
    return typer.Exit(code)
