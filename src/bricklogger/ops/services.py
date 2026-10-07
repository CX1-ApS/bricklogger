"""``bricklogger services``: the daemon, the web interface and the MCP server as
``systemd --user`` units of the login that installed Bricklogger.

``install`` writes the units from the templates in the package, enables
lingering so they start at boot and outlive a logout, reloads systemd, and
enables and starts what it wrote; it can be run again, adding a unit and
rewriting one only when the templates differ. ``uninstall`` takes them away
again and leaves the configuration and the data. See
``docs/features/cli.md``, "services".
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.ops import units
from bricklogger.ops.errors import OperationError
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.ops.status import answers, binding

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

SYSTEMCTL = ("systemctl", "--user")


@dataclass(frozen=True)
class Host:
    """What decides whether and where the units can go: the login, whether
    it is root, whether this is a container, and the login's unit directory."""

    login: str
    root: bool
    container: bool
    unit_dir: Path
    systemctl_found: bool = True


def this_host() -> Host:
    geteuid = getattr(os, "geteuid", None)
    return Host(
        login=os.environ.get("USER") or getpass.getuser(),
        root=geteuid is not None and geteuid() == 0,
        container=plugin_directory() is not None,
        unit_dir=Path.home() / ".config" / "systemd" / "user",
        systemctl_found=shutil.which("systemctl") is not None,
    )


def selected(web: bool, mcp: bool) -> list[str]:
    """The daemon always, and the web interface and the MCP server when asked."""
    return [
        name
        for name, wanted in zip(units.UNITS, (True, web, mcp), strict=True)
        if wanted
    ]


def refusal(
    host: Host,
    config_dir: Path,
    run: Runner | None = None,
    answering: Callable[[str], bool] = answers,
) -> str | None:
    """Why the services cannot be set up here, or ``None`` when they can."""
    if host.root:
        return (
            "Bricklogger is installed per login, and its services run as that "
            "login; run this as the login that installed it, not as root"
        )
    if host.container:
        return (
            "in a container the processes are the compose file's services; "
            "see `docker compose up -d`"
        )
    if not host.systemctl_found or not _succeeds(run, [*SYSTEMCTL, "show-environment"]):
        return (
            "systemctl --user does not answer, so this machine has no systemd "
            "for the login; run `bricklogger daemon start` and `bricklogger serve` "
            "instead"
        )
    if not _active(run, units.DAEMON):
        url = _daemon_url(config_dir)
        if url is not None and answering(url):
            return (
                f"a daemon started by hand answers at {url}; stop it first: "
                "bricklogger daemon stop"
            )
    return None


@dataclass
class Installed:
    """What ``install`` did, for the command to tell."""

    units: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    linger: bool = True
    linger_command: str | None = None
    unit_dir: Path | None = None


def install(
    *,
    config_dir: Path,
    web: bool = False,
    mcp: bool = False,
    host: Host | None = None,
    run: Runner | None = None,
    answering: Callable[[str], bool] = answers,
    command: str | None = None,
) -> Installed:
    """Write, enable and start the daemon's unit and the ones asked for."""
    host = host if host is not None else this_host()
    reason = refusal(host, config_dir, run, answering)
    if reason is not None:
        raise OperationError(reason)
    names = selected(web, mcp)
    parameters = units.UnitParameters(
        command if command is not None else command_path(),
        str(config_dir.absolute()),
    )
    result = Installed(units=names, unit_dir=host.unit_dir)
    result.linger = _linger(run, host.login)
    if not result.linger:
        result.linger_command = f"sudo loginctl enable-linger {host.login}"
    result.written = units.write(host.unit_dir, parameters, names)
    if result.written:
        _call(run, [*SYSTEMCTL, "daemon-reload"])
    for name in names:
        _call(run, [*SYSTEMCTL, "enable", name])
        if not _active(run, name):
            _call(run, [*SYSTEMCTL, "start", name])
            result.started.append(name)
        elif name in result.written:
            _call(run, [*SYSTEMCTL, "restart", name])
            result.restarted.append(name)
    return result


@dataclass
class Uninstalled:
    removed: list[str] = field(default_factory=list)


