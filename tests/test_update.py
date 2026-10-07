"""``bricklogger update``: the units from templates, the look on PyPI, the
upgrade with its validation and return, the restart, and the daemon's look."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.config import validate_configuration
from bricklogger.daemon.updates import UpdateWatch
from bricklogger.ops import units
from bricklogger.ops import updates as updates_module
from bricklogger.ops.environment import Environment, ToolRecord
from bricklogger.ops.errors import OperationError
from bricklogger.ops.updates import (
    Installed,
    Machine,
    UpdateRefused,
    available_line,
    status,
    update,
)
from bricklogger.sdk.registry import PluginRegistry
from bricklogger.web.app import newer_releases_context

# --- the units -------------------------------------------------------------------

#: A daemon unit as an earlier version wrote it for a login, waiting on a
#: network target the login's systemd does not have.
EARLIER_DAEMON_UNIT = """[Unit]
Description=Bricklogger daemon
After=network-online.target
Wants=network-online.target

[Service]
Environment=BRICKLOGGER_CONFIG_DIR=/home/m/.config/bricklogger
ExecStart=/home/m/.local/bin/bricklogger daemon run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

MCP_UNIT = """[Unit]
Description=Bricklogger MCP server over HTTP
After=bricklogger.service

[Service]
Environment=BRICKLOGGER_CONFIG_DIR=/home/m/.config/bricklogger
ExecStart=/home/m/.local/bin/bricklogger mcp serve --http
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

PARAMETERS = units.UnitParameters(
    command="/home/m/.local/bin/bricklogger",
    config_dir="/home/m/.config/bricklogger",
)


def test_the_templates_render_the_command_and_the_config_directory() -> None:
    assert units.render("bricklogger-mcp.service", PARAMETERS) == MCP_UNIT
    assert units.parameters_of("bricklogger-mcp.service", MCP_UNIT) == PARAMETERS


def test_a_refresh_rewrites_the_units_that_are_there_and_no_others(
    tmp_path: Path,
) -> None:
    (tmp_path / "bricklogger.service").write_text(EARLIER_DAEMON_UNIT)
    (tmp_path / "bricklogger-web.service").write_text("[Unit]\nDescription=other\n")
    assert units.refresh(tmp_path) == ["bricklogger.service"]
    daemon = (tmp_path / "bricklogger.service").read_text()
    assert "network-online" not in daemon
    assert "ExecStart=/home/m/.local/bin/bricklogger daemon run" in daemon
    assert (tmp_path / "bricklogger-web.service").read_text().endswith("other\n"), (
        "a unit this program did not write is left alone"
    )
    assert not (tmp_path / "bricklogger-mcp.service").exists(), "not set up, not added"
    assert units.refresh(tmp_path) == []


def test_a_refresh_without_an_installed_unit_writes_nothing(tmp_path: Path) -> None:
    assert units.refresh(tmp_path) == []
    assert list(tmp_path.iterdir()) == []


def test_the_module_prints_the_units_it_wrote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bricklogger.service").write_text(EARLIER_DAEMON_UNIT)
    assert units.main(["refresh", "--dir", str(tmp_path)]) == 0
    assert units.changed_from(capsys.readouterr().out) == ["bricklogger.service"]


# --- the look on PyPI -----------------------------------------------------------

CORE_RELEASES = {
    "0.2.0": [{"yanked": False}],
    "0.2.1": [{"yanked": False}],
    "0.2.2": [{"yanked": True}],
    "0.3.0": [{"yanked": False}],
    "0.3.1rc1": [{"yanked": False}],
}
PLUGIN_RELEASES = {
    "0.1.0": [{"yanked": False}],
    "0.1.1": [{"yanked": False}],
    "0.2.0": [{"yanked": False}],
}
PLUGIN_REQUIRES = {
    "0.1.1": ["bricklogger>=0.2,<0.3"],
    "0.2.0": ["bricklogger>=0.3,<0.4", "httpx>=0.27"],
}


def pypi(url: str) -> Any:
    base = updates_module.PYPI
    if url == f"{base}/bricklogger/json":
        return {"releases": CORE_RELEASES}
    if url == f"{base}/bricklogger-ibos/json":
        return {"releases": PLUGIN_RELEASES}
    for version, requires in PLUGIN_REQUIRES.items():
        if url == f"{base}/bricklogger-ibos/{version}/json":
            return {"info": {"requires_dist": requires}}
    raise OperationError(f"404 {url}")


@pytest.fixture
def installed_set(monkeypatch: pytest.MonkeyPatch) -> None:
    core = Installed("bricklogger", "0.2.0")
    ibos = Installed("bricklogger-ibos", "0.1.0", ("ibos",), SpecifierSet(">=0.2,<0.3"))
    monkeypatch.setattr(updates_module, "installed", lambda: (core, [ibos]))


@pytest.mark.usefixtures("installed_set")
def test_status_finds_the_newest_and_the_newest_that_fits() -> None:
    core, ibos = status(pypi)
    assert (core.installed, core.newest, core.fits) == ("0.2.0", "0.3.0", "0.2.1")
    assert core.held_by == ("bricklogger-ibos",) and core.behind
    assert (ibos.installed, ibos.newest, ibos.fits) == ("0.1.0", "0.2.0", "0.1.1")
    assert ibos.held_by == ("bricklogger",) and ibos.types == ("ibos",)
    assert available_line({"available": [core.as_dict(), ibos.as_dict()]}) == (
        "newer releases: bricklogger 0.3.0, bricklogger-ibos 0.2.0 "
        "(bricklogger update status)"
    )


@pytest.mark.usefixtures("installed_set")
def test_status_says_what_it_could_not_look_up() -> None:
    def nothing(url: str) -> Any:
        raise OperationError("offline")

    rows = status(nothing)
    assert [row.error for row in rows] == ["offline", "offline"]
    assert updates_module.newer_releases(nothing) == []


@pytest.mark.usefixtures("installed_set")
def test_the_status_command_prints_the_rows_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(updates_module, "fetch_json", pypi)
    result = CliRunner().invoke(app, ["update", "status", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["fits"] for row in rows] == ["0.2.1", "0.1.1"]


def test_update_alone_shows_its_help_and_installs_nothing() -> None:
    result = CliRunner().invoke(app, ["update"], env={"COLUMNS": "200"})
    assert result.exit_code in (0, 2)
    for word in ("status", "core", "all"):
        assert word in result.output


# --- the upgrade ----------------------------------------------------------------


def dist(site: Path, name: str, version: str, types: Sequence[str] = ()) -> None:
    """A distribution's metadata in the directory, replacing another version."""
    module = name.replace("-", "_")
    for old in site.glob(f"{module}-*.dist-info"):
        for item in old.iterdir():
            item.unlink()
        old.rmdir()
    info = site / f"{module}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    )
    if types:
        lines = "".join(f"{t} = {module}:SOURCE\n" for t in types)
        (info / "entry_points.txt").write_text(f"[bricklogger.sources]\n{lines}")


