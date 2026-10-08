"""``bricklogger sources`` and ``bricklogger destinations``: one role's instances.

The two groups have the same subcommands — ``status`` with the detail of the
status tree, ``add``, ``edit`` and ``remove``, ``start``, ``stop`` and
``restart``, and ``config`` for the file — and any other word is the name of
an instance: ``sources NAME`` shows it, ``sources NAME <tool>`` runs one of its
type's tools on it. The role decides which file is written and which plugins
are eligible. Configuration is written the way the ``config`` views write it:
validated as a whole, atomically, through the API whenever a daemon answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.table import Table
from typer.core import TyperGroup
from typer.main import get_command as typer_command

from bricklogger.cli.client import ApiClient, reachable_client
from bricklogger.cli.config import config_view, failure
from bricklogger.cli.context import CliContext, cli_context, operations_of
from bricklogger.cli.guided import ask_settings, write_env
from bricklogger.cli.output import fail, print_json, stdout
from bricklogger.cli.plugins import (
    coerce_setting,
    declaration_of,
    echo_result,
    flag,
    is_secret,
    local_registry,
    parameters_from_args,
    run_tool_offline_cli,
)
from bricklogger.config import RESERVED_INSTANCE_NAMES, instances_in, read_texts
from bricklogger.ops.config import remove_instance_from_file, write_instance
from bricklogger.ops.errors import OperationError

YAML = "application/yaml"
STATES = ("starting", "running", "failed", "stopped", "unconfigured")
EXTRA = {"allow_extra_args": True, "ignore_unknown_options": True}

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit the API's JSON instead of a table.")
]
OutputOption = Annotated[
    Path | None, typer.Option("-o", "--output", help="Write the document to a file.")
]
NameArgument = Annotated[str, typer.Argument(help="The instance's name.")]
StateOption = Annotated[
    str | None, typer.Option("--state", help=f"Only these: {', '.join(STATES)}.")
]


class _RoleGroup(TyperGroup):
    """A group whose unknown subcommand is the name of an instance."""

    role = ""

    # Typer carries its own copy of click, so the types are left open here.
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        if command is not None or cmd_name.startswith("-"):
            return command
        return _instance_group(self.role, cli_context(ctx), cmd_name)


class _SourcesGroup(_RoleGroup):
    role = "source"


class _DestinationsGroup(_RoleGroup):
    role = "destination"


def _instance_group(role: str, context: CliContext, name: str) -> Any:
    """``sources NAME``, made for the name that was typed: a group with ``show``
    and one command per tool of the instance's type, so its help says what can
    be done with it."""
    client = reachable_client(context)
    file = _file(role)
    settings = instances_in(_read_file(context, client, file)).get(name)
    if not isinstance(settings, Mapping):
        return _missing(role, name)
    type_name = _type_of(settings)
    declaration = declaration_of(context, client, type_name)
    sub = typer.Typer(
        name=name,
        no_args_is_help=True,
        add_completion=False,
        help=f"The {role} {name!r} ({type_name}): show it, or run one of its tools.",
    )

    @sub.callback()
    def group() -> None:
        pass

    @sub.command("show")
    def show(ctx: typer.Context, json_output: JsonFlag = False) -> None:
        """The instance's state and its configuration as written."""
        _show_instance(role, client, name, settings, declaration, json_output)

    for tool in declaration.get("tools") or []:
        _add_tool(sub, role, context, client, name, type_name, tool)
    return typer_command(sub)


