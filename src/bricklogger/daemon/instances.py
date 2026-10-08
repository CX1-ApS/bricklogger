"""Running plugin instances: one thread each, a graceful stop with a deadline,
and a restart with capped exponential backoff after a crash. See
``docs/architecture.md``, "Isolation and resources" and "Stop"."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import timedelta

from bricklogger.daemon.spool import Entry, Spool
from bricklogger.daemon.state import RuntimeState
from bricklogger.model.timeseries import with_references
from bricklogger.sdk.contract import (
    AssignedPoint,
    Destination,
    InstanceState,
    Outcome,
    Sink,
    Source,
)

log = logging.getLogger(__name__)

BACKOFF_START = 1.0
BACKOFF_CAP = 60.0


def backoff(attempt: int) -> float:
    """Seconds to wait before restart number ``attempt`` (1-based): 1, 2, 4 … capped."""
    return float(min(BACKOFF_START * (2 ** (attempt - 1)), BACKOFF_CAP))


class InstanceStatus:
    """The status channel of one source instance: it writes into the runtime state."""

    def __init__(
        self,
        name: str,
        type_name: str,
        state: RuntimeState,
        on_outcomes: Callable[[str, Sequence[Outcome]], None] | None = None,
    ) -> None:
        self.name = name
        self.type_name = type_name
        self.state = state
        self._on_outcomes = on_outcomes
        self._skipped: dict[str, int] = {}

    def instance_state(self, state: InstanceState, error: str | None = None) -> None:
        self.state.set_instance(
            self.name, role="source", type_name=self.type_name, state=state, error=error
        )

    def device(
        self,
        device: str,
        *,
        reachable: bool,
        error: str | None = None,
        skipped_rounds: int | None = None,
    ) -> None:
        self.state.set_device(
            self.name,
            device,
            reachable=reachable,
            error=error,
            skipped_rounds=skipped_rounds,
        )
        previous = self._skipped.get(device)
        self._skipped[device] = skipped_rounds or 0
        if skipped_rounds and skipped_rounds != previous:
            self.state.warn(
                "poll_overrun",
                f"{self.name}/{device}",
                f"keeps missing its poll interval; {skipped_rounds} rounds skipped",
            )
        else:
            # a report without a new skip: the device keeps up again
            self.state.clear_warnings(
                code="poll_overrun", subject=f"{self.name}/{device}"
            )

    def outcomes(self, outcomes: Iterable[Outcome]) -> None:
        items = list(outcomes)
        self.state.set_outcomes(items)
        for outcome in items:
            if outcome.state == "rejected":
                self.state.warn(
                    "rejected",
                    outcome.point,
                    outcome.reason or "rejected by the source",
                )
            else:
                self.state.clear_warnings(code="rejected", subject=outcome.point)
        if self._on_outcomes is not None:
            self._on_outcomes(self.name, items)

    def warn(self, code: str, message: str, subject: str | None = None) -> None:
        self.state.warn(code, self._subject(subject), message)

    def clear_warning(self, code: str, subject: str | None = None) -> None:
        self.state.clear_warnings(code=code, subject=self._subject(subject))

    def _subject(self, subject: str | None) -> str:
        return self.name if subject is None else f"{self.name}/{subject}"


class SourceRunner:
    """Supervises one source instance in its own thread."""

    def __init__(
        self,
        name: str,
        type_name: str,
        source: Source,
        sink: Sink,
        status: InstanceStatus,
        state: RuntimeState,
        stop_timeout: timedelta,
    ) -> None:
        self.name = name
        self.type_name = type_name
        self.source = source
        self.sink = sink
        self.status = status
        self.state = state
        self.stop_timeout = stop_timeout
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._restarts = 0
        self._lock = threading.Lock()
        self._assignment: list[AssignedPoint] = []
        self.current_state: InstanceState = "stopped"

    def start(self) -> None:
        """Start the supervising thread; an operator-stopped instance stays stopped."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._supervise, name=f"source:{self.name}", daemon=True
            )
            self._set_state("starting")
            self._thread.start()

    def assign(self, points: Sequence[AssignedPoint]) -> None:
        """Hand over full desired state; repeated after a restart."""
        with self._lock:
            self._assignment = list(points)
        try:
            self.source.assign(self._with_history(points))
        except Exception as exc:
            log.exception("source %s failed to take its assignment", self.name)
            self._set_state("failed", str(exc))

    def _with_history(self, points: Sequence[AssignedPoint]) -> list[AssignedPoint]:
        """Each point with the latest observation the daemon has recorded for it,
        so a history source resumes where it left off."""
        latest = self.state.last_observations(point.uri for point in points)
        return [
            AssignedPoint(
                uri=point.uri,
                method=point.method,
                parameters=point.parameters,
                last_observation=latest.get(point.uri, point.last_observation),
            )
            for point in points
        ]

    def stop(self) -> bool:
        """Ask the source to stop and wait up to the deadline; False when abandoned."""
        self._stopping.set()
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._set_state("stopped")
            return True
        try:
            self.source.stop()
        except Exception:
            log.exception("source %s raised while stopping", self.name)
        seconds = self.stop_timeout.total_seconds()
        thread.join(seconds)
        if thread.is_alive():
            self.state.warn(
                "stop_timeout",
                self.name,
                f"did not stop within {seconds:g}s; abandoned",
            )
            return False
        self.state.clear_warnings(code="stop_timeout", subject=self.name)
        self._set_state("stopped")
        return True

    def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                self._set_state("running")
                self.state.clear_warnings(code="instance_failed", subject=self.name)
                self.source.start(self.sink, self.status)
                if not self._stopping.is_set():
                    raise RuntimeError("the loop returned without a stop")
                return
            except Exception as exc:
                if self._stopping.is_set():
                    return
                self._restarts += 1
                delay = backoff(self._restarts)
                log.exception("source %s failed; restart in %.0fs", self.name, delay)
                self._set_state("failed", str(exc), restarts=self._restarts)
                self.state.warn("instance_failed", self.name, f"failed: {exc}")
                if self._stopping.wait(delay):
                    return
                with self._lock:
                    assignment = list(self._assignment)
                try:
                    self.source.assign(self._with_history(assignment))
                except Exception:
                    log.exception("source %s failed to take its assignment", self.name)

    def _set_state(
        self,
        state: InstanceState,
        error: str | None = None,
        restarts: int | None = None,
    ) -> None:
        if state != self.current_state:
            log.info("source %s is %s", self.name, state)
        self.current_state = state
        self.state.set_instance(
            self.name,
            role="source",
            type_name=self.type_name,
            state=state,
            error=error,
            restart_count=restarts,
        )


