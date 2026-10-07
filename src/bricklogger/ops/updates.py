"""``bricklogger update``: Bricklogger and its plugins upgraded from PyPI.

``update status`` installs nothing: for Bricklogger and every installed plugin
it reads the releases on PyPI and says which is the newest, and which is the
newest that fits the rest. A plugin is built for a minor version of
Bricklogger and says so in its requirement on ``bricklogger``; that
requirement, in the installed metadata and in a release's, is what "fits"
means here, while uv's resolution at install time has the last word.

``update core``, ``update <type>`` and ``update all`` install with
``uv tool install``, as ``plugins add`` does — what is held, pinned for the
install, and uv's record left naming the packages without versions after it —
and then:

1. validate the configuration with the new version, in a process of its own,
   and put the noted versions back when it does not hold;
2. write the units that are there again from the new version's templates;
3. restart the units that are active, and wait until each answers.

In a container Bricklogger comes with the image, so only the plugins in the
volume are upgraded, and nothing is restarted. See
``docs/features/cli.md``, "update".
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.ops.environment import (
    PLUGIN_GROUPS,
    Environment,
    Runner,
    _environment,
    _plugins_directory,
    _run,
    _subprocess,
    _writable,
    add_plugins,
    installed_plugins,
    pinned,
    requirement_name,
    run_tool,
    settle,
    tool_install,
)
from bricklogger.ops.errors import OperationError
from bricklogger.ops.plugin_volume import read_manifest, remember, write_manifest
from bricklogger.ops.status import PID_FILE, answers, binding, process_alive, read_pid
from bricklogger.ops.units import UNITS, changed_from
from bricklogger.sdk.registry import BUILT_IN_DISTRIBUTION, RESERVED_TYPE_NAMES

PYPI = "https://pypi.org/pypi"
CORE = BUILT_IN_DISTRIBUTION

Fetch = Callable[[str], Any]
"""A JSON document by its URL; raises when it cannot be had."""


def fetch_json(url: str) -> Any:
    """GET a JSON document; an ``OperationError`` when it cannot be had."""
    import httpx

    try:
        response = httpx.get(url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OperationError(f"could not read {url}: {exc}") from exc


# --- what is installed ------------------------------------------------------------


@dataclass(frozen=True)
class Installed:
    """An installed distribution: its version, the types it provides, and the
    versions of Bricklogger it accepts."""

    name: str
    version: str
    types: tuple[str, ...] = ()
    requires_core: SpecifierSet | None = None


def installed() -> tuple[Installed, list[Installed]]:
    """Bricklogger and the installed plugins, from the metadata on the path;
    nothing is imported."""
    metadata.MetadataPathFinder.invalidate_caches()
    core: Installed | None = None
    plugins: dict[str, Installed] = {}
    for distribution in metadata.distributions():
        name = normalise(distribution.name or "")
        if not name or name in plugins:
            continue
        if name == CORE:
            if core is None:
                core = Installed(CORE, str(distribution.version))
            continue
        types = tuple(
            sorted(
                point.name
                for point in distribution.entry_points
                if point.group in PLUGIN_GROUPS
            )
        )
        if types:
            plugins[name] = Installed(
                name,
                str(distribution.version),
                types,
                requirement_on_core(distribution.requires or ()),
            )
    if core is None:
        raise OperationError("bricklogger itself is not installed here")
    return core, sorted(plugins.values(), key=lambda p: p.name)


def requirement_on_core(lines: Iterable[str]) -> SpecifierSet | None:
    """The versions of Bricklogger a list of requirements accepts, or ``None``
    when it names none."""
    for line in lines:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        if normalise(requirement.name) != CORE:
            continue
        if requirement.marker is not None and not requirement.marker.evaluate(
            {"extra": ""}
        ):
            continue
        return requirement.specifier
    return None


def normalise(name: str) -> str:
    return name.replace("_", "-").replace(".", "-").lower()


# --- what there is ----------------------------------------------------------------


@dataclass(frozen=True)
class Component:
    """One row of ``update status``."""

    name: str
    installed: str
    newest: str | None = None
    fits: str | None = None
    held_by: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    error: str | None = None

    @property
    def behind(self) -> bool:
        """Whether a newer release that fits exists."""
        return self.fits is not None and _newer(self.fits, self.installed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "installed": self.installed,
            "newest": self.newest,
            "fits": self.fits,
            "held_by": list(self.held_by),
            "types": list(self.types),
            "error": self.error,
        }


def releases(name: str, fetch: Fetch) -> list[Version]:
    """The final releases of a distribution on PyPI, newest first; a release
    whose every file is yanked does not count."""
    data = fetch(f"{PYPI}/{name}/json")
    found: list[Version] = []
    for text, files in (data.get("releases") or {}).items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if not files or all(item.get("yanked") for item in files):
            continue
        found.append(version)
    return sorted(found, reverse=True)


def release_requires_core(
    name: str, version: Version, fetch: Fetch
) -> SpecifierSet | None:
    data = fetch(f"{PYPI}/{name}/{version}/json")
    return requirement_on_core((data.get("info") or {}).get("requires_dist") or ())


def status(fetch: Fetch | None = None) -> list[Component]:
    """Bricklogger first, then every plugin: the version installed, the
    newest release, and the newest release that fits the rest."""
    fetch = fetch if fetch is not None else fetch_json
    core, plugins = installed()
    rows = [_core_status(core, plugins, fetch)]
    rows += [_plugin_status(plugin, core, fetch) for plugin in plugins]
    return rows


def _core_status(
    core: Installed, plugins: Sequence[Installed], fetch: Fetch
) -> Component:
    try:
        found = releases(CORE, fetch)
    except OperationError as exc:
        return Component(CORE, core.version, error=exc.message)
    if not found:
        return Component(CORE, core.version, fits=core.version)
    current = Version(core.version)
    fits = next(
        (
            version
            for version in found
            if version > current and _accepted_by(version, plugins)
        ),
        None,
    )
    newest = found[0]
    held = tuple(
        plugin.name
        for plugin in plugins
        if plugin.requires_core is not None
        and not plugin.requires_core.contains(newest, prereleases=True)
    )
    return Component(
        CORE,
        core.version,
        str(newest),
        str(fits) if fits is not None else core.version,
        held if newest > current else (),
    )


def _plugin_status(plugin: Installed, core: Installed, fetch: Fetch) -> Component:
    try:
        found = releases(plugin.name, fetch)
        current = Version(plugin.version)
        fits = plugin.version
        for version in found:
            if version <= current:
                break
            accepted = release_requires_core(plugin.name, version, fetch)
            if accepted is None or accepted.contains(core.version, prereleases=True):
                fits = str(version)
                break
    except (OperationError, InvalidVersion) as exc:
        message = exc.message if isinstance(exc, OperationError) else str(exc)
        return Component(plugin.name, plugin.version, types=plugin.types, error=message)
    newest = str(found[0]) if found else None
    held = (
        (CORE,)
        if newest is not None and newest != fits and _newer(newest, plugin.version)
        else ()
    )
    return Component(plugin.name, plugin.version, newest, fits, held, plugin.types)


def _accepted_by(version: Version, plugins: Sequence[Installed]) -> bool:
    return all(
        plugin.requires_core is None
        or plugin.requires_core.contains(version, prereleases=True)
        for plugin in plugins
    )


def _newer(candidate: str, than: str) -> bool:
    try:
        return Version(candidate) > Version(than)
    except InvalidVersion:
        return False


def available_line(updates: Mapping[str, Any] | None) -> str | None:
    """The line ``status`` and the daily summary carry when the daemon's look
    found newer releases; ``None`` when it found none."""
    available = (updates or {}).get("available") or []
    if not available:
        return None
    listing = ", ".join(f"{row['name']} {row['newest']}" for row in available)
    return f"newer releases: {listing} (bricklogger update status)"


def newer_releases(fetch: Fetch | None = None) -> list[dict[str, Any]]:
    """The components with a newer release than the installed one, for the
    daemon's daily look; a component that cannot be looked up is left out."""
    return [
        row.as_dict()
        for row in status(fetch)
        if row.error is None
        and row.newest is not None
        and _newer(row.newest, row.installed)
    ]