def uninstall(
    *,
    web: bool = False,
    mcp: bool = False,
    host: Host | None = None,
    run: Runner | None = None,
) -> Uninstalled:
    """Stop, disable and remove the units: all of them without a flag, the
    ones named with one. Lingering is left as it is."""
    host = host if host is not None else this_host()
    if host.root or host.container:
        reason = refusal(host, Path("."), run)
        raise OperationError(reason or "the services cannot be removed here")
    names = list(units.UNITS) if not (web or mcp) else selected(web, mcp)[1:]
    present = [name for name in names if (host.unit_dir / name).is_file()]
    result = Uninstalled()
    reachable = host.systemctl_found and _succeeds(
        run, [*SYSTEMCTL, "show-environment"]
    )
    for name in reversed(present):
        if reachable:
            _succeeds(run, [*SYSTEMCTL, "disable", "--now", name])
        (host.unit_dir / name).unlink()
        result.removed.insert(0, name)
    if present and reachable:
        _call(run, [*SYSTEMCTL, "daemon-reload"])
    return result


@dataclass(frozen=True)
class UnitState:
    name: str
    written: bool
    enabled: bool | None
    active: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit": self.name,
            "written": self.written,
            "enabled": self.enabled,
            "active": self.active,
        }


@dataclass(frozen=True)
class ServicesStatus:
    units: list[UnitState]
    linger: bool | None
    systemd: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "units": [state.as_dict() for state in self.units],
            "linger": self.linger,
            "systemd": self.systemd,
        }


def status(host: Host | None = None, run: Runner | None = None) -> ServicesStatus:
    """Each unit written, enabled and active; and lingering."""
    host = host if host is not None else this_host()
    reachable = host.systemctl_found and _succeeds(
        run, [*SYSTEMCTL, "show-environment"]
    )
    states = []
    for name in units.UNITS:
        enabled = active = None
        if reachable:
            enabled = _answer(run, [*SYSTEMCTL, "is-enabled", name]) == "enabled"
            active = _answer(run, [*SYSTEMCTL, "is-active", name]) == "active"
        states.append(
            UnitState(name, (host.unit_dir / name).is_file(), enabled, active)
        )
    linger = _lingering(run, host.login) if reachable else None
    return ServicesStatus(states, linger, reachable)


def command_path() -> str:
    """The ``bricklogger`` command of the environment this process runs in,
    by its absolute path, so a unit does not depend on the service's path."""
    for directory in ("bin", "Scripts"):
        candidate = Path(sys.prefix) / directory / "bricklogger"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("bricklogger")
    if found is not None:
        return str(Path(found).absolute())
    return str(Path(sys.argv[0]).absolute())


# --- systemd and logind --------------------------------------------------------


def _linger(run: Runner | None, login: str) -> bool:
    """Lingering on, enabled when it was not; ``False`` when only root may."""
    if _lingering(run, login):
        return True
    return _succeeds(run, ["loginctl", "enable-linger"])


def _lingering(run: Runner | None, login: str) -> bool:
    answer = _answer(run, ["loginctl", "show-user", login, "--property=Linger"])
    return answer in ("Linger=yes", "yes")


def _daemon_url(config_dir: Path) -> str | None:
    try:
        settings = load_daemon_settings(config_dir)
    except ConfigError:
        return None
    return binding(settings.api.host, settings.api.port)


def _active(run: Runner | None, name: str) -> bool:
    return _answer(run, [*SYSTEMCTL, "is-active", name]) == "active"


def _execute(
    run: Runner | None, command: Sequence[str]
) -> subprocess.CompletedProcess[str] | None:
    runner = run if run is not None else _subprocess
    try:
        return runner(command)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _answer(run: Runner | None, command: Sequence[str]) -> str:
    completed = _execute(run, command)
    return (completed.stdout or "").strip() if completed is not None else ""


def _succeeds(run: Runner | None, command: Sequence[str]) -> bool:
    completed = _execute(run, command)
    return completed is not None and completed.returncode == 0


def _call(run: Runner | None, command: Sequence[str]) -> None:
    completed = _execute(run, command)
    if completed is None:
        raise OperationError(f"could not run {command[0]}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise OperationError(
            f"{' '.join(command)} failed" + (f":\n{detail}" if detail else "")
        )


def _subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=False, timeout=30
    )