class Machinery:
    """uv, the new version's validate, the units module and systemctl, as a
    runner records them and answers."""

    def __init__(self, site: Path, valid: bool = True) -> None:
        self.site = site
        self.valid = valid
        self.commands: list[list[str]] = []
        self.after: dict[str, str] = {}
        self.active = {"bricklogger.service", "bricklogger-web.service"}
        self.index_has_the_old_versions = True

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.commands.append(command)
        out = ""
        if command[1:3] == ["tool", "install"]:
            specs = [command[3]] + [
                command[i + 1] for i, word in enumerate(command) if word == "--with"
            ]
            if "--reinstall-package" in command:  # the noted versions put back
                if not self.index_has_the_old_versions:
                    return subprocess.CompletedProcess(
                        command, 1, "", "no version of bricklogger==0.2.0"
                    )
                versions = dict(s.split("==") for s in specs if "==" in s)
            elif "--upgrade" in command or "--upgrade-package" in command:
                versions = self.after
            else:  # the record settled: nothing moves
                versions = {}
            for name, version in versions.items():
                dist(
                    self.site, name, version, () if name == "bricklogger" else ("ibos",)
                )
        elif command[1:3] == ["-m", "bricklogger"]:
            out = json.dumps(
                {
                    "valid": self.valid,
                    "errors": []
                    if self.valid
                    else [{"file": "sources", "subject": "x", "message": "bad"}],
                    "warnings": [],
                }
            )
        elif command[1:3] == ["-m", "bricklogger.ops.units"]:
            out = "bricklogger-web.service\n"
        elif command[0] == "systemctl" and command[-2] == "is-active":
            out = "active\n" if command[-1] in self.active else "inactive\n"
        return subprocess.CompletedProcess(command, 0, out, "")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    dist(site, "bricklogger", "0.2.0")
    dist(site, "bricklogger-ibos", "0.1.0", ("ibos",))
    return site


