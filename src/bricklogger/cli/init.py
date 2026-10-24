"""``bricklogger init``: a machine set up in one go.

In a terminal it is a guided setup — the plugins from the catalogue and
beyond, which sources and destinations with the settings of each from the
plugin's own schema, the database of a TimescaleDB destination, the rest of
the files, and last the offer to set up the services. It can run again: a file
that exists is asked about, replace or skip, and a replaced one is copied
first. ``--db`` runs the database step alone, and ``--non-interactive`` writes
the files that are missing and asks nothing. It never replaces a file without
asking, so it cannot destroy a configuration by itself. See
``docs/features/cli.md``, "init".
"""

from __future__ import annotations

import importlib
import os
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from bricklogger.catalogue import catalogue_rows
from bricklogger.cli.client import ApiClient
from bricklogger.cli.config import registry, report_validation
from bricklogger.cli.context import CliContext, cli_context
from bricklogger.cli.database import TYPE as DATABASE_TYPE
from bricklogger.cli.database import container_settings, database_step
from bricklogger.cli.guided import ask_settings, write_env
from bricklogger.cli.output import fail
from bricklogger.cli.services import install as install_services
from bricklogger.config import (
    CONFIG_FILES,
    RESERVED_INSTANCE_NAMES,
    config_file,
    default_data_dir,
    examples_for,
    instances_in,
    read_texts,
    set_instance,
    untouched,
    validate_configuration,
    write_missing,
    write_text,
)
from bricklogger.ops import services
from bricklogger.ops.environment import add_plugins
from bricklogger.ops.errors import OperationError
from bricklogger.ops.first_start import texts as first_texts
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.sdk.declaration import DestinationDeclaration, SourceDeclaration

NonInteractiveFlag = Annotated[
    bool,
    typer.Option(
        "--non-interactive",
        help="Ask nothing; write the files that are missing, with commented "
        "examples, and leave the others.",
    ),
]
DatabaseFlag = Annotated[
    bool,
    typer.Option(
        "--db",
        help="Run the database step alone: the TimescaleDB destination's "
        "database, installed and prepared.",
    ),
]
PluginOption = Annotated[
    list[str] | None,
    typer.Option(
        "--plugin",
        help="With --non-interactive: a package to install first, as for "
        "`plugins add`; repeat it for more.",
    ),
]


def init_command(
    ctx: typer.Context,
    db: DatabaseFlag = False,
    non_interactive: NonInteractiveFlag = False,
    plugin: PluginOption = None,
) -> None:
    """Set a machine up: plugins, configuration, database and services.

    Asks before it replaces a file that exists, and keeps a copy of it.
    """
    context = cli_context(ctx)
    packages = list(plugin or [])
    if packages and not non_interactive:
        raise fail("--plugin goes with --non-interactive; the guided setup asks")
    if db and non_interactive:
        raise fail("--db asks its questions; it cannot be --non-interactive")
    if context.api_url is not None:
        if not non_interactive or packages:
            raise fail(
                "through --api only `init --non-interactive` is available, "
                "without --plugin"
            )
        result = ApiClient.from_context(context).post("/v1/config/init")
        for path in result["written"]:
            typer.echo(f"wrote {path}")
        return
    if db:
        _database_only(context)
    elif non_interactive:
        _non_interactive(context, packages)
    else:
        _guided(context)


# --- asking nothing -------------------------------------------------------------


def _non_interactive(context: CliContext, packages: list[str]) -> None:
    failed = [package for package in packages if not _install(package)]
    config_dir = context.config_dir
    try:
        written, left = write_missing(config_dir, first_texts(config_dir, os.environ))
    except OSError as exc:
        raise fail(f"could not write to {config_dir}: {exc}") from exc
    for path in written:
        typer.echo(f"wrote {path}")
    for path in left:
        typer.echo(f"left {path}, which exists")
    if failed:
        raise fail(f"not installed: {', '.join(failed)}")


# --- the guided setup -----------------------------------------------------------


