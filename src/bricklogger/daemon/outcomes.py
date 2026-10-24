"""The daemon's share of a source's outcome reports, done off the source's thread.

A source reports outcomes on its status channel, often from inside its own
event loop. The channel records them and returns; what the daemon makes of
them — the rule's fallback — needs the daemon's lock, which an activation or a
reload holds for a while, so it is done here, in a thread of its own. A source
is then never held up by the daemon, and a stop always reaches it. See
``docs/architecture.md``, "Operation".
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence

from bricklogger.daemon.instances import InstanceStatus
from bricklogger.sdk.contract import Outcome

log = logging.getLogger(__name__)


class OutcomeQueue:
    """Hands each report to ``apply`` in order, one at a time, in its own thread."""

    def __init__(self, apply: Callable[[InstanceStatus, list[Outcome]], None]) -> None:
        self._apply = apply
        self._queue: queue.SimpleQueue[tuple[InstanceStatus, list[Outcome]] | None] = (
            queue.SimpleQueue()
        )
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="outcomes", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """End the thread; what is still queued is dropped with the daemon."""
        thread = self._thread
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout)
        self._thread = None

    def put(self, status: InstanceStatus, outcomes: Sequence[Outcome]) -> None:
        """Queue a report and return at once."""
        self._queue.put((status, list(outcomes)))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._apply(*item)
            except Exception:
                log.exception("outcomes could not be applied")
