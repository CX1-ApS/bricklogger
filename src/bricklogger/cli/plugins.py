"""``bricklogger plugins``: the catalogue of what this installation can do,
and the two commands that change it.

The list and the declarations come from the installed plugins themselves when
no daemon answers, so they answer before anything is configured. ``add`` and
``remove`` install and uninstall a plugin with the environment's own uv, and
say that the daemon must be restarted, since it reads its plugins at start.
The helpers the two role groups share — the parameters of a tool, the instance
it runs on, running it without a daemon and rendering its result — live here
too, beside the declaration they come from. See ``docs/features/cli.md``,
"plugins", and ``docs/features/plugins.md``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.table import Table
from typer.core import TyperGroup
from typer.main import get_command as typer_command

from bricklogger.cli.client import ApiClient, reachable_client
from bricklogger.cli.context import CliContext, cli_context, operations_of
from bricklogger.cli.output import fail, print_json, stdout
from bricklogger.ops.catalogue import describe, list_plugins
from bricklogger.ops.config import is_secret as is_secret
from bricklogger.ops.environment import add_plugins, remove_plugin
from bricklogger.ops.errors import OperationError
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.sdk.registry import PluginError, PluginRegistry

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit the API's JSON instead of a table.")
]


class _PluginsGroup(TyperGroup):
    """A group whose unknown subcommand is a plugin type to describe."""

    # Typer carries its own copy of click, so the types are left open here.
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        if command is not None or cmd_name.startswith("-"):
            return command
        return _describe_command(cmd_name)


plugins_app = typer.Typer(
    cls=_PluginsGroup,
    add_completion=False,
    help="The catalogue: what this installation can do. `plugins` lists the "
    "installed plugins, `plugins TYPE` describes one, and `add` and `remove` "
    "change what is installed. The instances of a plugin are configured and "
    "inspected with `sources` and `destinations`.",
)


@plugins_app.callback(invoke_without_command=True)
def plugins_root(ctx: typer.Context, json_output: JsonFlag = False) -> None:
    """The installed plugins: type, role, version and description, and
    `failed:` with the error for one that could not be loaded."""
    if ctx.invoked_subcommand is not None:
        return
    context = cli_context(ctx)
    _list(context, reachable_client(context), json_output)


def _describe_command(type_name: str) -> Any:
    """``plugins TYPE``, made for the type that was typed."""
    sub = typer.Typer(add_completion=False)

    @sub.command(type_name)
    def command(ctx: typer.Context, json_output: JsonFlag = False) -> None:
        """The plugin's declaration: reference types, vocabulary, collection
        methods, configuration schema and tools."""
        context = cli_context(ctx)
        data = declaration_of(context, reachable_client(context), type_name)
        if json_output:
            print_json(data)
            return
        _echo_declaration(data)

    return typer_command(sub)


@plugins_app.command("add")
def add_command(
    ctx: typer.Context,
    packages: Annotated[
        list[str],
        typer.Argument(
            help="Packages as uv takes them: a name, name==version, a wheel on "
            "disk or a git URL."
        ),
    ],
    json_output: JsonFlag = False,
) -> None:
    """Install one or more plugins into this installation's environment.

    Runs the environment's own uv, prints the catalogue as it now is, and
    says that the daemon must be restarted to see the change.
    """
    context = cli_context(ctx)
    try:
        added = add_plugins(packages)
    except OperationError as exc:
        raise fail(exc.message) from exc
    typer.echo(f"installed {', '.join(added.packages)}")
    typer.echo("")
    _list(context, None, json_output)
    _restart_note()


@plugins_app.command("remove")
def remove_command(
    ctx: typer.Context,
    type_name: Annotated[str, typer.Argument(help="The plugin type, e.g. httpjson.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Uninstall even while instances of the type are configured.",
        ),
    ] = False,
) -> None:
    """Uninstall the distribution that provides a plugin type.

    Refuses while an instance of the type is configured, unless --force; from
    the daemon's next start such an instance would be failed.
    """
    context = cli_context(ctx)
    try:
        removed = remove_plugin(type_name, config_dir=context.config_dir, force=force)
    except OperationError as exc:
        raise fail(exc.message) from exc
    version = f" {removed.version}" if removed.version else ""
    typer.echo(f"removed {removed.distribution}{version}, which provided {type_name}")
    others = [name for name in removed.types if name != type_name]
    if others:
        typer.echo(f"with it went {', '.join(others)}, from the same distribution")
    if removed.dependencies:
        typer.echo(
            f"with it went {', '.join(removed.dependencies)}, which nothing else needed"
        )
    _restart_note()


def _restart_note() -> None:
    typer.echo("")
    typer.echo("The daemon reads its plugins when it starts; restart it to see this:")
    if plugin_directory() is not None:
        typer.echo("  docker compose restart daemon      (in a container)")
    else:
        typer.echo("  systemctl --user restart bricklogger   (a service)")
        typer.echo("  bricklogger daemon restart             (started by hand)")
    typer.echo(
        "The MCP server over HTTP reads them the same way; the web interface "
        "needs nothing."
    )


def local_registry() -> PluginRegistry:
    """The installed plugins, found through their entry points."""
    try:
        return PluginRegistry.from_entry_points()
    except PluginError as exc:
        raise fail(str(exc)) from exc


def declaration_of(
    context: CliContext, client: ApiClient | None, type_name: str
) -> dict[str, Any]:
    """One plugin's declaration, from the daemon or from what is installed."""
    try:
        return describe(operations_of(context), client, type_name)
    except OperationError as exc:
        raise fail(exc.message) from exc


