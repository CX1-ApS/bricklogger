"""Adding and removing plugins: the environment this process runs in, when the
install script made it, and uv's commands against it.

A plugin lives in the environment beside Bricklogger, and the install script
puts uv next to that environment. ``add`` and ``remove`` run that uv against
the interpreter the command itself runs from; anywhere else they say which
command to run instead. Neither restarts anything: the daemon reads its
plugins when it starts, and when that happens is the operator's call.

Every plugin shares one environment, and a process imports one version of a
library, so ``add`` resolves what it was asked for together with the plugins
already installed, each held at its installed version: a package that cannot
live with them is refused before anything has changed, and only what was asked
for moves. See ``docs/features/plugins.md``, "Installing a plugin".
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from packaging.markers import Marker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement

from bricklogger.config import instances_in, read_texts
from bricklogger.ops.errors import OperationError
from bricklogger.ops.plugin_volume import forget as forget_package
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.ops.plugin_volume import remember as remember_packages
from bricklogger.sdk.registry import (
    BUILT_IN_DISTRIBUTION,
    DESTINATIONS_GROUP,
    SOURCES_GROUP,
    PluginRegistry,
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Environment:
    """Where the plugins go: uv, the interpreter and its site-packages —
    and in a container the plugin volume the packages go into instead."""

    uv: Path
    python: Path
    site_packages: Path
    target: Path | None = None


def installed_environment() -> Environment | None:
    """The environment this process runs in, when the install script made it:
    the venv under the prefix, and uv in ``bin`` beside it. ``None`` elsewhere."""
    uv = Path(sys.prefix).parent / "bin" / "uv"
    if not uv.is_file():
        return None
    return Environment(
        uv=uv,
        python=Path(sys.executable),
        site_packages=Path(sysconfig.get_paths()["purelib"]),
        target=plugin_directory(),
    )


def manual_command(action: str, packages: Sequence[str]) -> str:
    """The uv command that does the same by hand, against this interpreter."""
    return f"uv pip {action} --python {sys.executable} {' '.join(packages)}"


@dataclass(frozen=True)
class Added:
    packages: tuple[str, ...]
    command: tuple[str, ...]
    output: str


def add_plugins(
    packages: Sequence[str],
    *,
    environment: Environment | None = None,
    run: Runner | None = None,
    remember: bool = True,
) -> Added:
    """Install the packages into the environment, holding the other plugins.

    ``PACKAGE`` is anything uv installs. A package already present is brought
    to the version asked for, and a wheel is installed even when its version
    is already present, as the install script does for Bricklogger itself.
    The other installed plugins go into the resolution pinned where they are,
    so a package that cannot live with them is refused before anything has
    changed, and nothing but what was asked for moves.

    In a container the packages go into the plugin volume instead, and what
    was asked for is recorded in its manifest unless ``remember`` says not
    to — laying the manifest down again is not a new request.
    """
    if not packages:
        raise OperationError("give at least one package to install")
    found = _environment(environment, "install", packages)
    _writable(found)
    if found.target is not None:
        added = _add_into_volume(found, packages, run)
        if remember:
            remember_packages(found.target, packages)
        return added
    command = [str(found.uv), "pip", "install", "--python", str(found.python)]
    command += _moving(packages)
    command += _held(found, packages)
    command += list(packages)
    output = _run(run, command)
    return Added(tuple(packages), tuple(command), output)


#: A distribution with an entry point in one of these groups is a plugin.
PLUGIN_GROUPS = frozenset({SOURCES_GROUP, DESTINATIONS_GROUP})


def installed_plugins(directory: Path) -> dict[str, str]:
    """``name: version`` for every plugin installed in the directory: the
    distributions there that register an entry point in one of the two
    groups, except Bricklogger itself, whose types are built in. Read from
    the metadata alone; nothing is imported."""
    found: dict[str, str] = {}
    for distribution in _distributions(directory):
        name = _normalise(distribution.name or "")
        if not name or name == BUILT_IN_DISTRIBUTION:
            continue
        if any(point.group in PLUGIN_GROUPS for point in distribution.entry_points):
            found[name] = str(distribution.version)
    return found


def _distributions(directory: Path) -> Iterable[metadata.Distribution]:
    """The distributions in the directory, read afresh. importlib.metadata
    keeps a directory's listing until the directory's mtime changes, and the
    clock that stamps it ticks in milliseconds: a package laid down within the
    tick of the last look would be missed, and the next plugin resolved
    without it, or a library it needs removed with another."""
    metadata.MetadataPathFinder.invalidate_caches()
    return metadata.distributions(path=[str(directory)])


def _held(found: Environment, packages: Sequence[str]) -> list[str]:
    """The installed plugins other than those asked for, pinned to their
    versions: given to uv with the packages, so what it resolves must live
    with them. uv takes a pinned plugin from what is installed, so this needs
    no index and works when the plugin's wheel is no longer reachable."""
    asked = _asked(packages)
    return [
        f"{name}=={version}"
        for name, version in sorted(
            installed_plugins(_plugins_directory(found)).items()
        )
        if name not in asked
    ]