# --- installing -------------------------------------------------------------------


@dataclass
class Updated:
    """What an update did, for the command to tell."""

    moved: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    not_answering: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    container: bool = False


class UpdateRefused(OperationError):
    """The new version does not accept the configuration. ``restored`` says
    whether the previous versions were put back; when they could not be —
    one installed from a wheel on disk is on no index — the new version
    stays installed and nothing was restarted."""

    def __init__(
        self,
        message: str,
        errors: Sequence[Mapping[str, Any]],
        restored: bool = True,
    ) -> None:
        super().__init__(message)
        self.errors = list(errors)
        self.restored = restored


def _not_restored(refused: UpdateRefused, failure: OperationError) -> UpdateRefused:
    return UpdateRefused(
        f"{refused.message}, and the previous versions could not be put back: "
        f"{failure.message}\nNothing was restarted, so what runs is still the "
        "previous version. Make the configuration fit the new version, or "
        "install the previous one again with `uv tool install`, naming every "
        "plugin with --with",
        refused.errors,
        restored=False,
    )


@dataclass(frozen=True)
class Machine:
    """Where the units are and how systemd is reached; ``systemctl`` is
    ``None`` on a machine without it."""

    systemctl: tuple[str, ...] | None
    unit_dir: Path


def this_machine() -> Machine:
    """The login's own systemd, where ``services install`` put the units."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    if shutil.which("systemctl") is None:
        return Machine(None, unit_dir)
    return Machine(("systemctl", "--user"), unit_dir)


def update(
    target: str,
    *,
    config_dir: Path,
    restart: bool = True,
    environment: Environment | None = None,
    run: Runner | None = None,
    machine: Machine | None = None,
    answering: Callable[[str], bool] = answers,
    wait: float = 60.0,
) -> Updated:
    """``update core``, ``update all`` or ``update <type>``."""
    found = _environment(environment, "install --upgrade", [CORE])
    _writable(found)
    if found.target is not None:
        return _update_volume(target, found, config_dir, run)
    directory = _plugins_directory(found)
    before = _versions(directory)
    core, plugins, flags = _upgrade_arguments(target, found, before)
    run_tool(found, run, tool_install(found, core, plugins, flags))
    after = _versions(directory)
    result = Updated(moved=_moved(before, after))
    record = found.record
    assert record is not None
    try:
        if result.moved:
            result.warnings = _validate(found, config_dir, run)
    except UpdateRefused as refused:
        try:
            installed_back = _put_back(found, before, after, run)
        except OperationError as failure:
            raise _not_restored(refused, failure) from failure
        settle(found, run, record.core, record.plugins, installed_back)
        raise
    settle(found, run, core, plugins, [core, *plugins])
    if not result.moved:
        return result
    current = machine if machine is not None else this_machine()
    if current.systemctl is not None:
        result.units = _refresh_units(found, current, run)
    if restart:
        _restart(result, current, config_dir, run, answering, wait)
    return result


def _upgrade_arguments(
    target: str, found: Environment, before: Mapping[str, str]
) -> tuple[str, list[str], list[str]]:
    """Bricklogger, the plugins and the flags for ``uv tool install``:
    everything moving by name for ``all``, Bricklogger for ``core`` with the
    plugins held at their versions, one plugin with the rest held. What moves
    is named, not a file, since an upgrade comes from PyPI."""
    record = found.record
    assert record is not None
    if target == "all":
        names = [requirement_name(spec) or spec for spec in record.plugins]
        return CORE, names, ["--upgrade"]
    if target == "core":
        held = [pinned(spec, before) for spec in record.plugins]
        return CORE, held, ["--upgrade-package", CORE]
    distribution = _distribution_of(target)
    plugins = [
        distribution if requirement_name(spec) == distribution else pinned(spec, before)
        for spec in record.plugins
    ]
    if distribution not in map(requirement_name, record.plugins):
        plugins.append(distribution)
    return pinned(record.core, before), plugins, ["--upgrade-package", distribution]


def _distribution_of(type_name: str) -> str:
    """The installed distribution that provides the type."""
    if type_name in RESERVED_TYPE_NAMES:
        raise OperationError(f"{type_name!r} is not a plugin type")
    metadata.MetadataPathFinder.invalidate_caches()
    for distribution in metadata.distributions():
        for point in distribution.entry_points:
            if point.group in PLUGIN_GROUPS and point.name == type_name:
                name = normalise(distribution.name or "")
                if name == CORE:
                    raise OperationError(
                        f"{type_name!r} is built into bricklogger; "
                        "`bricklogger update core` upgrades it"
                    )
                return name
    raise OperationError(
        f"no installed plugin {type_name!r}; `bricklogger plugins` lists them"
    )


def _versions(directory: Path) -> dict[str, str]:
    """Bricklogger and the plugins in the directory, ``name: version``."""
    metadata.MetadataPathFinder.invalidate_caches()
    found = dict(installed_plugins(directory))
    for distribution in metadata.distributions(path=[str(directory)]):
        if normalise(distribution.name or "") == CORE:
            found[CORE] = str(distribution.version)
    return found


def _moved(
    before: Mapping[str, str], after: Mapping[str, str]
) -> dict[str, tuple[str | None, str | None]]:
    return {
        name: (before.get(name), after.get(name))
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }


def _validate(
    found: Environment, config_dir: Path, run: Runner | None
) -> list[dict[str, Any]]:
    """The new version's ``validate``, in a process of its own: the warnings
    when the configuration holds, ``UpdateRefused`` when it does not."""
    runner = run if run is not None else _subprocess
    command = [
        str(found.python),
        "-m",
        "bricklogger",
        "--config-dir",
        str(config_dir),
        "validate",
        "--json",
    ]
    try:
        completed = runner(command)
    except OSError as exc:
        raise UpdateRefused(f"the new version could not be run: {exc}", []) from exc
    try:
        answer = json.loads(completed.stdout or "")
    except ValueError:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise UpdateRefused(
            f"the new version could not validate the configuration:\n{detail}", []
        ) from None
    if not answer.get("valid"):
        errors = list(answer.get("errors") or [])
        raise UpdateRefused("the new version does not accept the configuration", errors)
    return list(answer.get("warnings") or [])


def _put_back(
    found: Environment,
    before: Mapping[str, str],
    after: Mapping[str, str],
    run: Runner | None,
) -> list[str]:
    """Install the noted versions again, with the record as it was; what was
    not there before is not in it, so uv takes it away. The requirements
    installed, Bricklogger first."""
    record = found.record
    assert record is not None
    moved = [name for name in sorted(before) if before[name] != after.get(name)]

    def back(spec: str) -> str:
        name = requirement_name(spec)
        if name in moved:
            return f"{name}=={before[name]}"
        return pinned(spec, before)

    flags = [flag for name in moved for flag in ("--reinstall-package", name)]
    specs = [back(record.core), *map(back, record.plugins)]
    run_tool(found, run, tool_install(found, specs[0], specs[1:], flags))
    return specs


def _refresh_units(
    found: Environment, machine: Machine, run: Runner | None
) -> list[str]:
    """The units written again by the new version; systemd reloaded when one
    changed."""
    assert machine.systemctl is not None
    output = _run(
        run,
        [
            str(found.python),
            "-m",
            "bricklogger.ops.units",
            "refresh",
            "--dir",
            str(machine.unit_dir),
        ],
    )
    changed = changed_from(output)
    if changed:
        _run(run, [*machine.systemctl, "daemon-reload"])
    return changed


def _restart(
    result: Updated,
    machine: Machine,
    config_dir: Path,
    run: Runner | None,
    answering: Callable[[str], bool],
    wait: float,
) -> None:
    """Restart the active units, daemon first, and wait for each to answer;
    say what runs by hand."""
    try:
        settings = load_daemon_settings(config_dir)
    except ConfigError as exc:
        result.notes.append(
            f"daemon.yaml could not be read, so nothing was restarted: {exc}"
        )
        return
    urls = {
        UNITS[0]: binding(settings.api.host, settings.api.port),
        UNITS[1]: binding(settings.web.host, settings.web.port),
        UNITS[2]: binding(settings.mcp.host, settings.mcp.port),
    }
    runner = run if run is not None else _subprocess
    by_hand = {
        UNITS[0]: "a daemon started by hand runs the old version until it is "
        "restarted: bricklogger daemon restart",
        UNITS[1]: f"`bricklogger serve` answers at {urls[UNITS[1]]} but not as "
        f"{UNITS[1]}; restart it by hand to run the new version",
        UNITS[2]: f"`bricklogger mcp serve --http` answers at {urls[UNITS[2]]} but "
        f"not as {UNITS[2]}; restart it by hand to run the new version",
    }
    systemctl = machine.systemctl
    for unit in UNITS:
        active = False
        if systemctl is not None:
            try:
                answer = runner([*systemctl, "is-active", unit])
                active = (answer.stdout or "").strip() == "active"
            except OSError:
                active = False
        if systemctl is not None and active:
            _run(run, [*systemctl, "restart", unit])
            result.restarted.append(unit)
            if not _answers_within(answering, urls[unit], wait):
                result.not_answering.append(unit)
            continue
        if unit == UNITS[0]:
            pid = read_pid(settings.data_dir / PID_FILE)
            running = pid is not None and process_alive(pid)
        else:
            running = answering(urls[unit])
        if running:
            result.notes.append(by_hand[unit])


def _answers_within(answering: Callable[[str], bool], url: str, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while True:
        if answering(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


# --- in a container ---------------------------------------------------------------


def _update_volume(
    target: str, found: Environment, config_dir: Path, run: Runner | None
) -> Updated:
    """Upgrade plugins in the volume, held to the image's Bricklogger; the
    manifest records them by name, so a new image lays the newest that fits
    down again. Nothing is restarted."""
    volume = found.target
    assert volume is not None
    if target == "core":
        raise OperationError(
            "in a container Bricklogger comes with the image; name the new tag "
            "in the compose file, then run:\n  docker compose pull\n"
            "  docker compose up -d"
        )
    before = installed_plugins(volume)
    if target == "all":
        packages = sorted(before)
        if not packages:
            raise OperationError("no plugins are installed in the volume")
    else:
        packages = [_distribution_of(target)]
    manifest = read_manifest(volume)
    add_plugins(packages, environment=found, run=run, remember=False)
    after = installed_plugins(volume)
    result = Updated(moved=_moved(before, after), container=True)
    if not result.moved:
        return result
    try:
        result.warnings = _validate(found, config_dir, run)
    except UpdateRefused as refused:
        write_manifest(volume, manifest)
        try:
            add_plugins(
                [f"{name}=={before[name]}" for name in result.moved if name in before],
                environment=found,
                run=run,
                remember=False,
            )
        except OperationError as failure:
            raise _not_restored(refused, failure) from failure
        raise
    remember(volume, packages)
    return result