def environment(site: Path) -> Environment:
    return Environment(
        uv=Path("/opt/bin/uv"),
        python=Path("/opt/venv/python"),
        site_packages=site,
        record=ToolRecord(("bricklogger", "bricklogger-ibos"), Path("/opt/tools")),
    )


def test_update_core_holds_the_plugins_validates_and_restarts_the_active_units(
    site: Path, tmp_path: Path
) -> None:
    machinery = Machinery(site)
    machinery.after = {"bricklogger": "0.2.1"}
    answered: list[str] = []

    def answering(url: str) -> bool:
        answered.append(url)
        return True

    result = update(
        "core",
        config_dir=tmp_path,
        environment=environment(site),
        run=machinery,
        machine=Machine(("systemctl",), tmp_path / "units"),
        answering=answering,
    )
    assert result.moved == {"bricklogger": ("0.2.0", "0.2.1")}
    install = machinery.commands[0]
    uv = str(Path("/opt/bin/uv"))
    assert install[:4] == [uv, "tool", "install", "bricklogger"]
    assert install[install.index("--upgrade-package") + 1] == "bricklogger"
    assert "bricklogger-ibos==0.1.0" in install, "the plugin is held"
    assert machinery.commands[1][:3] == ["/opt/venv/python", "-m", "bricklogger"]
    assert machinery.commands[2] == [
        uv,
        "tool",
        "install",
        "bricklogger",
        "--with",
        "bricklogger-ibos",
    ], "the record left without the version the plugin was held at"
    assert result.units == ["bricklogger-web.service"]
    assert ["systemctl", "daemon-reload"] in machinery.commands
    assert result.restarted == ["bricklogger.service", "bricklogger-web.service"]
    restarts = [c for c in machinery.commands if c[1:2] == ["restart"]]
    assert [c[-1] for c in restarts] == [
        "bricklogger.service",
        "bricklogger-web.service",
    ]
    assert answered == [
        "http://127.0.0.1:8420",
        "http://127.0.0.1:8421",
        "http://127.0.0.1:8422",
    ]
    assert result.not_answering == []


def test_update_of_a_type_moves_its_plugin_and_pins_bricklogger(
    site: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        updates_module, "_distribution_of", lambda t: "bricklogger-ibos"
    )
    machinery = Machinery(site)
    machinery.after = {"bricklogger-ibos": "0.1.1"}
    result = update(
        "ibos",
        config_dir=tmp_path,
        environment=environment(site),
        run=machinery,
        machine=Machine(None, tmp_path / "units"),
        restart=False,
    )
    assert result.moved == {"bricklogger-ibos": ("0.1.0", "0.1.1")}
    install = machinery.commands[0]
    assert "bricklogger==0.2.0" in install
    assert install[install.index("--upgrade-package") + 1] == "bricklogger-ibos"
    assert result.restarted == [] and result.units == []


def test_a_configuration_the_new_version_rejects_puts_the_old_one_back(
    site: Path, tmp_path: Path
) -> None:
    machinery = Machinery(site, valid=False)
    machinery.after = {"bricklogger": "0.3.0", "bricklogger-ibos": "0.2.0"}
    with pytest.raises(UpdateRefused) as refused:
        update(
            "all",
            config_dir=tmp_path,
            environment=environment(site),
            run=machinery,
            machine=Machine(("systemctl",), tmp_path / "units"),
        )
    assert refused.value.errors == [
        {"file": "sources", "subject": "x", "message": "bad"}
    ]
    put_back = machinery.commands[2]
    assert "bricklogger==0.2.0" in put_back and "bricklogger-ibos==0.1.0" in put_back
    assert machinery.commands[3][-3:] == ["bricklogger", "--with", "bricklogger-ibos"]
    assert updates_module._versions(site) == {
        "bricklogger": "0.2.0",
        "bricklogger-ibos": "0.1.0",
    }
    assert not any(c[0] == "systemctl" for c in machinery.commands), "nothing restarted"


