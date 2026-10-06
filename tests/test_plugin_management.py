"""Adding and removing plugins: uv against the installation's environment,
the refusals, and the two CLI commands in front of it."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.ops.environment import (
    Added,
    Environment,
    Removed,
    add_plugins,
    distribution_name,
    installed_environment,
    installed_plugins,
    orphaned_by,
    remove_plugin,
)
from bricklogger.ops.errors import OperationError
from bricklogger.sdk.registry import PluginFailure, PluginRegistry
from tests.fakes import (
    FAKE_DESTINATION,
    FAKE_SOURCE,
    in_the_same_tick,
    write_distribution,
)

runner = CliRunner()


class Recorder:
    """A stand-in for running uv: remembers the command, answers as told."""

    def __init__(
        self, returncode: int = 0, stderr: str = "Installed 1 package"
    ) -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(
            list(command), self.returncode, stdout="", stderr=self.stderr
        )


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    uv = tmp_path / "bin" / "uv"
    python = tmp_path / "venv" / "bin" / "python"
    site = tmp_path / "venv" / "lib" / "site-packages"
    for path in (uv, python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    site.mkdir(parents=True)
    return Environment(uv=uv, python=python, site_packages=site)


REGISTRY = PluginRegistry(
    sources={"fake-source": FAKE_SOURCE},
    destinations={"fake-destination": FAKE_DESTINATION},
    versions={"fake-source": "0.4.0", "other": "0.4.0", "fake-destination": "0.1.0"},
    failures={
        "other": PluginFailure("other", "source", "0.4.0", "boom", "bricklogger-fake")
    },
    distributions={
        "fake-source": "bricklogger-fake",
        "other": "bricklogger-fake",
        "fake-destination": "bricklogger",
    },
)


# --- add -----------------------------------------------------------------------


def test_add_runs_uv_against_the_environment(environment: Environment) -> None:
    run = Recorder()
    packages = [
        "bricklogger-httpjson==0.2.1",
        "./dist/bricklogger_other-0.1.0-py3-none-any.whl",
        "git+https://example.com/x.git",
    ]
    added = add_plugins(packages, environment=environment, run=run)
    assert added.packages == tuple(packages)
    assert run.commands == [
        [
            str(environment.uv),
            "pip",
            "install",
            "--python",
            str(environment.python),
            "--upgrade-package",
            "bricklogger-httpjson",
            "--reinstall-package",
            "bricklogger-httpjson",
            "--upgrade-package",
            "bricklogger-other",
            "--reinstall-package",
            "bricklogger-other",
            *packages,
        ]
    ], "only what was asked for moves; nothing else is installed to hold"
    assert added.output == "Installed 1 package"


def test_add_holds_the_other_installed_plugins(environment: Environment) -> None:
    """Two plugins that need incompatible versions of one library cannot both
    work, and uv resolves only what it is given: installed one at a time, the
    second would replace the library under the first without a word. So the
    installed plugins go into the resolution pinned where they are, and uv
    refuses a package that cannot live with them. The one asked for is not
    held against itself; Bricklogger's built-in types and a plain library are
    not plugins."""
    site = environment.site_packages
    sources, destinations = "bricklogger.sources", "bricklogger.destinations"
    write_distribution(
        site, "bricklogger", "0.1.0", entry_points={sources: "bacnet = b:S"}
    )
    write_distribution(
        site, "bricklogger-home-assistant", "0.1.0", entry_points={sources: "ha = h:S"}
    )
    write_distribution(
        site, "Bricklogger_CSV", "2.0", entry_points={destinations: "csv = c:D"}
    )
    write_distribution(site, "websockets", "17.1")
    write_distribution(
        site, "bricklogger-httpjson", "0.2.0", entry_points={sources: "j = j:S"}
    )

    assert installed_plugins(site) == {
        "bricklogger-csv": "2.0",
        "bricklogger-home-assistant": "0.1.0",
        "bricklogger-httpjson": "0.2.0",
    }

    run = Recorder()
    add_plugins(["bricklogger-httpjson==0.2.1"], environment=environment, run=run)
    command = run.commands[0]
    assert command[-3:] == [
        "bricklogger-csv==2.0",
        "bricklogger-home-assistant==0.1.0",
        "bricklogger-httpjson==0.2.1",
    ], "the others pinned, the one asked for free to move"
    assert "websockets==17.1" not in command and "bricklogger==0.1.0" not in command
    assert command.count("--upgrade-package") == 1 and "--upgrade" not in command


def test_a_plugin_installed_in_the_same_tick_as_the_last_look_is_seen(
    environment: Environment,
) -> None:
    """importlib.metadata keeps a directory's listing until the directory's
    mtime changes, so a plugin laid down within the tick of the last look would
    be missed, and the next one resolved without it."""
    site = environment.site_packages
    assert installed_plugins(site) == {}
    with in_the_same_tick(site):
        write_distribution(
            site,
            "bricklogger-fake",
            "1.0",
            entry_points={"bricklogger.sources": "f = f:S"},
        )
    assert installed_plugins(site) == {"bricklogger-fake": "1.0"}


def test_remove_does_not_take_a_library_a_plugin_added_in_the_same_tick_needs(
    environment: Environment,
) -> None:
    """The costlier half of the same cache: a stale listing would leave out the
    plugin just added, and so uninstall the library it shares with the one
    being removed."""
    site = environment.site_packages
    sources = "bricklogger.sources"
    write_distribution(
        site,
        "bricklogger-a",
        "1.0",
        requires=["shared>=1"],
        entry_points={sources: "a = a:S"},
    )
    write_distribution(site, "shared", "1.0")
    assert orphaned_by("bricklogger-a", site) == ["shared"]
    with in_the_same_tick(site):
        write_distribution(
            site,
            "bricklogger-b",
            "1.0",
            requires=["shared>=1"],
            entry_points={sources: "b = b:S"},
        )
    assert orphaned_by("bricklogger-a", site) == []


@pytest.mark.parametrize(
    ("spec", "name"),
    [
        ("bricklogger-httpjson", "bricklogger-httpjson"),
        ("Bricklogger_HttpJson==0.2.1", "bricklogger-httpjson"),
        ("bricklogger-x>=1,<2", "bricklogger-x"),
        ("./dist/bricklogger_httpjson-0.2.1-py3-none-any.whl", "bricklogger-httpjson"),
        ("bricklogger-x @ git+https://example.com/x.git", "bricklogger-x"),
        ("git+https://example.com/x.git", None),
        ("https://example.com/x.tar.gz", None),
        ("./checkout", None),
    ],
)
def test_the_distribution_name_a_spec_names(spec: str, name: str | None) -> None:
    assert distribution_name(spec) == name


def test_add_needs_the_installed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bricklogger.ops.environment.installed_environment", lambda: None
    )
    with pytest.raises(OperationError) as caught:
        add_plugins(["bricklogger-httpjson"])
    assert "not an installation the install script made" in caught.value.message
    assert f"uv pip install --python {sys.executable} bricklogger-httpjson" in (
        caught.value.message
    )


def test_add_says_to_use_sudo_when_the_environment_is_not_writable(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bricklogger.ops.environment.os.access", lambda *_: False)
    with pytest.raises(OperationError, match="sudo"):
        add_plugins(["x"], environment=environment, run=Recorder())


def test_add_reports_what_uv_said_when_it_failed(environment: Environment) -> None:
    run = Recorder(returncode=1, stderr="  x error: No solution found for nope\n")
    with pytest.raises(OperationError) as caught:
        add_plugins(["nope"], environment=environment, run=run)
    assert caught.value.message.startswith("uv pip install failed:")
    assert "No solution found" in caught.value.message


def test_add_wants_at_least_one_package(environment: Environment) -> None:
    with pytest.raises(OperationError, match="at least one"):
        add_plugins([], environment=environment, run=Recorder())


# --- remove --------------------------------------------------------------------


def test_remove_uninstalls_the_distribution_with_every_type_it_provides(
    tmp_path: Path, environment: Environment
) -> None:
    run = Recorder(stderr="Uninstalled 1 package")
    removed = remove_plugin(
        "fake-source",
        config_dir=tmp_path,
        registry=REGISTRY,
        environment=environment,
        run=run,
    )
    assert removed == Removed(
        "bricklogger-fake",
        "0.4.0",
        ("fake-source", "other"),
        (
            str(environment.uv),
            "pip",
            "uninstall",
            "--python",
            str(environment.python),
            "bricklogger-fake",
        ),
        "Uninstalled 1 package",
    )
    assert run.commands == [list(removed.command)]


def test_remove_takes_what_only_the_plugin_needed_with_it(
    tmp_path: Path, environment: Environment
) -> None:
    """The plugin's requirements are followed through the installed metadata,
    extras included and markers evaluated; what they reach that no other
    plugin and not Bricklogger reaches goes in the same uninstall. What the
    plugin never needed is not touched, even when nothing needs it."""
    site = environment.site_packages
    sources = "bricklogger.sources"
    write_distribution(
        site,
        "bricklogger",
        "0.1.0",
        requires=["httpx>=0.27", "pydantic>=2"],
        entry_points={sources: "bacnet-ip = b:S"},
    )
    write_distribution(
        site,
        "bricklogger-fake",
        "0.4.0",
        requires=[
            "bricklogger>=0.1,<0.2",
            "websockets>=13",
            "httpx[http2]>=0.27",  # the extra reaches h2, which Bricklogger's does not
            'yarl>=1; python_version < "3.0"',  # a marker that does not hold
            "shared>=1",
        ],
        entry_points={sources: "fake-source = f:S\nother = f:O"},
    )
    write_distribution(
        site,
        "bricklogger-other",
        "1.0",
        requires=["shared"],
        entry_points={sources: "x = o:S"},
    )
    write_distribution(
        site,
        "httpx",
        "0.27.0",
        requires=["certifi", "httpcore", 'h2>=3; extra == "http2"'],
    )
    write_distribution(site, "websockets", "17.1", requires=["idna"])
    for name in (
        "pydantic",
        "certifi",
        "httpcore",
        "h2",
        "idna",
        "yarl",
        "shared",
        "leftover",
    ):
        write_distribution(site, name, "1.0")

    run = Recorder(stderr="Uninstalled 4 packages")
    removed = remove_plugin(
        "fake-source",
        config_dir=tmp_path,
        registry=REGISTRY,
        environment=environment,
        run=run,
    )

    assert removed.dependencies == ("h2", "idna", "websockets")
    assert run.commands == [
        [
            str(environment.uv),
            "pip",
            "uninstall",
            "--python",
            str(environment.python),
            "bricklogger-fake",
            "h2",
            "idna",
            "websockets",
        ]
    ], "not httpx, which Bricklogger needs; not shared, which the other plugin needs"


def test_remove_refuses_while_instances_are_configured_unless_forced(
    tmp_path: Path, environment: Environment
) -> None:
    (tmp_path / "sources.yaml").write_text(
        "a:\n  type: fake-source\nb:\n  type: other\nc:\n  type: fake-destination\n"
    )
    with pytest.raises(OperationError) as caught:
        remove_plugin(
            "fake-source",
            config_dir=tmp_path,
            registry=REGISTRY,
            environment=environment,
            run=Recorder(),
        )
    message = caught.value.message
    assert "a in sources.yaml" in message and "b in sources.yaml" in message
    assert "c in" not in message, "another distribution's instance is not in the way"
    assert "--force" in message and "sources remove NAME" in message

    run = Recorder()
    remove_plugin(
        "fake-source",
        config_dir=tmp_path,
        force=True,
        registry=REGISTRY,
        environment=environment,
        run=run,
    )
    assert len(run.commands) == 1


def test_remove_refuses_a_built_in_type_and_an_unknown_one(
    tmp_path: Path, environment: Environment
) -> None:
    with pytest.raises(OperationError, match="built into bricklogger"):
        remove_plugin(
            "fake-destination",
            config_dir=tmp_path,
            registry=REGISTRY,
            environment=environment,
            run=Recorder(),
        )
    with pytest.raises(OperationError, match="no installed plugin 'nope'"):
        remove_plugin(
            "nope",
            config_dir=tmp_path,
            registry=REGISTRY,
            environment=environment,
            run=Recorder(),
        )


def test_the_installed_environment_is_uv_beside_the_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    assert installed_environment() is None
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "uv").write_text("")
    found = installed_environment()
    assert found is not None
    assert found.uv == tmp_path / "bin" / "uv"
    assert found.python == Path(sys.executable)


# --- the CLI -------------------------------------------------------------------


def test_plugins_add_and_remove_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_add(packages: Sequence[str], **_: Any) -> Added:
        calls.append(("add", list(packages)))
        return Added(tuple(packages), (), "")

    def fake_remove(
        type_name: str, *, config_dir: Path, force: bool = False
    ) -> Removed:
        calls.append(("remove", type_name, force, config_dir))
        return Removed(
            "bricklogger-httpjson",
            "0.2.1",
            ("httpjson", "httpjson-history"),
            (),
            "",
            ("websockets",),
        )

    monkeypatch.setattr("bricklogger.cli.plugins.add_plugins", fake_add)
    monkeypatch.setattr("bricklogger.cli.plugins.remove_plugin", fake_remove)
    base = ["--config-dir", str(tmp_path), "plugins"]

    result = runner.invoke(app, [*base, "add", "bricklogger-httpjson", "./x.whl"])
    assert result.exit_code == 0, result.output
    assert "installed bricklogger-httpjson, ./x.whl" in result.output
    assert "bacnet-ip" in result.output, "the catalogue as it now is"
    assert "restart it" in result.output and "MCP server" in result.output

    result = runner.invoke(app, [*base, "remove", "httpjson", "--force"])
    assert result.exit_code == 0, result.output
    assert (
        "removed bricklogger-httpjson 0.2.1, which provided httpjson" in result.output
    )
    assert "with it went httpjson-history" in result.output
    assert "with it went websockets, which nothing else needed" in result.output
    assert "restart it" in result.output

    assert calls == [
        ("add", ["bricklogger-httpjson", "./x.whl"]),
        ("remove", "httpjson", True, tmp_path),
    ]


def test_an_operation_error_is_a_plain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(packages: Sequence[str], **_: Any) -> Added:
        raise OperationError("no right to write to /opt; run the command with sudo")

    monkeypatch.setattr("bricklogger.cli.plugins.add_plugins", refuse)
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "plugins", "add", "bricklogger-x"]
    )
    assert result.exit_code != 0
    assert "run the command with sudo" in result.output


def test_plugins_lists_describes_and_shows_its_subcommands(tmp_path: Path) -> None:
    base = ["--config-dir", str(tmp_path), "plugins"]
    listed = runner.invoke(app, base)
    assert listed.exit_code == 0, listed.output
    assert "bacnet-ip" in listed.output and "timescaledb" in listed.output

    described = runner.invoke(app, [*base, "bacnet-ip"])
    assert described.exit_code == 0, described.output
    assert described.output.startswith("bacnet-ip (source")

    unknown = runner.invoke(app, [*base, "nope"])
    assert unknown.exit_code != 0
    assert "unknown plugin type 'nope'" in unknown.output

    shown = runner.invoke(app, [*base, "--help"])
    assert "add" in shown.output and "remove" in shown.output