def _add_tool(
    sub: typer.Typer,
    role: str,
    context: CliContext,
    client: ApiClient | None,
    name: str,
    type_name: str,
    tool: Mapping[str, Any],
) -> None:
    tool_name = str(tool["name"])
    lines = [str(tool.get("description") or "")]
    parameters = (tool.get("parameters") or {}).get("properties") or {}
    if parameters:
        required = set((tool.get("parameters") or {}).get("required") or [])
        lines.append("\n\b")
        for key, spec in parameters.items():
            kind = spec.get("type") or "value"
            note = "required" if key in required else f"default {spec.get('default')!r}"
            lines.append(f"{flag(key)} {kind}, {note}: {spec.get('description') or ''}")

    if tool.get("document"):
        lines.append(
            "\nThe result is a document: JSON to standard output, or to a file "
            "with -o FILE."
        )

        @sub.command(tool_name, context_settings=dict(EXTRA), help="\n".join(lines))
        def run_document(
            ctx: typer.Context,
            output: OutputOption = None,
        ) -> None:
            _run_tool(
                role,
                context,
                client,
                name,
                type_name,
                tool_name,
                ctx.args,
                True,
                output,
            )

        return

    @sub.command(tool_name, context_settings=dict(EXTRA), help="\n".join(lines))
    def run(ctx: typer.Context, json_output: JsonFlag = False) -> None:
        _run_tool(
            role, context, client, name, type_name, tool_name, ctx.args, json_output
        )


def _missing(role: str, name: str) -> Any:
    """A command for a name that is no instance: it fails plainly when run."""
    sub = typer.Typer(add_completion=False)

    @sub.command(name, context_settings=dict(EXTRA))
    def command(ctx: typer.Context) -> None:
        file = _file(role)
        raise fail(
            f"no {role} named {name!r}; `bricklogger {file} status` lists them, "
            f"and `bricklogger {file} --help` the subcommands"
        )

    return typer_command(sub)


def _role_app(role: str, group: type[_RoleGroup]) -> typer.Typer:
    file = _file(role)
    app = typer.Typer(
        cls=group,
        no_args_is_help=True,
        help=f"The configured {file}: their state, their configuration and their "
        f"tools. Any other word is an instance's name: `{file} NAME`, and "
        f"`{file} NAME TOOL` runs one of its tools.",
    )

    @app.command("status")
    def status(
        ctx: typer.Context, state: StateOption = None, json_output: JsonFlag = False
    ) -> None:
        """The instances with their state and the status tree's detail."""
        if state is not None and state not in STATES:
            raise fail(f"unknown state {state!r}; choose one of {', '.join(STATES)}")
        context = cli_context(ctx)
        _echo_instances(role, context, reachable_client(context), state, json_output)

    @app.command("add", context_settings=dict(EXTRA))
    def add(
        ctx: typer.Context,
        name: NameArgument,
        type_name: Annotated[
            str, typer.Option("--type", help="The plugin type, e.g. bacnet-ip.")
        ],
    ) -> None:
        """Write a new instance; settings as --name value, or asked for."""
        context = cli_context(ctx)
        _write_instance(
            role, context, reachable_client(context), "add", name, type_name, ctx.args
        )

    @app.command("edit", context_settings=dict(EXTRA))
    def edit(
        ctx: typer.Context,
        name: NameArgument,
        type_name: Annotated[
            str | None, typer.Option("--type", help="Change the plugin type.")
        ] = None,
    ) -> None:
        """Change the settings given as --name value, or walk through them."""
        context = cli_context(ctx)
        _write_instance(
            role, context, reachable_client(context), "edit", name, type_name, ctx.args
        )

    @app.command("remove")
    def remove(ctx: typer.Context, name: NameArgument) -> None:
        """Remove the instance from the file."""
        context = cli_context(ctx)
        _remove(role, context, reachable_client(context), name)

    def control(action: str) -> None:
        def command(
            ctx: typer.Context, name: NameArgument, json_output: JsonFlag = False
        ) -> None:
            context = cli_context(ctx)
            _control(role, reachable_client(context), action, name, json_output)

        command.__doc__ = f"{action.capitalize()} the instance; needs the daemon."
        app.command(action)(command)

    for action in ("start", "stop", "restart"):
        control(action)

    app.add_typer(config_view(file), name="config")
    return app


# --- the instances and their state -------------------------------------------


def _echo_instances(
    role: str,
    context: CliContext,
    client: ApiClient | None,
    state: str | None,
    json_output: bool,
) -> None:
    rows = _rows(role, context, client, unused=state == "unconfigured")
    if state is not None:
        rows = [row for row in rows if row["state"] == state]
    if json_output:
        print_json(rows)
        return
    if not rows:
        typer.echo(f"no {_file(role)} to show")
        return
    _echo_table(role, rows)
    for row in rows:
        _echo_detail(role, row)


