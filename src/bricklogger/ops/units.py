"""The systemd units, written from the templates in the package.

The install script and ``bricklogger update`` write them alike: the script
when it installs, with what it knows of the machine, and ``update`` when a new
version's templates differ from what is installed, with the parameters the
installed units already carry. Run as ``python -m bricklogger.ops.units``, so
the install script and an update both render with the version just installed.
See ``docs/features/cli.md``, "update".
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
"""The units in the order they are restarted: the daemon before the two that
talk to it."""

_PLACEHOLDER = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True)
class UnitParameters:
    """What differs between machines: the command, the service user (system
    mode only), the config directory (user mode only) and the target."""

    command: str
    wanted_by: str
    user: str = ""
    config_dir: str = ""

    def values(self) -> dict[str, str]:
        return {
            "command": self.command,
            "wanted_by": self.wanted_by,
            "user": self.user,
            "config_dir": self.config_dir,
        }


def template(name: str) -> str:
    return (files("bricklogger") / "units" / name).read_text(encoding="utf-8")


def render(name: str, parameters: UnitParameters) -> str:
    """The unit with the parameters filled in; a line whose placeholders are
    all empty is left out, as the user line is for a unit in user mode."""
    values = parameters.values()
    lines: list[str] = []
    for line in template(name).splitlines():
        names = _PLACEHOLDER.findall(line)
        if names and not any(values.get(n) for n in names):
            continue
        lines.append(_PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), line))
    return "\n".join(lines) + "\n"


def parameters_of(text: str) -> UnitParameters | None:
    """The parameters an installed daemon unit carries, or ``None`` when it
    does not look like one this program wrote."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.strip().partition("=")
        if not separator:
            continue
        if key == "User":
            found["user"] = value
        elif key == "Environment" and value.startswith("BRICKLOGGER_CONFIG_DIR="):
            found["config_dir"] = value.split("=", 1)[1]
        elif key == "ExecStart" and value.endswith(" daemon run"):
            found["command"] = value.removesuffix(" daemon run")
        elif key == "WantedBy":
            found["wanted_by"] = value
    if "command" not in found or "wanted_by" not in found:
        return None
    return UnitParameters(**found)


def write(directory: Path, parameters: UnitParameters) -> list[str]:
    """Write the three units; the names of those that changed."""
    directory.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for name in UNITS:
        path = directory / name
        text = render(name, parameters)
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != text:
            path.write_text(text, encoding="utf-8")
            changed.append(name)
    return changed


def refresh(directory: Path) -> list[str]:
    """Write the units again from the templates, with the parameters the
    installed daemon unit carries; nothing when there is none to read."""
    try:
        installed = (directory / UNITS[0]).read_text(encoding="utf-8")
    except OSError:
        return []
    parameters = parameters_of(installed)
    if parameters is None:
        return []
    return write(directory, parameters)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bricklogger.ops.units")
    commands = parser.add_subparsers(dest="action", required=True)
    write_parser = commands.add_parser("write", help="write the units")
    write_parser.add_argument("--dir", required=True, type=Path)
    write_parser.add_argument("--command", required=True)
    write_parser.add_argument("--wanted-by", required=True)
    write_parser.add_argument("--user", default="")
    write_parser.add_argument("--config-dir", default="")
    refresh_parser = commands.add_parser(
        "refresh", help="write them again with the parameters they carry"
    )
    refresh_parser.add_argument("--dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if arguments.action == "write":
        changed = write(
            arguments.dir,
            UnitParameters(
                command=arguments.command,
                wanted_by=arguments.wanted_by,
                user=arguments.user,
                config_dir=arguments.config_dir,
            ),
        )
    else:
        changed = refresh(arguments.dir)
    for name in changed:
        print(name)
    return 0


def changed_from(output: str) -> list[str]:
    """The unit names ``main`` printed."""
    return [line.strip() for line in output.splitlines() if line.strip() in UNITS]


if __name__ == "__main__":
    sys.exit(main())