def parameters_from_args(extra: list[str]) -> dict[str, Any]:
    """``--name value`` pairs (or ``--name=value``, or a bare ``--flag``) as a dict."""
    parameters: dict[str, Any] = {}
    index = 0
    while index < len(extra):
        item = extra[index]
        if not item.startswith("--"):
            raise fail(
                f"unexpected argument {item!r}; parameters are given as --name value"
            )
        key, separator, value = item[2:].partition("=")
        if separator:
            parameters[key.replace("-", "_")] = value
            index += 1
            continue
        following = extra[index + 1] if index + 1 < len(extra) else None
        if following is None or following.startswith("--"):
            parameters[key.replace("-", "_")] = True
            index += 1
        else:
            parameters[key.replace("-", "_")] = following
            index += 2
    return parameters


def instance_name(client: ApiClient, type_name: str, instance: str | None) -> str:
    """The instance a tool runs on: the one given, or the type's only one."""
    if instance is not None:
        return instance
    data = client.get(f"/v1/plugins/{type_name}")
    instances = data.get("instances") or []
    if len(instances) == 1:
        return str(instances[0])
    if not instances:
        raise fail(f"no instance of {type_name!r} is configured")
    raise fail(
        f"{type_name!r} has several instances ({', '.join(instances)}); give --instance"
    )


def run_tool_offline_cli(
    context: CliContext,
    type_name: str,
    instance: str | None,
    tool: str,
    parameters: dict[str, Any],
) -> Any:
    """Bind the instance's configuration and run the tool without a daemon."""
    from bricklogger.daemon.plugins import run_tool_offline

    registry = local_registry()
    try:
        return run_tool_offline(
            context.config_dir, registry, type_name, instance, tool, parameters
        )
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise fail("invalid tool parameters:\n" + "\n".join(lines)) from exc
    except (KeyError, LookupError, RuntimeError, OSError) as exc:
        raise fail(str(exc)) from exc


def echo_result(result: Any) -> None:
    """A tool's result as a table, as key-value lines, or as JSON."""
    if isinstance(result, list) and result and all(isinstance(r, dict) for r in result):
        columns: list[str] = []
        for row in result:
            for key in row:
                if key not in columns:
                    columns.append(key)
        table = Table(box=None)
        for column in columns:
            table.add_column(column)
        for row in result:
            table.add_row(*(_cell(row.get(column)) for column in columns))
        stdout.print(table)
        typer.echo(f"{len(result)} row{'' if len(result) == 1 else 's'}")
    elif isinstance(result, dict):
        for key, value in result.items():
            typer.echo(f"{key}: {_cell(value)}")
    else:
        typer.echo(json.dumps(result, indent=2, default=str))