def _rows(
    role: str,
    context: CliContext,
    client: ApiClient | None,
    *,
    unused: bool = False,
) -> list[dict[str, Any]]:
    """The instances of this role; the installed types without one only on ask.

    An installed type with no instance is not a source, so the table leaves it
    out: it would otherwise stand there saying nothing for the life of a
    machine that never uses that plugin. `--state unconfigured` asks for them,
    and only then is the catalogue fetched at all.
    """
    if client is not None:
        rows = [dict(row) for row in client.get(f"/v1/status/{_file(role)}")]
        if not unused:
            return rows
        catalogue = client.get("/v1/plugins")
        unconfigured = [
            entry["type"]
            for entry in catalogue
            if entry["role"] == role and not entry.get("instances")
        ]
    else:
        typer.echo(
            f"no daemon answers, so only the configuration in {context.config_dir} "
            "is shown and no state is known",
            err=True,
        )
        configured = instances_in(read_texts(context.config_dir)[_file(role)])
        rows = [
            _row(role, name, _type_of(settings), "unknown")
            for name, settings in configured.items()
        ]
        if not unused:
            return rows
        installed = local_registry()
        available = installed.sources if role == "source" else installed.destinations
        used = {_type_of(settings) for settings in configured.values()}
        unconfigured = [name for name in available if name not in used]
    rows.extend(_row(role, "—", name, "unconfigured") for name in sorted(unconfigured))
    return rows


def _type_of(settings: Any) -> str:
    return str(settings.get("type", "")) if isinstance(settings, Mapping) else ""


def _row(role: str, name: str, type_name: str, state: str) -> dict[str, Any]:
    """A row the tables can render for an instance the daemon knows nothing about."""
    row: dict[str, Any] = {
        "name": name,
        "type": type_name,
        "state": state,
        "last_error": None,
    }
    if role == "source":
        return row | {"points": {}, "stopped_by_operator": False, "devices": []}
    return row | {
        "written": 0,
        "last_write": None,
        "stores_metadata": None,
        "stores_model": None,
        "model_version": None,
        "spool": {
            "observations": 0,
            "bytes": 0,
            "oldest_age_seconds": None,
            "dropped": 0,
        },
    }


def _echo_table(role: str, rows: Sequence[Mapping[str, Any]]) -> None:
    table = Table(box=None)
    if role == "source":
        columns = (
            "Source",
            "Type",
            "State",
            "Active",
            "Unsupported",
            "Rejected",
            "Last error",
        )
    else:
        columns = (
            "Destination",
            "Type",
            "State",
            "Written",
            "Spooled",
            "Dropped",
            "Last error",
        )
    for column in columns:
        table.add_column(column)
    for row in rows:
        # A type without an instance has no counts to show, only its state.
        counted = row["state"] != "unconfigured"
        if role == "source":
            counts = row.get("points") or {}
            table.add_row(
                row["name"],
                row["type"],
                row["state"]
                + (" (operator)" if row.get("stopped_by_operator") else ""),
                _count(counts.get("active", 0), counted),
                _count(counts.get("unsupported", 0), counted),
                _count(counts.get("rejected", 0), counted),
                row.get("last_error") or "",
            )
        else:
            spool = row.get("spool") or {}
            table.add_row(
                row["name"],
                row["type"],
                row["state"],
                _count(row.get("written", 0), counted),
                _count(spool.get("observations", 0), counted),
                _count(spool.get("dropped", 0), counted),
                row.get("last_error") or "",
            )
    stdout.print(table)


def _count(value: Any, counted: bool) -> str:
    return str(value) if counted else ""