def _plugins_directory(found: Environment) -> Path:
    """Where the plugins are installed: the volume in a container, otherwise
    the environment's site-packages."""
    return found.target if found.target is not None else found.site_packages


def _moving(packages: Sequence[str]) -> list[str]:
    """uv's flags for what was asked for, and for nothing else: upgraded when
    a newer version is asked for, reinstalled when the same one is, as a
    second build of a development wheel. The blanket ``--upgrade`` would move
    the other plugins' libraries too."""
    flags: list[str] = []
    for name in sorted(_asked(packages)):
        flags += ["--upgrade-package", name, "--reinstall-package", name]
    return flags


def _asked(packages: Sequence[str]) -> set[str]:
    """The distributions the specs name, where they name one."""
    return {name for name in map(distribution_name, packages) if name}


def _add_into_volume(
    found: Environment, packages: Sequence[str], run: Runner | None
) -> Added:
    """Install into the plugin volume: constrained to the versions the image
    already has, holding the volume's other plugins, and without a second copy
    of what the image already provides.

    The volume is on the path **after** the image's own packages, so a
    shared dependency is the image's. Constraining the resolution to those
    versions is what makes that safe — a plugin that cannot live with them
    fails to install and says which package it wanted — and what resolves to
    a version the image already has is removed again afterwards.

    A ``--target`` resolution does not see the interpreter's packages, and
    what the image installed from a file — Bricklogger itself — is on no
    index, so a plugin's dependency on it could never be met. Those packages
    are handed to the resolver as wheels with the metadata and nothing else,
    at the image's version; the copies it installs are pruned with the rest.
    """
    target = found.target
    assert target is not None  # only called when there is a volume
    pins, from_files = _freeze(
        run,
        [
            str(found.uv),
            "pip",
            "freeze",
            "--python",
            str(found.python),
            "--exclude-editable",
        ],
    )
    stand_ins = _listed(run, found, from_files) if from_files else {}
    present = {**pins, **stand_ins}
    with tempfile.TemporaryDirectory() as scratch:
        stand_in_dir = Path(scratch) / "stand-ins"
        for name, version in sorted(stand_ins.items()):
            _stand_in(stand_in_dir, name, version)
        constraints = Path(scratch) / "image.txt"
        constraints.write_text(
            "".join(
                f"{name}=={version}\n" for name, version in sorted(present.items())
            ),
            encoding="utf-8",
        )
        command = [
            str(found.uv),
            "pip",
            "install",
            "--python",
            str(found.python),
            "--target",
            str(target),
        ]
        command += _moving(packages)
        if present:
            command += ["--constraint", str(constraints)]
        if stand_ins:
            command += ["--find-links", str(stand_in_dir)]
        command += _held(found, packages)
        command += list(packages)
        output = _run(run, command)
    output += _prune(found, present, run)
    return Added(tuple(packages), tuple(command), output)


def _stand_in(directory: Path, name: str, version: str) -> Path:
    """A wheel with the metadata and nothing else: the package at this
    version, for a resolver that must be satisfied without fetching it."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{name.replace('-', '_')}-{version}"
    info = f"{stem}.dist-info"
    files = {
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            "Summary: stands in for the image's copy while a plugin is resolved\n"
        ),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: bricklogger\nRoot-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    record = []
    for path, text in files.items():
        data = text.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode()},{len(data)}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    wheel = directory / f"{stem}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel


def _prune(found: Environment, present: Mapping[str, str], run: Runner | None) -> str:
    """Remove from the volume what the image already provides, version for
    version: the copy on the path is the image's anyway."""
    target = found.target
    assert target is not None
    in_volume = _versions(run, [str(found.uv), "pip", "freeze", "--path", str(target)])
    duplicates = sorted(
        name for name, version in in_volume.items() if present.get(name) == version
    )
    if not duplicates:
        return ""
    return _run(
        run,
        [str(found.uv), "pip", "uninstall", "--target", str(target), *duplicates],
    )


