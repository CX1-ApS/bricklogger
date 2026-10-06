"""Jobs: the API's long operations, model upload and activation.

They run one at a time in a worker thread, and their state can be read while
they run and after they finish, until the daemon restarts. See
``docs/features/api.md``, "Models and jobs".
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

log = logging.getLogger(__name__)

JobState = Literal["queued", "running", "done", "failed"]
Work = Callable[["Job"], dict[str, Any]]

KEEP_FINISHED = 200
FINISHED = frozenset({"done", "failed"})


class JobFailed(Exception):
    """The work failed for a reason the client should see as a problem document."""

    def __init__(self, problem: dict[str, Any]) -> None:
        self.problem = problem
        super().__init__(problem.get("detail") or problem.get("title") or "job failed")


@dataclass
class Job:
    """One queued, running or finished operation."""

    id: str
    operation: str
    state: JobState = "queued"
    step: str | None = None
    created: datetime = field(default_factory=lambda: datetime.now(UTC))
    started: datetime | None = None
    finished: datetime | None = None
    result: dict[str, Any] | None = None
    problem: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "state": self.state,
            "step": self.step,
            "created": self.created.isoformat(),
            "started": self.started.isoformat() if self.started else None,
            "finished": self.finished.isoformat() if self.finished else None,
            "result": self.result,
            "problem": self.problem,
        }


class JobRunner:
    """Runs submitted work one job at a time and keeps the finished ones."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[tuple[Job, Work] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, operation: str, work: Work) -> Job:
        """Queue work; it runs when the jobs before it are done."""
        job = Job(id=uuid.uuid4().hex[:12], operation=operation)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="jobs", daemon=True
                )
                self._thread.start()
        self._queue.put((job, work))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def stop(self, timeout: float = 30.0) -> None:
        """Let the running job finish, then end the worker."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._queue.put(None)
        thread.join(timeout)

    def _prune(self) -> None:
        finished = [job for job in self._jobs.values() if job.state in FINISHED]
        for job in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            del self._jobs[job.id]

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, work = item
            job.state = "running"
            job.started = datetime.now(UTC)
            try:
                job.result = work(job)
                job.state = "done"
            except JobFailed as exc:
                job.problem = exc.problem
                job.state = "failed"
            except Exception as exc:
                log.exception("job %s (%s) failed", job.id, job.operation)
                job.problem = {
                    "type": "urn:bricklogger:problem:job-failed",
                    "title": "Job failed",
                    "status": 500,
                    "detail": str(exc),
                }
                job.state = "failed"
            finally:
                job.step = None
                job.finished = datetime.now(UTC)