def _list(context: CliContext, client: ApiClient | None, json_output: bool) -> None:
    try:
        rows = list_plugins(operations_of(context), client)
    except OperationError as exc:
        raise fail(exc.message) from exc
    if json_output:
        print_json(rows)
        return
    table = Table(box=None)
    for column in ("Type", "Role", "Version", "Instances", "Description"):
        table.add_column(column)
    for row in rows:
        instances = row.get("instances")
        table.add_row(
            row["type"],
            row["role"],
            str(row.get("version") or ""),
            ", ".join(instances)
            if instances
            else ("" if instances is None else "none"),
            f"failed: {row['error']}"
            if row.get("error")
            else (row.get("description") or ""),
        )
    stdout.print(table)


def _echo_declaration(data: dict[str, Any]) -> None:
    typer.echo(f"{data['type']} ({data['role']}, version {data.get('version') or '?'})")
    if data.get("error"):
        typer.echo(f"failed: {data['error']}")
        if data.get("instances") is not None:
            typer.echo(f"instances: {', '.join(data['instances']) or 'none'}")
        return
    typer.echo(data.get("description") or "")
    if data.get("instances") is not None:
        typer.echo(f"instances: {', '.join(data['instances']) or 'none'}")
    if data["role"] == "source":
        typer.echo(f"reference types: {', '.join(data.get('reference_types', []))}")
        vocabulary = data.get("vocabulary")
        if vocabulary:
            typer.echo(
                f"vocabulary: {vocabulary['prefix']}: <{vocabulary['namespace']}>"
            )
        typer.echo("collection methods:")
        if data.get("supports_poll", True):
            typer.echo("  poll: defined centrally; interval in the rule")
        for method in data.get("methods", []):
            typer.echo(f"  {method['name']}: {method['description']}")
            echo_schema(method.get("parameters"), indent="    ")
        typer.echo("tools:")
        if not data.get("tools"):
            typer.echo("  none")
        for tool in data.get("tools", []):
            note = " (a document; -o FILE)" if tool.get("document") else ""
            typer.echo(f"  {tool['name']}{note}: {tool['description']}")
            echo_schema(tool.get("parameters"), indent="    ")
    else:
        typer.echo(f"stores metadata: {'yes' if data.get('stores_metadata') else 'no'}")
    typer.echo("configuration:")
    echo_schema(data.get("config_schema"), indent="  ")


def echo_schema(schema: dict[str, Any] | None, indent: str) -> None:
    if not schema or not schema.get("properties"):
        if schema is not None:
            typer.echo(f"{indent}(no parameters)")
        return
    required = set(schema.get("required", []))
    for name, spec in schema["properties"].items():
        kind = spec.get("type") or _any_of(spec) or "value"
        parts = [f"--{name.replace('_', '-')} <{kind}>"]
        if name in required:
            parts.append("required")
        elif "default" in spec:
            parts.append(f"default {json.dumps(spec['default'])}")
        if spec.get("description"):
            parts.append(spec["description"])
        typer.echo(f"{indent}{' — '.join(parts)}")


def _any_of(spec: dict[str, Any]) -> str | None:
    options = spec.get("anyOf")
    if not options:
        return None
    kinds = [
        option.get("type")
        for option in options
        if option.get("type") not in (None, "null")
    ]
    return " | ".join(kinds) if kinds else None


def flag(key: str) -> str:
    """The flag a setting is given as: ``device_instance`` is ``--device-instance``."""
    return f"--{key.replace('_', '-')}"


def coerce_setting(key: str, value: Any, spec: Mapping[str, Any]) -> Any:
    """A value typed by the setting's schema: integer, number, boolean, list or text."""
    kind = spec.get("type") or _first_type(spec)
    if value is True:
        return True if kind in (None, "boolean") else value
    text = str(value)
    if kind == "integer":
        return _number(key, text, int)
    if kind == "number":
        return _number(key, text, float)
    if kind == "boolean":
        if text.lower() in ("true", "yes", "on", "1"):
            return True
        if text.lower() in ("false", "no", "off", "0"):
            return False
        raise fail(f"{flag(key)}: {text!r} is not true or false")
    if kind == "array":
        return [item.strip() for item in text.split(",") if item.strip()]
    return text


def _number(key: str, text: str, kind: type) -> Any:
    try:
        return kind(text)
    except ValueError as exc:
        raise fail(f"{flag(key)}: {text!r} is not a number") from exc


def _first_type(spec: Mapping[str, Any]) -> str | None:
    for option in spec.get("anyOf") or []:
        if option.get("type") not in (None, "null"):
            return str(option["type"])
    return None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)
