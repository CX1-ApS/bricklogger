"""The daemon core end to end with fake plugins: plan, instances, sink, spool,
destinations, status, reload, restart, fallback and the API."""

from __future__ import annotations

import gc
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bricklogger.daemon.api import create_app
from bricklogger.daemon.core import Daemon, DaemonError
from bricklogger.model.timeseries import REF
from bricklogger.sdk.contract import Observation
from tests.fakes import FakeDestination, FakeSource
from tests.support import (
    DESTINATIONS,
    EX,
    INVALID_MODEL,
    MODEL_WITH_RAT,
    REGISTRY,
    RULES_ALL,
    SOURCES,
    finished_job,
    make_config,
    wait_for,
)


def test_end_to_end_collection(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(lambda: len(FakeDestination.received.get("sink_a", [])) >= 6)
        status = daemon.status()
        assert status["health"] == "ok"
        assert status["model"]["active"]["version"] == 1
        assert status["points"]["accepted"] == 6
        assert status["points"]["assigned"] == 3
        assert status["points"]["active"] == 3
        warnings = {w["code"]: w["subject"] for w in daemon.status_warnings()}
        assert warnings == {
            "unclaimed": f"{EX}ZAT_1_18",
            "unknown_reference": f"{EX}CO2",
            "no_reference": f"{EX}Meter_kWh",
            "unit_conflict": f"{EX}SAT",
        }
        points = daemon.points()
        assert points["total"] == 3
        sat = next(item for item in points["items"] if item["uri"] == f"{EX}SAT")
        assert sat["outcome"] == "active" and sat["last_valid"]["value"] >= 1
        assert sat["metadata"]["unit"] == "unit:DEG_C", "the protocol's unit"
        assert sat["metadata"]["graph_unit"] == "unit:DEG_F", "the graph's unit"
        assert sat["metadata"]["brick_class"] == "brick:Supply_Air_Temperature_Sensor"
        assert sat["metadata"]["equipment"] == "ex:AHU_01"
        assert sat["metadata"]["name"] == "SAT"
        (source,) = daemon.status_sources()
        assert source["state"] == "running" and source["resources"] == ["fake:fake_a"]
        (destination,) = daemon.status_destinations()
        assert destination["state"] == "running"
        wait_for(lambda: daemon.status_destinations()[0]["written"] >= 6)
        received = FakeDestination.metadata_received["sink_a"]
        assert any(
            entry.point == f"{EX}SAT"
            and entry.brick_class == "brick:Supply_Air_Temperature_Sensor"
            and entry.equipment == "ex:AHU_01"
            for entry in received
        ), "the graph's part of the metadata reaches the destination"
        assert any(
            entry.point == f"{EX}SAT"
            and entry.unit == "unit:DEG_C"
            and entry.name == "SAT"
            for entry in received
        ), "the plugin's part arrives merged with the graph's"
        assert destination["spool"]["dropped"] == 0
    finally:
        daemon.stop()
    assert daemon.sources["fake_a"].current_state == "stopped"
    assert daemon.destinations["sink_a"].current_state == "stopped"


def test_api_serves_health_status_and_points(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        live = client.get("/health/live").json()
        assert live["live"] == "ok" and live["pid"] == os.getpid()
        assert live["started_at"].endswith("Z"), "UTC to the second"
        assert client.get("/health").json() == {"health": "ok"}
        status = client.get("/v1/status").json()
        assert status["points"]["assigned"] == 3
        page = client.get("/v1/points", params={"limit": 1}).json()
        assert page["total"] == 3 and len(page["items"]) == 1
        assert {w["code"] for w in client.get("/v1/status/warnings").json()} == {
            "unclaimed",
            "unknown_reference",
            "no_reference",
            "unit_conflict",
        }
        assert client.get("/v1/status/sources").json()[0]["name"] == "fake_a"
        missing = client.get("/v1/nothing")
        assert missing.status_code == 404
        assert missing.headers["content-type"] == "application/problem+json"
    finally:
        daemon.stop()


def test_api_requires_a_token_when_exposed(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path,
        template,
        sources=SOURCES,
        destinations=DESTINATIONS,
        daemon_extra="api:\n  host: 0.0.0.0\n  token: secret\n",
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        assert client.get("/health").status_code == 200, "health stays open"
        refused = client.get("/v1/status")
        assert refused.status_code == 401
        assert refused.headers["content-type"] == "application/problem+json"
        allowed = client.get("/v1/status", headers={"Authorization": "Bearer secret"})
        assert allowed.status_code == 200
    finally:
        daemon.stop()


def test_reload_applies_a_new_rule_set_and_rejects_an_invalid_one(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        assert len(daemon.assignment_of("fake_a")) == 3
        (config_dir / "rules.yaml").write_text(
            "- match: { class: brick:Point }\n  action: deny\n"
        )
        result = daemon.reload()
        assert result.valid
        assert daemon.assignment_of("fake_a") == []
        assert daemon.status()["points"]["accepted"] == 0
        assert daemon.status_warnings() == []
        (config_dir / "rules.yaml").write_text("- action: accept\n")
        rejected = daemon.reload()
        assert not rejected.valid
        assert daemon.configuration is not None
        assert daemon.configuration.rules[0].action == "deny", "the running one is kept"
    finally:
        daemon.stop()


def test_a_crashed_source_is_restarted_with_its_assignment(
    tmp_path: Path, template: Path
) -> None:
    sources = (
        "fake_a:\n  type: fake-source\n  claims: [SAT]\n"
        "  crash_after: 2\n  interval: 0.02\n"
    )
    config_dir = make_config(
        tmp_path, template, sources=sources, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: (
                daemon.status_sources()[0]["restart_count"] >= 1
                and daemon.status_sources()[0]["state"] == "running"
            ),
            timeout=10.0,
        )
        assert (
            any(w["code"] == "instance_failed" for w in daemon.status_warnings())
            is False
        )
        wait_for(lambda: len(FakeDestination.received.get("sink_a", [])) >= 3)
    finally:
        daemon.stop()


def test_unsupported_points_get_the_rules_fallback(
    tmp_path: Path, template: Path
) -> None:
    rules = (
        "- match: { class: brick:Point }\n  action: accept\n  method: subscribe\n"
        "  fallback: { method: poll, interval: 1m }\n"
    )
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS, rules=rules
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: any(
                point.uri == f"{EX}Unsupported_Damper" and point.method == "poll"
                for point in daemon.assignment_of("fake_a")
            )
        )
        damper = next(
            item
            for item in daemon.points()["items"]
            if item["uri"] == f"{EX}Unsupported_Damper"
        )
        assert damper["fallback_active"] is True and damper["method"] == "poll"
    finally:
        daemon.stop()


def test_double_claims_and_resource_collisions_are_refused(
    tmp_path: Path, template: Path
) -> None:
    sources = (
        "fake_a:\n  type: fake-source\n  claims: [SAT]\n"
        "fake_b:\n  type: fake-source\n  claims: [SAT]\n"
    )
    config_dir = make_config(
        tmp_path, template, sources=sources, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    with pytest.raises(DaemonError, match="claimed by several"):
        daemon.start()
    daemon.stop()


@pytest.mark.skipif(os.getuid() == 0, reason="root writes where it likes")
def test_a_data_directory_it_cannot_be_written_fails_with_one_line(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    data_dir = tmp_path / "var"
    data_dir.chmod(0o500)
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    try:
        with pytest.raises(DaemonError, match=r"the data directory .* cannot be used"):
            daemon.start()
    finally:
        data_dir.chmod(0o700)
    daemon.stop()


def test_a_destination_that_cannot_start_is_retried_while_the_spool_fills(
    tmp_path: Path, template: Path
) -> None:
    destinations = (
        "sink_a:\n  type: fake-destination\n  fail_first_start: true\n"
        "  batch: { size: 5, interval: 1s }\n"
    )
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=destinations
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: daemon.status_destinations()[0]["state"] == "running", timeout=10.0
        )
        wait_for(lambda: len(FakeDestination.received.get("sink_a", [])) >= 3)
        assert daemon.status()["health"] == "ok"
    finally:
        daemon.stop()


def test_a_destination_whose_connection_breaks_is_started_again(
    tmp_path: Path, template: Path
) -> None:
    """A far end that goes away — a restarted database — must not be permanent."""
    destinations = (
        "sink_a:\n  type: fake-destination\n  break_after: 1\n"
        "  batch: { size: 5, interval: 1s }\n"
    )
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=destinations
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: daemon.status_destinations()[0]["state"] == "failed", timeout=10.0
        )
        assert any(w["code"] == "instance_failed" for w in daemon.status_warnings())
        assert daemon.status()["health"] == "degraded"
        broken_at = len(FakeDestination.received.get("sink_a", []))

        wait_for(
            lambda: daemon.status_destinations()[0]["state"] == "running", timeout=15.0
        )
        assert FakeDestination.starts["sink_a"] == 2, "started afresh, not reused"
        wait_for(
            lambda: len(FakeDestination.received.get("sink_a", [])) > broken_at,
            timeout=10.0,
        )
        assert daemon.status()["health"] == "ok"
        assert not any(w["code"] == "instance_failed" for w in daemon.status_warnings())
        assert daemon.status_destinations()[0]["spool"]["dropped"] == 0
    finally:
        daemon.stop()


def test_without_a_model_the_daemon_is_idle(tmp_path: Path) -> None:
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "daemon.yaml").write_text(f"data_dir: {tmp_path / 'var'}\n")
    (config_dir / "sources.yaml").write_text(SOURCES)
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        assert daemon.health() == "idle"
        assert daemon.status()["model"]["active"] is None
        assert daemon.assignment_of("fake_a") == []
    finally:
        daemon.stop()


def test_api_config_endpoints_read_validate_and_replace(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        everything = client.get("/v1/config").json()
        assert set(everything) == {"daemon", "sources", "destinations", "rules"}
        assert everything["sources"]["fake_a"]["type"] == "fake-source"
        as_text = client.get("/v1/config/rules")
        assert as_text.headers["content-type"].startswith("application/yaml")
        assert as_text.text == RULES_ALL
        as_json = client.get("/v1/config/rules", headers={"Accept": "application/json"})
        assert as_json.json()[0]["name"] == "Everything"
        assert client.get("/v1/config/web").status_code == 404

        broken = "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"
        rejected = client.put("/v1/config/rules", content=broken)
        assert rejected.status_code == 422
        assert rejected.headers["content-type"] == "application/problem+json"
        assert "interval" in json.dumps(rejected.json()["errors"])
        assert (config_dir / "rules.yaml").read_text() == RULES_ALL, "not written"

        deny = "- match: { class: brick:Point }\n  action: deny\n"
        applied = client.put("/v1/config/rules", content=deny)
        assert applied.status_code == 200 and applied.json()["valid"] is True
        assert (config_dir / "rules.yaml").read_text() == deny
        assert daemon.assignment_of("fake_a") == []
        assert client.get("/v1/status").json()["points"]["accepted"] == 0

        proposed = client.post("/v1/config/validate", json={"rules": RULES_ALL})
        assert proposed.status_code == 200 and proposed.json()["valid"] is True
        on_disk = client.post("/v1/config/validate")
        assert on_disk.status_code == 200 and on_disk.json()["valid"] is True
        assert client.post("/v1/config/validate", json={"web": "x"}).status_code == 422
        assert client.post("/v1/config/init").status_code == 409
    finally:
        daemon.stop()


def test_api_points_filter_on_warning_and_class(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: any(w["code"] == "unit_conflict" for w in daemon.status_warnings())
        )
        client = TestClient(create_app(daemon))
        by_warning = client.get(
            "/v1/points", params={"warning": "unit_conflict"}
        ).json()
        assert [item["uri"] for item in by_warning["items"]] == [f"{EX}SAT"]
        prefixed = client.get(
            "/v1/points", params={"class": "brick:Supply_Air_Temperature_Sensor"}
        ).json()
        assert prefixed["total"] == 1 and prefixed["items"][0]["uri"] == f"{EX}SAT"
        full = client.get(
            "/v1/points",
            params={
                "class": "https://brickschema.org/schema/Brick#Supply_Air_Temperature_Sensor"
            },
        ).json()
        assert full["total"] == 1, "a full IRI is compacted before it is compared"
        assert (
            client.get("/v1/points", params={"class": "brick:CO2_Sensor"}).json()[
                "total"
            ]
            == 0
        )

        # The two text filters search: part of the value is enough.
        def classes(**params: str) -> list[str]:
            page = client.get("/v1/points", params=params).json()
            return [item["metadata"]["brick_class"] for item in page["items"]]

        searched = classes(**{"class": "Temperature"})
        assert searched and all("Temperature" in name for name in searched)
        assert classes(**{"class": "temperature"}) == searched, "case is ignored"
        assert classes(**{"class": "Air_Temperature"}) == searched
        assert classes(**{"class": "Air%Temperature"}) == [], "% is not a wildcard"
        assert classes(**{"class": "AirXTemperature"}) == [], "_ is not a wildcard"
        assert [
            item["uri"]
            for item in client.get("/v1/points", params={"warning": "unit"}).json()[
                "items"
            ]
        ] == [f"{EX}SAT"]
        # The precise filters stay precise.
        assert (
            client.get("/v1/points", params={"instance": "fake"}).json()["total"] == 0
        )
        assert (
            client.get("/v1/points", params={"instance": "fake_a"}).json()["total"] > 0
        )
    finally:
        daemon.stop()


def test_api_models_and_jobs(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        listing = client.get("/v1/models").json()
        assert listing["active"] == 1
        assert [v["version"] for v in listing["versions"]] == [1]
        assert listing["versions"][0]["active"] is True

        accepted = client.post(
            "/v1/models",
            content=MODEL_WITH_RAT.encode(),
            headers={"Content-Type": "text/turtle"},
        )
        assert accepted.status_code == 202
        job = accepted.json()
        assert accepted.headers["location"] == f"/v1/jobs/{job['id']}"
        assert job["operation"] == "upload" and job["state"] in ("queued", "running")
        job = finished_job(client, job["id"])
        assert job["state"] == "done", job
        result = job["result"]
        assert result["version"]["version"] == 2 and result["activated"] is True
        assert result["previous"] == 1 and result["diff"]["added"] == [f"{EX}RAT"]
        assert result["activation"]["inferred_triples"] > 0
        assert daemon.active_version == 2
        assert client.get("/v1/models").json()["active"] == 2
        without_reference = {
            w["subject"]
            for w in client.get("/v1/status/warnings").json()
            if w["code"] == "no_reference"
        }
        assert f"{EX}RAT" in without_reference, "the plan follows the new model"

        rejected = client.post(
            "/v1/models",
            content=INVALID_MODEL.encode(),
            headers={"Content-Type": "text/turtle"},
        )
        job = finished_job(client, rejected.json()["id"])
        assert job["state"] == "failed" and job["problem"]["status"] == 422
        assert job["problem"]["errors"][0]["focus"] == f"{EX}Bad"
        versions = [v["version"] for v in client.get("/v1/models").json()["versions"]]
        assert versions == [1, 2], "an invalid upload is not stored"

        back = client.post("/v1/models/1/activate")
        assert back.status_code == 202
        job = finished_job(client, back.json()["id"])
        assert job["state"] == "done" and job["result"]["diff"]["removed"] == [
            f"{EX}RAT"
        ]
        assert daemon.active_version == 1
        assert client.post("/v1/models/99/activate").status_code == 404

        diff = client.get("/v1/models/diff", params={"a": 1, "b": 2}).json()
        assert diff == {"added": [f"{EX}RAT"], "removed": [], "changed": []}
        exported = client.get("/v1/models/2")
        assert exported.headers["content-type"].startswith("text/turtle")
        assert exported.text == MODEL_WITH_RAT
        with_inferred = client.get("/v1/models/1", params={"inferred": "true"})
        assert with_inferred.status_code == 200
        assert "hasPoint" in with_inferred.text, "the inverse relation is inferred"
        assert (
            client.get("/v1/models/2", params={"inferred": "true"}).status_code == 409
        )
        assert client.get("/v1/models/7").status_code == 404
        assert client.get("/v1/jobs/nope").status_code == 404
        unsupported = client.post(
            "/v1/models", content=b"x", headers={"Content-Type": "application/pdf"}
        )
        assert unsupported.status_code == 415
    finally:
        daemon.stop()


def test_api_sparql_endpoint(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        select = "SELECT ?p WHERE { ?p a brick:Supply_Air_Temperature_Sensor }"
        answer = client.get("/v1/sparql", params={"query": select})
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith(
            "application/sparql-results+json"
        )
        bindings = answer.json()["results"]["bindings"]
        assert [b["p"]["value"] for b in bindings] == [f"{EX}SAT"], (
            "prefixes are declared"
        )

        inferred = client.get(
            "/v1/sparql",
            params={"query": "SELECT ?p WHERE { ex:AHU_01 brick:hasPoint ?p }"},
        ).json()
        assert len(inferred["results"]["bindings"]) >= 3, (
            "the inferred graph is queried"
        )

        csv = client.get(
            "/v1/sparql", params={"query": select}, headers={"Accept": "text/csv"}
        )
        assert csv.headers["content-type"].startswith("text/csv") and "SAT" in csv.text

        posted = client.post(
            "/v1/sparql",
            content="CONSTRUCT WHERE { ex:SAT ?p ?o }",
            headers={"Content-Type": "application/sparql-query"},
        )
        assert posted.status_code == 200
        assert posted.headers["content-type"].startswith("text/turtle")
        assert "Supply_Air_Temperature_Sensor" in posted.text

        form = client.post("/v1/sparql", data={"query": "ASK { ex:SAT a brick:Point }"})
        assert form.status_code == 200 and form.json()["boolean"] is True

        update = client.get(
            "/v1/sparql", params={"query": "INSERT DATA { ex:X a brick:Point }"}
        )
        assert update.status_code == 400 and "read-only" in update.json()["detail"]
        broken = client.get("/v1/sparql", params={"query": "SELECT ?p WHERE {"})
        assert (
            broken.status_code == 400 and broken.json()["title"] == "Query is invalid"
        )
        missing = client.get("/v1/sparql")
        assert missing.status_code == 422
        assert missing.headers["content-type"] == "application/problem+json"
        wrong_type = client.post(
            "/v1/sparql", content="x", headers={"Content-Type": "text/plain"}
        )
        assert wrong_type.status_code == 415
    finally:
        daemon.stop()


def test_api_plugins_tools_and_instance_control(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
        listed = client.get("/v1/plugins").json()
        assert {p["type"]: p["role"] for p in listed} == {
            "fake-source": "source",
            "fake-destination": "destination",
        }
        assert next(p for p in listed if p["type"] == "fake-source")["instances"] == [
            "fake_a"
        ]

        declared = client.get("/v1/plugins/fake-source").json()
        assert [m["name"] for m in declared["methods"]] == ["subscribe"]
        assert [t["name"] for t in declared["tools"]] == [
            "echo",
            "fail",
            "dump",
            "claims",
        ]
        assert [t["document"] for t in declared["tools"]] == [
            False,
            False,
            True,
            False,
        ]
        assert declared["tools"][2]["offered_on"] == {
            "tool": "claims",
            "parameters": {"scope": "claim"},
        }
        assert declared["tools"][0]["offered_on"] is None
        assert "claims" in declared["config_schema"]["properties"]
        assert declared["tools"][0]["parameters"]["required"] == ["text"]
        assert client.get("/v1/plugins/nope").status_code == 404

        base = "/v1/plugins/fake-source/instances/fake_a"
        echoed = client.post(f"{base}/tools/echo", json={"text": "ab", "times": 2})
        assert echoed.status_code == 200 and echoed.json() == {
            "instance": "fake_a",
            "echo": "abab",
        }
        invalid = client.post(f"{base}/tools/echo", json={"text": "ab", "times": "x"})
        assert (
            invalid.status_code == 422 and invalid.json()["errors"][0]["key"] == "times"
        )
        assert client.post(f"{base}/tools/nothing", json={}).status_code == 404
        failed = client.post(f"{base}/tools/fail", json={})
        assert failed.status_code == 500 and "broke" in failed.json()["detail"]
        assert (
            client.post("/v1/plugins/fake-source/instances/nope/tools/echo").status_code
            == 404
        )

        stopped = client.post(f"{base}/stop")
        assert stopped.status_code == 200 and stopped.json()["state"] == "stopped"
        assert stopped.json()["stopped_by_operator"] is True
        assert {w["code"] for w in daemon.status_warnings()} >= {"instance_stopped"}
        assert client.get("/health").json() == {"health": "idle"}
        assert client.post(f"{base}/tools/echo", json={"text": "a"}).status_code == 409

        started = client.post(f"{base}/start")
        assert (
            started.status_code == 200
            and started.json()["stopped_by_operator"] is False
        )
        wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
        assert not any(
            w["code"] == "instance_stopped" for w in daemon.status_warnings()
        )
        assert client.get("/health").json() == {"health": "ok"}

        restarted = client.post(f"{base}/restart")
        assert restarted.status_code == 200
        wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
        assert client.post(f"{base}/dance").status_code == 404

        sink = "/v1/plugins/fake-destination/instances/sink_a"
        assert client.post(f"{sink}/stop").json()["state"] == "stopped"
        assert client.post(f"{sink}/start").status_code == 200
        wait_for(lambda: daemon.status_destinations()[0]["state"] == "running")
    finally:
        daemon.stop()


def test_an_operator_stop_survives_a_restart_of_the_daemon(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        daemon.control_instance("fake-source", "fake_a", "stop")
        daemon.control_instance("fake-destination", "sink_a", "stop")
    finally:
        daemon.stop()
    FakeSource.instances.clear()
    del daemon
    gc.collect()
    again = Daemon(config_dir, registry=REGISTRY, env={})
    again.start()
    try:
        assert again.status_sources()[0]["state"] == "stopped"
        assert again.status_destinations()[0]["state"] == "stopped"
        assert {
            w["subject"]
            for w in again.status_warnings()
            if w["code"] == "instance_stopped"
        } == {
            "fake_a",
            "sink_a",
        }
        again.control_instance("fake-destination", "sink_a", "start")
        wait_for(lambda: again.status_destinations()[0]["state"] == "running")
    finally:
        again.stop()


def test_the_value_overlay_follows_the_observations_and_the_plan(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        wait_for(lambda: daemon.status()["observations_received"] >= 3)
        assert daemon.overlay is not None
        daemon.overlay.flush()
        query = (
            "SELECT ?v ?t WHERE { ex:SAT brick:lastKnownValue ?n . "
            "?n brick:value ?v ; brick:timestamp ?t }"
        )
        bindings = client.get("/v1/sparql", params={"query": query}).json()["results"][
            "bindings"
        ]
        assert len(bindings) == 1, bindings
        assert float(bindings[0]["v"]["value"]) >= 1.0
        assert bindings[0]["v"]["datatype"].endswith("#double")
        assert bindings[0]["t"]["datatype"].endswith("#dateTime")
        exported = client.get("/v1/models/1", params={"values": "true"}).text
        assert "lastKnownValue" in exported and "SAT" in exported

        applied = client.put(
            "/v1/config/rules",
            content="- match: { class: brick:Point }\n  action: deny\n",
        )
        assert applied.status_code == 200
        daemon.overlay.flush()
        gone = client.get("/v1/sparql", params={"query": query}).json()["results"]
        assert gone["bindings"] == [], "a point that leaves the plan leaves the overlay"
    finally:
        daemon.stop()


def test_warnings_end_and_can_be_cleared(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        _exercise_warnings(daemon)
    finally:
        daemon.stop()
    FakeSource.instances.clear()
    del daemon
    gc.collect()
    again = Daemon(config_dir, registry=REGISTRY, env={})
    again.start()
    try:
        assert not any(w["code"] == "stop_timeout" for w in again.status_warnings()), (
            "the abandoned instance died with the old process"
        )
    finally:
        again.stop()


def _exercise_warnings(daemon: Daemon) -> None:
    """Every end the daemon watches for, and the manual clear, on a running daemon."""
    wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
    state = daemon.state
    assert state is not None and daemon.sink is not None

    # first seen stays, last seen moves, the count grows
    state.warn("stop_timeout", "ghost", "did not stop")
    (stale,) = [w for w in daemon.status_warnings() if w["code"] == "stop_timeout"]
    assert stale["first_seen"] == stale["last_seen"] and stale["count"] == 1
    state.warn("stop_timeout", "ghost", "did not stop")
    (stale,) = [w for w in daemon.status_warnings() if w["code"] == "stop_timeout"]
    assert stale["count"] == 2 and stale["last_seen"] >= stale["first_seen"]

    # health follows the spool_drop warning, not the lifetime counter
    state.increment("spool_drop:sink_a", 3)
    assert daemon.health() == "ok"
    state.warn("spool_drop", "sink_a", "the spool dropped 3 observations")
    assert daemon.health() == "degraded"
    assert state.clear_warnings(code="spool_drop", subject="sink_a") == 1
    assert daemon.health() == "ok"

    # a device that reports a round without a new skip withdraws poll_overrun
    channel = daemon.sources["fake_a"].status
    channel.device("d1", reachable=True, skipped_rounds=2)
    assert state.has_warning("poll_overrun", "fake_a/d1")
    channel.device("d1", reachable=True, skipped_rounds=2)
    assert not state.has_warning("poll_overrun", "fake_a/d1"), "no new skip"
    channel.device("d1", reachable=True, skipped_rounds=3)
    assert state.has_warning("poll_overrun", "fake_a/d1")
    channel.device("d1", reachable=True)
    assert not state.has_warning("poll_overrun", "fake_a/d1")

    # a point whose timestamps come right again withdraws future_timestamp
    sat = f"{EX}SAT"
    later = datetime.now(UTC) + timedelta(hours=1)
    daemon.sink.observations([Observation(sat, later, "number", 1.0)])
    assert state.has_warning("future_timestamp", sat)
    daemon.sink.observations([Observation(sat, datetime.now(UTC), "number", 1.0)])
    assert not state.has_warning("future_timestamp", sat)

    # clearing by hand answers with the count
    state.warn("stop_timeout", "other", "x")
    assert daemon.clear_warnings("stop_timeout", "other") == 1
    assert daemon.clear_warnings("stop_timeout") == 1, "ghost"
    assert daemon.clear_warnings("stop_timeout") == 0

    # a clean stop withdraws the instance's own stop_timeout
    state.warn("stop_timeout", "fake_a", "did not stop")
    state.warn("stop_timeout", "sink_a", "did not stop")
    daemon.control_instance("fake-source", "fake_a", "stop")
    daemon.control_instance("fake-destination", "sink_a", "stop")
    assert not state.has_warning("stop_timeout", "fake_a")
    assert not state.has_warning("stop_timeout", "sink_a")
    state.warn("stop_timeout", "ghost", "left from an earlier process")


def test_the_api_clears_warnings_by_hand(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
        client = TestClient(create_app(daemon))
        assert daemon.state is not None
        daemon.state.warn("stop_timeout", "ghost", "x")
        daemon.state.warn("stop_timeout", "other", "x")
        assert client.delete(
            "/v1/status/warnings", params={"code": "stop_timeout", "subject": "other"}
        ).json() == {"cleared": 1}
        assert client.delete(
            "/v1/status/warnings", params={"code": "stop_timeout"}
        ).json() == {"cleared": 1}
        listed = client.get("/v1/status/warnings").json()
        assert listed and all("last_seen" in w for w in listed)
        assert not any(w["code"] == "stop_timeout" for w in listed)
        assert client.delete("/v1/status/warnings").json() == {"cleared": len(listed)}
        assert client.get("/v1/status/warnings").json() == []
    finally:
        daemon.stop()


def test_a_restart_hands_the_sources_their_latest_observations(
    tmp_path: Path, template: Path
) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        _collect_a_few(daemon)
    finally:
        daemon.stop()
    FakeSource.instances.clear()
    del daemon
    gc.collect()
    again = Daemon(config_dir, registry=REGISTRY, env={})
    again.start()
    try:
        wait_for(lambda: bool(FakeSource.instances.get("fake_a")))
        wait_for(lambda: bool(FakeSource.instances["fake_a"].assignments))
        resumed = FakeSource.instances["fake_a"].assignments[0]
        stamps = [point.last_observation for point in resumed]
        assert stamps and all(
            stamp is not None and stamp.tzinfo is not None for stamp in stamps
        ), "the runtime state remembers the latest observation per point"
    finally:
        again.stop()


def _collect_a_few(daemon: Daemon) -> None:
    wait_for(lambda: daemon.status()["observations_received"] >= 3)
    first = FakeSource.instances["fake_a"].assignments[0]
    assert all(point.last_observation is None for point in first), (
        "nothing recorded before the first round"
    )


def test_api_entities_projects_the_active_model(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        wait_for(
            lambda: any(w["code"] == "unclaimed" for w in daemon.status_warnings())
        )
        client = TestClient(create_app(daemon))
        document = client.get("/v1/entities").json()
        assert document["version"] == 1
        assert document["prefixes"]["ex"] == EX
        elements = {entity["uri"]: entity for entity in document["entities"]}

        # The kinds, including a node that is neither point, equipment nor place.
        assert elements["ex:SAT"]["kind"] == "point"
        assert elements["ex:AHU_01"]["kind"] == "equipment"
        assert elements["ex:Room_1_17"]["kind"] == "location"
        assert elements["ex:Ctrl"]["kind"] == "other"

        # What the model lacks. Nothing in this model has a location at all.
        assert "no_location" in elements["ex:AHU_01"]["findings"]
        assert "deprecated_class" in elements["ex:Room_1_17"]["findings"]
        assert elements["ex:Ctrl"]["findings"] == [], "a device is never orphaned"
        assert "no_reference" in elements["ex:Meter_kWh"]["findings"]
        assert "no_reference" not in elements["ex:SAT"]["findings"]

        # The two lenses: the graph's findings and the daemon's own reading.
        assert elements["ex:Meter_kWh"]["warnings"] == ["no_reference"]
        assert elements["ex:CO2"]["warnings"] == ["unknown_reference"]
        assert elements["ex:ZAT_1_18"]["warnings"] == ["unclaimed"]
        assert elements["ex:SAT"]["runtime"]["accepted"] is True
        assert elements["ex:SAT"]["runtime"]["instance"] == "fake_a"
        assert elements["ex:AHU_01"]["runtime"] is None

        relation = next(
            r
            for r in document["relations"]
            if (r["subject"], r["object"]) == ("ex:SAT", "ex:AHU_01")
        )
        assert relation["predicate"] == "brick:isPointOf"
        assert relation["role"] == "point" and relation["child"] == "subject"

        # Narrowing: a class with its subclasses, a finding, a root, a search.
        subclasses = client.get(
            "/v1/entities", params={"class": "brick:Temperature_Sensor"}
        ).json()
        assert {e["uri"] for e in subclasses["entities"] if not e["context"]} == {
            "ex:SAT",
            "ex:ZAT_1_17",
            "ex:ZAT_1_18",
        }
        assert subclasses["counts"]["entities"] == 3
        assert any(e["context"] for e in subclasses["entities"]), "ancestors are kept"

        points = client.get("/v1/entities", params={"kind": "point"}).json()
        assert points["counts"]["kinds"]["point"] == 6
        missing = client.get("/v1/entities", params={"finding": "no_reference"}).json()
        assert [e["uri"] for e in missing["entities"] if not e["context"]] == [
            "ex:Meter_kWh"
        ]
        under = client.get("/v1/entities", params={"root": "ex:AHU_01"}).json()
        assert {e["uri"] for e in under["entities"]} == {
            "ex:AHU_01",
            "ex:SAT",
            "ex:Unsupported_Damper",
            "ex:Meter_kWh",
        }
        assert client.get("/v1/entities", params={"root": "ex:Nope"}).status_code == 404
        assert client.get("/v1/entities", params={"kind": "dance"}).status_code == 422

        # The value overlay reaches the document once the daemon has flushed it.
        def value_of(uri: str) -> dict[str, Any] | None:
            page = client.get("/v1/entities", params={"kind": "point"}).json()
            for entity in page["entities"]:
                if entity["uri"] == uri:
                    reading: dict[str, Any] | None = entity["last_known_value"]
                    return reading
            return None

        wait_for(lambda: value_of("ex:SAT") is not None, timeout=30)
        reading = value_of("ex:SAT")
        assert reading is not None
        assert isinstance(reading["value"], int | float)
        assert reading["time"], "the observation's own time travels with it"
        assert value_of("ex:Meter_kWh") is None, "a point no source claims has none"
    finally:
        daemon.stop()


def test_the_model_lies_beside_the_data(tmp_path: Path, template: Path) -> None:
    config_dir = make_config(
        tmp_path, template, sources=SOURCES, destinations=DESTINATIONS
    )
    daemon = Daemon(config_dir, registry=REGISTRY, env={})
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        wait_for(lambda: daemon.status_destinations()[0]["model_version"] == 1)
        (destination,) = daemon.status_destinations()
        assert destination["stores_model"] is True
        model = FakeDestination.models_received["sink_a"][-1]
        assert model.version == 1 and len(model.activations) == 1
        keys = FakeDestination.keys["sink_a"]
        assert f"{EX}SAT" in keys, "the destination met the point through metadata"
        ours = f'hasTimeseriesId "{keys[f"{EX}SAT"]}"'
        assert ours in model.turtle

        query = (
            "SELECT ?id WHERE { GRAPH <urn:bricklogger:timeseries:sink_a> { "
            "ex:SAT ref:hasExternalReference ?r . ?r ref:hasTimeseriesId ?id } }"
        )
        bindings = client.get("/v1/sparql", params={"query": query}).json()["results"][
            "bindings"
        ]
        assert [b["id"]["value"] for b in bindings] == [keys[f"{EX}SAT"]]
        assert not daemon.status_destinations()[0]["last_error"]
        assert daemon.graph is not None
        ask = (
            f"ASK {{ <{EX}SAT> <{REF}hasExternalReference> ?r . "
            f"?r a <{REF}TimeseriesReference> }}"
        )
        assert daemon.graph.query(ask, references=True)
        assert not daemon.graph.query(ask), "the plan and the sources do not see them"

        exported = client.get("/v1/models/1", params={"timeseries": "true"})
        assert exported.status_code == 200 and ours in exported.text
        named = client.get("/v1/models/1", params={"timeseries": "sink_a"})
        assert named.status_code == 200
        assert client.get(
            "/v1/models/1", params={"timeseries": "nope"}
        ).status_code == (404)
        assert ours not in client.get("/v1/models/1").text, "the model as uploaded"

        accepted = client.post(
            "/v1/models",
            content=MODEL_WITH_RAT.encode(),
            headers={"Content-Type": "text/turtle"},
        )
        assert finished_job(client, accepted.json()["id"])["state"] == "done"
        wait_for(lambda: daemon.status_destinations()[0]["model_version"] == 2)
        assert client.get(
            "/v1/models/1", params={"timeseries": "true"}
        ).status_code == (409), "the references exist for the active version only"

        # a changed instance starts afresh and is offered every version
        FakeDestination.models_received["sink_a"].clear()
        changed = client.put(
            "/v1/config/destinations",
            content=DESTINATIONS.replace("size: 5", "size: 6"),
        )
        assert changed.status_code == 200, changed.text
        wait_for(
            lambda: (
                {m.version for m in FakeDestination.models_received["sink_a"]} == {1, 2}
            )
        )
        first = next(
            m for m in FakeDestination.models_received["sink_a"] if m.version == 1
        )
        assert len(first.activations) == 1 and ours in first.turtle
    finally:
        daemon.stop()