def _versions(run: Runner | None, command: Sequence[str]) -> dict[str, str]:
    """``name: version`` from a uv freeze, and nothing when it cannot be read."""
    return _freeze(run, command)[0]


def _freeze(
    run: Runner | None, command: Sequence[str]
) -> tuple[dict[str, str], set[str]]:
    """A uv freeze read two ways: ``name: version`` for what came from an
    index, and the names of what was installed from a file or URL, whose
    version the freeze does not say. Both empty when it cannot be read."""
    runner = run if run is not None else _subprocess
    try:
        completed = runner(command)
    except OSError:
        return {}, set()
    if completed.returncode != 0:
        return {}, set()
    pinned: dict[str, str] = {}
    from_files: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-e ")):
            continue
        if " @ " in line:
            from_files.add(_normalise(line.split(" @ ", 1)[0].strip()))
            continue
        name, separator, version = line.partition("==")
        if separator:
            pinned[_normalise(name.strip())] = version.strip()
    return pinned, from_files


def _listed(run: Runner | None, found: Environment, names: set[str]) -> dict[str, str]:
    """The versions of these packages in the environment, from ``uv pip list``,
    which names a version for a package installed from a file too."""
    runner = run if run is not None else _subprocess
    command = [
        str(found.uv),
        "pip",
        "list",
        "--format",
        "json",
        "--python",
        str(found.python),
    ]
    try:
        completed = runner(command)
        rows = json.loads(completed.stdout or "[]") if completed.returncode == 0 else []
    except (OSError, ValueError):
        return {}
    return {
        _normalise(str(row["name"])): str(row["version"])
        for row in rows
        if isinstance(row, dict) and _normalise(str(row.get("name", ""))) in names
    }


@dataclass(frozen=True)
class Removed:
    distribution: str
    version: str | None
    types: tuple[str, ...]
    command: tuple[str, ...]
    output: str
    dependencies: tuple[str, ...] = ()
    """What only this plugin needed, uninstalled with it."""


class InstancesConfigured(OperationError):
    """The removal is refused because instances of the type are configured."""


def remove_plugin(
    type_name: str,
    *,
    config_dir: Path,
    force: bool = False,
    registry: PluginRegistry | None = None,
    environment: Environment | None = None,
    run: Runner | None = None,
) -> Removed:
    """Uninstall the distribution that provides the type, and with it every
    other type it provides and what only that plugin needed.

    Refuses while an instance of any of those types is configured, unless
    ``force``: the daemon would reject the configuration at its next start.
    The built-in types cannot be removed, since their distribution is
    Bricklogger itself.
    """
    installed = registry if registry is not None else PluginRegistry.from_entry_points()
    distribution = installed.distribution_of(type_name)
    if distribution is None:
        if installed.role_of(type_name) is not None:
            raise OperationError(
                f"the distribution that provides {type_name!r} is not known; "
                "remove it with uv by hand"
            )
        raise OperationError(
            f"no installed plugin {type_name!r}; `bricklogger plugins` lists them"
        )
    if distribution == BUILT_IN_DISTRIBUTION:
        raise OperationError(
            f"{type_name!r} is built into bricklogger and cannot be removed"
        )
    types = tuple(installed.types_of(distribution))
    configured = configured_instances(config_dir, types)
    if configured and not force:
        listing = ", ".join(f"{name} in {file}.yaml" for file, name in configured)
        raise InstancesConfigured(
            f"instances of {', '.join(types)} are configured: {listing}. Remove "
            "them first with `bricklogger sources remove NAME` or `destinations "
            "remove NAME`, or pass --force and the daemon will reject the "
            "configuration at its next start until they are gone"
        )
    found = _environment(environment, "uninstall", [distribution])
    _writable(found)
    location = (
        ["--target", str(found.target)]
        if found.target is not None
        else ["--python", str(found.python)]
    )
    orphans = orphaned_by(distribution, _plugins_directory(found))
    command = [str(found.uv), "pip", "uninstall", *location, distribution, *orphans]
    output = _run(run, command)
    if found.target is not None:
        forget_package(found.target, distribution)
    return Removed(
        distribution,
        installed.version_of(type_name),
        types,
        tuple(command),
        output,
        tuple(orphans),
    )


