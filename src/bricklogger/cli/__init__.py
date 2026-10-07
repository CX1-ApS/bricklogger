"""The ``bricklogger`` command: the control plane and the primary user interface.

Every command maps to one operation on the daemon's API; see
``docs/features/cli.md``. This module holds the root of the command tree and
the global options; the command groups live in their own modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from bricklogger import __version__
from bricklogger.cli.config import rules_app, validate_command
from bricklogger.cli.context import CliContext
from bricklogger.cli.daemon_commands import daemon_app, points_command, status_command
from bricklogger.cli.init import init_command
from bricklogger.cli.mcp_command import mcp_app
from bricklogger.cli.model import model_app
from bricklogger.cli.notify import notify_app
from bricklogger.cli.plugins import plugins_app
from bricklogger.cli.query import query_command
from bricklogger.cli.roles import destinations_app, sources_app
from bricklogger.cli.serve import serve_command
from bricklogger.cli.services import services_app
from bricklogger.cli.update import update_app
from bricklogger.config import resolve_config_dir

app = typer.Typer(
    name="bricklogger",
    help="Data bridge for building automation, driven by a Brick model.",
    no_args_is_help=True,
    add_completion=False,
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"bricklogger {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    config_dir: Annotated[
        Path | None,
        typer.Option(
            "--config-dir",
            help="The config directory; default $BRICKLOGGER_CONFIG_DIR, "
            "then ~/.config/bricklogger.",
            show_default=False,
        ),
    ] = None,
    api: Annotated[
        str | None,
        typer.Option(
            "--api",
            help="The daemon's API URL, for a deliberately exposed daemon; "
            "default the binding in daemon.yaml.",
            show_default=False,
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            envvar="BRICKLOGGER_API_TOKEN",
            help="The API token; default the one in daemon.yaml.",
            show_default=False,
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Collect data from a building's automation systems based on its Brick model."""
    ctx.obj = CliContext(
        config_dir=resolve_config_dir(config_dir), api_url=api, token=token
    )


app.command("init")(init_command)
app.command("validate")(validate_command)
app.command("status")(status_command)
app.command("points")(points_command)
app.add_typer(daemon_app, name="daemon")
app.add_typer(services_app, name="services")
app.add_typer(sources_app, name="sources")
app.add_typer(destinations_app, name="destinations")
app.add_typer(rules_app, name="rules")
app.add_typer(notify_app, name="notify")
app.add_typer(plugins_app, name="plugins")
app.add_typer(update_app, name="update")
app.add_typer(model_app, name="model")
app.command("query")(query_command)
app.command("serve")(serve_command)
app.add_typer(mcp_app, name="mcp")


def main() -> None:
    """Entry point of the ``bricklogger`` console script."""
    app()
