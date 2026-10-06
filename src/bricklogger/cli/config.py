"""The ``config`` views — one per owner of a file — and ``validate``.

``daemon config``, ``sources config``, ``destinations config`` and ``rules
config`` print their file as written; ``… config edit`` opens it in ``$EDITOR``
and writes it validated as a whole, through the API when a daemon answers and
directly to the file otherwise. ``validate`` checks the whole directory with
the daemon's validation, or asks the daemon with ``--api``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any

import click
import typer

from bricklogger.cli.client import ApiClient, reachable_client
from bricklogger.cli.context import CliContext, cli_context, operations_of
from bricklogger.cli.output import fail, issues_table, print_json, stderr
from bricklogger.config import (
    CONFIG_FILES,
    ConfigIssue,
    config_file,
    parse_text,
    read_texts,
    validate_configuration,
)
from bricklogger.ops.config import write_file as write_config_file
from bricklogger.ops.errors import OperationError
from bricklogger.sdk.registry import PluginError, PluginRegistry

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit the API's JSON instead of a table.")
]
YAML = "application/yaml"


def edit_text(text: str) -> str | None:
    """Open the text in the user's editor; ``None`` when it was not saved."""
    return click.edit(text, extension=".yaml", require_save=True)


def registry() -> PluginRegistry:
    """The installed plugins, or a plain failure when one cannot be loaded."""
    try:
        return PluginRegistry.from_entry_points()
    except PluginError as exc:
        raise fail(str(exc)) from exc


def validate_command(ctx: typer.Context, json_output: JsonFlag = False) -> None:
    """Validate the whole config directory, including the plugins' schemas."""
    context = cli_context(ctx)
    if context.api_url is not None:
        client = ApiClient.from_context(context)
        data = client.post("/v1/config/validate")
        report_validation(data, json_output, ok_line=f"ok: {client.base_url}")
        return
    result = validate_configuration(context.config_dir, registry())
    summary = ""
    if result.configuration is not None:
        config = result.configuration
        summary = ", ".join(
            (
                count(len(config.sources), "source"),
                count(len(config.destinations), "destination"),
                count(len(config.rules), "rule"),
            )
        )
    report_validation(
        result.as_dict(),
        json_output,
        ok_line=f"ok: {context.config_dir} ({summary})",
        where=str(context.config_dir),
    )


def report_validation(
    data: Mapping[str, Any], json_output: bool, *, ok_line: str, where: str = ""
) -> None:
    """Print a validation answer the way every command does, and exit on errors."""
    if json_output:
        print_json(data)
        raise typer.Exit(0 if data["valid"] else 1)
    if data["errors"]:
        stderr.print(_issues_table("Errors", data["errors"]))
    if data["warnings"]:
        stderr.print(_issues_table("Warnings", data["warnings"]))
    if not data["valid"]:
        prefix = f"{where}: " if where else ""
        typer.echo(f"{prefix}{count(len(data['errors']), 'error')}", err=True)
        raise typer.Exit(1)
    typer.echo(ok_line)


def count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _issues_table(title: str, issues: list[dict[str, Any]]) -> Any:
    from bricklogger.cli.output import issues_table

    return issues_table(title, [ConfigIssue.from_dict(issue) for issue in issues])


def config_view(name: str) -> typer.Typer:
    """A ``config`` group for one file: ``show`` prints it, ``edit`` edits it."""
    if name not in CONFIG_FILES:
        raise ValueError(f"unknown configuration file {name!r}")
    app = typer.Typer(
        no_args_is_help=True,
        help=f"{name}.yaml: show it as written, or edit it in $EDITOR.",
    )

    @app.command("show")
    def show(ctx: typer.Context, json_output: JsonFlag = False) -> None:
        """Print the file as written; environment variables are not expanded."""
        show_file(cli_context(ctx), name, json_output)

    @app.command("edit")
    def edit(ctx: typer.Context) -> None:
        """Edit the file in $EDITOR; validated as a whole before it is written."""
        edit_file(cli_context(ctx), name)

    return app


def show_file(context: CliContext, name: str, json_output: bool) -> None:
    """One file as written — through the API when asked for, from disk otherwise."""
    if context.api_url is not None:
        client = ApiClient.from_context(context)
        if json_output:
            parsed = client.get_text(f"/v1/config/{name}", accept="application/json")
            print_json(json.loads(parsed))
            return
        typer.echo(f"# {client.base_url}")
        typer.echo(client.get_text(f"/v1/config/{name}", accept=YAML).rstrip("\n"))
        return
    text = read_texts(context.config_dir)[name]
    if json_output:
        parsed, issues = parse_text(name, text)
        if issues:
            raise fail(str(issues[0]))
        print_json(parsed)
        return
    path = config_file(context.config_dir, name)
    typer.echo(f"# {context.config_dir}")
    if text is None:
        typer.echo(f"# {path.name} does not exist and is read as empty")
    else:
        typer.echo(text.rstrip("\n"))


def edit_file(context: CliContext, name: str) -> None:
    """Edit one file: through the API when a daemon answers, on disk otherwise."""
    client = reachable_client(context)
    if client is not None:
        original = client.get_text(f"/v1/config/{name}", accept=YAML)
    else:
        original = read_texts(context.config_dir)[name] or ""
    edited = edit_text(original)
    if edited is None or edited == original:
        typer.echo("no change")
        return
    result = write_file(context, client, name, edited)
    if client is not None:
        typer.echo(f"{name}.yaml written and applied")
    else:
        typer.echo(f"wrote {config_file(context.config_dir, name)}")
    for warning in result.get("warnings", []):
        typer.echo(f"warning: {warning['message']}")


def write_file(
    context: CliContext, client: ApiClient | None, name: str, text: str
) -> Mapping[str, Any]:
    """Write one file validated as a whole, and return what the validation said.

    Through the API the daemon validates and applies; without one the directory
    is validated with the change in place of the file, and nothing is written
    when it does not hold.
    """
    try:
        return write_config_file(operations_of(context), client, name, text)
    except OperationError as exc:
        raise failure(exc) from exc


def failure(exc: OperationError) -> typer.Exit:
    """The exit of a failed operation: its issues as a table, then the message."""
    if exc.issues:
        stderr.print(issues_table("Errors", exc.issues))
    return fail(exc.message)


rules_app = typer.Typer(no_args_is_help=True, help="The rule set: rules.yaml.")
rules_app.add_typer(config_view("rules"), name="config")