def _guided(context: CliContext) -> None:
    config_dir = context.config_dir
    typer.echo(f"configuration: {config_dir}")
    typer.echo(f"data:          {default_data_dir(config_dir)}")
    typer.echo("")

    added = _plugins_step()
    importlib.invalidate_caches()  # the plugins just installed are found now
    installed = registry()

    present = read_texts(config_dir)
    examples = examples_for(config_dir)
    secrets: dict[str, str] = {}
    wanted: dict[str, str] = {}
    if _replace(config_dir, "sources", present):
        wanted["sources"] = _collect(
            "source", "sources", installed.sources, secrets, config_dir
        )
    if _replace(config_dir, "destinations", present):
        wanted["destinations"] = _collect(
            "destination", "destinations", installed.destinations, secrets, config_dir
        )
    for name in ("daemon", "rules"):
        if present.get(name) == examples[name]:
            continue  # what would be written is there already
        if _replace(config_dir, name, present):
            wanted[name] = examples[name]

    written = _write(config_dir, wanted, secrets)
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
    running = _daemon_answers(context)
    started = False if running else _offer_services(context)
    if running and (added or written):
        _offer_restart(context)
    typer.echo("")
    if running or started:
        typer.echo("Next: upload the model.")
    else:
        typer.echo("Next: start the daemon and upload the model.")
        typer.echo("  bricklogger daemon start")
    typer.echo("  bricklogger model upload <model.ttl>")
    typer.echo("  bricklogger status")


def _plugins_step() -> list[str]:
    """Show the catalogue, install what is chosen and what else is named; the
    packages that were installed."""
    rows = catalogue_rows(registry())
    typer.echo("Plugins — the catalogue of approved plugins:")
    if not rows:
        typer.echo("  (empty)")
    for index, row in enumerate(rows, start=1):
        types = ", ".join(f"{t['type']} ({t['role']})" for t in row["types"])
        mark = f"  [installed {row['installed']}]" if row.get("installed") else ""
        typer.echo(
            f"  {index}) {row['package']:<24} {types}: {row['description']}{mark}"
        )
    chosen: list[str] = []
    if rows:
        chosen = _choose_packages(
            f"Install which? [1-{len(rows)} or names, separated by spaces; "
            "Enter for none]",
            [str(row["package"]) for row in rows],
        )
    others = str(
        typer.prompt(
            "Other packages, as for `plugins add`? They are custom plugins "
            "[Enter for none]",
            default="",
            show_default=False,
        )
    ).split()
    added = [package for package in [*chosen, *others] if _install(package)]
    typer.echo("")
    return added


def _choose_packages(question: str, packages: list[str]) -> list[str]:
    while True:
        answer = str(typer.prompt(question, default="", show_default=False)).split()
        chosen: list[str] = []
        for word in answer:
            if word.isdigit() and 1 <= int(word) <= len(packages):
                chosen.append(packages[int(word) - 1])
            elif word in packages:
                chosen.append(word)
            else:
                break
        else:
            return list(dict.fromkeys(chosen))
        typer.echo(
            f"  choose numbers from 1 to {len(packages)} or package names, "
            "or press Enter"
        )


def _install(package: str) -> bool:
    """Install one package as `plugins add` does; report a failure, go on."""
    try:
        add_plugins([package])
    except OperationError as exc:
        typer.echo(f"  could not install {package}: {exc.message}")
        return False
    typer.echo(f"  installed {package}")
    return True


def _replace(config_dir: Path, name: str, present: Mapping[str, str | None]) -> bool:
    """Whether to write the file: always when it is missing, else as answered,
    replace by default only for an example nobody has touched."""
    text = present.get(name)
    if text is None:
        return True
    default = "replace" if untouched(config_dir, name, text) else "skip"
    while True:
        answer = (
            str(
                typer.prompt(
                    f"{name}.yaml exists — replace or skip?",
                    default=default,
                )
            )
            .strip()
            .lower()
        )
        if answer in ("replace", "r"):
            return True
        if answer in ("skip", "s"):
            return False
        typer.echo("  replace or skip")


