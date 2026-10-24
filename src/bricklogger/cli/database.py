"""The database step of ``init``: a TimescaleDB destination asked about where
its database is, PostgreSQL with TimescaleDB installed on this machine when
wanted, the role, the database and the extension prepared, and the role's
password generated into the ``env`` file.

In a container the database is the compose file's own: the step installs
nothing and asks for no admin, and writes the destination from the
``BRICKLOGGER_DB_*`` variables. See ``docs/features/destinations.md``,
"Preparing the database".
"""

from __future__ import annotations

import os
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from bricklogger.config import read_env_file
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.plugins.timescaledb import prepare
from bricklogger.plugins.timescaledb.prepare import PrepareError, Target

#: The type the step is for; every other destination is asked from its schema.
TYPE = "timescaledb"
#: The variable a container's destination names its password with.
CONTAINER_PASSWORD = "BRICKLOGGER_DB_PASSWORD"


def container_settings(env: Mapping[str, str]) -> dict[str, Any] | None:
    """In a container with the database's variables, the destination written
    from them; ``None`` elsewhere, or without the password."""
    if plugin_directory() is None or not env.get(CONTAINER_PASSWORD):
        return None
    target = Target.from_environment(env)
    typer.echo(
        f"  the compose file's database: {target.dsn}, with the password from "
        f"{CONTAINER_PASSWORD}"
    )
    return {"type": TYPE, "dsn": target.dsn, "password": f"${{{CONTAINER_PASSWORD}}}"}


def database_step(
    name: str,
    config_dir: Path,
    secrets: dict[str, str],
    current: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The settings of a TimescaleDB destination, its database prepared.

    Nothing that fails here stops the setup: the settings are returned
    whatever the database answered, and ``init --db`` tries again.
    """
    target = _ask_target(_current_target(current))
    variable = prepare.password_variable(name)
    if target.local and _ask_install(target):
        _install()

    kept = read_env_file(config_dir).get(variable) or os.environ.get(variable)
    password: str | None = None
    if kept and prepare.logs_in(target, kept):
        typer.echo(f"  the password in the env file as {variable} logs in; it is kept")
    else:
        password = prepare.generate_password()

    try:
        if target.local:
            typer.echo(
                "  preparing the role, the database and the extension as postgres "
                "(sudo may ask for your password)"
            )
            prepare.prepare_locally(target, password)
        else:
            admin = _prompt(
                "  admin login on the database's host", prepare.DEFAULT_ADMIN
            )
            admin_password = str(
                typer.prompt(
                    f"  {admin}'s password (used for this step, kept nowhere)",
                    hide_input=True,
                )
            )
            prepare.prepare_remotely(target, password, admin, admin_password)
    except PrepareError as exc:
        typer.echo(f"  the database could not be prepared: {exc}")
        typer.echo(
            "  the configuration is written anyway; `bricklogger init --db` tries again"
        )
        if password is not None and not kept:
            secrets[variable] = password  # the role does not know it yet either
        return _settings(target, variable)
    if password is not None:
        secrets[variable] = password
        typer.echo(f"  the role's password is generated, in the env file as {variable}")
    typer.echo(f"  ready: {target.dsn}")
    return _settings(target, variable)


def _settings(target: Target, variable: str) -> dict[str, Any]:
    return {"type": TYPE, "dsn": target.dsn, "password": f"${{{variable}}}"}


def _current_target(current: Mapping[str, Any] | None) -> Target:
    """The target an existing destination's DSN names, to offer its parts."""
    dsn = str((current or {}).get("dsn") or "")
    if not dsn:
        return Target()
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(dsn)
        port = parts.port
    except ValueError:
        return Target()
    return Target(
        host=parts.hostname or prepare.DEFAULT_HOST,
        port=port or prepare.DEFAULT_PORT,
        database=parts.path.lstrip("/") or prepare.DEFAULT_DATABASE,
        role=parts.username or prepare.DEFAULT_ROLE,
    )


def _ask_target(offered: Target) -> Target:
    typer.echo("  Where the database is; Enter keeps what is offered.")
    host = _prompt("  host", offered.host)
    port = _ask_port(offered.port)
    database = _ask_name("database", offered.database)
    role = _ask_name("role", offered.role)
    return Target(host, port, database, role)


def _ask_port(offered: int) -> int:
    while True:
        answer = _prompt("  port", str(offered))
        if answer.isdigit() and 0 < int(answer) < 65536:
            return int(answer)
        typer.echo("  a port is a number from 1 to 65535")


def _ask_name(kind: str, offered: str) -> str:
    while True:
        answer = _prompt(f"  {kind}", offered)
        problem = prepare.check_name(kind, answer)
        if problem is None:
            return answer
        typer.echo(f"  {problem}")


def _ask_install(target: Target) -> bool:
    there = prepare.answers(target)
    return bool(
        typer.confirm(
            "  Install PostgreSQL with TimescaleDB on this machine? sudo asks "
            "for your password while it runs",
            default=not there,
        )
    )


def _install() -> None:
    """Install PostgreSQL with TimescaleDB through sudo, or say how."""
    distribution = prepare.this_distribution()
    if distribution is None:
        typer.echo(
            "  this machine is not one init installs on (Debian or Ubuntu); an "
            f"administrator installs PostgreSQL {prepare.POSTGRES_MAJOR} with "
            "TimescaleDB, as https://docs.timescale.com/self-hosted/latest/install/ "
            "describes, and the setup goes on"
        )
        return
    installed = distribution.installed()
    commands = distribution.commands(installed)
    major = distribution.major(installed)
    if installed.anything:
        found = ", ".join(str(m) for m in installed.majors) or "?"
        extension = " with TimescaleDB" if installed.timescaledb else ""
        typer.echo(
            f"  PostgreSQL {found}{extension} is installed already: its packages "
            "will be installed or upgraded, and timescaledb-tune will tune "
            "PostgreSQL's configuration again, after a copy of postgresql.conf "
            "beside it. No database and no data is touched."
        )
        if not typer.confirm("  Go on?", default=False):
            return
    if shutil.which("sudo") is None:
        _remaining("sudo is not on this machine", commands)
        return
    typer.echo(f"  installing on {distribution.name}:")
    for command in commands:
        typer.echo(f"    {shlex.join(command)}")
    remaining = prepare.install(commands)
    if remaining:
        _remaining("a command failed", remaining)
        return
    typer.echo(
        f"  installed PostgreSQL {major} with TimescaleDB; timescaledb-tune set its "
        "memory and workers from this machine, and `sudo timescaledb-tune` tunes "
        "it again"
    )


def _remaining(why: str, commands: list[list[str]]) -> None:
    typer.echo(f"  {why}; what remains, for an administrator to run:")
    for command in commands:
        typer.echo(f"    {shlex.join(command)}")
    typer.echo("  the setup goes on")


def _prompt(label: str, default: str) -> str:
    return str(typer.prompt(label, default=default)).strip() or default
