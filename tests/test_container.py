"""The container: the plugin volume in the code, and the image, the entrypoint,
the compose file and the workflow that carry it.

The three files are not Python, so what a test can hold them to is their
syntax, the paths and commands the documentation promises, and that the compose
file printed on the Docker page is the one in the repository.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from bricklogger.ops.environment import Environment, add_plugins, remove_plugin
from bricklogger.ops.plugin_volume import (
    MANIFEST,
    STAMP,
    current_stamp,
    forget,
    plugin_directory,
    read_manifest,
    read_stamp,
    reconcile,
    remember,
    write_manifest,
    write_stamp,
)
from bricklogger.sdk.registry import PluginRegistry
from tests.fakes import (
    FAKE_DESTINATION,
    FAKE_SOURCE,
    in_the_same_tick,
    write_distribution,
)
from tests.support import INVOCATION, cli_has

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
COMPOSE = ROOT / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PAGE = ROOT / "src" / "bricklogger" / "docs" / "docker.md"

IMAGE = "ghcr.io/cx1-aps/bricklogger"
PLUGIN_DIR = "/var/lib/bricklogger/plugins"


# --- the image, the entrypoint and the compose file -----------------------------


def shell() -> str:
    found = shutil.which("sh")
    if found is None:
        pytest.skip("no POSIX shell on this machine")
    return found


def test_the_entrypoint_is_valid_posix_shell() -> None:
    subprocess.run([shell(), "-n", str(ENTRYPOINT)], check=True)


def test_the_entrypoint_runs_the_command_it_was_given() -> None:
    """Without the exec the daemon would run as a child of the shell, and the
    SIGTERM Docker sends on stop would go to the wrong process."""
    assert ENTRYPOINT.read_text(encoding="utf-8").rstrip().endswith('exec "$@"')


@pytest.mark.skipif(os.getuid() == 0, reason="root writes where it likes")
def test_the_entrypoint_reaches_the_command_on_a_configuration_it_cannot_write(
    tmp_path: Path,
) -> None:
    """A read-only configuration directory is a mount the operator got wrong,
    and the daemon still has to start and say so. `: >file` took the shell
    down with it instead, and the container restarted for ever in silence."""
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "occupied.yaml").write_text("", encoding="utf-8")
    config_dir.chmod(0o500)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("bricklogger", "python"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        stub.chmod(0o755)
    try:
        result = subprocess.run(
            [shell(), str(ENTRYPOINT), "echo", "the command ran"],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{stubs}:/usr/bin:/bin",
                "BRICKLOGGER_CONFIG_DIR": str(config_dir),
            },
        )
    finally:
        config_dir.chmod(0o700)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "the command ran"


def test_every_command_the_entrypoint_names_exists_in_the_cli() -> None:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    named = {tuple(match.group(1).split()) for match in INVOCATION.finditer(text)}
    for command in sorted(named):
        assert cli_has(command), (
            f"the entrypoint runs `bricklogger {' '.join(command)}`, "
            "which the CLI does not have"
        )


def test_the_first_start_is_the_daemons_alone() -> None:
    """The web interface and the MCP server start from the same entrypoint;
    only the daemon's container writes the configuration and the plugins."""
    import importlib

    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert '[ "$3" = "run" ]' in text and '[ "$2" = "daemon" ]' in text
    assert "python -m bricklogger.ops.first_start" in text
    assert text.index("bricklogger.ops.first_start") < text.index(
        '"$CONFIG_DIR/env"'
    ), "before the env file exists, which would make the volume look used"
    assert hasattr(importlib.import_module("bricklogger.ops.first_start"), "main")


def test_the_image_lays_out_what_the_documentation_promises() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for path in (
        "/opt/bricklogger/venv",
        "/opt/bricklogger/bin/uv",
        "/etc/bricklogger",
    ):
        assert path in text, path
    assert f"BRICKLOGGER_PLUGIN_DIR={PLUGIN_DIR}" in text
    assert "BRICKLOGGER_CONFIG_DIR=/etc/bricklogger" in text
    assert "USER bricklogger" in text, "the container does not run as root"
    assert "EXPOSE 8420 8421 8422" in text
    assert 'CMD ["bricklogger", "daemon", "run"]' in text, "the default is the daemon"


