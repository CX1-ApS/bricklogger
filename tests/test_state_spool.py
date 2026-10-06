"""The contract's invariants, the runtime state, the spool and the sink."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bricklogger.daemon.sink import DaemonSink
from bricklogger.daemon.spool import Spool
from bricklogger.daemon.state import RuntimeState
from bricklogger.sdk.contract import Observation, Outcome, PointMetadata

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
P1, P2 = "https://example.com/bldg#SAT", "https://example.com/bldg#ZAT"


def test_observation_invariants() -> None:
    Observation(P1, NOW, "number", 21.5)
    Observation(P1, NOW, "null", reason="unreachable")
    with pytest.raises(ValueError, match="timezone-aware"):
        Observation(P1, datetime(2026, 9, 5, 12, 0), "number", 1.0)
    with pytest.raises(ValueError, match="needs a value"):
        Observation(P1, NOW, "number")
    with pytest.raises(ValueError, match="needs a reason"):
        Observation(P1, NOW, "null")
    with pytest.raises(ValueError, match="carries no value"):
        Observation(P1, NOW, "null", 1.0, reason="fault")
    with pytest.raises(ValueError, match="only null"):
        Observation(P1, NOW, "number", 1.0, reason="fault")
    with pytest.raises(ValueError, match="unknown value type"):
        Observation(P1, NOW, "float", 1.0)  # type: ignore[arg-type]


def test_observations_and_metadata_round_trip_through_dicts() -> None:
    stamp = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    for observation in (
        Observation(P1, NOW, "number", 21.5),
        Observation(P1, NOW, "datetime", stamp),
        Observation(P1, NOW, "enum", 2),
        Observation(P1, NOW, "null", reason="fault"),
    ):
        assert Observation.from_dict(observation.as_dict()) == observation
    metadata = PointMetadata(P1, "enum", "unit:DEG_C", {1: "Off", 2: "Auto"}, None)
    assert PointMetadata.from_dict(metadata.as_dict()) == metadata
    booleans = PointMetadata(P2, "boolean", None, None, ("Inactive", "Active"))
    assert PointMetadata.from_dict(booleans.as_dict()) == booleans


def test_runtime_state_points_instances_devices_and_warnings(tmp_path: Path) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    state.replace_plan([(P1, "bacnet_main", "poll"), (P2, "bacnet_main", "poll")])
    state.record_observations(
        [
            Observation(P1, NOW, "number", 21.5),
            Observation(P2, NOW, "null", reason="unreachable"),
        ]
    )
    state.set_outcomes(
        [Outcome(P1, "active"), Outcome(P2, "rejected", "unknown object")]
    )
    state.set_metadata([PointMetadata(P1, "number", "unit:DEG_C")])
    total, rows = state.points()
    assert total == 2
    sat, zat = rows
    assert sat["last_valid"] == {"value": 21.5, "time": NOW.isoformat()}
    assert sat["metadata"]["unit"] == "unit:DEG_C"
    assert zat["last_valid"] is None and zat["last_observation"] == NOW.isoformat()
    assert zat["outcome_reason"] == "unknown object"
    assert state.point_counts() == {"active": 1, "rejected": 1}
    assert state.points(outcome="active")[0] == 1

    state.replace_plan([(P1, "bacnet_main", "poll")])
    assert state.points()[0] == 1, "points that left the plan are dropped"
    assert state.points()[1][0]["last_valid"]["value"] == 21.5, "what is known is kept"

    state.set_instance(
        "bacnet_main", role="source", type_name="bacnet-ip", state="running"
    )
    state.set_instance(
        "bacnet_main",
        role="source",
        type_name="bacnet-ip",
        state="failed",
        error="boom",
        restart_count=2,
    )
    (instance,) = state.instances()
    assert (instance["state"], instance["last_error"], instance["restart_count"]) == (
        "failed",
        "boom",
        2,
    )
    assert not state.stop_intent("bacnet_main")
    state.set_stop_intent("bacnet_main", True)
    assert state.stop_intent("bacnet_main")

    state.set_device("bacnet_main", "1201", reachable=True)
    state.set_device("bacnet_main", "1201", reachable=False, error="timeout")
    state.set_device("bacnet_main", "1201", reachable=False, skipped_rounds=3)
    (device,) = state.devices("bacnet_main")
    assert (device["reachable"], device["error_count"], device["skipped_rounds"]) == (
        False,
        2,
        3,
    )
    assert device["last_success"] is not None

    state.warn("unclaimed", P2, "no instance claims the point")
    state.warn("unclaimed", P2, "no instance claims the point")
    (warning,) = state.warnings()
    assert (warning["code"], warning["count"]) == ("unclaimed", 2)
    state.clear_warnings(code="unclaimed")
    assert state.warnings() == []

    state.record_activation(3)
    activation = state.last_activation()
    assert activation is not None and activation[0] == 3
    state.increment("observations_received", 5)
    state.increment("observations_received", 2)
    assert state.counter("observations_received") == 7
    state.close()


def test_spool_orders_batches_and_enforces_caps(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool" / "tsdb.sqlite")
    spool.append_metadata([PointMetadata(P1, "number", "unit:DEG_C")])
    spool.append_observations(
        [Observation(P1, NOW, "number", 1.0), Observation(P2, NOW, "number", 2.0)]
    )
    spool.append_observations(
        [Observation(P1, NOW + timedelta(minutes=1), "number", 3.0)]
    )
    assert spool.wakeup.is_set()

    first = spool.next_entries(100)
    assert [e.kind for e in first] == ["metadata"], "metadata alone, in order"
    spool.ack(e.id for e in first)
    second = spool.next_entries(100)
    assert [e.kind for e in second] == ["observations", "observations"]
    assert [o.value for e in second for o in e.observations()] == [1.0, 2.0, 3.0]
    limited = spool.next_entries(1)
    assert len(limited) == 1, "the batch size bounds a run"
    stats = spool.stats()
    assert (stats.entries, stats.observations) == (2, 3)
    assert stats.oldest_age is not None and stats.oldest_age >= 0

    dropped = spool.enforce_caps(max_bytes=10, max_age=timedelta(days=1))
    assert dropped == 3
    assert spool.stats().entries == 0
    spool.append_observations([Observation(P1, NOW, "number", 1.0)])
    assert spool.enforce_caps(max_bytes=10**9, max_age=timedelta(seconds=0)) == 1
    spool.close()


def test_sink_records_spools_and_rejects_the_future(tmp_path: Path) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    state.replace_plan([(P1, "src", "poll")])
    spools = {"a": Spool(tmp_path / "a.sqlite"), "b": Spool(tmp_path / "b.sqlite")}
    sink = DaemonSink(state, spools)
    future = datetime.now(UTC) + timedelta(hours=1)
    sink.observations(
        [Observation(P1, NOW, "number", 20.0), Observation(P1, future, "number", 99.0)]
    )
    sink.metadata([PointMetadata(P1, "number", "unit:DEG_C")])
    for spool in spools.values():
        assert spool.stats().observations == 1, "every destination gets the same"
        assert spool.stats().entries == 2
    assert state.points()[1][0]["last_valid"]["value"] == 20.0
    assert state.counter("observations_received") == 1
    assert state.counter("observations_rejected_future") == 1
    (warning,) = state.warnings()
    assert warning["code"] == "future_timestamp"


def test_the_last_value_is_the_latest_by_timestamp(tmp_path: Path) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    state.replace_plan([(P1, "ibos_main", "poll")])
    later = NOW + timedelta(minutes=5)
    state.record_observations([Observation(P1, later, "number", 22.0)])
    state.record_observations([Observation(P1, NOW, "number", 21.0)])
    state.record_observations([Observation(P1, NOW, "null", reason="no_value")])
    (row,) = state.points()[1]
    assert row["last_valid"] == {"value": 22.0, "time": later.isoformat()}
    assert row["last_observation"] == later.isoformat()
    state.close()
