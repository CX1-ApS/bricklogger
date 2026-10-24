"""Preparing a TimescaleDB database for the destination: where it is, the
role's generated password, installing PostgreSQL with TimescaleDB on this
machine, and creating the role, the database and the extension.

This is the one place outside the plugin contract where Bricklogger knows a
type by name; ``init`` and ``init --db`` drive it. Nothing here deletes
anything: a role, a database or an extension that exists is used as it is.
See ``docs/features/destinations.md``, "Preparing the database".
"""

from __future__ import annotations

import re
import secrets
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

#: The PostgreSQL major version installed on a machine that has none.
POSTGRES_MAJOR = 18

#: apt run through sudo, with debconf asking nothing: a question it put to the
#: terminal would stop the setup halfway.
APT = ("sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get")

#: The names offered for a new database.
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "bricklogger"
DEFAULT_ROLE = "bricklogger"
DEFAULT_ADMIN = "postgres"

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: A database or role name `init` writes: lowercase, so it needs no quoting
#: anywhere it is written, and safe to put into SQL as an identifier.
_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class PrepareError(Exception):
    """A step of the preparation failed; the message says which and why."""


@dataclass(frozen=True)
class Target:
    """Where the destination's database is, and the role it logs in as."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    database: str = DEFAULT_DATABASE
    role: str = DEFAULT_ROLE

    @property
    def dsn(self) -> str:
        """The connection string the destination is written with, no password."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"postgres://{self.role}@{host}:{self.port}/{self.database}"

    @property
    def local(self) -> bool:
        """Whether the database is on this machine, where it can be installed
        and prepared as ``postgres`` through ``sudo``."""
        return self.host in LOCAL_HOSTS

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> Target:
        """The target the ``BRICKLOGGER_DB_*`` variables describe."""
        port = env.get("BRICKLOGGER_DB_PORT", "").strip()
        return cls(
            host=env.get("BRICKLOGGER_DB_HOST", "").strip() or DEFAULT_HOST,
            port=int(port) if port.isdigit() else DEFAULT_PORT,
            database=env.get("BRICKLOGGER_DB_NAME", "").strip() or DEFAULT_DATABASE,
            role=env.get("BRICKLOGGER_DB_USER", "").strip() or DEFAULT_ROLE,
        )


def check_name(kind: str, name: str) -> str | None:
    """Why a database or role name cannot be used, or ``None`` when it can."""
    if _NAME.match(name):
        return None
    return (
        f"a {kind} name is lowercase letters, digits and underscores, beginning "
        "with a letter or an underscore"
    )


def generate_password() -> str:
    """A password for the role, which nobody types: 32 URL-safe characters."""
    return secrets.token_urlsafe(24)


def password_variable(instance: str) -> str:
    """The env file's variable for an instance's password: ``TSDB_PASSWORD``."""
    return f"{instance}_password".upper().replace("-", "_")


# --- what answers ---------------------------------------------------------------


def logs_in(target: Target, password: str, timeout: float = 5.0) -> bool:
    """Whether the role logs in to the database with this password."""
    import psycopg

    try:
        with psycopg.connect(
            target.dsn, password=password, connect_timeout=max(1, int(timeout))
        ):
            return True
    except psycopg.Error:
        return False


def answers(target: Target, timeout: float = 3.0) -> bool:
    """Whether a PostgreSQL server answers at the target's host and port."""
    import psycopg

    try:
        with psycopg.connect(
            host=target.host,
            port=target.port,
            dbname="postgres",
            user=target.role,
            connect_timeout=max(1, int(timeout)),
        ):
            return True
    except psycopg.OperationalError as exc:
        # A server that refuses the login has answered; one that is not there
        # cannot say anything about passwords or roles.
        text = str(exc).lower()
        return "password" in text or "authentication" in text or "role" in text
    except psycopg.Error:
        return True


# --- installing on this machine -------------------------------------------------


@dataclass(frozen=True)
class Installed:
    """What is on this machine already: the PostgreSQL major versions, and
    whether TimescaleDB is installed for the one that would be used."""

    majors: tuple[int, ...]
    timescaledb: bool

    @property
    def anything(self) -> bool:
        return bool(self.majors) or self.timescaledb