def test_the_plugin_directory_is_on_the_path_after_the_image() -> None:
    """A .pth file is appended to sys.path; PYTHONPATH would come before the
    image's own packages and let a plugin shadow them."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "bricklogger-plugins.pth" in text
    assert "PYTHONPATH=" not in text, "the .pth file is the way, not the variable"


def compose_of(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    return data


def test_the_compose_file_runs_the_database_and_the_three_processes() -> None:
    data = compose_of(COMPOSE.read_text(encoding="utf-8"))
    services = data["services"]
    assert data["name"] == "bricklogger", "the stack names itself, not its directory"
    assert set(services) == {"db", "daemon", "web", "mcp"}
    assert services["daemon"]["command"] == ["bricklogger", "daemon", "run"]
    assert services["web"]["command"] == ["bricklogger", "serve"]
    assert services["mcp"]["command"] == ["bricklogger", "mcp", "serve", "--http"]
    for name, service in services.items():
        assert "profiles" not in service, f"{name} starts with the others"
        assert service["network_mode"] == "host", name
        if name != "db":
            assert service["image"].startswith(f"{IMAGE}:"), name
            assert f"plugins:{PLUGIN_DIR}" in service["volumes"], name
    assert set(data["volumes"]) == {"config", "data", "plugins", "db"}


def test_the_database_is_prepared_once_and_waited_for() -> None:
    data = compose_of(COMPOSE.read_text(encoding="utf-8"))
    db, daemon = data["services"]["db"], data["services"]["daemon"]
    assert db["image"] == "timescale/timescaledb-ha:pg18", "named by its major"
    assert "db:/home/postgres/pgdata" in db["volumes"], "the HA image's data"
    assert "listen_addresses=127.0.0.1" in db["command"], "loopback alone"
    assert db["environment"]["POSTGRES_INITDB_ARGS"] == "--auth-host=scram-sha-256", (
        "the image trusts loopback, which on the host's network is every process"
    )
    assert db["environment"]["POSTGRES_PASSWORD"].endswith(
        ":?set POSTGRES_PASSWORD in .env}"
    )
    assert (
        daemon["environment"]["BRICKLOGGER_DB_PASSWORD"]
        == "${BRICKLOGGER_DB_PASSWORD:-}"
    )
    assert daemon["depends_on"] == {"db": {"condition": "service_healthy"}}
    for name in ("web", "mcp"):
        assert data["services"][name]["depends_on"] == {
            "daemon": {"condition": "service_healthy"}
        }
    script = data["configs"]["bricklogger-role"]["content"]
    assert "SET ROLE" in script and "CREATE EXTENSION" in script, (
        "the role owns the extension, so the destination can update it"
    )
    assert "$$BRICKLOGGER_DB_PASSWORD" in script, "the shell's variable, not compose's"


def test_no_published_ports() -> None:
    """Host networking binds the ports itself, and `ports:` would say otherwise."""
    services = compose_of(COMPOSE.read_text(encoding="utf-8"))["services"]
    for name, service in services.items():
        assert "ports" not in service, name


def documented_compose() -> dict[str, Any]:
    """The whole file the page prints, not the snippets beside it: it is the
    one block that declares both the services and the volumes."""
    blocks = re.findall(r"```yaml\n(.*?)```", PAGE.read_text(encoding="utf-8"), re.S)
    for block in blocks:
        data = yaml.safe_load(block)
        if isinstance(data, dict) and {"services", "volumes"} <= set(data):
            return data
    raise AssertionError("the Docker page prints no compose file")


def test_the_documented_compose_file_is_the_one_in_the_repository() -> None:
    """The page shows the file to copy; a change in one that misses the other
    is a change the reader cannot reproduce."""
    assert documented_compose() == compose_of(COMPOSE.read_text(encoding="utf-8"))


def test_the_workflow_publishes_what_the_documentation_names() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert data["env"]["IMAGE"] == IMAGE
    # `on` is YAML's true, since the 1.1 booleans are what safe_load reads.
    triggers = data[True]
    assert triggers == {"push": {"tags": ["v*"]}}, "released versions only"
    jobs = data["jobs"]
    assert jobs["build"]["needs"] == "test" and jobs["pypi"]["needs"] == "build"
    image = jobs["image"]
    assert image["needs"] == "pypi", "nothing is published before the suite passes"
    assert image["permissions"]["packages"] == "write"
    steps = image["steps"]
    meta = next(step for step in steps if step.get("id") == "meta")
    tags = meta["with"]["tags"]
    assert "type=semver,pattern={{version}}" in tags
    assert "type=semver,pattern={{major}}.{{minor}}" in tags
    assert "value=latest" in tags
    assert "edge" not in tags
    build = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("docker/build-push")
    )
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert build["with"]["push"] is True


# --- the plugin volume ----------------------------------------------------------


class Recorder:
    """A stand-in for running uv: remembers the commands, answers as told."""

    def __init__(self, freezes: dict[str, str] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.freezes = freezes or {}
        self.fail: str | None = None
        # What an install was given in files that are gone when it returns.
        self.handed: list[dict[str, Any]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if self.fail is not None and self.fail in " ".join(command):
            return subprocess.CompletedProcess(list(command), 1, "", "no such package")
        stdout = ""
        if "freeze" in command:
            key = "path" if "--path" in command else "python"
            stdout = self.freezes.get(key, "")
        elif "list" in command:
            stdout = self.freezes.get("list", "[]")
        elif list(command[1:3]) == ["pip", "install"]:
            self.handed.append(_handed(command))
        return subprocess.CompletedProcess(list(command), 0, stdout, "")

    def installs(self) -> list[list[str]]:
        return [c for c in self.commands if c[1:3] == ["pip", "install"]]

    def uninstalls(self) -> list[list[str]]:
        return [c for c in self.commands if c[1:3] == ["pip", "uninstall"]]


def _handed(command: Sequence[str]) -> dict[str, Any]:
    """The constraint file and the stand-in wheels an install command names."""
    found: dict[str, Any] = {}
    args = list(command)
    if "--constraint" in args:
        found["constraints"] = Path(args[args.index("--constraint") + 1]).read_text()
    if "--find-links" in args:
        wheels = sorted(Path(args[args.index("--find-links") + 1]).iterdir())
        found["stand_ins"] = [wheel.name for wheel in wheels]
        found["metadata"] = {
            wheel.name: zipfile.ZipFile(wheel)
            .read(f"{wheel.name.rsplit('-py3', 1)[0]}.dist-info/METADATA")
            .decode()
            for wheel in wheels
        }
    return found


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    return directory


@pytest.fixture
def container(tmp_path: Path, volume: Path) -> Environment:
    uv = tmp_path / "bin" / "uv"
    python = tmp_path / "venv" / "bin" / "python"
    site = tmp_path / "venv" / "lib" / "site-packages"
    for path in (uv, python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    site.mkdir(parents=True)
    return Environment(uv=uv, python=python, site_packages=site, target=volume)


def test_the_plugin_directory_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRICKLOGGER_PLUGIN_DIR", raising=False)
    assert plugin_directory() is None
    monkeypatch.setenv("BRICKLOGGER_PLUGIN_DIR", PLUGIN_DIR)
    assert plugin_directory() == Path(PLUGIN_DIR)
    monkeypatch.setenv("BRICKLOGGER_PLUGIN_DIR", "   ")
    assert plugin_directory() is None


def test_add_installs_into_the_volume_constrained_to_the_image(
    container: Environment, volume: Path
) -> None:
    run = Recorder(
        freezes={
            "python": "bricklogger==0.1.0\npydantic==2.7.0\n",
            "path": "pydantic==2.7.0\nbricklogger-homeassistant==0.2.0\n",
        }
    )
    added = add_plugins(
        ["bricklogger-homeassistant==0.2.0"], environment=container, run=run
    )

    install = run.installs()[0]
    assert install[:3] == [str(container.uv), "pip", "install"]
    assert "--target" in install and str(volume) in install
    assert "--constraint" in install, "the image's versions bound the resolution"
    assert install[-1] == "bricklogger-homeassistant==0.2.0"
    assert added.command == tuple(install)

    # What the image already provides at the same version is removed again.
    assert run.uninstalls() == [
        [str(container.uv), "pip", "uninstall", "--target", str(volume), "pydantic"]
    ]
    assert read_manifest(volume) == ["bricklogger-homeassistant==0.2.0"]


def test_what_the_image_installed_from_a_file_gets_a_stand_in(
    container: Environment, volume: Path
) -> None:
    """The image installs Bricklogger from its wheel, so no index can supply
    it, and a plugin's dependency on it could not be resolved into the volume.
    The resolver is handed a wheel with the metadata alone, at the image's
    version, and the copy it installs goes with the other duplicates."""
    run = Recorder(
        freezes={
            "python": (
                "bricklogger @ file:///tmp/wheel/bricklogger-0.1.1-py3-none-any.whl\n"
                "pydantic==2.7.0\n"
            ),
            "list": (
                '[{"name": "bricklogger", "version": "0.1.1"}, '
                '{"name": "pydantic", "version": "2.7.0"}]'
            ),
            "path": "bricklogger==0.1.1\nbricklogger-fake==1.0\ntomli-w==1.2.0\n",
        }
    )
    add_plugins(["bricklogger-fake==1.0"], environment=container, run=run)

    install = run.installs()[0]
    assert "--find-links" in install and "--constraint" in install
    handed = run.handed[0]
    assert handed["stand_ins"] == ["bricklogger-0.1.1-py3-none-any.whl"]
    metadata = handed["metadata"]["bricklogger-0.1.1-py3-none-any.whl"]
    assert "Name: bricklogger" in metadata and "Version: 0.1.1" in metadata
    assert "bricklogger==0.1.1" in handed["constraints"], "pinned to the image"
    assert run.uninstalls() == [
        [str(container.uv), "pip", "uninstall", "--target", str(volume), "bricklogger"]
    ], "the stand-in's copy is pruned; the plugin and its dependency stay"


def test_the_volume_keeps_no_copy_of_the_environments_own_packages(
    container: Environment, volume: Path
) -> None:
    """A plugin depends on bricklogger itself; a second copy of it and its
    dependencies in the volume would be hundreds of megabytes and shadowed."""
    run = Recorder(
        freezes={
            "python": "bricklogger==0.1.0\nrdflib==7.0.0\n",
            "path": "bricklogger==0.1.0\nrdflib==7.0.0\nbricklogger-fake==1.0\n",
        }
    )
    add_plugins(["bricklogger-fake==1.0"], environment=container, run=run)
    removed = run.uninstalls()[0]
    assert removed[-2:] == ["bricklogger", "rdflib"]


def test_add_holds_the_volumes_other_plugins(
    container: Environment, volume: Path
) -> None:
    """A container's plugins are the volume's, so those are the ones held
    while a package is resolved; the image's built-in types belong to
    bricklogger, and a library in the volume is nobody's plugin."""
    sources = {"bricklogger.sources": "fake-source = f:S"}
    write_distribution(volume, "bricklogger-fake", "1.0", entry_points=sources)
    write_distribution(volume, "websockets", "17.1")
    write_distribution(
        container.site_packages, "bricklogger", "0.1.0", entry_points=sources
    )
    run = Recorder(freezes={"python": "bricklogger==0.1.0\n"})

    add_plugins(["bricklogger-other"], environment=container, run=run)

    install = run.installs()[0]
    assert install[-2:] == ["bricklogger-fake==1.0", "bricklogger-other"]
    assert "websockets==17.1" not in install and "bricklogger==0.1.0" not in install
    assert install[install.index("--upgrade-package") + 1] == "bricklogger-other"
    assert "--upgrade" not in install, "the held plugins' libraries stay where they are"


