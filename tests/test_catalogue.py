"""The catalogue of approved plugins: the file Bricklogger ships, the origin
of an installed plugin, and the catalogue in the CLI and the API. See
``docs/features/plugins.md``, "The catalogue"."""

from __future__ import annotations

import json
from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bricklogger.catalogue import catalogue, catalogue_rows, origin_of
from bricklogger.cli import app
from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon
from bricklogger.sdk import registry as registry_module
from bricklogger.sdk.registry import PluginRegistry
from tests.fakes import FAKE_DESTINATION, FAKE_SOURCE
from tests.support import DESTINATIONS, SOURCES, make_config

runner = CliRunner()


def test_the_shipped_catalogue_names_the_ibos_source() -> None:
    entries = {entry.package: entry for entry in catalogue()}
    ibos = entries["bricklogger-ibos"]
    assert [(t.type, t.role) for t in ibos.types] == [("ibos", "source")]
    assert ibos.description and ibos.publisher and ibos.link.startswith("https://")


@pytest.mark.parametrize(
    ("distribution", "origin"),
    [
        ("bricklogger", "built-in"),
        (None, "built-in"),
        ("bricklogger-ibos", "catalogue"),
        ("Bricklogger_IBOS", "catalogue"),
        ("bricklogger-httpjson", "custom"),
    ],
)
def test_the_origin_is_decided_by_the_package_name_alone(
    distribution: str | None, origin: str
) -> None:
    assert origin_of(distribution) == origin


def test_an_entry_shows_the_version_installed() -> None:
    registry = PluginRegistry(
        sources={},
        versions={"ibos": "0.3.1"},
        distributions={"ibos": "bricklogger_ibos"},
    )
    rows = {row["package"]: row for row in catalogue_rows(registry)}
    assert rows["bricklogger-ibos"]["installed"] == "0.3.1"
    assert rows["bricklogger-ibos"]["types"] == [{"type": "ibos", "role": "source"}]
    nothing = {row["package"]: row for row in catalogue_rows(PluginRegistry())}
    assert nothing["bricklogger-ibos"]["installed"] is None


def test_a_plugin_cannot_take_catalogue_as_its_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = metadata.entry_points

    def patched(group: str) -> list[EntryPoint]:
        found = list(real(group=group))
        if group == registry_module.SOURCES_GROUP:
            found.append(EntryPoint("catalogue", "tests.broken_plugin:SOURCE", group))
        return found

    monkeypatch.setattr(registry_module, "entry_points", patched)
    loaded = PluginRegistry.from_entry_points()
    failure = loaded.failure_of("catalogue")
    assert failure is not None
    assert "`bricklogger plugins`" in failure.error


def test_the_cli_lists_the_catalogue_and_the_origin(tmp_path: Path) -> None:
    base = ["--config-dir", str(tmp_path), "plugins"]
    shown = runner.invoke(app, [*base, "catalogue"])
    assert shown.exit_code == 0, shown.output
    assert "bricklogger-ibos" in shown.output and "plugins add PACKAGE" in shown.output

    as_json = runner.invoke(app, [*base, "catalogue", "--json"])
    rows = {row["package"]: row for row in json.loads(as_json.output)}
    assert rows["bricklogger-ibos"]["installed"] is None

    installed = runner.invoke(app, [*base, "--json"])
    origins = {row["type"]: row["origin"] for row in json.loads(installed.output)}
    assert origins["bacnet-ip"] == "built-in" and origins["timescaledb"] == "built-in"


def test_the_api_serves_the_catalogue_beside_the_installed_plugins(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    registry = PluginRegistry.of(FAKE_SOURCE, FAKE_DESTINATION)
    daemon = Daemon(config_dir, registry=registry, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        listed = client.get("/v1/plugins/catalogue")
        assert listed.status_code == 200
        assert "bricklogger-ibos" in {row["package"] for row in listed.json()}
        rows = client.get("/v1/plugins").json()
        assert {row["origin"] for row in rows} == {"built-in"}
        assert client.get("/v1/plugins/fake-source").status_code == 200
    finally:
        daemon.stop()