def test_a_return_that_fails_says_so_and_restarts_nothing(
    site: Path, tmp_path: Path
) -> None:
    """A version installed from a wheel on disk is on no index, so it cannot
    be put back; the refusal must not claim otherwise."""
    machinery = Machinery(site, valid=False)
    machinery.after = {"bricklogger": "0.3.0"}
    machinery.index_has_the_old_versions = False
    with pytest.raises(UpdateRefused) as refused:
        update(
            "core",
            config_dir=tmp_path,
            environment=environment(site),
            run=machinery,
            machine=Machine(("systemctl",), tmp_path / "units"),
        )
    assert refused.value.restored is False
    assert "could not be put back" in refused.value.message
    assert "Nothing was restarted" in refused.value.message
    assert refused.value.errors, "the reason the new version refused stays"
    assert not any(c[0] == "systemctl" for c in machinery.commands)


def test_nothing_new_is_nothing_more(site: Path, tmp_path: Path) -> None:
    machinery = Machinery(site)
    result = update(
        "all",
        config_dir=tmp_path,
        environment=environment(site),
        run=machinery,
        machine=Machine(("systemctl",), tmp_path / "units"),
    )
    assert result.moved == {} and len(machinery.commands) == 1


def test_a_unit_that_does_not_answer_is_reported_not_rolled_back(
    site: Path, tmp_path: Path
) -> None:
    machinery = Machinery(site)
    machinery.after = {"bricklogger": "0.2.1"}
    machinery.active = {"bricklogger.service"}
    result = update(
        "core",
        config_dir=tmp_path,
        environment=environment(site),
        run=machinery,
        machine=Machine(("systemctl",), tmp_path / "units"),
        answering=lambda url: False,
        wait=0.0,
    )
    assert result.not_answering == ["bricklogger.service"]
    assert updates_module._versions(site)["bricklogger"] == "0.2.1"


def test_in_a_container_core_comes_with_the_image(tmp_path: Path) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    found = Environment(
        uv=Path("/opt/bin/uv"),
        python=Path("/opt/venv/python"),
        site_packages=tmp_path,
        target=volume,
    )
    with pytest.raises(OperationError, match="docker compose pull"):
        update("core", config_dir=tmp_path, environment=found, run=Machinery(tmp_path))


def test_a_word_of_update_is_not_a_type() -> None:
    with pytest.raises(OperationError, match="not a plugin type"):
        updates_module._distribution_of("status")


# --- the daemon's look, status and the web --------------------------------------


def test_the_watch_keeps_its_last_answer_and_forgets_it_when_switched_off() -> None:
    found = [{"name": "bricklogger", "installed": "0.2.0", "newest": "0.2.1"}]
    watch = UpdateWatch(True, lambda: found, first_look=timedelta(hours=1))
    assert watch.summary() is None
    watch.look()
    summary = watch.summary()
    assert summary is not None and summary["available"] == found

    def failing() -> list[dict[str, Any]]:
        raise OperationError("offline")

    watch._lookup = failing
    watch.look()
    assert watch.summary() == summary, "a failed look keeps the previous answer"
    watch.reconfigure(False)
    assert watch.summary() is None


def test_daemon_yaml_takes_the_switch(tmp_path: Path) -> None:
    (tmp_path / "daemon.yaml").write_text(
        f"data_dir: {tmp_path / 'data'}\nupdates:\n  check: false\n"
    )
    result = validate_configuration(tmp_path, PluginRegistry(), {})
    assert result.valid and result.configuration is not None
    assert result.configuration.daemon.updates.check is False


def test_the_plugins_screen_offers_an_update_per_type() -> None:
    context = newer_releases_context(
        {
            "checked_at": "2026-10-06T07:00:00+00:00",
            "available": [
                {
                    "name": "bricklogger",
                    "installed": "0.2.0",
                    "newest": "0.3.0",
                    "fits": "0.2.1",
                    "types": [],
                },
                {
                    "name": "bricklogger-ibos",
                    "installed": "0.1.0",
                    "newest": "0.2.0",
                    "fits": "0.1.1",
                    "types": ["ibos"],
                },
                {
                    "name": "bricklogger-ha",
                    "installed": "0.1.0",
                    "newest": "0.2.0",
                    "fits": "0.1.0",
                    "types": ["homeassistant"],
                },
            ],
        }
    )
    assert context["core_newest"] == "0.3.0"
    assert context["update_to"] == {"ibos": "0.1.1"}
    assert newer_releases_context(None)["checked_at"] is None
