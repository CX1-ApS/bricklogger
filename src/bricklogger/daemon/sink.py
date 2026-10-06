"""The sink the sources push into: it records the runtime state and spools
everything for every destination. Thread-safe, because every source calls it
from its own thread."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from bricklogger.daemon.overlay import ValueOverlay
from bricklogger.daemon.spool import Spool
from bricklogger.daemon.state import RuntimeState
from bricklogger.model.prefixes import WELL_KNOWN, UnknownPrefix, expand
from bricklogger.sdk.contract import Observation, PointMetadata

FUTURE_TOLERANCE = timedelta(seconds=30)


class DaemonSink:
    """Observations and metadata from the sources, on their way to the destinations."""

    def __init__(
        self,
        state: RuntimeState,
        spools: Mapping[str, Spool],
        tolerance: timedelta = FUTURE_TOLERANCE,
        overlay: ValueOverlay | None = None,
    ) -> None:
        self.state = state
        self.spools = dict(spools)
        self.tolerance = tolerance
        self.overlay = overlay
        self._lock = threading.Lock()
        self._future = {
            w["subject"] for w in state.warnings() if w["code"] == "future_timestamp"
        }

    def observations(self, batch: Iterable[Observation]) -> None:
        accepted: list[Observation] = []
        limit = datetime.now(UTC) + self.tolerance
        rejected = 0
        with self._lock:
            for observation in batch:
                if observation.timestamp > limit:
                    rejected += 1
                    stamp = observation.timestamp.isoformat()
                    self.state.warn(
                        "future_timestamp",
                        observation.point,
                        f"observation rejected: timestamp {stamp} lies in the future",
                    )
                    self._future.add(observation.point)
                    continue
                if observation.point in self._future:
                    self._future.discard(observation.point)
                    self.state.clear_warnings(
                        code="future_timestamp", subject=observation.point
                    )
                accepted.append(observation)
            if rejected:
                self.state.increment("observations_rejected_future", rejected)
            if not accepted:
                return
            self.state.record_observations(accepted)
            if self.overlay is not None:
                self.overlay.mark(o.point for o in accepted if o.type != "null")
            self.state.increment("observations_received", len(accepted))
            for observation in accepted:
                if observation.type == "null" and observation.reason == "read_error":
                    self.state.warn(
                        "read_error",
                        observation.point,
                        "the device answers the read with an error",
                    )
                elif observation.type != "null":
                    self.state.clear_warnings(
                        code="read_error", subject=observation.point
                    )
            for spool in self.spools.values():
                spool.append_observations(accepted)

    def metadata(self, entries: Iterable[PointMetadata]) -> None:
        """Merge the graph's and the plugins' parts, check units, spool the whole."""
        items = list(entries)
        if not items:
            return
        with self._lock:
            merged_entries = [self.state.merge_metadata(entry) for entry in items]
            for merged in merged_entries:
                self._check_units(merged)
            for spool in self.spools.values():
                spool.append_metadata(merged_entries)

    def _check_units(self, merged: PointMetadata) -> None:
        if (
            merged.unit
            and merged.graph_unit
            and not same_unit(merged.unit, merged.graph_unit)
        ):
            self.state.warn(
                "unit_conflict",
                merged.point,
                f"the graph says {merged.graph_unit}, the protocol {merged.unit}",
            )
        else:
            self.state.clear_warnings(code="unit_conflict", subject=merged.point)


def same_unit(first: str, second: str) -> bool:
    """Whether two unit designations name the same unit, prefixed or not."""
    try:
        return expand(first, WELL_KNOWN) == expand(second, WELL_KNOWN)
    except UnknownPrefix:
        return first == second
