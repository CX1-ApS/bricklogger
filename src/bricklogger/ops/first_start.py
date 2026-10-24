"""What ``init --non-interactive`` and a container's first start write, and the
container's plugins named before it existed.

The four files are the examples, except that a working TimescaleDB destination
is written instead of the example when ``BRICKLOGGER_DB_PASSWORD`` is set —
from ``BRICKLOGGER_DB_HOST``, ``_PORT``, ``_NAME`` and ``_USER``, the password
as a reference. A container writes them once, into an empty configuration
volume, with the examples commented out; and it installs the packages in
``BRICKLOGGER_PLUGINS`` once, into a plugin volume that was never asked for
anything. See ``docs/docker.md``, "The first start".
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from bricklogger.config import (
    commented,
    destination_text,
    examples_for,
    resolve_config_dir,
    write_missing,
)
from bricklogger.ops.plugin_volume import MANIFEST, plugin_directory, write_stamp
from bricklogger.plugins.timescaledb.prepare import Target

#: The variable whose presence makes the destination a working one.
PASSWORD_VARIABLE = "BRICKLOGGER_DB_PASSWORD"
#: The packages a new container installs, written as for ``plugins add``.
PLUGINS_VARIABLE = "BRICKLOGGER_PLUGINS"
#: The name of the destination written from the variables.
DESTINATION = "tsdb"

Log = Callable[[str], None]


def texts(config_dir: Path, env: Mapping[str, str]) -> dict[str, str]:
    """The four files: the examples, with a working destination when the
    database's password is in the environment."""
    found = dict(examples_for(config_dir))
    if env.get(PASSWORD_VARIABLE):
        target = Target.from_environment(env)
        found["destinations"] = destination_text(
            DESTINATION, target.dsn, PASSWORD_VARIABLE
        )
    return found


def container_texts(config_dir: Path, env: Mapping[str, str]) -> dict[str, str]:
    """The files of a container's first start: the example source commented
    out, since it names another building, and the destination too unless it
    is a working one."""
    found = texts(config_dir, env)
    examples = examples_for(config_dir)
    for name in ("sources", "destinations"):
        if found[name] == examples[name]:
            found[name] = commented(found[name])
    return found


def packages_of(value: str) -> list[str]:
    """The packages in ``BRICKLOGGER_PLUGINS``: separated by spaces, or one per
    line, where a line written ``name @ url`` is one package."""
    found: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if " @ " in stripped:
            found.append(stripped)
        else:
            found.extend(stripped.split())
    return found


def write_configuration(config_dir: Path, env: Mapping[str, str], log: Log) -> None:
    """Fill an empty configuration directory; leave one with anything in it."""
    if config_dir.is_dir() and any(config_dir.iterdir()):
        return
    working = bool(env.get(PASSWORD_VARIABLE))
    log(
        f"{config_dir} is empty; writing the four files, "
        + (
            "with a working destination from the database's variables"
            if working
            else "with commented examples"
        )
    )
    try:
        write_missing(config_dir, container_texts(config_dir, env))
    except OSError as exc:
        log(f"could not write the configuration ({exc}); the daemon starts with none")


def install_plugins(env: Mapping[str, str], log: Log) -> int:
    """Install ``BRICKLOGGER_PLUGINS`` into a plugin volume that was never asked
    for anything; the number that could not be installed. From then on the
    manifest is what counts, so a changed variable installs nothing."""
    from bricklogger.ops.environment import add_plugins
    from bricklogger.ops.errors import OperationError

    directory = plugin_directory()
    packages = packages_of(env.get(PLUGINS_VARIABLE, ""))
    if directory is None or not packages or (directory / MANIFEST).exists():
        return 0
    failed = 0
    for package in packages:
        try:
            add_plugins([package])
        except OperationError as exc:
            failed += 1
            log(f"the plugin {package} could not be installed: {exc.message}")
        else:
            log(f"installed {package}")
    # What was installed is the manifest now, laid down against this image.
    write_stamp(directory)
    return failed


def main() -> int:
    """The container's entrypoint runs this for the daemon, before it starts."""
    env = dict(os.environ)
    try:
        write_configuration(resolve_config_dir(), env, _say)
        install_plugins(env, _say)
    except OSError as exc:  # pragma: no cover - a volume that cannot be read
        _say(f"the first start could not finish: {exc}")
    return 0


def _say(message: str) -> None:
    print(f"bricklogger: {message}", file=sys.stderr, flush=True)


if __name__ == "__main__":  # pragma: no cover - run as a module by the entrypoint
    raise SystemExit(main())
