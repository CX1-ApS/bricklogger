"""The plugin volume of a container: the directory plugins are installed into,
the manifest of what was asked for, and laying it down again against a new image.

An image cannot grow a package, and its environment is replaced whole when the
image is upgraded, so a container keeps its plugins in a volume of its own and
puts that directory in ``BRICKLOGGER_PLUGIN_DIR``. Beside the packages the
volume holds a **manifest** of what was asked for and a **stamp** of the
Bricklogger and Python versions they were installed against; when a starting
container finds a different image, it lays the manifest down again before the
daemon starts. See ``docs/docker.md``, "Plugins" and "Upgrading".
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import sysconfig
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the environment imports this module, so only for the types
    from bricklogger.ops.environment import Environment, Runner

#: The environment variable the image sets to the plugin volume.
DIRECTORY_VARIABLE = "BRICKLOGGER_PLUGIN_DIR"
#: What was asked for, one requirement per line, in the volume.
MANIFEST = "plugins.txt"
#: What the packages in the volume were installed against.
STAMP = "built-against.json"
#: Held while the volume is written, so containers starting together do not race.
#: Not ``.lock``, which is uv's own lock file in a target directory — holding
#: that one makes the install we are about to run wait for us forever.
LOCK = ".bricklogger.lock"

#: The volume's own files, which an upgrade must not sweep away.
KEEP = frozenset({MANIFEST, STAMP, LOCK})

Log = Callable[[str], None]


def plugin_directory() -> Path | None:
    """The plugin volume, when this process runs in a container that has one."""
    value = os.environ.get(DIRECTORY_VARIABLE, "").strip()
    return Path(value) if value else None


# --- the manifest ---------------------------------------------------------------


def read_manifest(directory: Path) -> list[str]:
    """The packages the volume was asked for, in the order they were added."""
    path = directory / MANIFEST
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and line[0] != "#"]


def write_manifest(directory: Path, specs: Iterable[str]) -> None:
    """Replace the manifest with these packages."""
    directory.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{spec}\n" for spec in specs)
    header = (
        "# The plugins of this installation, as they were asked for.\n"
        "# Written by `bricklogger plugins add`; laid down again by the\n"
        "# container when the image changes. See docs/docker.md.\n"
    )
    (directory / MANIFEST).write_text(header + body, encoding="utf-8")


def remember(directory: Path, packages: Sequence[str]) -> None:
    """Record the packages, one line per distribution: a package installed
    again under another version replaces its line rather than adding one."""
    from bricklogger.ops.environment import distribution_name

    specs = read_manifest(directory)
    for package in packages:
        name = distribution_name(package)
        kept = [
            spec
            for spec in specs
            if name is None or distribution_name(spec) != name or spec == package
        ]
        specs = [spec for spec in kept if spec != package] + [package]
    write_manifest(directory, specs)


def forget(directory: Path, distribution: str) -> None:
    """Drop the distribution's line from the manifest."""
    from bricklogger.ops.environment import distribution_name

    name = distribution_name(distribution)
    specs = [
        spec for spec in read_manifest(directory) if distribution_name(spec) != name
    ]
    write_manifest(directory, specs)


# --- the stamp ------------------------------------------------------------------


def current_stamp() -> dict[str, str]:
    """What the packages in the volume would be installed against now."""
    try:
        installed = version("bricklogger")
    except PackageNotFoundError:  # pragma: no cover - always installed in practice
        installed = "unknown"
    return {
        "bricklogger": installed,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "platform": sysconfig.get_platform(),
    }


def read_stamp(directory: Path) -> dict[str, str] | None:
    """What the volume was last written against, when it says."""
    path = directory / STAMP
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def write_stamp(directory: Path, stamp: dict[str, str] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    body = json.dumps(stamp if stamp is not None else current_stamp(), indent=2)
    (directory / STAMP).write_text(body + "\n", encoding="utf-8")


# --- laying the manifest down again ----------------------------------------------


def reconcile(
    *,
    directory: Path | None = None,
    environment: Environment | None = None,
    run: Runner | None = None,
    log: Log | None = None,
) -> int:
    """Install the manifest into the volume again when the image has changed.

    The manifest is laid down in its order, each package resolved with the
    plugins laid down before it, so of two that cannot live together the later
    one is left out.

    Returns the number of packages that could not be installed. Nothing is
    raised: a plugin that cannot be laid down must not keep the daemon from
    starting — it shows as failed in the catalogue, like any other plugin that
    cannot be loaded.
    """
    from bricklogger.ops.environment import add_plugins
    from bricklogger.ops.errors import OperationError

    say = log if log is not None else _say
    found = directory if directory is not None else plugin_directory()
    if found is None:
        return 0
    found.mkdir(parents=True, exist_ok=True)
    wanted = current_stamp()
    if read_stamp(found) == wanted:
        return 0
    specs = read_manifest(found)
    if not specs:
        write_stamp(found, wanted)
        return 0
    with _locked(found):
        if read_stamp(found) == wanted:  # another container did the work
            return 0
        say(
            f"the plugin volume was built against {_describe(read_stamp(found))}, "
            f"this image is {_describe(wanted)}; installing its plugins again"
        )
        _clear(found)
        failed = 0
        for spec in specs:
            try:
                add_plugins([spec], environment=environment, run=run, remember=False)
            except OperationError as exc:
                failed += 1
                say(f"the plugin {spec} could not be installed: {exc.message}")
            else:
                say(f"installed {spec}")
        if failed:
            say(
                f"{failed} of {len(specs)} plugins could not be installed; the daemon "
                "starts without them, and `bricklogger plugins` says why"
            )
            return failed
        write_stamp(found, wanted)
    return 0


def _describe(stamp: dict[str, str] | None) -> str:
    if not stamp:
        return "an unknown version"
    return (
        f"bricklogger {stamp.get('bricklogger', '?')} "
        f"on Python {stamp.get('python', '?')}"
    )


def _clear(directory: Path) -> None:
    """Empty the volume of packages, keeping the manifest, stamp and lock.

    The packages were built against another Python or another contract version,
    so they are removed rather than installed over.
    """
    for entry in directory.iterdir():
        if entry.name in KEEP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold the volume while it is written, where the platform can: containers
    starting together must not install over each other."""
    if sys.platform == "win32":  # pragma: no cover - no flock, and no containers
        yield
        return
    import fcntl

    path = directory / LOCK
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _say(message: str) -> None:
    print(f"bricklogger: {message}", file=sys.stderr, flush=True)


def main() -> int:
    """The container's entrypoint runs this before the command it was given."""
    try:
        reconcile()
    except OSError as exc:  # pragma: no cover - a volume that cannot be read
        _say(f"the plugin volume could not be read: {exc}")
    return 0


if __name__ == "__main__":  # pragma: no cover - run as a module by the entrypoint
    raise SystemExit(main())
