"""The job runner: one queue for all jobs, a list newest first, and a stop that
finishes the configuration jobs it has accepted. See docs/features/api.md, "Jobs".
"""

from __future__ import annotations

import threading
from typing import Any

from bricklogger.daemon.jobs import Job, JobRunner
from tests.support import wait_for


def blocked(release: threading.Event) -> Any:
    """Work that holds the queue until ``release`` is set."""

    def work(job: Job) -> dict[str, Any]:
        job.step = "holding"
        release.wait(5)
        return {"held": True}

    return work


def done(job: Job) -> dict[str, Any]:
    return {"ran": job.operation}


def test_jobs_run_one_at_a_time_and_list_newest_first() -> None:
    runner = JobRunner()
    release = threading.Event()
    first = runner.submit("activate", blocked(release))
    second = runner.submit("config", done, file="rules", finish_on_stop=True)
    wait_for(lambda: first.state == "running")
    assert second.state == "queued", "the second waits behind the first"
    assert [job.id for job in runner.jobs()] == [second.id, first.id]
    assert [job.id for job in runner.jobs(["queued"])] == [second.id]
    assert {job.id for job in runner.jobs(["queued", "running"])} == {
        first.id,
        second.id,
    }
    assert second.as_dict()["file"] == "rules"
    assert first.as_dict()["file"] is None
    release.set()
    wait_for(lambda: second.state == "done")
    assert second.result == {"ran": "config"}
    assert runner.jobs(["queued", "running"]) == []
    runner.stop(1.0)


def test_a_stop_finishes_configuration_jobs_and_drops_queued_model_jobs() -> None:
    runner = JobRunner()
    release = threading.Event()
    running = runner.submit("upload", blocked(release))
    model = runner.submit("activate", done)
    config = runner.submit("config", done, file="sources", finish_on_stop=True)
    wait_for(lambda: running.state == "running")
    stopper = threading.Thread(target=runner.stop, args=(5.0,))
    stopper.start()
    release.set()
    stopper.join(5)
    assert running.state == "done", "the running job finishes"
    assert config.state == "done", "an accepted configuration job is finished"
    assert model.state == "failed", "a queued model job is dropped"
    assert model.problem is not None and model.problem["title"] == "Job dropped"


def test_a_job_that_outlasts_the_stop_fails_with_a_problem() -> None:
    runner = JobRunner()
    release = threading.Event()
    running = runner.submit("reload", blocked(release), finish_on_stop=True)
    queued = runner.submit("config", done, file="rules", finish_on_stop=True)
    wait_for(lambda: running.state == "running")
    runner.stop(0.2)
    for job in (running, queued):
        assert job.state == "failed"
        assert job.problem is not None
        assert job.problem["detail"] == "the daemon stopped before the job finished"
    release.set()
