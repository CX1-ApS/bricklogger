"""Validation and inference in a child process: the same answers as in the
daemon's own process, and a clean failure when the child dies."""

from __future__ import annotations

import os
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rdflib import BNode

from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon
from bricklogger.model import child
from bricklogger.model.child import (
    ChildEnded,
    validate_and_infer_in_child,
    validate_model_in_child,
)
from bricklogger.model.inference import ModelInvalid, validate_and_infer
from tests.support import (
    DESTINATIONS,
    EX,
    INVALID_MODEL,
    MODEL,
    MODEL_WITH_RAT,
    REGISTRY,
    SOURCES,
    finished_job,
    make_config,
)


def named(triples: set[Any]) -> set[Any]:
    return {t for t in triples if not any(isinstance(term, BNode) for term in t)}


def die_at_once(conn: Connection, *args: Any) -> None:
    """A child that ends the way an out-of-memory kill does: without a word."""
    os._exit(9)


def test_the_child_infers_what_the_daemon_would() -> None:
    steps: list[str] = []
    in_child = validate_and_infer_in_child(MODEL.encode(), "turtle", steps.append, ())
    in_process = validate_and_infer(MODEL.encode(), "turtle", vocabularies=())
    assert steps == ["validating", "inferring"]
    # each parse names the blank nodes afresh, so compare what has no blank node
    assert named(in_child.model) == named(in_process.model)
    assert named(in_child.inferred) == named(in_process.inferred)
    assert len(in_child.model) == len(in_process.model)
    assert len(in_child.inferred) == len(in_process.inferred)
    assert in_child.report.valid


def test_the_child_reports_violations_as_the_daemon_would() -> None:
    with pytest.raises(ModelInvalid) as raised:
        validate_and_infer_in_child(INVALID_MODEL.encode(), "turtle", None, ())
    assert raised.value.report.violations[0].focus == f"{EX}Bad"
    report = validate_model_in_child(INVALID_MODEL.encode(), "turtle", ())
    assert not report.valid


def test_a_child_that_dies_fails_without_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "_work", die_at_once)
    with pytest.raises(ChildEnded, match="exit code 9"):
        validate_and_infer_in_child(MODEL.encode(), "turtle", None, ())


def test_a_dead_child_fails_the_job_and_the_running_model_stays(
    tmp_path: Path, template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        monkeypatch.setattr(child, "_work", die_at_once)
        upload = client.post(
            "/v1/models",
            content=MODEL_WITH_RAT.encode(),
            headers={"Content-Type": "text/turtle"},
        )
        job = finished_job(client, upload.json()["id"])
        assert job["state"] == "failed"
        assert "ended without a result" in job["problem"]["detail"]
        assert daemon.active_version == 1
        versions = [v["version"] for v in client.get("/v1/models").json()["versions"]]
        assert versions == [1], "nothing is stored without a result"
        assert client.get("/v1/status").json()["health"] in ("ok", "idle")
    finally:
        daemon.stop()