def _write(
    config_dir: Path, wanted: Mapping[str, str], secrets: Mapping[str, str]
) -> list[Path]:
    """Write the files wanted, each one replaced copied first; and the secrets."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    written: list[Path] = []
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        for name in CONFIG_FILES:
            if name not in wanted:
                continue
            path = config_file(config_dir, name)
            if path.exists():
                backup = path.with_name(f"{path.name}.{stamp}.bak")
                shutil.copy2(path, backup)
                typer.echo(f"kept a copy of {path.name} as {backup.name}")
            written.append(write_text(config_dir, name, wanted[name]))
        if secrets:
            written.append(write_env(config_dir, secrets))
    except OSError as exc:
        raise fail(f"could not write to {config_dir}: {exc}") from exc
    return written


def _daemon_answers(context: CliContext) -> bool:
    client = ApiClient.from_context(context)
    try:
        return bool(client.reachable())
    finally:
        client.close()


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


def _offer_restart(context: CliContext) -> None:
    """The daemon reads its plugins when it starts: offer the restart, no by
    default, since it interrupts collection; in a container, name it."""
    if plugin_directory() is not None:
        typer.echo("The daemon reads its plugins and files when it starts:")
        typer.echo("  docker compose restart daemon")
        return
    as_service = services.daemon_active()
    command = (
        "systemctl --user restart bricklogger"
        if as_service
        else "bricklogger daemon restart"
    )
    if not typer.confirm(
        "The daemon is running. Restart it now to load the new plugins and "
        "configuration? It interrupts collection",
        default=False,
    ):
        typer.echo(f"The same later: {command}")
        return
    try:
        if as_service:
            services.restart_daemon()
            typer.echo("restarted")
        else:
            from bricklogger.cli.daemon_commands import restart_started_by_hand

            restart_started_by_hand(context)
    except OperationError as exc:
        raise fail(exc.message) from exc


def _collect(
    role: str,
    file: str,
    declarations: Mapping[str, SourceDeclaration | DestinationDeclaration],
    secrets: dict[str, str],
    config_dir: Path,
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
        settings = _settings(name, chosen, declaration, secrets, config_dir)
        text = set_instance(text, name, settings)
        names.append(name)
        typer.echo("")
    return text


def _settings(
    name: str,
    type_name: str,
    declaration: SourceDeclaration | DestinationDeclaration,
    secrets: dict[str, str],
    config_dir: Path,
) -> dict[str, Any]:
    """An instance's settings: a TimescaleDB destination through the database
    step, every other from its schema."""
    if declaration.role == "destination" and type_name == DATABASE_TYPE:
        found = container_settings(os.environ)
        if found is not None:
            return found
        if plugin_directory() is None:
            return database_step(name, config_dir, secrets)
    schema = declaration.config_schema.model_json_schema()
    return {"type": type_name, **ask_settings(name, schema, secrets)}


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


def _ask_name(role: str, type_name: str, taken: list[str], suggested: str = "") -> str:
    suggested = suggested or type_name.replace("-", "_") + ("" if taken else "_main")
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


# --- the database step alone ----------------------------------------------------


def _database_only(context: CliContext) -> None:
    """``init --db``: the TimescaleDB destination's database, asked about like
    any other step; a destination is added when none is configured."""
    config_dir = context.config_dir
    text = read_texts(config_dir)["destinations"]
    instances = instances_in(text)
    configured = sorted(
        name
        for name, data in instances.items()
        if isinstance(data, dict) and data.get("type") == DATABASE_TYPE
    )
    if len(configured) > 1:
        name = _choose("Which TimescaleDB destination?", configured)
        if name is None:
            return
    elif configured:
        name = configured[0]
    else:
        typer.echo("No TimescaleDB destination is configured; adding one.")
        name = _ask_name("destination", DATABASE_TYPE, list(instances), "tsdb")
    current: dict[str, Any] = dict(instances.get(name) or {})
    if current:
        answer = str(
            typer.prompt(f"{name} is configured — replace or skip?", default="skip")
        )
        if answer.strip().lower() not in ("replace", "r"):
            return
    secrets: dict[str, str] = {}
    settings = container_settings(os.environ)
    if settings is None:
        if plugin_directory() is not None:
            raise fail(
                "in a container the database is the compose file's; set "
                "BRICKLOGGER_DB_PASSWORD for the daemon, as the Docker page shows"
            )
        settings = database_step(name, config_dir, secrets, current)
    kept = {k: v for k, v in current.items() if k not in ("type", "dsn", "password")}
    written = _write(
        config_dir,
        {"destinations": set_instance(text, name, {**settings, **kept})},
        secrets,
    )
    for path in written:
        typer.echo(f"wrote {path}")
    result = validate_configuration(config_dir, registry())
    report_validation(
        result.as_dict(), False, ok_line=f"ok: {config_dir}", where=str(config_dir)
    )
    if _daemon_answers(context):
        _offer_restart(context)