def orphaned_by(distribution: str, directory: Path) -> list[str]:
    """What only this plugin needs: the distributions its requirements reach,
    transitively and as installed in the directory, that no other plugin
    there and not Bricklogger itself reaches. They go with the plugin, so a
    library nothing needs any more does not stay behind."""
    present = {
        _normalise(found.name): found
        for found in _distributions(directory)
        if found.name
    }
    removed = _normalise(distribution)
    roots = set(installed_plugins(directory)) - {removed}
    if BUILT_IN_DISTRIBUTION in present:
        roots.add(BUILT_IN_DISTRIBUTION)
    kept = _reached(roots, present)
    candidates = _reached({removed}, present) - {removed, BUILT_IN_DISTRIBUTION}
    return sorted(candidates - kept)


def _reached(
    roots: Iterable[str], present: Mapping[str, metadata.Distribution]
) -> set[str]:
    """The installed distributions the roots' requirements reach, following
    each requirement under the extras it was asked with and skipping one
    whose marker does not hold. A requirement not installed here — the
    image's, in a container — ends the walk there."""
    reached: set[str] = set()
    pending: list[tuple[str, frozenset[str]]] = [(root, frozenset()) for root in roots]
    visited: set[tuple[str, frozenset[str]]] = set()
    while pending:
        item = pending.pop()
        if item in visited:
            continue
        visited.add(item)
        name, extras = item
        found = present.get(name)
        if found is None:
            continue
        reached.add(name)
        for line in found.requires or ():
            try:
                requirement = Requirement(line)
            except InvalidRequirement:
                continue
            if requirement.marker is not None and not _holds(
                requirement.marker, extras
            ):
                continue
            pending.append(
                (_normalise(requirement.name), frozenset(requirement.extras))
            )
    return reached


def _holds(marker: Marker, extras: frozenset[str]) -> bool:
    """Whether the marker holds for this interpreter under any of the extras
    the requirement was asked with, or under none. A marker that cannot be
    evaluated is taken to hold: better to keep a library than to pull it
    from under something."""
    try:
        return any(marker.evaluate({"extra": extra}) for extra in (extras or {""}))
    except (UndefinedComparison, UndefinedEnvironmentName):
        return True


def configured_instances(
    config_dir: Path, types: Sequence[str]
) -> list[tuple[str, str]]:
    """``(file, name)`` for every configured instance of one of the types."""
    texts = read_texts(config_dir)
    found: list[tuple[str, str]] = []
    for file in ("sources", "destinations"):
        for name, settings in instances_in(texts[file]).items():
            if isinstance(settings, Mapping) and settings.get("type") in types:
                found.append((file, name))
    return found


_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def distribution_name(spec: str) -> str | None:
    """The normalised distribution name a package spec names, when it does:
    a wheel by its file name, a requirement by its head, a URL only in the
    ``name @ url`` form."""
    if spec.endswith(".whl"):
        return _normalise(Path(spec).name.split("-")[0])
    if "://" in spec or spec.startswith("git+"):
        head, at, _ = spec.partition("@")
        return _normalise(head.strip()) if at and "://" not in head else None
    if spec.startswith((".", "/", "~")) or spec.endswith((".tar.gz", ".zip")):
        return None
    match = _NAME.match(spec)
    return _normalise(match.group(1)) if match else None


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _environment(
    environment: Environment | None, action: str, packages: Sequence[str]
) -> Environment:
    found = environment if environment is not None else installed_environment()
    if found is None:
        raise OperationError(
            "this is not an installation the install script made, so its uv is "
            "not beside the environment; run instead:\n  "
            + manual_command(action, packages)
        )
    return found


def _writable(environment: Environment) -> None:
    if environment.target is not None:
        environment.target.mkdir(parents=True, exist_ok=True)
        if not os.access(environment.target, os.W_OK):
            raise OperationError(
                f"no right to write to the plugin volume at "
                f"{environment.target}; check who owns it"
            )
        return
    if not os.access(environment.site_packages, os.W_OK):
        raise OperationError(
            f"no right to write to {environment.site_packages}; run the command "
            "with sudo"
        )


def _run(run: Runner | None, command: Sequence[str]) -> str:
    runner = run if run is not None else _subprocess
    try:
        completed = runner(command)
    except OSError as exc:
        raise OperationError(f"could not run {command[0]}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = f"{Path(command[0]).name} {command[1]} {command[2]} failed"
        raise OperationError(f"{message}:\n{_tail(detail)}" if detail else message)
    return (completed.stderr or "") + (completed.stdout or "")


def _subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.splitlines()[-lines:])
