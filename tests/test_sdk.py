"""The plugin SDK: its public surface, the registry's failure path, and the
test kit a plugin outside the repository tests itself with."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

import bricklogger.sdk as sdk
from bricklogger.sdk import registry as registry_module
from bricklogger.sdk.contract import GraphReader
from bricklogger.sdk.registry import PluginRegistry
from bricklogger.sdk.testing import Collector, assigned, graph_from_turtle, run_source
from tests.fakes import FAKE_SOURCE, FakeSourceConfig
from tests.support import EX, INVALID_MODEL, MODEL

BRICK = "https://brickschema.org/schema/Brick#"


def test_the_sdk_exports_everything_a_plugin_needs() -> None:
    expected = {
        "Source",
        "Destination",
        "Observation",
        "PointMetadata",
        "Outcome",
        "AssignedPoint",
        "Sink",
        "StatusChannel",
        "GraphReader",
        "ValueType",
        "NullReason",
        "OutcomeState",
        "InstanceState",
        "VALUE_TYPES",
        "NULL_REASONS",
        "SourceDeclaration",
        "DestinationDeclaration",
        "CollectionMethod",
        "ToolDeclaration",
        "ToolOffer",
        "Vocabulary",
        "Duration",
        "DURATION_HELP",
        "parse_duration",
        "format_duration",
        "expand",
        "UnknownPrefix",
    }
    assert expected <= set(sdk.__all__)
    for name in expected:
        assert hasattr(sdk, name), name


def test_prefixed_names_and_durations_come_from_the_sdk() -> None:
    assert sdk.expand("brick:Point", {"brick": BRICK}) == f"{BRICK}Point"
    assert sdk.expand(f"<{BRICK}Point>", {}) == f"{BRICK}Point"
    with pytest.raises(sdk.UnknownPrefix):
        sdk.expand("nope:Point", {"brick": BRICK})
    assert sdk.parse_duration("5m") == timedelta(minutes=5)
    assert sdk.format_duration(timedelta(hours=1)) == "1h"
    assert "30s" in sdk.DURATION_HELP


def test_the_bacnet_tables_are_one_with_the_bacnet_ip_source() -> None:
    from bricklogger.plugins.bacnet_ip import values
    from bricklogger.sdk import bacnet

    assert values.UNIT_MAP is bacnet.UNIT_MAP
    assert values.ANALOG_TYPES is bacnet.ANALOG_TYPES
    assert bacnet.protocol_unit("degrees-celsius") == bacnet.QUDT + "DEG_C"
    assert bacnet.protocol_unit("no-units") is None
    assert bacnet.protocol_unit("furlongs") == "furlongs"
    assert "analog-input" in bacnet.ANALOG_TYPES
    assert "multi-state-value" in bacnet.MULTISTATE_TYPES


def test_a_plugin_that_does_not_load_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = {
        registry_module.SOURCES_GROUP: [
            EntryPoint("fake-source", "tests.fakes:FAKE_SOURCE", "bricklogger.sources"),
            EntryPoint("broken", "tests.broken_plugin:SOURCE", "bricklogger.sources"),
            EntryPoint(
                "wrong-role", "tests.fakes:FAKE_DESTINATION", "bricklogger.sources"
            ),
            EntryPoint("other-name", "tests.fakes:FAKE_SOURCE", "bricklogger.sources"),
        ],
        registry_module.DESTINATIONS_GROUP: [
            EntryPoint(
                "fake-destination",
                "tests.fakes:FAKE_DESTINATION",
                "bricklogger.destinations",
            ),
        ],
    }
    monkeypatch.setattr(registry_module, "entry_points", lambda group: points[group])

    registry = PluginRegistry.from_entry_points()

    assert set(registry.sources) == {"fake-source"}
    assert set(registry.destinations) == {"fake-destination"}
    assert set(registry.failures) == {"broken", "wrong-role", "other-name"}
    broken = registry.failure_of("broken")
    assert broken is not None and broken.role == "source"
    assert "nothing_installed" in broken.error
    assert "must declare a SourceDeclaration" in registry.failures["wrong-role"].error
    assert "must match" in registry.failures["other-name"].error
    assert registry.role_of("broken") == "source"
    assert registry.role_of("fake-destination") == "destination"
    assert registry.role_of("nope") is None
    assert registry.version_of("broken") is None, "a hand-made entry point has none"
    assert registry.distribution_of("broken") is None
    assert registry.failure_of("fake-source") is None


def test_the_built_in_plugins_come_from_the_bricklogger_distribution() -> None:
    registry = PluginRegistry.from_entry_points()
    assert registry.failures == {}
    assert registry.distribution_of("bacnet-ip") == "bricklogger"
    assert set(registry.types_of("bricklogger")) == {"bacnet-ip", "timescaledb"}


# --- the test kit --------------------------------------------------------------


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> GraphReader:
    return graph_from_turtle(MODEL, tmp_path_factory.mktemp("graph"), vocabularies=())


def test_the_graph_is_built_as_the_daemon_builds_it(graph: GraphReader) -> None:
    assert graph.prefixes["ex"] == EX
    assert graph.prefixes["brick"] == BRICK
    rows = graph.query(f"SELECT ?p WHERE {{ ?p a <{BRICK}Point> }}")
    points = {row["p"].value for row in rows}
    assert f"{EX}SAT" in points, "inferred from the sensor's class, as in the daemon"
    assert f"{EX}AHU_01" not in points


def test_a_model_that_does_not_conform_is_refused(tmp_path: Path) -> None:
    from bricklogger.model import ModelInvalid

    with pytest.raises(ModelInvalid):
        graph_from_turtle(INVALID_MODEL, tmp_path, vocabularies=())


def test_run_source_drives_a_source_and_collects(graph: GraphReader) -> None:
    config = FakeSourceConfig(claims=["SAT"], interval=0.02)
    source = FAKE_SOURCE.create("kit", config, graph)
    assert source.claim() == {f"{EX}SAT"}

    with run_source(source, [assigned(f"{EX}SAT", interval="1s")]) as collected:
        first = collected.wait_until(lambda: collected.observations_for(f"{EX}SAT"))
        assert first[0].type == "number" and first[0].point == f"{EX}SAT"
        outcome = collected.outcome_of(f"{EX}SAT")
        assert outcome is not None and outcome.state == "active"
        metadata = collected.metadata_for(f"{EX}SAT")
        assert metadata is not None and metadata.unit == "unit:DEG_C"
    assert collected.states == [], "the fake reports no instance state itself"


def test_run_source_reports_a_loop_that_raised(graph: GraphReader) -> None:
    config = FakeSourceConfig(claims=["SAT"], interval=0.01, crash_after=1)
    source = FAKE_SOURCE.create("crash", config, graph)
    with (
        pytest.raises(RuntimeError, match="fake crash"),
        run_source(source, [assigned(f"{EX}SAT", interval=1)]),
    ):
        time.sleep(0.2)


def test_the_collector_remembers_the_status_channel() -> None:
    collector = Collector()
    collector.instance_state("running")
    collector.device("dev-1", reachable=False, error="timeout")
    collector.warn("rate_limited", "one per second", None)
    collector.clear_warning("rate_limited")
    assert collector.states == [("running", None)]
    assert collector.devices == [
        {
            "device": "dev-1",
            "reachable": False,
            "error": "timeout",
            "skipped_rounds": None,
        }
    ]
    assert collector.warnings == {} and collector.cleared == ["rate_limited"]
    with pytest.raises(TimeoutError):
        collector.wait_until(lambda: False, timeout=0.05, interval=0.01)


def test_assigned_takes_intervals_as_text_or_seconds() -> None:
    assert assigned("p", interval="30s").parameters == {
        "interval": timedelta(seconds=30)
    }
    assert assigned("p", interval=0.5).parameters == {
        "interval": timedelta(seconds=0.5)
    }
    assert assigned("p", interval=timedelta(minutes=1)).parameters == {
        "interval": timedelta(minutes=1)
    }
    seen = datetime(2026, 9, 19, 10, tzinfo=UTC)
    point = assigned("p", "history", last_observation=seen, depth=3)
    assert point.method == "history" and point.parameters == {"depth": 3}
    assert point.last_observation == seen
