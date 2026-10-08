"""The TimescaleDB destination: one narrow numeric hypertable, a text side
table, and the point metadata and the model beside them. See
``docs/features/destinations.md``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
from pydantic import BaseModel

from bricklogger.plugins.timescaledb.config import TimescaleDBConfig
from bricklogger.sdk.contract import (
    Destination,
    ModelDocument,
    Observation,
    PointMetadata,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

REASON_CODES: dict[str, int] = {
    "fault": 1,
    "out_of_service": 2,
    "overridden": 3,
    "no_value": 4,
    "unreachable": 5,
    "read_error": 6,
}

NUMERIC_TYPES = frozenset({"number", "integer", "boolean", "enum", "null"})

TABLES = [
    """
    CREATE TABLE IF NOT EXISTS bricklogger_schema (
        version integer NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS points (
        point_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        uri text NOT NULL UNIQUE,
        name text,
        class text,
        equipment text,
        location text,
        unit text,
        protocol_unit text,
        value_type text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point_states (
        point_id integer NOT NULL REFERENCES points (point_id),
        ordinal smallint NOT NULL,
        text text NOT NULL,
        PRIMARY KEY (point_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reasons (
        code smallint PRIMARY KEY,
        name text NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations (
        time timestamptz NOT NULL,
        point_id integer NOT NULL,
        value double precision,
        reason smallint,
        PRIMARY KEY (point_id, time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observations_text (
        time timestamptz NOT NULL,
        point_id integer NOT NULL,
        value_text text,
        value_time timestamptz,
        PRIMARY KEY (point_id, time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS models (
        version integer PRIMARY KEY,
        uploaded_at timestamptz NOT NULL,
        document text NOT NULL,
        written_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_activations (
        version integer NOT NULL REFERENCES models (version),
        activated_at timestamptz NOT NULL,
        PRIMARY KEY (version, activated_at)
    )
    """,
]

HYPERTABLES = [
    "SELECT create_hypertable('observations', 'time', if_not_exists => TRUE)",
    "SELECT create_hypertable('observations_text', 'time', if_not_exists => TRUE)",
    """
    ALTER TABLE observations SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'point_id',
        timescaledb.compress_orderby = 'time'
    )
    """,
    """
    ALTER TABLE observations_text SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'point_id',
        timescaledb.compress_orderby = 'time'
    )
    """,
    "SELECT add_compression_policy('observations', INTERVAL '7 days', "
    "if_not_exists => TRUE)",
    "SELECT add_compression_policy('observations_text', INTERVAL '7 days', "
    "if_not_exists => TRUE)",
]

INSERT_OBSERVATION = (
    "INSERT INTO observations (time, point_id, value, reason) VALUES (%s, %s, %s, %s) "
    "ON CONFLICT (point_id, time) DO NOTHING"
)
INSERT_TEXT = (
    "INSERT INTO observations_text (time, point_id, value_text, value_time) "
    "VALUES (%s, %s, %s, %s) ON CONFLICT (point_id, time) DO NOTHING"
)
UPSERT_POINT = """
    INSERT INTO points (uri, name, class, equipment, location, unit,
                        protocol_unit, value_type)
    VALUES (%(uri)s, %(name)s, %(class)s, %(equipment)s, %(location)s, %(unit)s,
            %(protocol_unit)s, %(value_type)s)
    ON CONFLICT (uri) DO UPDATE SET
        name = COALESCE(EXCLUDED.name, points.name),
        class = COALESCE(EXCLUDED.class, points.class),
        equipment = COALESCE(EXCLUDED.equipment, points.equipment),
        location = COALESCE(EXCLUDED.location, points.location),
        unit = COALESCE(EXCLUDED.unit, points.unit),
        protocol_unit = COALESCE(EXCLUDED.protocol_unit, points.protocol_unit),
        value_type = COALESCE(EXCLUDED.value_type, points.value_type),
        updated_at = now()
    RETURNING point_id
"""
UPSERT_MODEL = """
    INSERT INTO models (version, uploaded_at, document)
    VALUES (%s, %s, %s)
    ON CONFLICT (version) DO UPDATE SET
        uploaded_at = EXCLUDED.uploaded_at,
        document = EXCLUDED.document,
        written_at = now()
"""
INSERT_ACTIVATION = (
    "INSERT INTO model_activations (version, activated_at) VALUES (%s, %s) "
    "ON CONFLICT DO NOTHING"
)


def encode_observation(
    point_id: int, observation: Observation
) -> tuple[str, tuple[Any, ...]]:
    """The table and row an observation becomes.

    ``number``, ``integer``, ``boolean`` and ``enum`` go into ``observations``
    as a double; ``null`` too, with the reason code and no value; ``string``
    and ``datetime`` go into ``observations_text``.
    """
    time = observation.timestamp.astimezone(UTC)
    if observation.type == "null":
        assert observation.reason is not None
        return "observations", (time, point_id, None, REASON_CODES[observation.reason])
    if observation.type in NUMERIC_TYPES:
        value = observation.value
        number = float(value) if isinstance(value, bool | int | float) else None
        return "observations", (time, point_id, number, None)
    if observation.type == "datetime":
        stamp = observation.value if isinstance(observation.value, datetime) else None
        return "observations_text", (time, point_id, None, stamp)
    return "observations_text", (time, point_id, str(observation.value), None)


def point_row(entry: PointMetadata) -> dict[str, Any]:
    """The ``points`` row a metadata entry updates; unknown fields stay NULL."""
    return {
        "uri": entry.point,
        "name": entry.name,
        "class": entry.brick_class,
        "equipment": entry.equipment,
        "location": entry.location,
        "unit": entry.graph_unit,
        "protocol_unit": entry.unit,
        "value_type": entry.value_type,
    }


def state_rows(entry: PointMetadata) -> list[tuple[int, str]]:
    """The ``point_states`` rows: enum ordinals or 0 and 1 for a boolean's texts."""
    if entry.enum_texts is not None:
        return [
            (int(ordinal), text) for ordinal, text in sorted(entry.enum_texts.items())
        ]
    if entry.boolean_texts is not None:
        return [(0, entry.boolean_texts[0]), (1, entry.boolean_texts[1])]
    return []


class TimescaleDBDestination(Destination):
    """One ``timescaledb`` instance; writes are idempotent on (point, time)."""

    def __init__(self, name: str, config: BaseModel) -> None:
        super().__init__(name, config)
        self.settings = (
            config
            if isinstance(config, TimescaleDBConfig)
            else TimescaleDBConfig.model_validate(config.model_dump())
        )
        self.connection: psycopg.Connection[Any] | None = None
        self._point_ids: dict[str, int] = {}
        self.timescale = False

    def start(self) -> None:
        arguments: dict[str, Any] = {}
        if self.settings.password:
            arguments["password"] = self.settings.password
        self.connection = psycopg.connect(
            self.settings.dsn, autocommit=False, **arguments
        )
        self._point_ids.clear()
        self._ensure_schema()

    def stop(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def write(self, batch: Sequence[Observation]) -> None:
        connection = self._require_connection()
        numeric: list[tuple[Any, ...]] = []
        text: list[tuple[Any, ...]] = []
        with connection.transaction():
            for observation in batch:
                point_id = self._point_id(observation.point)
                table, row = encode_observation(point_id, observation)
                (numeric if table == "observations" else text).append(row)
            with connection.cursor() as cursor:
                if numeric:
                    cursor.executemany(INSERT_OBSERVATION, numeric)
                if text:
                    cursor.executemany(INSERT_TEXT, text)

    def write_metadata(self, entries: Sequence[PointMetadata]) -> None:
        connection = self._require_connection()
        with connection.transaction(), connection.cursor() as cursor:
            for entry in entries:
                cursor.execute(UPSERT_POINT, point_row(entry))
                row = cursor.fetchone()
                assert row is not None
                point_id = int(row[0])
                self._point_ids[entry.point] = point_id
                states = state_rows(entry)
                if states:
                    cursor.execute(
                        "DELETE FROM point_states WHERE point_id = %s", (point_id,)
                    )
                    cursor.executemany(
                        "INSERT INTO point_states (point_id, ordinal, text) "
                        "VALUES (%s, %s, %s)",
                        [(point_id, ordinal, text) for ordinal, text in states],
                    )

    def timeseries_ids(self) -> Mapping[str, str]:
        """Every point's ``point_id``, as text, by URI."""
        connection = self._require_connection()
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute("SELECT uri, point_id FROM points")
            return {str(uri): str(point_id) for uri, point_id in cursor.fetchall()}

    def write_model(self, model: ModelDocument) -> None:
        """One version with its references; written again, it replaces the row."""
        connection = self._require_connection()
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                UPSERT_MODEL, (model.version, model.uploaded_at, model.turtle)
            )
            cursor.executemany(
                INSERT_ACTIVATION,
                [(model.version, stamp) for stamp in model.activations],
            )

    def _point_id(self, uri: str) -> int:
        """The point's id, assigned by the destination on first sight of the URI."""
        cached = self._point_ids.get(uri)
        if cached is not None:
            return cached
        connection = self._require_connection()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO points (uri) VALUES (%s) ON CONFLICT (uri) DO UPDATE "
                "SET uri = EXCLUDED.uri RETURNING point_id",
                (uri,),
            )
            row = cursor.fetchone()
            assert row is not None
            point_id = int(row[0])
        self._point_ids[uri] = point_id
        return point_id

    def _ensure_schema(self) -> None:
        """Create the tables on first start; hypertables if the extension exists."""
        connection = self._require_connection()
        with connection.transaction(), connection.cursor() as cursor:
            for statement in TABLES:
                cursor.execute(statement)
            cursor.executemany(
                "INSERT INTO reasons (code, name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                [(code, name) for name, code in REASON_CODES.items()],
            )
            cursor.execute("SELECT max(version) FROM bricklogger_schema")
            row = cursor.fetchone()
            current = int(row[0]) if row and row[0] is not None else 0
            if current < SCHEMA_VERSION:
                cursor.execute(
                    "INSERT INTO bricklogger_schema (version) VALUES (%s)",
                    (SCHEMA_VERSION,),
                )
        try:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        except psycopg.Error:
            pass
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
            self.timescale = cursor.fetchone() is not None
        connection.commit()
        if not self.timescale:
            log.warning(
                "%s: the timescaledb extension is not installed; plain tables without "
                "hypertables or compression",
                self.name,
            )
            return
        for statement in HYPERTABLES:
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute(statement)
            except psycopg.Error as exc:
                log.warning(
                    "%s: %s failed: %s", self.name, statement.split("(")[0].strip(), exc
                )

    def _require_connection(self) -> psycopg.Connection[Any]:
        if self.connection is None:
            raise RuntimeError(f"{self.name} is not started")
        return self.connection
