"""The daemon's daily look for newer releases.

Once a day, unless ``updates.check`` is off, the daemon asks PyPI which
releases of Bricklogger and its plugins are newer than those installed; what
it finds stands in the status summary and the daily summary mail. A look that
cannot reach PyPI finds nothing and is not a warning: a machine on a closed
network has nothing to be told. See ``docs/features/cli.md``, "update".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from bricklogger.ops.errors import OperationError
from bricklogger.ops.updates import newer_releases

log = logging.getLogger(__name__)

INTERVAL = timedelta(days=1)
FIRST_LOOK = timedelta(minutes=1)
"""The first look waits a little, so a start is not slowed by the network."""

Lookup = Callable[[], list[dict[str, Any]]]


class UpdateWatch:
    """A thread that looks once a day and keeps the last answer."""

    def __init__(
        self,
        enabled: bool,
        lookup: Lookup | None = None,
        *,
        first_look: timedelta = FIRST_LOOK,
        interval: timedelta = INTERVAL,
    ) -> None:
        self.enabled = enabled
        self._lookup = lookup if lookup is not None else newer_releases
        self._first_look = first_look
        self._interval = interval
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.checked_at: datetime | None = None
        self.available: list[dict[str, Any]] = []

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="update-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reconfigure(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            if not enabled:
                self.checked_at = None
                self.available = []

    def look(self) -> None:
        """Look now; a failure keeps the previous answer."""
        if not self.enabled:
            return
        try:
            found = self._lookup()
        except (OperationError, OSError) as exc:
            log.debug("the look for newer releases failed: %s", exc)
            return
        with self._lock:
            self.checked_at = datetime.now(UTC)
            self.available = found

    def summary(self) -> dict[str, Any] | None:
        """``checked_at`` and the newer releases, or ``None`` when the look is
        off or has not been made."""
        with self._lock:
            if not self.enabled or self.checked_at is None:
                return None
            return {
                "checked_at": self.checked_at.isoformat(),
                "available": list(self.available),
            }

    def _run(self) -> None:
        if self._stopping.wait(self._first_look.total_seconds()):
            return
        while True:
            self.look()
            if self._stopping.wait(self._interval.total_seconds()):
                return
