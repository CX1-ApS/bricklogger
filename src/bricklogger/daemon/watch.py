"""The daemon's watch: a thread that looks over the instances at a steady pace.

It restarts an instance whose thread has ended without the daemon having seen
a failure, and raises or withdraws ``stale`` on the points of the running
sources. See ``docs/architecture.md``, "Stop", and ``docs/features/daemon.md``,
"Stale points".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from bricklogger.config.values import format_duration
from bricklogger.daemon.codes import STALE
from bricklogger.daemon.health import stale_since, staleness_limit
from bricklogger.daemon.state import RuntimeState
from bricklogger.sdk.contract import AssignedPoint

log = logging.getLogger(__name__)

WATCH_PERIOD = 15.0


@dataclass(frozen=True)
class Watched:
    """What the watch needs of one source instance at one look."""

    points: list[AssignedPoint]
    running_since: datetime | None
    stale_after: timedelta | None
    default: timedelta | None


class Watch:
    """Looks over the instances every ``period`` seconds in a thread of its own."""

    def __init__(
        self,
        state: RuntimeState,
        revive: Callable[[], None],
        sources: Callable[[], Mapping[str, Watched]],
        period: float = WATCH_PERIOD,
    ) -> None:
        self.state = state
        self._revive = revive
        self._sources = sources
        self.period = period
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stopping.wait(self.period):
            try:
                self.check()
            except Exception:
                log.exception("the watch failed; it looks again in %gs", self.period)

    def check(self, now: datetime | None = None) -> None:
        """One look: revive what ended unseen, then judge every point's staleness."""
        self._revive()
        self.stale(now or datetime.now(UTC))

    def stale(self, now: datetime) -> None:
        """Raise ``stale`` on the points that have gone quiet, and withdraw it
        from those that delivered again, left the plan or whose source is not
        running."""
        watched = self._sources()
        uris = [point.uri for w in watched.values() for point in w.points]
        latest = self.state.last_observations(uris)
        standing = {w["subject"] for w in self.state.warnings() if w["code"] == STALE}
        found: set[str] = set()
        for instance in watched.values():
            if instance.running_since is None:
                continue
            for point in instance.points:
                limit = staleness_limit(point, instance.stale_after, instance.default)
                if limit is None:
                    continue
                last = latest.get(point.uri)
                since = stale_since(last, instance.running_since, limit, now)
                if since is None:
                    continue
                found.add(point.uri)
                if point.uri not in standing:
                    what = "nothing delivered" if last is None else "nothing new"
                    self.state.warn(
                        STALE,
                        point.uri,
                        f"{what} since {since.isoformat(timespec='seconds')}; "
                        f"the limit is {format_duration(limit)}",
                    )
        for uri in standing - found:
            self.state.clear_warnings(code=STALE, subject=uri)
