"""``bricklogger services``: the daemon, the web interface and the MCP server as
``systemd --user`` units of the login. See ``docs/features/cli.md``,
"services"."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from bricklogger.cli.context import cli_context
from bricklogger.cli.output import fail, print_json, stdout
from bricklogger.ops import services
from bricklogger.ops.errors import OperationError

services_app = typer.Typer(
    no_args_is_help=True,
    help="Run the daemon, the web interface and the MCP server as systemd --user "
    "services of this login, started at boot.",
)

WebFlag = Annotated[bool, typer.Option("--web", help="The web interface.")]
McpFlag = Annotated[bool, typer.Option("--mcp", help="The MCP server over HTTP.")]


@services_app.command("install")
def install_command(
    ctx: typer.Context, web: WebFlag = False, mcp: McpFlag = False
) -> None:
    """Set up the daemon as a service, and the web interface and the MCP server
    when their flag is given; enable lingering, and start what was written.

    Can be run again: a flag adds its unit and leaves the others as they are.
    """
    install(cli_context(ctx).config_dir, web=web, mcp=mcp)


def install(config_dir: Path, *, web: bool, mcp: bool) -> None:
    """``services install``, and what ``init`` runs when it is told yes."""
    try:
        result = services.install(config_dir=config_dir, web=web, mcp=mcp)
    except OperationError as exc:
        raise fail(exc.message) from exc
    if result.written:
        typer.echo(f"wrote {', '.join(result.written)} in {result.unit_dir}")
    if result.started:
        typer.echo(f"started {', '.join(result.started)}")
    if result.restarted:
        typer.echo(f"restarted {', '.join(result.restarted)} with the new unit")
    if not (result.written or result.started or result.restarted):
        typer.echo(f"{', '.join(result.units)} already set up and running")
    if result.linger_command is not None:
        typer.echo(
            "lingering could not be enabled, so the services stop at logout and do "
            "not start at boot; enable it once with:"
        )
        typer.echo(f"  {result.linger_command}")


@services_app.command("uninstall")
def uninstall_command(web: WebFlag = False, mcp: McpFlag = False) -> None:
    """Stop, disable and remove the services: all of them without a flag, the
    ones named with one. The configuration and the data stay."""
    try:
        result = services.uninstall(web=web, mcp=mcp)
    except OperationError as exc:
        raise fail(exc.message) from exc
    if result.removed:
        typer.echo(f"removed {', '.join(result.removed)}")
    else:
        typer.echo("no service was set up")


@services_app.command("status")
def status_command(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a table.")
    ] = False,
) -> None:
    """For each unit: written, enabled and active; and whether lingering is on."""
    state = services.status()
    if json_output:
        print_json(state.as_dict())
        return
    table = Table(box=None)
    for column in ("Unit", "Written", "Enabled", "Active"):
        table.add_column(column)
    for unit in state.units:
        table.add_row(
            unit.name, _word(unit.written), _word(unit.enabled), _word(unit.active)
        )
    stdout.print(table)
    if not state.systemd:
        typer.echo("systemctl --user does not answer on this machine")
    else:
        typer.echo(f"lingering: {_word(state.linger)}")


def _word(value: bool | None) -> str:
    return "?" if value is None else "yes" if value else "no"