def test_a_second_version_replaces_its_line_in_the_manifest(
    container: Environment, volume: Path
) -> None:
    run = Recorder()
    add_plugins(["bricklogger-fake==1.0"], environment=container, run=run)
    add_plugins(["bricklogger-fake==1.1"], environment=container, run=run)
    add_plugins(["bricklogger-other"], environment=container, run=run)
    assert read_manifest(volume) == ["bricklogger-fake==1.1", "bricklogger-other"]


def test_laying_the_manifest_down_again_does_not_record_it_twice(
    container: Environment, volume: Path
) -> None:
    write_manifest(volume, ["bricklogger-fake==1.0"])
    add_plugins(
        ["bricklogger-fake==1.0"], environment=container, run=Recorder(), remember=False
    )
    assert read_manifest(volume) == ["bricklogger-fake==1.0"]


REGISTRY = PluginRegistry(
    sources={"fake-source": FAKE_SOURCE},
    destinations={"fake-destination": FAKE_DESTINATION},
    versions={"fake-source": "0.4.0"},
    failures={},
    distributions={"fake-source": "bricklogger-fake"},
)


def test_remove_uninstalls_from_the_volume_and_forgets_it(
    container: Environment, volume: Path, tmp_path: Path
) -> None:
    write_manifest(volume, ["bricklogger-fake==1.0", "bricklogger-other"])
    config = tmp_path / "config"
    config.mkdir()
    run = Recorder()
    removed = remove_plugin(
        "fake-source",
        config_dir=config,
        registry=REGISTRY,
        environment=container,
        run=run,
    )
    assert removed.command == (
        str(container.uv),
        "pip",
        "uninstall",
        "--target",
        str(volume),
        "bricklogger-fake",
    )
    assert read_manifest(volume) == ["bricklogger-other"]


