"""``bricklogger init``: the first command on a new machine.

In a terminal it is a guided setup — which sources and destinations, a name
and the settings of each from the plugin's own schema, secrets straight into
the ``env`` file, and last the offer to set up the services — and
``--non-interactive`` writes the four files with commented examples instead.
Either way it refuses to overwrite a file that exists, so it can never destroy
a configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

import typer

from bricklogger.cli.client import ApiClient
from bricklogger.cli.config import registry, report_validation
from bricklogger.cli.context import CliContext, cli_context
from bricklogger.cli.guided import ask_settings, write_env
from bricklogger.cli.output import fail
from bricklogger.cli.services import install as install_services
from bricklogger.config import (
    CONFIG_FILES,
    RESERVED_INSTANCE_NAMES,
    FilesExist,
    config_file,
    default_data_dir,
    examples_for,
    set_instance,
    validate_configuration,
    write_examples,
    write_text,
)
from bricklogger.ops import services
from bricklogger.sdk.declaration import DestinationDeclaration, SourceDeclaration

NonInteractiveFlag = Annotated[
    bool,
    typer.Option(
        "--non-interactive",
        help="Ask nothing; write the four files with commented examples.",
    ),
]


def init_command(
    ctx: typer.Context, non_interactive: NonInteractiveFlag = False
) -> None:
    """Set up a new configuration: guided, or the commented examples.

    Refuses to overwrite a file that exists.
    """
    context = cli_context(ctx)
    if context.api_url is not None:
        if not non_interactive:
            raise fail("through --api only `init --non-interactive` is available")
        result = ApiClient.from_context(context).post("/v1/config/init")
        for path in result["written"]:
            typer.echo(f"wrote {path}")
        return
    if non_interactive:
        try:
            written = write_examples(context.config_dir)
        except FilesExist as exc:
            raise fail(str(exc)) from exc
        except OSError as exc:
            raise fail(f"could not write to {context.config_dir}: {exc}") from exc
        for path in written:
            typer.echo(f"wrote {path}")
        return
    _guided(context)


# --- the guided setup ---------------------------------------------------------


def _guided(context: CliContext) -> None:
    config_dir = context.config_dir
    existing = [
        config_file(config_dir, name)
        for name in CONFIG_FILES
        if config_file(config_dir, name).exists()
    ]
    if existing:
        raise fail(str(FilesExist(existing)))
    installed = registry()
    typer.echo(f"configuration: {config_dir}")
    typer.echo(f"data:          {default_data_dir(config_dir)}")
    typer.echo("")

    secrets: dict[str, str] = {}
    sources = _collect("source", "sources", installed.sources, secrets)
    destinations = _collect(
        "destination", "destinations", installed.destinations, secrets
    )

    examples = examples_for(config_dir)
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        written = [
            write_text(config_dir, "daemon", examples["daemon"]),
            write_text(config_dir, "sources", sources),
            write_text(config_dir, "destinations", destinations),
            write_text(config_dir, "rules", examples["rules"]),
        ]
        written.append(write_env(config_dir, secrets))
    except OSError as exc:
        raise fail(f"could not write to {config_dir}: {exc}") from exc
    typer.echo("")
    for path in written:
        typer.echo(f"wrote {path}")

    result = validate_configuration(config_dir, installed)
    typer.echo("")
    report_validation(
        result.as_dict(),
        False,
        ok_line=f"ok: {config_dir}",
        where=str(config_dir),
    )
    typer.echo("")
    started = _offer_services(context)
    typer.echo("")
    if started:
        typer.echo("Next: upload the model.")
    else:
        typer.echo("Next: start the daemon and upload the model.")
        typer.echo("  bricklogger daemon start")
    typer.echo("  bricklogger model upload <model.ttl>")
    typer.echo("  bricklogger status")


def _offer_services(context: CliContext) -> bool:
    """Ask whether to set up the services, and do it on a yes; whether the
    daemon now runs as one. Nothing is asked where they cannot be set up."""
    if services.refusal(services.this_host(), context.config_dir) is not None:
        return False
    if not typer.confirm(
        "Set up the services now? The daemon then starts at boot", default=True
    ):
        typer.echo("The same later: bricklogger services install [--web] [--mcp]")
        return False
    web = typer.confirm("  Also the web interface?", default=True)
    mcp = typer.confirm("  Also the MCP server over HTTP?", default=False)
    install_services(context.config_dir, web=web, mcp=mcp)
    return True


def _collect(
    role: str,
    file: str,
    declarations: Mapping[str, SourceDeclaration | DestinationDeclaration],
    secrets: dict[str, str],
) -> str:
    """Ask for the instances of one role, and return the file's text."""
    text = f"# {file}.yaml: one entry per {role} instance, with the name as the key.\n"
    types = sorted(declarations)
    if not types:
        typer.echo(f"No {role} plugin is installed; {file}.yaml stays empty.")
        return text
    typer.echo(f"{file.capitalize()} — installed types:")
    for index, type_name in enumerate(types, start=1):
        typer.echo(f"  {index}) {type_name:<12} {declarations[type_name].description}")
    names: list[str] = []
    while True:
        question = (
            f"Add a{'nother' if names else ''} {role}? [1-{len(types)}, Enter for no]"
        )
        chosen = _choose(question, types)
        if chosen is None:
            break
        declaration = declarations[chosen]
        name = _ask_name(role, chosen, names)
        schema = declaration.config_schema.model_json_schema()
        settings = ask_settings(name, schema, secrets)
        text = set_instance(text, name, {"type": chosen, **settings})
        names.append(name)
        typer.echo("")
    return text


def _choose(question: str, types: list[str]) -> str | None:
    while True:
        answer = str(typer.prompt(question, default="", show_default=False)).strip()
        if not answer:
            return None
        if answer in types:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(types):
            return types[int(answer) - 1]
        typer.echo(f"  choose a number from 1 to {len(types)}, a type name, or Enter")


def _ask_name(role: str, type_name: str, taken: list[str]) -> str:
    suggested = type_name.replace("-", "_") + ("" if taken else "_main")
    while True:
        name = str(typer.prompt(f"  name of the {role}", default=suggested)).strip()
        if name in RESERVED_INSTANCE_NAMES:
            typer.echo(f"  {name!r} is a subcommand; choose another name")
        elif name in taken:
            typer.echo(f"  {name!r} is taken; choose another name")
        elif not name or any(c.isspace() for c in name):
            typer.echo("  a name has no spaces")
        else:
            return name
