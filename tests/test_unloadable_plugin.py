"""A plugin that cannot be loaded stops nothing: validation warns, the daemon
runs the other instances and shows the failed one, and the catalogue, the API
and the CLI carry the error."""

from __future__ import annotations

import json
from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.config import ConfigIssue, validate_configuration
from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon
from bricklogger.sdk import registry as registry_module
from bricklogger.sdk.registry import PluginFailure, PluginRegistry
from tests.fakes import FAKE_DESTINATION, FAKE_SOURCE
from tests.support import DESTINATIONS, SOURCES, make_config, wait_for

ERROR = "plugin 'broken' could not be loaded: No module named 'nothing_installed'"
BROKEN = PluginFailure("broken", "source", "0.3.0", ERROR, "bricklogger-broken")
REGISTRY = PluginRegistry(
    sources={"fake-source": FAKE_SOURCE},
    destinations={"fake-destination": FAKE_DESTINATION},
    versions={},
    failures={"broken": BROKEN},
    distributions={"broken": "bricklogger-broken"},
)
WITH_BROKEN = SOURCES + "home:\n  type: broken\n  anything: goes\n"


def test_validation_warns_and_the_configuration_stays_valid(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(WITH_BROKEN)
    (tmp_path / "destinations.yaml").write_text(DESTINATIONS)
    result = validate_configuration(tmp_path, REGISTRY, {})
    assert result.valid and result.errors == []
    assert result.warnings == [
        ConfigIssue(
            "sources",
            f"the plugin could not be loaded, so the instance will be failed: {ERROR}",
            "home",
            "type",
        )
    ]


def test_the_daemon_runs_on_and_shows_the_instance_failed(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=WITH_BROKEN, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(lambda: any(s["state"] == "running" for s in daemon.status_sources()))
        rows = {row["name"]: row for row in daemon.status_sources()}
        assert rows["fake_a"]["state"] == "running"
        assert rows["home"]["state"] == "failed"
        assert rows["home"]["last_error"] == ERROR
        assert rows["home"]["type"] == "broken" and rows["home"]["resources"] == []
        assert daemon.health() == "degraded"

        warnings = {
            (w["code"], w["subject"]): w["message"] for w in daemon.status_warnings()
        }
        assert warnings[("instance_failed", "home")] == f"failed: {ERROR}"

        summary = {item["name"]: item for item in daemon.status()["instances"]}
        assert summary["home"] == {
            "name": "home",
            "role": "source",
            "type": "broken",
            "state": "failed",
        }

        client = TestClient(create_app(daemon))
        listed = {row["type"]: row for row in client.get("/v1/plugins").json()}
        assert listed["broken"]["error"] == ERROR
        assert listed["broken"]["instances"] == ["home"]
        assert listed["broken"]["version"] == "0.3.0"
        assert listed["broken"]["description"] is None
        assert listed["fake-source"]["error"] is None
        assert client.get("/v1/plugins/broken").json() == {
            "type": "broken",
            "role": "source",
            "version": "0.3.0",
            "description": None,
            "instances": ["home"],
            "error": ERROR,
        }
        started = client.post("/v1/plugins/broken/instances/home/start")
        assert started.status_code == 409
        assert "could not be loaded" in started.json()["detail"]

        # The instance leaves the configuration: nothing of it stays behind.
        (config_dir / "sources.yaml").write_text(SOURCES)
        assert daemon.reload().valid
        assert "home" not in {row["name"] for row in daemon.status_sources()}
        assert not any(w["code"] == "instance_failed" for w in daemon.status_warnings())
        assert daemon.health() == "ok"
    finally:
        daemon.stop()


def test_a_type_that_is_not_installed_fails_its_instance_and_nothing_else(
    tmp_path: Path, template: Path
) -> None:
    """The upgrade to 0.2 left `type: ibos` behind without the plugin; the
    daemon starts, and the instance says what to add."""
    missing = SOURCES + "home:\n  type: ibos\n  token: x\n"
    config_dir = make_config(
        tmp_path, template, sources=missing, destinations=DESTINATIONS
    )
    expected = (
        "the type 'ibos' is not installed; add its plugin with "
        "`bricklogger plugins add`"
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(lambda: any(s["state"] == "running" for s in daemon.status_sources()))
        rows = {row["name"]: row for row in daemon.status_sources()}
        assert rows["fake_a"]["state"] == "running"
        assert rows["home"]["state"] == "failed"
        assert rows["home"]["last_error"] == expected
        assert daemon.health() == "degraded"
        warnings = {
            (w["code"], w["subject"]): w["message"] for w in daemon.status_warnings()
        }
        assert warnings[("instance_failed", "home")] == f"failed: {expected}"

        # A written change may leave it alone, but not add another.
        kept = daemon.replace_config_file("sources", missing + "more:\n  type: ibso\n")
        assert not kept.valid
        assert [issue.subject for issue in kept.errors] == ["more"]
    finally:
        daemon.stop()


def test_a_plugin_cannot_take_a_word_of_update_as_its_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = metadata.entry_points

    def patched(group: str) -> list[EntryPoint]:
        found = list(real(group=group))
        if group == registry_module.SOURCES_GROUP:
            found.append(EntryPoint("core", "tests.broken_plugin:SOURCE", group))
        return found

    monkeypatch.setattr(registry_module, "entry_points", patched)
    loaded = PluginRegistry.from_entry_points()
    assert "core" not in loaded.sources
    failure = loaded.failure_of("core")
    assert failure is not None
    assert "words of `bricklogger update`" in failure.error


def test_the_cli_shows_a_plugin_that_did_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = metadata.entry_points

    def patched(group: str) -> list[EntryPoint]:
        found = list(real(group=group))
        if group == registry_module.SOURCES_GROUP:
            found.append(EntryPoint("broken", "tests.broken_plugin:SOURCE", group))
        return found

    monkeypatch.setattr(registry_module, "entry_points", patched)
    runner = CliRunner()
    base = ["--config-dir", str(tmp_path), "plugins"]

    listed = runner.invoke(app, base)
    assert listed.exit_code == 0, listed.output
    assert "broken" in listed.output and "failed:" in listed.output

    as_json = runner.invoke(app, [*base, "--json"])
    rows = {row["type"]: row for row in json.loads(as_json.output)}
    assert rows["broken"]["error"].startswith("plugin 'broken' could not be loaded")
    assert rows["broken"]["role"] == "source"
    assert rows["bacnet-ip"]["error"] is None

    described = runner.invoke(app, [*base, "broken"])
    assert described.exit_code == 0, described.output
    lines = described.output.splitlines()
    assert lines[0] == "broken (source, version ?)"
    assert lines[1].startswith("failed: plugin 'broken' could not be loaded")