def test_remove_takes_the_volumes_orphans_but_not_the_images_packages(
    container: Environment, volume: Path, tmp_path: Path
) -> None:
    """What the plugin needed and only the volume holds goes with it; a
    requirement the image provides is not in the volume and is left alone."""
    sources = "bricklogger.sources"
    write_distribution(
        volume,
        "bricklogger-fake",
        "1.0",
        requires=["bricklogger>=0.1", "httpx>=0.27", "websockets>=13"],
        entry_points={sources: "fake-source = f:S"},
    )
    write_distribution(volume, "websockets", "17.1")
    write_distribution(
        container.site_packages,
        "bricklogger",
        "0.1.0",
        requires=["httpx"],
        entry_points={sources: "bacnet-ip = b:S"},
    )
    write_distribution(container.site_packages, "httpx", "0.27.0")
    write_manifest(volume, ["bricklogger-fake==1.0"])
    config = tmp_path / "config"
    config.mkdir()
    run = Recorder()

    removed = remove_plugin(
        "fake-source",
        config_dir=config,
        registry=REGISTRY,
        environment=container,
        run=run,
    )

    assert removed.dependencies == ("websockets",)
    assert run.uninstalls() == [
        [
            str(container.uv),
            "pip",
            "uninstall",
            "--target",
            str(volume),
            "bricklogger-fake",
            "websockets",
        ]
    ]
    assert read_manifest(volume) == []


