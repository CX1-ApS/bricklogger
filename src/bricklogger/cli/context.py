"""What every command needs from the root: the config directory and API access."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from bricklogger.ops import Operations


@dataclass
class CliContext:
    config_dir: Path
    api_url: str | None = None
    token: str | None = None


def cli_context(ctx: typer.Context) -> CliContext:
    """The context the root callback stored, whichever subcommand is running."""
    found = ctx.find_object(CliContext)
    if found is None:
        raise RuntimeError("the CLI root has not set up its context")
    return found


def operations_of(context: CliContext) -> Operations:
    """The operations the command works through, from the root options."""
    return Operations(
        config_dir=context.config_dir, api_url=context.api_url, token=context.token
    )
