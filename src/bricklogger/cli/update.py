"""``bricklogger update``: what there is to upgrade, and the upgrade itself.

``update status`` installs nothing. ``update core``, ``update all`` and
``update TYPE`` upgrade from PyPI with ``uv tool install``, validate with
the new version, put the previous versions back when that fails, write the
units again and restart what runs as a unit. ``update`` alone shows this help,
as every group does. See ``docs/features/cli.md``, "update".
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.table import Table
from typer.core import TyperGroup
from typer.main import get_command as typer_command

from bricklogger.cli.context import cli_context
from bricklogger.cli.output import fail, print_json, stderr, stdout
from bricklogger.ops.errors import OperationError
from bricklogger.ops.updates import CORE, Updated, UpdateRefused, status, update

NoRestart = Annotated[
    bool,
    typer.Option(
        "--no-restart",
        help="Leave the daemon, the web interface and the MCP server alone.",
    ),
]


class _UpdateGroup(TyperGroup):
    """A group whose unknown subcommand is a plugin type to upgrade."""

    # Typer carries its own copy of click, so the types are left open here.
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        if command is not None or cmd_name.startswith("-"):
            return command
        return _type_command(cmd_name)


update_app = typer.Typer(
    cls=_UpdateGroup,
    add_completion=False,
    no_args_is_help=True,
    help="Upgrade Bricklogger and its plugins from PyPI. `update status` shows what "
    "there is; `update core` upgrades Bricklogger alone, as far as the plugins "
    "allow; `update TYPE` upgrades the plugin that provides the type; `update all` "
    "upgrades everything together. The new version validates the configuration "
    "before anything is restarted, and the previous versions are put back when it "
    "does not hold.",
)


@update_app.command("status")
def status_command(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of a table.")
    ] = False,
) -> None:
    """For Bricklogger and every plugin: installed, newest, and newest that fits."""
    try:
        rows = status()
    except OperationError as exc:
        raise fail(exc.message) from exc
    if json_output:
        print_json([row.as_dict() for row in rows])
        return
    table = Table(box=None)
    for column in ("Package", "Installed", "Newest", "Fits", "Note"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.name,
            row.installed,
            row.newest or "?",
            row.fits or "?",
            _note(row.name, row.error, row.newest, row.fits, row.held_by),
        )
    stdout.print(table)


def _note(
    name: str,
    error: str | None,
    newest: str | None,
    fits: str | None,
    held_by: tuple[str, ...],
) -> str:
    if error is not None:
        return f"not looked up: {error}"
    if newest is not None and fits is not None and newest != fits and held_by:
        return (
            f"{newest} held back by {', '.join(held_by)}; "
            "`update all` moves them together"
        )
    return ""


@update_app.command("core")
def core_command(ctx: typer.Context, no_restart: NoRestart = False) -> None:
    """Upgrade Bricklogger alone, to the newest release the plugins allow."""
    _update(ctx, "core", no_restart)


@update_app.command("all")
def all_command(ctx: typer.Context, no_restart: NoRestart = False) -> None:
    """Upgrade Bricklogger and every plugin to the newest releases that fit together."""
    _update(ctx, "all", no_restart)


def _type_command(type_name: str) -> Any:
    """``update TYPE``, made for the type that was typed."""
    sub = typer.Typer(add_completion=False)

    @sub.command(type_name)
    def command(ctx: typer.Context, no_restart: NoRestart = False) -> None:
        """Upgrade the plugin that provides this type, the rest held."""
        _update(ctx, type_name, no_restart)

    return typer_command(sub)


def _update(ctx: typer.Context, target: str, no_restart: bool) -> None:
    context = cli_context(ctx)
    try:
        result = update(target, config_dir=context.config_dir, restart=not no_restart)
    except UpdateRefused as exc:
        for error in exc.errors:
            where = ".".join(
                str(part)
                for part in (
                    f"{error.get('file')}.yaml",
                    error.get("subject"),
                    error.get("key"),
                )
                if part and part != "None.yaml"
            )
            stderr.print(f"  {where}: {error.get('message')}", highlight=False)
        back = "; the previous versions are back in place" if exc.restored else ""
        raise fail(f"{exc.message}{back}") from exc
    except OperationError as exc:
        raise fail(exc.message) from exc
    _report(result, target, no_restart)


def _report(result: Updated, target: str, no_restart: bool) -> None:
    if not result.moved:
        typer.echo("already up to date")
        if target == "core":
            _held_back()
        return
    typer.echo(
        "updated "
        + ", ".join(
            _movement(name, old, new) for name, (old, new) in result.moved.items()
        )
    )
    for warning in result.warnings:
        subject = warning.get("subject") or warning.get("file")
        typer.echo(f"warning: {subject}: {warning.get('message')}")
    if result.units:
        typer.echo(f"rewrote {', '.join(result.units)} from the new templates")
    if result.restarted:
        typer.echo(f"restarted {', '.join(result.restarted)}")
    for unit in result.not_answering:
        typer.echo(
            f"{unit} was restarted but does not answer yet; check its log with "
            f"journalctl --user -u {unit}"
        )
    for note in result.notes:
        typer.echo(note)
    if result.container:
        typer.echo("Restart the containers to run it: docker compose restart")
        if target == "all":
            typer.echo("Bricklogger itself comes with a new image.")
    elif no_restart:
        typer.echo("nothing was restarted (--no-restart)")
    if target == "core":
        _held_back()


def _movement(name: str, old: str | None, new: str | None) -> str:
    if old is None:
        return f"{name} {new} (new)"
    if new is None:
        return f"{name} {old} (removed)"
    return f"{name} {old} → {new}"


def _held_back() -> None:
    """Name a newer Bricklogger the installed plugins exclude."""
    try:
        rows = status()
    except OperationError:
        return
    core = next((row for row in rows if row.name == CORE), None)
    if core is None or not core.held_by or core.newest is None:
        return
    typer.echo(
        f"bricklogger {core.newest} is out, but {', '.join(core.held_by)} does not "
        "allow it yet; `bricklogger update all` moves them together"
    )
