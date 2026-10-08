"""The TimescaleDB destination: encoding without a database, and the whole
path against a real database when one is given through BRICKLOGGER_TEST_DSN."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from bricklogger.plugins.timescaledb.declaration import (
    DESTINATION,
    TimescaleDBConfig,
    TimescaleDBDestination,
)
from bricklogger.plugins.timescaledb.destination import (
    REASON_CODES,
    encode_observation,
    point_row,
    state_rows,
)
from bricklogger.sdk.contract import ModelDocument, Observation, PointMetadata

P = "https://example.com/bldg#SAT"
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_observations_are_encoded_into_the_two_tables() -> None:
    assert encode_observation(7, Observation(P, NOW, "number", 21.5)) == (
        "observations",
        (NOW, 7, 21.5, None),
    )
    assert encode_observation(7, Observation(P, NOW, "integer", 3)) == (
        "observations",
        (NOW, 7, 3.0, None),
    )
    assert encode_observation(7, Observation(P, NOW, "boolean", True)) == (
        "observations",
        (NOW, 7, 1.0, None),
    )
    assert encode_observation(7, Observation(P, NOW, "enum", 2)) == (
        "observations",
        (NOW, 7, 2.0, None),
    )
    assert encode_observation(7, Observation(P, NOW, "null", reason="fault")) == (
        "observations",
        (NOW, 7, None, REASON_CODES["fault"]),
    )
    assert encode_observation(7, Observation(P, NOW, "string", "Auto")) == (
        "observations_text",
        (NOW, 7, "Auto", None),
    )
    stamp = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
    assert encode_observation(7, Observation(P, NOW, "datetime", stamp)) == (
        "observations_text",
        (NOW, 7, None, stamp),
    )


def test_metadata_becomes_point_rows_and_state_rows() -> None:
    entry = PointMetadata(
        P,
        "enum",
        "http://qudt.org/vocab/unit/DEG_C",
        {1: "Off", 2: "Auto"},
        None,
        name="SAT",
        brick_class="brick:Supply_Air_Temperature_Sensor",
        equipment="ex:AHU_01",
        location="ex:Plant_Room",
        graph_unit="unit:DEG_C",
    )
    assert point_row(entry) == {
        "uri": P,
        "name": "SAT",
        "class": "brick:Supply_Air_Temperature_Sensor",
        "equipment": "ex:AHU_01",
        "location": "ex:Plant_Room",
        "unit": "unit:DEG_C",
        "protocol_unit": "http://qudt.org/vocab/unit/DEG_C",
        "value_type": "enum",
    }
    assert state_rows(entry) == [(1, "Off"), (2, "Auto")]
    assert state_rows(PointMetadata(P, "boolean", boolean_texts=("Off", "On"))) == [
        (0, "Off"),
        (1, "On"),
    ]
    assert state_rows(PointMetadata(P, "number")) == []


def test_metadata_merges_the_graphs_and_the_plugins_parts() -> None:
    graph_part = PointMetadata(
        P, name="SAT", brick_class="brick:Sensor", graph_unit="unit:DEG_C"
    )
    plugin_part = PointMetadata(P, "number", "unit:DEG_F")
    merged = graph_part.merged_with(plugin_part)
    assert merged.name == "SAT" and merged.value_type == "number"
    assert merged.unit == "unit:DEG_F" and merged.graph_unit == "unit:DEG_C"
    assert PointMetadata.from_dict(merged.as_dict()) == merged


def test_declaration_has_a_factory() -> None:
    assert DESTINATION.factory is TimescaleDBDestination
    assert DESTINATION.stores_model is True


@pytest.fixture(scope="module")
def database_dsn() -> Iterable[str]:
    """A database: BRICKLOGGER_TEST_DSN if set, else TimescaleDB in Docker."""
    import psycopg

    given = os.environ.get("BRICKLOGGER_TEST_DSN")
    if given:
        yield given
        return
    if shutil.which("docker") is None:
        pytest.skip("no database: set BRICKLOGGER_TEST_DSN or install docker")
    name = f"bricklogger-test-{os.getpid()}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=bricklogger",
            "-p",
            "127.0.0.1:5433:5432",
            "timescale/timescaledb:latest-pg16",
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(
            f"docker could not start timescaledb: {started.stderr.strip()[:200]}"
        )
    dsn = "postgres://postgres:bricklogger@127.0.0.1:5433/postgres"
    try:
        deadline = time.monotonic() + 120
        while True:
            try:
                with psycopg.connect(dsn, connect_timeout=2):
                    break
            except psycopg.Error:
                if time.monotonic() > deadline:
                    pytest.skip("the timescaledb container did not become ready")
                time.sleep(1)
        yield dsn
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


def test_round_trip_against_a_real_database(database_dsn: str) -> None:
    import psycopg

    dsn = database_dsn
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "DROP TABLE IF EXISTS observations, observations_text, point_states, "
            "points, reasons, model_activations, models, bricklogger_schema CASCADE"
        )
    destination = TimescaleDBDestination("tsdb", TimescaleDBConfig(dsn=dsn))
    destination.start()
    try:
        destination.write_metadata(
            [
                PointMetadata(
                    P,
                    "enum",
                    None,
                    {1: "Off", 2: "Auto", 3: "Manual"},
                    name="Mode",
                    brick_class="brick:Mode_Status",
                )
            ]
        )
        batch = [
            Observation(P, NOW, "enum", 2),
            Observation("https://example.com/bldg#Text", NOW, "string", "hello"),
            Observation(
                P, datetime(2026, 9, 1, 12, 5, tzinfo=UTC), "null", reason="fault"
            ),
        ]
        destination.write(batch)
        destination.write(batch)  # at-least-once delivery: a repeat is harmless
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT count(*) FROM observations"
            ).fetchone() == (2,)
            assert connection.execute(
                "SELECT count(*) FROM observations_text"
            ).fetchone() == (1,)
            row = connection.execute(
                "SELECT p.name, p.class, ps.text FROM observations o "
                "JOIN points p USING (point_id) JOIN point_states ps "
                "ON ps.point_id = p.point_id AND ps.ordinal = o.value::int "
                "WHERE o.value IS NOT NULL"
            ).fetchone()
            assert row == ("Mode", "brick:Mode_Status", "Auto")
            reason = connection.execute(
                "SELECT r.name FROM observations o JOIN reasons r ON r.code = o.reason "
                "WHERE o.value IS NULL"
            ).fetchone()
            assert reason == ("fault",)
            assert connection.execute(
                "SELECT max(version) FROM bricklogger_schema"
            ).fetchone() == (2,)
        ids = destination.timeseries_ids()
        assert set(ids) == {P, "https://example.com/bldg#Text"}
        first = datetime(2026, 10, 1, tzinfo=UTC)
        model = ModelDocument(1, first, (first,), "<urn:a> <urn:b> <urn:c> .\n")
        destination.write_model(model)
        again = datetime(2026, 10, 8, tzinfo=UTC)
        destination.write_model(replace(model, activations=(first, again)))
        with psycopg.connect(dsn) as connection:
            assert connection.execute(
                "SELECT m.version, m.document FROM models m "
                "JOIN model_activations a USING (version) "
                "ORDER BY a.activated_at DESC LIMIT 1"
            ).fetchone() == (1, model.turtle)
            assert connection.execute(
                "SELECT count(*) FROM model_activations"
            ).fetchone() == (2,), "a version written again replaces its row"
    finally:
        destination.stop()