def test_the_manifest_survives_being_written_and_read(volume: Path) -> None:
    remember(volume, ["bricklogger-fake==1.0"])
    text = (volume / MANIFEST).read_text(encoding="utf-8")
    assert text.startswith("#"), "the file says what it is"
    assert read_manifest(volume) == ["bricklogger-fake==1.0"]
    forget(volume, "bricklogger-fake")
    assert read_manifest(volume) == []


# --- laying the manifest down again ---------------------------------------------


def test_an_unchanged_image_touches_nothing(
    container: Environment, volume: Path
) -> None:
    write_manifest(volume, ["bricklogger-fake==1.0"])
    write_stamp(volume)
    (volume / "bricklogger_fake").mkdir()
    run = Recorder()
    assert (
        reconcile(directory=volume, environment=container, run=run, log=lambda m: None)
        == 0
    )
    assert run.commands == []
    assert (volume / "bricklogger_fake").is_dir(), "nothing was swept away"


def test_an_empty_manifest_only_stamps_the_volume(
    container: Environment, volume: Path
) -> None:
    run = Recorder()
    assert (
        reconcile(directory=volume, environment=container, run=run, log=lambda m: None)
        == 0
    )
    assert run.commands == []
    assert read_stamp(volume) == current_stamp()


def test_another_image_lays_the_manifest_down_again(
    container: Environment, volume: Path
) -> None:
    write_manifest(volume, ["bricklogger-fake==1.0", "bricklogger-other"])
    write_stamp(volume, {"bricklogger": "0.0.1", "python": "3.11", "platform": "linux"})
    stale = volume / "bricklogger_fake"
    stale.mkdir()
    (stale / "__init__.py").write_text("")
    said: list[str] = []
    run = Recorder()

    failed = reconcile(
        directory=volume, environment=container, run=run, log=said.append
    )

    assert failed == 0
    assert not stale.exists(), "packages built against another Python are removed"
    installed = [command[-1] for command in run.installs()]
    assert installed == ["bricklogger-fake==1.0", "bricklogger-other"]
    assert read_manifest(volume) == ["bricklogger-fake==1.0", "bricklogger-other"]
    assert read_stamp(volume) == current_stamp()
    assert any("installing its plugins again" in line for line in said)