def _echo_detail(role: str, row: Mapping[str, Any]) -> None:
    if row["state"] in ("unconfigured", "unknown"):
        return
    typer.echo("")
    if role == "source":
        resources = ", ".join(row.get("resources") or [])
        line = f"{row['name']}: restarts {row.get('restart_count', 0)}"
        typer.echo(f"{line}, resources {resources}" if resources else line)
        devices = row.get("devices") or []
        if not devices:
            typer.echo("  no device seen yet")
            return
        table = Table(box=None)
        for column in (
            "Device",
            "Reachable",
            "Last success",
            "Errors",
            "Skipped",
            "Last error",
        ):
            table.add_column(column)
        for device in devices:
            table.add_row(
                str(device.get("device", "")),
                "yes" if device.get("reachable") else "no",
                device.get("last_success") or "",
                str(device.get("error_count", 0)),
                str(device.get("skipped_rounds", 0)),
                device.get("last_error") or "",
            )
        stdout.print(table)
        return
    spool = row.get("spool") or {}
    age = spool.get("oldest_age_seconds")
    typer.echo(
        f"{row['name']}: last write {row.get('last_write') or 'never'}, "
        f"stores metadata {'yes' if row.get('stores_metadata') else 'no'}, "
        f"stores the model {_model_note(row)}"
    )
    typer.echo(
        f"  spool: {spool.get('observations', 0)} observations, "
        f"{spool.get('bytes', 0)} bytes, "
        f"oldest {'—' if age is None else f'{age:.0f}s'}, "
        f"dropped {spool.get('dropped', 0)}"
    )


# --- one instance, and its tools ----------------------------------------------


def _run_tool(
    role: str,
    context: CliContext,
    client: ApiClient | None,
    name: str,
    type_name: str,
    tool: str,
    args: list[str],
    json_output: bool,
    output: Path | None = None,
) -> None:
    parameters = parameters_from_args(args)
    if client is not None:
        result = client.post(
            f"/v1/plugins/{type_name}/instances/{name}/tools/{tool}", json=parameters
        )
    else:
        result = run_tool_offline_cli(context, type_name, name, tool, parameters)
    if output is not None:
        output.write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
        typer.echo(f"wrote {tool} to {output}")
    elif json_output:
        print_json(result)
    else:
        echo_result(result)


def _show_instance(
    role: str,
    client: ApiClient | None,
    name: str,
    settings: Mapping[str, Any],
    declaration: Mapping[str, Any],
    json_output: bool,
) -> None:
    status: dict[str, Any] | None = None
    if client is not None:
        rows = client.get(f"/v1/status/{_file(role)}")
        status = next((dict(row) for row in rows if row["name"] == name), None)
    if json_output:
        print_json(
            {
                "name": name,
                "type": declaration["type"],
                "configuration": dict(settings),
                "status": status,
                "tools": [tool["name"] for tool in declaration.get("tools") or []],
            }
        )
        return
    state = status["state"] if status else "unknown (no daemon answers)"
    typer.echo(f"{name} ({declaration['type']}, {role}): {state}")
    if status is not None:
        _echo_detail(role, status)
    typer.echo("")
    typer.echo("configuration, as written:")
    block = yaml.safe_dump(dict(settings), sort_keys=False, default_flow_style=False)
    for line in block.rstrip("\n").splitlines():
        typer.echo(f"  {line}")
    tools = [tool["name"] for tool in declaration.get("tools") or []]
    if tools:
        typer.echo("")
        typer.echo(f"tools: {', '.join(tools)} — `{_file(role)} {name} <tool> --help`")


# --- add, edit and remove -----------------------------------------------------


def _write_instance(
    role: str,
    context: CliContext,
    client: ApiClient | None,
    action: str,
    name: str,
    type_name: str | None,
    args: list[str],
) -> None:
    file = _file(role)
    if name in RESERVED_INSTANCE_NAMES:
        raise fail(
            f"{name!r} is a subcommand of `bricklogger {file}`; choose another name"
        )
    text = _read_file(context, client, file)
    current = instances_in(text).get(name)
    if action == "add" and current is not None:
        raise fail(f"{name!r} is already configured; change it with edit")
    if action == "edit" and current is None:
        raise fail(f"no {role} named {name!r}; write it with add")
    data: dict[str, Any] = dict(current) if isinstance(current, Mapping) else {}
    chosen = type_name or _type_of(data)
    if not chosen:
        raise fail("add needs --type; `bricklogger plugins` lists the installed types")
    declaration = declaration_of(context, client, chosen)
    if declaration["role"] != role:
        raise fail(
            f"{chosen!r} is a {declaration['role']}; "
            f"use `bricklogger {_file(declaration['role'])}`"
        )
    data["type"] = chosen
    if args:
        settings = _settings(declaration, args)
    else:
        asked = _asked(context, declaration, name, data)
        settings = {key: value for key, value in asked.items() if key != "type"}
    try:
        _, result = write_instance(
            operations_of(context), client, role, action, name, chosen, settings
        )
    except OperationError as exc:
        raise failure(exc) from exc
    typer.echo(f"{file}.yaml: {name} ({chosen})")
    _echo_warnings(result)


