"""Jobs: the API's long operations — model upload and activation, a
configuration write and a reload.

They run one at a time in one worker thread, and their state can be read while
they run and after they finish, until the daemon restarts. A stop finishes the
configuration jobs it has accepted and drops the queued model jobs. See
``docs/features/api.md``, "Jobs".
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable, Collection
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
    file: str | None = None
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
            "file": self.file,
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
        # the jobs a stop finishes rather than drops: the configuration's
        self._kept: set[str] = set()
        self._stopping = False

    def submit(
        self,
        operation: str,
        work: Work,
        *,
        file: str | None = None,
        finish_on_stop: bool = False,
    ) -> Job:
        """Queue work; it runs when the jobs before it are done.

        ``finish_on_stop`` marks a job that a stop runs to the end instead of
        dropping it while it is still queued.
        """
        job = Job(id=uuid.uuid4().hex[:12], operation=operation, file=file)
        with self._lock:
            self._jobs[job.id] = job
            if finish_on_stop:
                self._kept.add(job.id)
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

    def jobs(self, states: Collection[str] | None = None) -> list[Job]:
        """The jobs kept, newest first; ``states`` narrows them."""
        with self._lock:
            found = list(self._jobs.values())
        found.reverse()
        if states:
            found = [job for job in found if job.state in states]
        return found

    def stop(self, timeout: float = 30.0) -> None:
        """Finish the running job and the queued ones a stop keeps, then end.

        Queued jobs that are not kept are dropped. What has not finished when
        ``timeout`` runs out fails with a problem saying so.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        with self._lock:
            self._stopping = True
        self._queue.put(None)
        thread.join(timeout)
        if not thread.is_alive():
            return
        with self._lock:
            unfinished = [
                job for job in self._jobs.values() if job.state not in FINISHED
            ]
        for job in unfinished:
            log.error(
                "job %s (%s) did not finish before the stop", job.id, job.operation
            )
            job.problem = {
                "type": "urn:bricklogger:problem:job-not-finished",
                "title": "Job not finished",
                "status": 503,
                "detail": "the daemon stopped before the job finished",
            }
            job.step = None
            job.state = "failed"
            job.finished = datetime.now(UTC)

    def _prune(self) -> None:
        finished = [job for job in self._jobs.values() if job.state in FINISHED]
        for job in finished[: max(0, len(finished) - KEEP_FINISHED)]:
            del self._jobs[job.id]
            self._kept.discard(job.id)

    def _dropped(self, job: Job) -> bool:
        """Whether a stop drops this queued job instead of running it."""
        with self._lock:
            if not self._stopping or job.id in self._kept:
                return False
        log.info("job %s (%s) dropped at the stop", job.id, job.operation)
        job.problem = {
            "type": "urn:bricklogger:problem:job-dropped",
            "title": "Job dropped",
            "status": 503,
            "detail": "the daemon stopped before the job began",
        }
        job.state = "failed"
        job.finished = datetime.now(UTC)
        return True

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, work = item
            if self._dropped(job):
                continue
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