def test_the_manifest_is_laid_down_each_with_those_before_it(
    container: Environment, volume: Path
) -> None:
    """Of two plugins that cannot live together, the later in the manifest is
    the one left out: each is resolved with the plugins already laid down —
    also when the one before landed within the tick of the last look at the
    volume, which a fake install always does and a real one can."""
    write_manifest(volume, ["bricklogger-fake==1.0", "bricklogger-other"])
    write_stamp(volume, {"bricklogger": "0.0.1", "python": "3.11", "platform": "linux"})
    recorder = Recorder()

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        completed = recorder(command)
        if list(command[1:3]) == [
            "pip",
            "install",
        ]:  # as uv would: it lands in the volume
            name, _, version = command[-1].partition("==")
            with in_the_same_tick(volume):
                write_distribution(
                    volume,
                    name,
                    version or "0.9",
                    entry_points={"bricklogger.sources": f"{name} = {name}:S"},
                )
        return completed

    reconcile(directory=volume, environment=container, run=run, log=lambda _: None)

    first, second = recorder.installs()
    assert first[-1] == "bricklogger-fake==1.0" and "==" not in " ".join(first[:-1])
    assert second[-2:] == ["bricklogger-fake==1.0", "bricklogger-other"]


def test_a_plugin_that_cannot_be_installed_leaves_the_rest_running(
    container: Environment, volume: Path
) -> None:
    write_manifest(volume, ["bricklogger-fake==1.0", "bricklogger-gone"])
    write_stamp(volume, {"bricklogger": "0.0.1", "python": "3.11", "platform": "linux"})
    said: list[str] = []
    run = Recorder()
    run.fail = "bricklogger-gone"

    failed = reconcile(
        directory=volume, environment=container, run=run, log=said.append
    )

    assert failed == 1
    assert [command[-1] for command in run.installs()] == [
        "bricklogger-fake==1.0",
        "bricklogger-gone",
    ]
    assert any("could not be installed" in line for line in said)
    assert read_stamp(volume) != current_stamp(), "the next start tries again"


def test_the_stamp_names_the_versions_the_volume_was_built_against(
    volume: Path,
) -> None:
    write_stamp(volume)
    stamp = json.loads((volume / STAMP).read_text(encoding="utf-8"))
    assert set(stamp) == {"bricklogger", "python", "platform"}
    assert stamp["python"].count(".") == 1, "the minor version, which is what breaks"