class DestinationRunner:
    """Drains one destination's spool in its own thread and writes in batches."""

    def __init__(
        self,
        name: str,
        type_name: str,
        destination: Destination,
        spool: Spool,
        state: RuntimeState,
        *,
        batch_size: int,
        batch_interval: timedelta,
        spool_max_size: int,
        spool_max_age: timedelta,
        stop_timeout: timedelta,
        stores_metadata: bool,
        stores_model: bool = False,
        on_model_written: Callable[[str, int, Mapping[str, str]], None] | None = None,
    ) -> None:
        self.name = name
        self.type_name = type_name
        self.destination = destination
        self.spool = spool
        self.state = state
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.spool_max_size = spool_max_size
        self.spool_max_age = spool_max_age
        self.stop_timeout = stop_timeout
        self.stores_metadata = stores_metadata
        self.stores_model = stores_model
        self.on_model_written = on_model_written
        self.model_version: int | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._failures = 0
        self._restarts = 0
        self._drop_pending = False
        self.current_state: InstanceState = "stopped"
        self.last_write: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"destination:{self.name}", daemon=True
        )
        self._set_state("starting")
        self._thread.start()

    def stop(self) -> bool:
        self._stopping.set()
        self.spool.wakeup.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            seconds = self.stop_timeout.total_seconds()
            thread.join(seconds)
            if thread.is_alive():
                self.state.warn(
                    "stop_timeout",
                    self.name,
                    f"did not stop within {seconds:g}s; abandoned",
                )
                return False
        self.state.clear_warnings(code="stop_timeout", subject=self.name)
        self._set_state("stopped")
        return True

    def _run(self) -> None:
        if not self._start_destination():
            return
        try:
            while not self._stopping.is_set():
                self._enforce_caps()
                entries = self.spool.next_entries(self.batch_size)
                if not entries:
                    self.spool.wakeup.clear()
                    self.spool.wakeup.wait(self.batch_interval.total_seconds())
                    continue
                if not self._write(entries):
                    if self._stopping.wait(backoff(self._failures)):
                        break
                    if not self._restart_destination():
                        break
            self._flush_within_deadline()
        finally:
            try:
                self.destination.stop()
            except Exception:
                log.exception("destination %s raised while stopping", self.name)

    def _start_destination(self) -> bool:
        attempt = 0
        while not self._stopping.is_set():
            try:
                self.destination.start()
                self._failures = 0
                self._set_state("running")
                self.state.clear_warnings(code="instance_failed", subject=self.name)
                self._drop_pending = self.state.has_warning("spool_drop", self.name)
                return True
            except Exception as exc:
                attempt += 1
                self._set_state("failed", str(exc), restarts=attempt)
                self.state.warn("instance_failed", self.name, f"could not start: {exc}")
                log.exception("destination %s could not start", self.name)
                if self._stopping.wait(backoff(attempt)):
                    break
        return False

    def _restart_destination(self) -> bool:
        """Start the destination afresh after a failed write, so a connection the
        far end has closed is replaced. The failed state and the warning stand
        until a write succeeds, so a restart alone never reports recovery."""
        try:
            self.destination.stop()
        except Exception:
            log.exception("destination %s raised while stopping to restart", self.name)
        while not self._stopping.is_set():
            self._restarts += 1
            try:
                self.destination.start()
            except Exception as exc:
                self._set_state("failed", str(exc), restarts=self._restarts)
                self.state.warn("instance_failed", self.name, f"could not start: {exc}")
                log.exception("destination %s could not start again", self.name)
                if self._stopping.wait(backoff(self._restarts)):
                    break
                continue
            log.info("destination %s started again after a failed write", self.name)
            return True
        return False

    def _write(self, batch: Sequence[Entry]) -> bool:
        try:
            if batch[0].kind == "metadata":
                if self.stores_metadata:
                    for entry in batch:
                        self.destination.write_metadata(entry.metadata())
            elif batch[0].kind == "model":
                if self.stores_model:
                    for entry in batch:
                        self._write_model(entry)
            else:
                observations = [o for entry in batch for o in entry.observations()]
                self.destination.write(observations)
                self.state.increment(f"written:{self.name}", len(observations))
        except Exception as exc:
            self._failures += 1
            self._set_state("failed", str(exc))
            self.state.warn("instance_failed", self.name, f"write failed: {exc}")
            log.exception("destination %s failed to write", self.name)
            return False
        self.spool.ack(entry.id for entry in batch)
        if self._drop_pending and self.spool.stats().entries == 0:
            # drained again after a drop; the dropped total stays in status
            self._drop_pending = False
            self.state.clear_warnings(code="spool_drop", subject=self.name)
        if self._failures:
            self._failures = 0
            self.state.clear_warnings(code="instance_failed", subject=self.name)
        self._set_state("running")
        self.last_write = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return True

    def _write_model(self, entry: Entry) -> None:
        """Add the destination's references to a model version and store it.

        The keys are asked for at write time, after the metadata ahead of the
        model in the spool, so every point the plan offered already has one.
        """
        model = entry.model()
        turtle, used = with_references(model.turtle, self.destination.timeseries_ids())
        self.destination.write_model(replace(model, turtle=turtle))
        self.model_version = model.version
        if self.on_model_written is not None:
            self.on_model_written(self.name, model.version, used)

    def _enforce_caps(self) -> None:
        dropped = self.spool.enforce_caps(self.spool_max_size, self.spool_max_age)
        if dropped:
            self._drop_pending = True
            self.state.increment(f"spool_drop:{self.name}", dropped)
            self.state.warn(
                "spool_drop", self.name, f"the spool dropped {dropped} observations"
            )

    def _flush_within_deadline(self) -> None:
        deadline = time.monotonic() + self.stop_timeout.total_seconds()
        while time.monotonic() < deadline:
            entries = self.spool.next_entries(self.batch_size)
            if not entries or not self._write(entries):
                return

    def _set_state(
        self,
        state: InstanceState,
        error: str | None = None,
        restarts: int | None = None,
    ) -> None:
        if state != self.current_state:
            log.info("destination %s is %s", self.name, state)
        self.current_state = state
        self.state.set_instance(
            self.name,
            role="destination",
            type_name=self.type_name,
            state=state,
            error=error,
            restart_count=restarts,
        )
