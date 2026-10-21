"""Health read off the warnings: severities, the reason, stale points, the
watchdog and the backoff that starts over. See ``docs/features/daemon.md``,
"Health" and "Stale points"."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from bricklogger.cli.output import health_line
from bricklogger.config.schema import SourceInstance
from bricklogger.daemon import instances
from bricklogger.daemon.codes import severity_of, worst
from bricklogger.daemon.health import (
    InstanceHealth,
    assess,
    overall,
    stale_since,
    staleness_limit,
    told,
)
from bricklogger.daemon.instances import InstanceStatus, SourceRunner
from bricklogger.daemon.state import RuntimeState
from bricklogger.daemon.watch import Watch, Watched
from bricklogger.sdk.contract import (
    AssignedPoint,
    Observation,
    PointMetadata,
    Sink,
    Source,
    StatusChannel,
)
from bricklogger.sdk.declaration import SourceDeclaration, WarningCode
from bricklogger.sdk.registry import PluginRegistry
from tests.support import wait_for

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def warning(
    code: str, subject: str, message: str = "m", first_seen: str = ""
) -> dict[str, str]:
    return {
        "code": code,
        "subject": subject,
        "message": message,
        "first_seen": first_seen or NOW.isoformat(),
    }


# --- severities and the reason -----------------------------------------------


def test_every_code_has_the_severity_the_warning_table_gives() -> None:
    assert severity_of("instance_failed") == "failed"
    assert severity_of("spool_drop") == "failed", "dropped data is lost data"
    assert severity_of("stop_timeout") == "failed"
    for code in ("read_error", "poll_overrun", "rate_limited", "future_timestamp"):
        assert severity_of(code) == "degraded"
    assert severity_of("device_unreachable") == "degraded"
    assert severity_of("stale") == "degraded"
    for code in ("instance_stopped", "rejected", "unclaimed", "notify_failed"):
        assert severity_of(code) is None, code


def test_a_plugin_code_counts_as_declared_or_else_as_degraded() -> None:
    assert severity_of("token_rejected", {"token_rejected": "failed"}) == "failed"
    assert severity_of("something_of_its_own") == "degraded"
    assert (
        severity_of("instance_failed", {"instance_failed": "degraded"}) == "failed"
    ), "a plugin cannot soften the daemon's own codes"


def test_the_registry_takes_the_worse_of_two_declarations() -> None:
    class Config(BaseModel):
        pass

    def source(name: str, severity: Any) -> SourceDeclaration:
        return SourceDeclaration(
            type_name=name,
            description="",
            config_schema=Config,
            reference_types=(),
            warning_codes=(WarningCode("quota", "out of quota", severity),),
        )

    registry = PluginRegistry.of(source("a", "failed"), source("b", "degraded"))
    assert registry.severities() == {"quota": "failed"}
    with pytest.raises(ValueError, match="degraded or failed"):
        WarningCode("quota", "out of quota", "fatal")  # type: ignore[arg-type]


def test_health_is_the_worst_severity_on_the_instance_its_devices_and_points() -> None:
    owners = {"p1": "ibos", "p2": "ibos", "p3": "ibos", "q1": "bacnet"}
    healths = assess(
        ["ibos", "bacnet", "tsdb"],
        [
            warning("stale", "p1", first_seen="2026-10-21T10:00:00+00:00"),
            warning("stale", "p2", first_seen="2026-10-21T09:00:00+00:00"),
            warning("rate_limited", "ibos", "keeps 1 round in 2"),
            warning("device_unreachable", "bacnet/1200", "no answer to Who-Is"),
            warning("rejected", "q1", "the source cannot read it"),
            warning("spool_drop", "tsdb", "the spool dropped 12 observations"),
        ],
        owners,
    )
    assert healths["ibos"] == InstanceHealth(
        "degraded", "2 of 3 points stale", "2026-10-21T09:00:00+00:00", 1
    ), "points of one code are one condition, the oldest first among equals"
    assert healths["bacnet"].health == "degraded"
    assert healths["bacnet"].reason == "1200: no answer to Who-Is"
    assert healths["bacnet"].more == 0, "a model warning does not count"
    assert healths["tsdb"].health == "failed"
    assert overall(healths.values()) == "failed"
    assert worst([None, "degraded"]) == "degraded"


def test_a_failed_warning_wins_over_an_older_degraded_one() -> None:
    healths = assess(
        ["ibos"],
        [
            warning("rate_limited", "ibos", first_seen="2026-10-20T00:00:00+00:00"),
            warning("instance_failed", "ibos", "failed: the token is not accepted"),
        ],
        {},
    )
    assert healths["ibos"].health == "failed"
    assert healths["ibos"].reason == "failed: the token is not accepted"
    assert told(healths["ibos"].as_dict()) == (
        "failed: the token is not accepted, and 1 more"
    )


def test_an_instance_without_warnings_is_ok() -> None:
    healths = assess(["ibos"], [warning("unclaimed", "p9")], {"p9": None})
    assert healths["ibos"] == InstanceHealth()
    assert overall([]) == "ok"


def test_the_status_line_tells_state_health_reason_and_since() -> None:
    row = {
        "state": "running",
        "health": "degraded",
        "reason": "the token is not accepted (401)",
        "since": "2026-10-10T08:01:08Z",
        "more": 1,
    }
    assert health_line(row) == (
        "running, degraded since 2026-10-10T08:01:08Z — "
        "the token is not accepted (401), and 1 more"
    )
    assert health_line({"state": "running", "health": "ok"}) == "running, ok"
    assert health_line({"state": "running"}) == "running", "an older daemon"


# --- staleness ---------------------------------------------------------------


def test_the_staleness_limit_is_the_instance_s_the_plugin_s_or_three_intervals() -> (
    None
):
    point = AssignedPoint("p", "poll", {"interval": timedelta(minutes=5)})
    assert staleness_limit(point, None, None) == timedelta(minutes=15)
    assert staleness_limit(point, None, timedelta(hours=1)) == timedelta(hours=1)
    assert staleness_limit(point, timedelta(hours=2), timedelta(hours=1)) == (
        timedelta(hours=2)
    )
    assert staleness_limit(AssignedPoint("p", "cov"), None, None) is None


def test_staleness_counts_from_the_later_of_the_observation_and_the_start() -> None:
    limit = timedelta(minutes=15)
    started = NOW - timedelta(minutes=10)
    assert stale_since(None, started, limit, NOW) is None, "a fresh start"
    assert stale_since(NOW - timedelta(hours=1), started, limit, NOW) is None
    old_start = NOW - timedelta(hours=1)
    last = NOW - timedelta(minutes=20)
    assert stale_since(last, old_start, limit, NOW) == last


def test_the_watch_raises_and_withdraws_stale(tmp_path: Path) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    state.replace_plan([("p1", "ibos", "poll"), ("p2", "ibos", "poll")])
    started = NOW - timedelta(hours=2)
    points = [
        AssignedPoint(uri, "poll", {"interval": timedelta(minutes=5)})
        for uri in ("p1", "p2")
    ]
    watched = {"ibos": Watched(points, started, None, None)}
    watch = Watch(state, lambda: None, lambda: watched)
    state.record_observations(
        [
            Observation("p1", NOW - timedelta(hours=1), "number", 1.0),
            Observation("p2", NOW - timedelta(minutes=1), "null", reason="unreachable"),
        ]
    )

    watch.stale(NOW)
    stale = [w for w in state.warnings() if w["code"] == "stale"]
    assert [w["subject"] for w in stale] == ["p1"], "a null counts as delivering"
    assert "the limit is 15m" in stale[0]["message"]

    watch.stale(NOW)
    assert next(w for w in state.warnings() if w["code"] == "stale")["count"] == 1, (
        "a standing warning is not asserted again at every look"
    )

    state.record_observations([Observation("p1", NOW, "number", 2.0)])
    watch.stale(NOW)
    assert not state.has_warning("stale")

    state.record_observations(
        [Observation("p2", NOW - timedelta(hours=1), "number", 1)]
    )
    watched["ibos"] = Watched(points, None, None, None)
    watch.stale(NOW + timedelta(hours=1))
    assert not state.has_warning("stale"), "a source that does not run is not judged"


# --- the status channel ------------------------------------------------------


def test_an_unreachable_device_raises_its_warning_once_and_withdraws_it(
    tmp_path: Path,
) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    status = InstanceStatus("bacnet", "bacnet-ip", state)
    status.device("1200", reachable=False, error="timeout")
    status.device("1200", reachable=False, error="timeout")
    [row] = [w for w in state.warnings() if w["code"] == "device_unreachable"]
    assert (row["subject"], row["message"], row["count"]) == (
        "bacnet/1200",
        "timeout",
        1,
    )
    status.device("1200", reachable=True)
    assert not state.has_warning("device_unreachable")


def test_stale_after_is_the_daemon_s_key_and_never_reaches_the_plugin() -> None:
    instance = SourceInstance.model_validate(
        {"type": "ibos", "stale_after": "2h", "project": 414777}
    )
    assert instance.stale_after == timedelta(hours=2)
    assert instance.settings == {"project": 414777}


# --- the supervisor: backoff and the watchdog ---------------------------------


class _NullSink(Sink):
    def observations(self, batch: Iterable[Observation]) -> None:
        pass

    def metadata(self, entries: Iterable[PointMetadata]) -> None:
        pass


class _Config(BaseModel):
    pass


class _Source(Source):
    """Fails at once while ``fail`` is set; otherwise runs until stopped."""

    def __init__(self, fail: bool, runs_for: float = 0.0) -> None:
        super().__init__("s", _Config(), None)  # type: ignore[arg-type]
        self.fail = fail
        self.runs_for = runs_for
        self.failures = 0
        self._stop = threading.Event()

    def resources(self) -> Iterable[str]:
        return []

    def claim(self) -> set[str]:
        return set()

    def assign(self, points: Any) -> None:
        pass

    def start(self, sink: Sink, status: StatusChannel) -> None:
        if self.fail:
            self._stop.wait(self.runs_for)
            self.failures += 1
            raise RuntimeError("the token is not accepted (401)")
        self._stop.clear()
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


def _state(runner: SourceRunner) -> str:
    return runner.current_state


def _runner(tmp_path: Path, source: Source) -> tuple[SourceRunner, RuntimeState]:
    state = RuntimeState(tmp_path / "state.sqlite")
    status = InstanceStatus("s", "fake", state)
    runner = SourceRunner(
        "s", "fake", source, _NullSink(), status, state, timedelta(seconds=2)
    )
    return runner, state


def test_failures_in_a_row_lengthen_the_backoff(tmp_path: Path) -> None:
    source = _Source(fail=True)
    runner, state = _runner(tmp_path, source)
    runner._attempt = 5  # five failures in a row before this one
    runner._restarts = 5
    runner.start()
    try:
        wait_for(lambda: source.failures == 1 and runner.current_state == "failed")
        assert runner._attempt == 6
        assert runner._restarts == 6
        assert runner.next_retry is not None
        assert runner.next_retry - datetime.now(UTC) > timedelta(seconds=30)
        assert state.has_warning("instance_failed", "s")
    finally:
        runner.stop()


def test_the_backoff_starts_over_after_a_clean_run_as_long_as_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "BACKOFF_CAP", 0.02)
    source = _Source(fail=True, runs_for=0.05)  # every run outlasts the cap
    runner, _ = _runner(tmp_path, source)
    runner._attempt = 5
    runner._restarts = 5
    runner.start()
    try:
        wait_for(lambda: source.failures >= 2)
    finally:
        runner.stop()
    assert runner._attempt == 1, "each failure after a clean run is the first again"
    assert runner._restarts >= 7, "the restart count keeps the total"


def test_the_watchdog_revives_a_supervisor_that_ended_unseen(tmp_path: Path) -> None:
    source = _Source(fail=False)
    runner, state = _runner(tmp_path, source)
    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    runner._thread = ended
    runner.current_state = "running"
    assert runner.ended_unseen()

    runner.revive()
    try:
        assert _state(runner) == "failed"
        [row] = [w for w in state.warnings() if w["code"] == "instance_failed"]
        assert "ended without a failure being seen" in row["message"]
        wait_for(lambda: runner.current_state == "running", timeout=5.0)
        assert not state.has_warning("instance_failed")
        assert not runner.ended_unseen()
    finally:
        runner.stop()
    assert not runner.ended_unseen(), "a stopped instance is not revived"