def test_no_volume_means_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRICKLOGGER_PLUGIN_DIR", raising=False)
    assert reconcile(log=lambda m: None) == 0


def test_the_lock_is_not_the_one_uv_uses(volume: Path) -> None:
    """uv locks `.lock` in a target directory. Holding that file while calling
    uv made the install wait for us forever, and the container never started."""
    from bricklogger.ops.plugin_volume import LOCK

    assert LOCK != ".lock"


def test_the_first_start_leaves_a_configuration_the_daemon_can_start_on(
    tmp_path: Path,
) -> None:
    """The examples name another building's BACnet address and a database whose
    password nobody has set, and the daemon refuses a configuration it cannot
    resolve, so the first start writes them commented out; without that the
    first `docker compose up -d` restarted for ever."""
    from bricklogger.ops.first_start import write_configuration

    said: list[str] = []
    write_configuration(tmp_path, {}, said.append)
    for name in ("sources", "destinations"):
        text = (tmp_path / f"{name}.yaml").read_text(encoding="utf-8")
        assert yaml.safe_load(text) is None, f"{name}.yaml configures an instance"
    assert "commented examples" in said[0]


def test_the_first_start_finds_its_database_in_the_environment(
    tmp_path: Path,
) -> None:
    from bricklogger.config import validate_configuration
    from bricklogger.ops.first_start import write_configuration

    env = {"BRICKLOGGER_DB_PASSWORD": "secret", "BRICKLOGGER_DB_PORT": "5544"}
    write_configuration(tmp_path, env, lambda _: None)
    data = yaml.safe_load((tmp_path / "destinations.yaml").read_text("utf-8"))
    assert data == {
        "tsdb": {
            "type": "timescaledb",
            "dsn": "postgres://bricklogger@localhost:5544/bricklogger",
            "password": "${BRICKLOGGER_DB_PASSWORD}",
        }
    }, "the password named, not written"
    assert validate_configuration(
        tmp_path, PluginRegistry.from_entry_points(), env
    ).valid


def test_the_first_start_leaves_a_volume_with_anything_in_it(tmp_path: Path) -> None:
    from bricklogger.ops.first_start import write_configuration

    (tmp_path / "rules.yaml").write_text("[]\n", encoding="utf-8")
    write_configuration(tmp_path, {"BRICKLOGGER_DB_PASSWORD": "x"}, lambda _: None)
    assert not (tmp_path / "destinations.yaml").exists()


@pytest.mark.parametrize(
    ("value", "packages"),
    [
        ("", []),
        ("bricklogger-ibos", ["bricklogger-ibos"]),
        ("a b>=0.2,<0.3", ["a", "b>=0.2,<0.3"]),
        ("a\nname @ https://x/y.whl\n# a comment\n", ["a", "name @ https://x/y.whl"]),
    ],
)
def test_the_plugins_variable_is_split_as_documented(
    value: str, packages: list[str]
) -> None:
    from bricklogger.ops.first_start import packages_of

    assert packages_of(value) == packages


def test_the_plugins_variable_is_read_only_into_a_volume_never_asked(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bricklogger.ops import environment
    from bricklogger.ops.first_start import install_plugins

    monkeypatch.setenv("BRICKLOGGER_PLUGIN_DIR", str(volume))
    asked: list[list[str]] = []

    def fake_add(packages: Sequence[str], **_: Any) -> None:
        asked.append(list(packages))
        remember(volume, packages)

    monkeypatch.setattr(environment, "add_plugins", fake_add)
    env = {"BRICKLOGGER_PLUGINS": "bricklogger-ibos bricklogger-x"}
    assert install_plugins(env, lambda _: None) == 0
    assert asked == [["bricklogger-ibos"], ["bricklogger-x"]]
    assert read_manifest(volume) == ["bricklogger-ibos", "bricklogger-x"]
    assert read_stamp(volume) == current_stamp(), "not laid down again at once"

    asked.clear()
    install_plugins({"BRICKLOGGER_PLUGINS": "bricklogger-y"}, lambda _: None)
    assert asked == [], "the manifest counts from then on"