@dataclass(frozen=True)
class Distribution:
    """A Linux distribution `init` knows how to install on."""

    id: str
    name: str
    codename: str

    def major(self, installed: Installed) -> int:
        """The newest PostgreSQL installed, or the one this release names."""
        return max(installed.majors) if installed.majors else POSTGRES_MAJOR

    def commands(self, installed: Installed) -> list[list[str]]:
        """Every command of the install, each run with ``sudo``."""
        raise NotImplementedError

    def installed(self, run: Runner | None = None) -> Installed:
        """What is installed already."""
        raise NotImplementedError


@dataclass(frozen=True)
class Apt(Distribution):
    """Debian and Ubuntu: PostgreSQL from its own repository, TimescaleDB from
    Timescale's, as Timescale's installation guide does it."""

    def commands(self, installed: Installed) -> list[list[str]]:
        major = self.major(installed)
        # timescaledb-tune keeps its own copy in /tmp, gone at the next boot.
        conf = f"/etc/postgresql/{major}/main/postgresql.conf"
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        repository = (
            f"deb https://packagecloud.io/timescale/timescaledb/{self.id}/ "
            f"{self.codename} main"
        )
        steps: list[list[str]] = [
            [*APT, "install", "-y", "gnupg", "postgresql-common",
             "apt-transport-https", "lsb-release", "wget"],
        ]  # fmt: skip
        if not installed.majors:
            steps.append(
                ["sudo", "/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh",
                 "-y"]
            )  # fmt: skip
        steps += [
            ["sudo", "sh", "-c",
             f"echo '{repository}' > /etc/apt/sources.list.d/timescaledb.list"],
            ["sudo", "sh", "-c",
             "wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey"
             " | gpg --dearmor --yes -o /etc/apt/trusted.gpg.d/timescaledb.gpg"],
            [*APT, "update"],
            [*APT, "install", "-y",
             f"timescaledb-2-postgresql-{major}", f"postgresql-client-{major}"],
            ["sudo", "sh", "-c",
             f"[ ! -f {conf} ] || cp -a {conf} {conf}.{stamp}.bak"],
            ["sudo", "timescaledb-tune", "--quiet", "--yes",
             f"--pg-config=/usr/lib/postgresql/{major}/bin/pg_config"],
            ["sudo", "systemctl", "enable", "postgresql"],
            ["sudo", "systemctl", "restart", "postgresql"],
        ]  # fmt: skip
        return steps

    def installed(self, run: Runner | None = None) -> Installed:
        root = Path("/usr/lib/postgresql")
        majors = tuple(
            sorted(
                int(entry.name)
                for entry in (root.iterdir() if root.is_dir() else [])
                if entry.name.isdigit() and (entry / "bin" / "postgres").exists()
            )
        )
        major = max(majors) if majors else POSTGRES_MAJOR
        package = f"timescaledb-2-postgresql-{major}"
        answer = _run(run, ["dpkg-query", "-W", "-f=${Status}", package])
        return Installed(majors, answer is not None and "installed" in answer)


#: The distributions `init` installs on, by ``ID`` in ``/etc/os-release``.
#: Another family is added here with its own commands.
DISTRIBUTIONS: dict[str, type[Distribution]] = {"debian": Apt, "ubuntu": Apt}


def this_distribution(
    os_release: Path = Path("/etc/os-release"),
) -> Distribution | None:
    """The distribution of this machine, when `init` knows how to install on it."""
    try:
        text = os_release.read_text(encoding="utf-8")
    except OSError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip().strip('"').strip("'")
    found = fields.get("ID", "").lower()
    family = DISTRIBUTIONS.get(found)
    if family is None:
        return None
    codename = fields.get("VERSION_CODENAME") or fields.get("UBUNTU_CODENAME") or ""
    if not codename:
        return None
    return family(found, fields.get("PRETTY_NAME") or found, codename)


def install(
    commands: Sequence[Sequence[str]],
    run: Callable[[Sequence[str]], int] | None = None,
) -> list[list[str]]:
    """Run the commands in the terminal, ``sudo`` asking for its password
    itself; the commands that remain when one fails, the failed one first."""
    runner = run if run is not None else _interactive
    for index, command in enumerate(commands):
        if runner(command) != 0:
            return [list(c) for c in commands[index:]]
    return []