def _asked(
    context: CliContext,
    declaration: Mapping[str, Any],
    name: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """The instance's settings asked one by one, as init asks them.

    What is there is offered to keep; a secret typed goes to the env file, so
    this works on the machine whose env file it is, not through --api.
    """
    if context.api_url is not None:
        raise fail("through --api the settings are given as flags")
    schema = declaration.get("config_schema") or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    current = {key: value for key, value in data.items() if key in properties}
    kept = {
        key: value
        for key, value in data.items()
        if key not in properties and key != "type"
    }
    secrets: dict[str, str] = {}
    settings = ask_settings(name, schema, secrets, current or None)
    if secrets:
        write_env(context.config_dir, secrets)
    return {"type": data["type"], **settings, **kept}


def _remove(
    role: str, context: CliContext, client: ApiClient | None, name: str
) -> None:
    file = _file(role)
    try:
        result = remove_instance_from_file(operations_of(context), client, role, name)
    except OperationError as exc:
        raise failure(exc) from exc
    typer.echo(f"{file}.yaml: {name} removed")
    _echo_warnings(result)


def _settings(declaration: Mapping[str, Any], args: list[str]) -> dict[str, Any]:
    """The ``--name value`` pairs as the plugin's configuration schema wants them.

    ``--<key>-env NAME`` writes ``${NAME}``; a setting the plugin marks as a
    secret takes only that form.
    """
    schema = declaration.get("config_schema") or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    settings: dict[str, Any] = {}
    for key, value in parameters_from_args(args).items():
        from_env = key.endswith("_env")
        target = key[: -len("_env")] if from_env else key
        if target not in properties:
            raise fail(
                f"{declaration['type']} has no setting {flag(target)}; "
                f"it takes {_known(properties)}"
            )
        spec = properties[target]
        if from_env:
            if value is True:
                raise fail(f"{flag(key)} needs the name of an environment variable")
            settings[target] = f"${{{value}}}"
            continue
        if is_secret(spec):
            raise fail(
                f"{flag(target)} is a secret and is never written into the file; "
                f"put it in the env file and give {flag(target)}-env NAME"
            )
        settings[target] = coerce_setting(target, value, spec)
    return settings


def _known(properties: Mapping[str, Any]) -> str:
    return ", ".join(flag(key) for key in properties) or "no settings"


def _read_file(context: CliContext, client: ApiClient | None, file: str) -> str:
    if client is not None:
        return client.get_text(f"/v1/config/{file}", accept=YAML)
    return read_texts(context.config_dir)[file] or ""


def _echo_warnings(result: Mapping[str, Any]) -> None:
    for warning in result.get("warnings", []):
        typer.echo(f"warning: {warning['message']}")


# --- start, stop and restart --------------------------------------------------


def _control(
    role: str,
    client: ApiClient | None,
    action: str,
    name: str,
    json_output: bool,
) -> None:
    if client is None:
        raise fail(f"{action} needs the running daemon")
    rows = client.get(f"/v1/status/{_file(role)}")
    found = next((row for row in rows if row["name"] == name), None)
    if found is None:
        raise fail(f"no {role} named {name!r} is configured")
    row = client.post(f"/v1/plugins/{found['type']}/instances/{name}/{action}")
    if json_output:
        print_json(row)
    else:
        typer.echo(f"{name}: {row['state']}")


def _file(role: str) -> str:
    return "sources" if role == "source" else "destinations"


sources_app = _role_app("source", _SourcesGroup)
destinations_app = _role_app("destination", _DestinationsGroup)


def _model_note(row: Mapping[str, Any]) -> str:
    """Whether a destination stores the model, and the version it last wrote."""
    if not row.get("stores_model"):
        return "no"
    version = row.get("model_version")
    return "yes" if version is None else f"yes (version {version})"
