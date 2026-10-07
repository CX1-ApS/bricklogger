"""``bricklogger services``: the units written, enabled and started for the
login, the refusals, lingering, uninstall and status, and ``init``'s offer."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.ops import services, units
from bricklogger.ops.errors import OperationError
from bricklogger.ops.services import Host

runner = CliRunner()


class Systemd:
    """systemctl --user and loginctl as a runner records them and answers."""

    def __init__(
        self,
        *,
        reachable: bool = True,
        linger: bool = False,
        may_linger: bool = True,
        active: Sequence[str] = (),
    ) -> None:
        self.commands: list[list[str]] = []
        self.reachable = reachable
        self.linger = linger
        self.may_linger = may_linger
        self.active = set(active)
        self.enabled: set[str] = set()

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(command)
        self.commands.append(command)
        code, out = 0, ""
        if command[:2] == ["systemctl", "--user"]:
            if not self.reachable:
                return subprocess.CompletedProcess(command, 1, "", "no bus")
            verb, *rest = command[2:]
            if verb == "is-active":
                out = "active" if rest[0] in self.active else "inactive"
            elif verb == "is-enabled":
                out = "enabled" if rest[0] in self.enabled else "disabled"
            elif verb == "enable":
                self.enabled.add(rest[0])
            elif verb == "start":
                self.active.add(rest[0])
            elif verb == "disable":
                self.enabled.discard(rest[-1])
                self.active.discard(rest[-1])
        elif command[:2] == ["loginctl", "show-user"]:
            out = f"Linger={'yes' if self.linger else 'no'}"
        elif command[:2] == ["loginctl", "enable-linger"]:
            code = 0 if self.may_linger else 1
            self.linger = self.may_linger
        return subprocess.CompletedProcess(command, code, out + "\n", "")

    def verbs(self) -> list[str]:
        return [
            " ".join(c[2:])
            for c in self.commands
            if c[:2] == ["systemctl", "--user"]
            and c[2] not in ("is-active", "is-enabled", "show-environment")
        ]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(
        login="logger",
        root=False,
        container=False,
        unit_dir=tmp_path / "units",
    )


def install(host: Host, systemd: Systemd, tmp_path: Path, **flags: bool):  # type: ignore[no-untyped-def]
    return services.install(
        config_dir=tmp_path / "config",
        host=host,
        run=systemd,
        answering=lambda url: False,
        command="/home/logger/.local/share/uv/tools/bricklogger/bin/bricklogger",
        **flags,
    )


def test_install_sets_up_the_daemon_and_starts_it(host: Host, tmp_path: Path) -> None:
    systemd = Systemd()
    result = install(host, systemd, tmp_path)
    assert result.written == ["bricklogger.service"] == result.started
    assert units.written(host.unit_dir) == ["bricklogger.service"]
    daemon = (host.unit_dir / "bricklogger.service").read_text()
    assert f"BRICKLOGGER_CONFIG_DIR={(tmp_path / 'config').absolute()}" in daemon
    command = "/home/logger/.local/share/uv/tools/bricklogger/bin/bricklogger"
    assert f"ExecStart={command} daemon run" in daemon
    assert systemd.verbs() == [
        "daemon-reload",
        "enable bricklogger.service",
        "start bricklogger.service",
    ]
    assert result.linger and result.linger_command is None
    assert ["loginctl", "enable-linger"] in systemd.commands


def test_a_flag_adds_its_unit_and_an_unchanged_one_is_left_alone(
    host: Host, tmp_path: Path
) -> None:
    systemd = Systemd(linger=True)
    install(host, systemd, tmp_path)
    systemd.commands.clear()
    result = install(host, systemd, tmp_path, web=True)
    assert result.written == ["bricklogger-web.service"]
    assert result.started == ["bricklogger-web.service"] and result.restarted == []
    assert "restart bricklogger.service" not in systemd.verbs()
    assert ["loginctl", "enable-linger"] not in systemd.commands, "it was on already"


def test_a_running_unit_whose_template_changed_is_rewritten_and_restarted(
    host: Host, tmp_path: Path
) -> None:
    systemd = Systemd()
    install(host, systemd, tmp_path)
    (host.unit_dir / "bricklogger.service").write_text("[Unit]\n", encoding="utf-8")
    result = install(host, systemd, tmp_path)
    assert result.written == ["bricklogger.service"] == result.restarted
    assert result.started == []


def test_lingering_only_root_may_enable_is_named_and_the_rest_goes_on(
    host: Host, tmp_path: Path
) -> None:
    systemd = Systemd(may_linger=False)
    result = install(host, systemd, tmp_path, mcp=True)
    assert not result.linger
    assert result.linger_command == "sudo loginctl enable-linger logger"
    assert result.started == ["bricklogger.service", "bricklogger-mcp.service"]


@pytest.mark.parametrize(
    ("change", "systemd", "says"),
    [
        ({"root": True}, Systemd(), "not as root"),
        ({"container": True}, Systemd(), "docker compose"),
        ({"systemctl_found": False}, Systemd(), "daemon start"),
        ({}, Systemd(reachable=False), "daemon start"),
    ],
)
def test_install_refuses_where_the_services_cannot_be(
    host: Host,
    tmp_path: Path,
    change: dict[str, bool],
    systemd: Systemd,
    says: str,
) -> None:
    refused = Host(**{**host.__dict__, **change})
    with pytest.raises(OperationError, match=says):
        install(refused, systemd, tmp_path)
    assert not refused.unit_dir.exists(), "nothing is changed"


def test_install_refuses_while_a_daemon_started_by_hand_answers(
    host: Host, tmp_path: Path
) -> None:
    with pytest.raises(OperationError, match="bricklogger daemon stop"):
        services.install(
            config_dir=tmp_path,
            host=host,
            run=Systemd(),
            answering=lambda url: url == "http://127.0.0.1:8420",
        )
    # Run as a service, the daemon answers too, and that is no reason to refuse.
    assert (
        services.refusal(
            host, tmp_path, Systemd(active=["bricklogger.service"]), lambda url: True
        )
        is None
    )


def test_uninstall_takes_all_or_the_ones_named_and_leaves_lingering(
    host: Host, tmp_path: Path
) -> None:
    systemd = Systemd()
    install(host, systemd, tmp_path, web=True, mcp=True)
    systemd.commands.clear()
    removed = services.uninstall(mcp=True, host=host, run=systemd)
    assert removed.removed == ["bricklogger-mcp.service"]
    assert units.written(host.unit_dir) == [
        "bricklogger.service",
        "bricklogger-web.service",
    ]
    removed = services.uninstall(host=host, run=systemd)
    assert removed.removed == ["bricklogger.service", "bricklogger-web.service"]
    assert units.written(host.unit_dir) == []
    assert "disable --now bricklogger-web.service" in systemd.verbs()
    assert not any(c[0] == "loginctl" and "disable" in c[1] for c in systemd.commands)
    assert services.uninstall(host=host, run=systemd).removed == []


def test_status_shows_each_unit_and_lingering(host: Host, tmp_path: Path) -> None:
    systemd = Systemd()
    install(host, systemd, tmp_path, web=True)
    state = services.status(host, systemd)
    assert [(u.name, u.written, u.enabled, u.active) for u in state.units] == [
        ("bricklogger.service", True, True, True),
        ("bricklogger-web.service", True, True, True),
        ("bricklogger-mcp.service", False, False, False),
    ]
    assert state.linger is True and state.systemd
    unknown = services.status(host, Systemd(reachable=False))
    assert unknown.units[0].active is None and unknown.linger is None


def test_the_cli_installs_and_names_the_lingering_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_install(**kwargs: object) -> services.Installed:
        assert kwargs["web"] is True and kwargs["mcp"] is False
        return services.Installed(
            units=["bricklogger.service", "bricklogger-web.service"],
            written=["bricklogger.service", "bricklogger-web.service"],
            started=["bricklogger.service", "bricklogger-web.service"],
            linger=False,
            linger_command="sudo loginctl enable-linger logger",
            unit_dir=tmp_path,
        )

    monkeypatch.setattr(services, "install", fake_install)
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "services", "install", "--web"]
    )
    assert result.exit_code == 0, result.output
    assert "started bricklogger.service, bricklogger-web.service" in result.output
    assert "sudo loginctl enable-linger logger" in result.output

    monkeypatch.setattr(
        services,
        "install",
        lambda **_: (_ for _ in ()).throw(OperationError("not as root")),
    )
    refused = runner.invoke(app, ["services", "install"])
    assert refused.exit_code == 1 and "not as root" in refused.output


def test_services_alone_shows_its_help() -> None:
    result = runner.invoke(app, ["services"])
    assert "install" in result.output and "uninstall" in result.output
