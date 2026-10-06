"""The install script. It is shell, so what a test can hold it to is its syntax,
its usage and the paths the documentation promises."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command

from bricklogger.cli import app

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"

#: What the script prints to the user.
SAY = re.compile(r'say "([^"]*)"')
#: ``bricklogger`` followed by its subcommands, and nothing that merely
#: contains the word: not ``/opt/bricklogger``, not ``bricklogger.service``,
#: and not ``systemctl enable --now bricklogger``, which names a unit.
INVOCATION = re.compile(r"(?<![\w/-])bricklogger((?: [a-z][a-z0-9-]*)+)")


def shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX shell on this machine")
    return found


def test_the_script_is_valid_posix_shell() -> None:
    subprocess.run([shell(), "-n", str(SCRIPT)], check=True)


def test_usage_names_every_option() -> None:
    result = subprocess.run(
        [shell(), str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    for option in ("--version", "--wheel", "--uninstall"):
        assert option in result.stdout
    assert "--with" not in result.stdout, "a plugin is added with `plugins add`"


def test_an_unknown_option_is_refused() -> None:
    result = subprocess.run(
        [shell(), str(SCRIPT), "--nonsense"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "unknown option" in result.stderr


def test_a_wheel_that_does_not_exist_is_refused() -> None:
    result = subprocess.run(
        [shell(), str(SCRIPT), "--wheel", "/nonexistent/x.whl"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "no such wheel" in result.stderr


def cli_has(path: tuple[str, ...]) -> bool:
    """Whether the command tree has this path as commands, not as arguments."""
    node: Any = get_command(app)
    for word in path:
        commands = getattr(node, "commands", None)
        if not commands or word not in commands:
            return False
        node = commands[word]
    return True


def commands_the_script_names() -> list[tuple[str, ...]]:
    """Every ``bricklogger`` command the script tells the user to run."""
    text = SCRIPT.read_text(encoding="utf-8")
    found: list[tuple[str, ...]] = []
    for printed in SAY.finditer(text):
        for invocation in INVOCATION.finditer(printed.group(1)):
            found.append(tuple(invocation.group(1).split()))
    return found


def test_every_command_the_script_names_exists_in_the_cli() -> None:
    """The command tree was reshaped once and left the installer behind.

    It told the user to run `bricklogger config init` and
    `bricklogger config validate`, which had become `init` and `validate` at
    the top level, and nothing noticed until someone followed the message.
    """
    named = commands_the_script_names()
    assert len(named) >= 3, f"no commands were found in the script: {named}"
    assert ("init",) in named, "the script should say how to configure"

    for command in sorted(set(named)):
        assert cli_has(command), (
            f"install.sh tells the user to run "
            f"`bricklogger {' '.join(command)}`, which the CLI does not have"
        )


def test_the_check_rejects_a_command_that_is_gone() -> None:
    """The two the reshape left behind, and one that was never a command.

    `bricklogger status sources` answers `--help` with 0, because `status`
    takes positional arguments, so asking the tree is the only honest check.
    """
    assert not cli_has(("config", "init"))
    assert not cli_has(("config", "validate"))
    assert not cli_has(("status", "sources"))
    assert cli_has(("sources", "status"))


def test_the_script_installs_where_the_documentation_says() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for path in (
        "/opt/bricklogger",
        "/etc/bricklogger",
        "/var/lib/bricklogger",
        "/usr/local/bin",
        ".config/bricklogger",
        ".local/share/bricklogger",
        ".local/bin",
    ):
        assert path in text, path


def test_the_script_writes_the_three_units() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for unit in (
        "bricklogger.service",
        "bricklogger-web.service",
        "bricklogger-mcp.service",
    ):
        assert unit in text, unit
    assert "mcp serve --http" in text, "the MCP unit serves HTTP"