def _interactive(command: Sequence[str]) -> int:
    # The output goes to the terminal; the input does not come from it. sudo
    # asks for its password on the terminal itself, and apt and dpkg would
    # otherwise read what was typed ahead for the questions that follow.
    try:
        return subprocess.run(
            list(command), stdin=subprocess.DEVNULL, check=False
        ).returncode
    except OSError:
        return 127


def _run(run: Runner | None, command: Sequence[str]) -> str | None:
    try:
        done = (
            run(command)
            if run is not None
            else subprocess.run(
                list(command), capture_output=True, text=True, check=False
            )
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


# --- preparing the role, the database and the extension -------------------------


def psql_script(target: Target, password: str | None) -> str:
    """The preparation as a psql script, for ``sudo -u postgres psql`` on this
    machine. Every step is conditional or idempotent, so it can run again; a
    password given is set on the role, ``None`` leaves it as it is.

    The database is made from ``template0``: a server whose ``template1``
    carries the extension, as Timescale's images do, would otherwise hand the
    new database an extension owned by ``postgres``, which the role cannot
    update."""
    role, database = _identifier(target.role), _identifier(target.database)
    lines = [
        "\\set ON_ERROR_STOP on",
        f"SELECT 'CREATE ROLE {role} LOGIN' WHERE NOT EXISTS "
        f"(SELECT FROM pg_roles WHERE rolname = {_literal(target.role)})\\gexec",
    ]
    if password is not None:
        lines.append(f"ALTER ROLE {role} LOGIN PASSWORD {_literal(password)};")
    lines += [
        f"SELECT 'CREATE DATABASE {database} OWNER {role} TEMPLATE template0' "
        "WHERE NOT EXISTS "
        f"(SELECT FROM pg_database WHERE datname = {_literal(target.database)})"
        "\\gexec",
        f"\\connect {database}",
        f"SET ROLE {role};",
        "CREATE EXTENSION IF NOT EXISTS timescaledb;",
    ]
    return "\n".join(lines) + "\n"


def prepare_locally(
    target: Target,
    password: str | None,
    run: Callable[[Sequence[str], str], subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Prepare the database on this machine as ``postgres``, through ``sudo``."""
    command = ["sudo", "-u", "postgres", "psql", "-X", "-q", "-p", str(target.port)]
    script = psql_script(target, password)
    try:
        done = (
            run(command, script)
            if run is not None
            else subprocess.run(
                command, input=script, capture_output=True, text=True, check=False
            )
        )
    except OSError as exc:
        raise PrepareError(f"could not run {' '.join(command)}: {exc}") from exc
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip()
        raise PrepareError(f"psql as postgres failed: {said or done.returncode}")


def prepare_remotely(
    target: Target,
    password: str | None,
    admin: str,
    admin_password: str,
    connect: Callable[..., Any] | None = None,
) -> None:
    """Prepare the database on another host with an admin login, which is used
    for this and kept nowhere."""
    import psycopg

    opener = connect if connect is not None else psycopg.connect
    role, database = _identifier(target.role), _identifier(target.database)
    login: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
        "user": admin,
        "password": admin_password,
        "autocommit": True,
        "connect_timeout": 10,
    }
    try:
        with opener(dbname="postgres", **login) as connection:
            if not _exists(connection, "pg_roles", "rolname", target.role):
                connection.execute(f"CREATE ROLE {role} LOGIN")
            if password is not None:
                connection.execute(
                    f"ALTER ROLE {role} LOGIN PASSWORD {_literal(password)}"
                )
            if not _exists(connection, "pg_database", "datname", target.database):
                connection.execute(
                    f"CREATE DATABASE {database} OWNER {role} TEMPLATE template0"
                )
        with opener(dbname=target.database, **login) as connection:
            connection.execute(f"SET ROLE {role}")
            connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except psycopg.Error as exc:
        raise PrepareError(f"as {admin} on {target.host}: {exc}".strip()) from exc


def _exists(connection: Any, table: str, column: str, name: str) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE {column} = %s", (name,)
    ).fetchone()
    return row is not None


def _identifier(name: str) -> str:
    problem = check_name("database or role", name)
    if problem is not None:
        raise PrepareError(f"{name!r}: {problem}")
    return f'"{name}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
