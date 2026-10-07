"""The ``systemd --user`` units, written from the templates in the package.

``bricklogger services install`` writes them with the command and the config
directory in use, and ``bricklogger update`` writes the ones that are there
again when a new version's templates differ, with the parameters those units
already carry. ``update`` runs this module as ``python -m
bricklogger.ops.units`` in the new version's interpreter, so the units are
rendered from the version just installed. See ``docs/features/cli.md``,
"services" and "update".
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

UNITS = ("bricklogger.service", "bricklogger-web.service", "bricklogger-mcp.service")
"""The units in the order they are started and restarted: the daemon before
the two that talk to it."""

DAEMON, WEB, MCP = UNITS

#: What each unit runs after the command.
ARGUMENTS = {DAEMON: "daemon run", WEB: "serve", MCP: "mcp serve --http"}

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True)
class UnitParameters:
    """What differs between machines: the command's absolute path and the
    config directory the services read."""

    command: str
    config_dir: str

    def values(self) -> dict[str, str]:
        return {"command": self.command, "config_dir": self.config_dir}


def template(name: str) -> str:
    return (files("bricklogger") / "units" / name).read_text(encoding="utf-8")


def render(name: str, parameters: UnitParameters) -> str:
    """The unit with the parameters filled in."""
    values = parameters.values()
    text = _PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), template(name))
    return text if text.endswith("\n") else text + "\n"


def parameters_of(name: str, text: str) -> UnitParameters | None:
    """The parameters an installed unit carries, or ``None`` when it does not
    look like one this program wrote."""
    command = config_dir = None
    suffix = " " + ARGUMENTS[name]
    for line in text.splitlines():
        key, separator, value = line.strip().partition("=")
        if not separator:
            continue
        if key == "Environment" and value.startswith("BRICKLOGGER_CONFIG_DIR="):
            config_dir = value.split("=", 1)[1]
        elif key == "ExecStart" and value.endswith(suffix):
            command = value.removesuffix(suffix)
    if command is None or config_dir is None:
        return None
    return UnitParameters(command, config_dir)


def write(
    directory: Path, parameters: UnitParameters, names: Sequence[str] = UNITS
) -> list[str]:
    """Write the named units; the names of those that changed."""
    directory.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name in UNITS:
        if name not in names:
            continue
        path = directory / name
        text = render(name, parameters)
        if _read(path) != text:
            path.write_text(text, encoding="utf-8")
            changed.append(name)
    return changed


def written(directory: Path) -> list[str]:
    """The units that are written in the directory."""
    return [name for name in UNITS if (directory / name).is_file()]


def refresh(directory: Path) -> list[str]:
    """Write the units that are there again from the templates, each with the
    parameters it carries; one this program did not write is left alone."""
    changed: list[str] = []
    for name in written(directory):
        current = _read(directory / name)
        parameters = parameters_of(name, current or "")
        if parameters is not None:
            changed += write(directory, parameters, [name])
    return changed


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bricklogger.ops.units")
    commands = parser.add_subparsers(dest="action", required=True)
    refresh_parser = commands.add_parser(
        "refresh", help="write the units that are there again from the templates"
    )
    refresh_parser.add_argument("--dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    for name in refresh(arguments.dir):
        print(name)
    return 0


def changed_from(output: str) -> list[str]:
    """The unit names ``main`` printed."""
    return [line.strip() for line in output.splitlines() if line.strip() in UNITS]


if __name__ == "__main__":
    sys.exit(main())
