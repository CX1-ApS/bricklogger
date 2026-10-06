"""The daemon's runtime state: an SQLite database in the data directory.

It holds what status answers from — per point, device and instance — plus the
warning list and the activation history. The tables are internal and may
change; status, the API and SPARQL are the interfaces. See
``docs/features/daemon.md``, "Runtime state".
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bricklogger.daemon.codes import kind_of
from bricklogger.sdk.contract import Observation, Outcome, PointMetadata

log = logging.getLogger(__name__)

STATE_FILE = "state.sqlite"

#: The character that makes a LIKE wildcard literal. Brick class names are full
#: of underscores, which LIKE would otherwise read as "any one character".
LIKE_ESCAPE = "\\"


def anywhere(text: str) -> str:
    """A LIKE pattern matching ``text`` anywhere, its wildcards taken literally.

    SQLite's LIKE ignores case for ASCII, which is what a search field wants.
    """
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    uri TEXT PRIMARY KEY,
    instance TEXT,
    method TEXT,
    fallback_active INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    outcome_reason TEXT,
    last_observation TEXT,
    last_valid_value TEXT,
    last_valid_time TEXT,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS instances (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    state TEXT NOT NULL,
    last_error TEXT,
    restart_count INTEGER NOT NULL DEFAULT 0,
    stop_intent INTEGER NOT NULL DEFAULT 0,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    instance TEXT NOT NULL,
    device TEXT NOT NULL,
    reachable INTEGER,
    last_success TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    skipped_rounds INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (instance, device)
);
CREATE TABLE IF NOT EXISTS warnings (
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (code, subject)
);
CREATE TABLE IF NOT EXISTS activations (
    version INTEGER NOT NULL,
    activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notified (
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    since TEXT NOT NULL,
    PRIMARY KEY (code, subject)
);
CREATE TABLE IF NOT EXISTS notify_memory (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_value(value: Any) -> str:
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    return json.dumps(value)


class RuntimeState:
    """The SQLite-backed state; every method is safe to call from any thread."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Bring a state file from an earlier version up to date."""
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(warnings)")
        }
        if "last_seen" not in columns:
            self._db.execute(
                "ALTER TABLE warnings ADD COLUMN last_seen TEXT NOT NULL DEFAULT ''"
            )
            self._db.execute("UPDATE warnings SET last_seen = first_seen")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # --- points -------------------------------------------------------------

    def replace_plan(self, rows: Iterable[tuple[str, str, str]]) -> None:
        """Set the planned points, (uri, instance, method), keeping what is known."""
        planned = list(rows)
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS planned (uri TEXT PRIMARY KEY)"
            )
            self._db.execute("DELETE FROM planned")
            self._db.executemany(
                "INSERT INTO planned (uri) VALUES (?)",
                [(uri,) for uri, _, _ in planned],
            )
            self._db.execute(
                "DELETE FROM points WHERE uri NOT IN (SELECT uri FROM planned)"
            )
            self._db.executemany(
                "INSERT INTO points (uri, instance, method, fallback_active) "
                "VALUES (?, ?, ?, 0) "
                "ON CONFLICT(uri) DO UPDATE SET instance = excluded.instance, "
                "method = excluded.method, fallback_active = 0",
                planned,
            )
            self._db.execute("COMMIT")

    def set_fallback(self, uri: str, method: str, active: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE points SET method = ?, fallback_active = ? WHERE uri = ?",
                (method, int(active), uri),
            )

    def record_observations(self, batch: Sequence[Observation]) -> None:
        """Note each observation; the last values are the latest by timestamp.

        A history source can deliver an older sample after a newer one, so a
        value replaces the last one only when its timestamp is not earlier.
        The timestamps compare as text because they are all written the same
        way, in UTC.
        """
        with self._lock:
            self._db.execute("BEGIN")
            for observation in batch:
                timestamp = observation.timestamp.astimezone(UTC).isoformat()
                if observation.type == "null":
                    self._db.execute(
                        "UPDATE points SET last_observation = "
                        "MAX(COALESCE(last_observation, ''), ?) WHERE uri = ?",
                        (timestamp, observation.point),
                    )
                else:
                    value = _json_value(observation.value)
                    self._db.execute(
                        "UPDATE points SET "
                        "last_observation = MAX(COALESCE(last_observation, ''), ?), "
                        "last_valid_value = CASE WHEN last_valid_time IS NULL "
                        "OR last_valid_time <= ? THEN ? ELSE last_valid_value END, "
                        "last_valid_time = MAX(COALESCE(last_valid_time, ''), ?) "
                        "WHERE uri = ?",
                        (timestamp, timestamp, value, timestamp, observation.point),
                    )
            self._db.execute("COMMIT")

    def set_outcomes(self, outcomes: Iterable[Outcome]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE points SET outcome = ?, outcome_reason = ? WHERE uri = ?",
                [(o.state, o.reason, o.point) for o in outcomes],
            )

    def set_metadata(self, entries: Iterable[PointMetadata]) -> None:
        with self._lock:
            self._db.executemany(
                "UPDATE points SET metadata = ? WHERE uri = ?",
                [(json.dumps(e.as_dict()), e.point) for e in entries],
            )

    def merge_metadata(self, entry: PointMetadata) -> PointMetadata:
        """Merge an entry into what is known about the point; returns the whole."""
        with self._lock:
            row = self._db.execute(
                "SELECT metadata FROM points WHERE uri = ?", (entry.point,)
            ).fetchone()
            existing = (
                PointMetadata.from_dict(json.loads(row["metadata"]))
                if row is not None and row["metadata"]
                else None
            )
            merged = existing.merged_with(entry) if existing is not None else entry
            self._db.execute(
                "UPDATE points SET metadata = ? WHERE uri = ?",
                (json.dumps(merged.as_dict()), entry.point),
            )
        return merged

    def points(
        self,
        *,
        instance: str | None = None,
        outcome: str | None = None,
        warning: str | None = None,
        brick_class: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[int, list[dict[str, Any]]]:
        """A page of the points view and the total.

        The instance and the outcome must match in full; the warning code and
        the asserted Brick class are searched, so the text given need only
        appear somewhere in the value. See ``docs/features/daemon.md``,
        "Points".
        """
        clauses: list[str] = []
        params: list[Any] = []
        if instance is not None:
            clauses.append("instance = ?")
            params.append(instance)
        if outcome is not None:
            clauses.append("outcome = ?")
            params.append(outcome)
        if warning is not None:
            clauses.append(
                "uri IN (SELECT subject FROM warnings "
                f"WHERE code LIKE ? ESCAPE '{LIKE_ESCAPE}')"
            )
            params.append(anywhere(warning))
        if brick_class is not None:
            clauses.append(
                f"json_extract(metadata, '$.brick_class') LIKE ? ESCAPE '{LIKE_ESCAPE}'"
            )
            params.append(anywhere(brick_class))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = self._db.execute(
                f"SELECT COUNT(*) FROM points{where}", params
            ).fetchone()[0]
            rows = self._db.execute(
                f"SELECT * FROM points{where} ORDER BY uri LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return int(total), [self._point_row(row) for row in rows]

    def points_with_values(
        self, uris: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """The planned points that have a last valid value, for the value overlay."""
        where = "WHERE last_valid_value IS NOT NULL"
        params: list[Any] = []
        if uris is not None:
            wanted = list(uris)
            if not wanted:
                return []
            marks = ", ".join("?" for _ in wanted)
            where += f" AND uri IN ({marks})"
            params = wanted
        with self._lock:
            rows = self._db.execute(
                "SELECT uri, last_valid_value, last_valid_time, metadata FROM points "
                f"{where} ORDER BY uri",
                params,
            ).fetchall()
        return [
            {
                "uri": row["uri"],
                "last_valid_value": json.loads(row["last_valid_value"]),
                "last_valid_time": row["last_valid_time"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else None,
            }
            for row in rows
        ]

    def point_states(self) -> dict[str, dict[str, Any]]:
        """Every planned point's instance, method, fallback flag and outcome.

        The whole table at once, for the model explorer's projection, which
        needs a point's runtime beside what the graph says about it.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT uri, instance, method, fallback_active, outcome FROM points"
            ).fetchall()
        return {
            str(row["uri"]): {
                "instance": row["instance"],
                "method": row["method"],
                "fallback_active": bool(row["fallback_active"]),
                "outcome": row["outcome"] or "pending",
            }
            for row in rows
        }

    def point_counts(self, instance: str | None = None) -> dict[str, int]:
        """How many points are active, unsupported, rejected or still pending."""
        where, params = ("WHERE instance = ?", [instance]) if instance else ("", [])
        with self._lock:
            rows = self._db.execute(
                f"SELECT COALESCE(outcome, 'pending') AS outcome, COUNT(*) AS n "
                f"FROM points {where} GROUP BY outcome",
                params,
            ).fetchall()
        return {str(row["outcome"]): int(row["n"]) for row in rows}

    @staticmethod
    def _point_row(row: sqlite3.Row) -> dict[str, Any]:
        value = row["last_valid_value"]
        metadata = row["metadata"]
        return {
            "uri": row["uri"],
            "instance": row["instance"],
            "method": row["method"],
            "fallback_active": bool(row["fallback_active"]),
            "outcome": row["outcome"],
            "outcome_reason": row["outcome_reason"],
            "last_observation": row["last_observation"],
            "last_valid": (
                {"value": json.loads(value), "time": row["last_valid_time"]}
                if value is not None
                else None
            ),
            "metadata": json.loads(metadata) if metadata else None,
        }

    # --- instances and devices ---------------------------------------------

    def set_instance(
        self,
        name: str,
        *,
        role: str,
        type_name: str,
        state: str,
        error: str | None = None,
        restart_count: int | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO instances "
                "(name, role, type, state, last_error, restart_count, updated) "
                "VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?) "
                "ON CONFLICT(name) DO UPDATE SET role = excluded.role, "
                "type = excluded.type, state = excluded.state, "
                "last_error = excluded.last_error, "
                "restart_count = COALESCE(?, instances.restart_count), "
                "updated = excluded.updated",
                (
                    name,
                    role,
                    type_name,
                    state,
                    error,
                    restart_count,
                    _now(),
                    restart_count,
                ),
            )

    def instances(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM instances ORDER BY role, name"
            ).fetchall()
        return [dict(row) | {"stop_intent": bool(row["stop_intent"])} for row in rows]

    def stop_intent(self, name: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT stop_intent FROM instances WHERE name = ?", (name,)
            ).fetchone()
        return bool(row["stop_intent"]) if row else False

    def set_stop_intent(self, name: str, stopped: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE instances SET stop_intent = ?, updated = ? WHERE name = ?",
                (int(stopped), _now(), name),
            )

    def set_device(
        self,
        instance: str,
        device: str,
        *,
        reachable: bool,
        error: str | None = None,
        skipped_rounds: int | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO devices (instance, device, reachable, last_success, "
                "error_count, skipped_rounds, last_error) "
                "VALUES (?, ?, ?, ?, ?, COALESCE(?, 0), ?) "
                "ON CONFLICT(instance, device) DO UPDATE SET "
                "reachable = excluded.reachable, "
                "last_success = CASE WHEN excluded.reachable "
                "THEN excluded.last_success ELSE devices.last_success END, "
                "error_count = devices.error_count "
                "+ CASE WHEN excluded.reachable THEN 0 ELSE 1 END, "
                "skipped_rounds = COALESCE(?, devices.skipped_rounds), "
                "last_error = excluded.last_error",
                (
                    instance,
                    device,
                    int(reachable),
                    _now() if reachable else None,
                    0 if reachable else 1,
                    skipped_rounds,
                    error,
                    skipped_rounds,
                ),
            )

    def devices(self, instance: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM devices WHERE instance = ? ORDER BY device", (instance,)
            ).fetchall()
        return [dict(row) | {"reachable": bool(row["reachable"])} for row in rows]

    def last_observations(self, uris: Iterable[str]) -> dict[str, datetime]:
        """The latest observation timestamp of each given point that has one."""
        wanted = set(uris)
        with self._lock:
            rows = self._db.execute(
                "SELECT uri, last_observation FROM points "
                "WHERE last_observation IS NOT NULL"
            ).fetchall()
        return {
            row["uri"]: datetime.fromisoformat(row["last_observation"])
            for row in rows
            if row["uri"] in wanted
        }

    # --- warnings, activations, counters -----------------------------------

    def warn(self, code: str, subject: str, message: str) -> bool:
        """Assert a warning; answers whether this is its first appearance.

        First seen stays, last seen moves and the count grows. The answer is
        what the daemon logs on, and what tells the notifier a condition has
        opened rather than merely been asserted again.
        """
        now = _now()
        with self._lock:
            row = self._db.execute(
                "INSERT INTO warnings "
                "(code, subject, message, first_seen, last_seen, count) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(code, subject) DO UPDATE SET "
                "message = excluded.message, last_seen = excluded.last_seen, "
                "count = warnings.count + 1 "
                "RETURNING count",
                (code, subject, message, now, now),
            ).fetchone()
        first = int(row["count"]) == 1
        if first:
            log.warning("%s on %s: %s", code, subject, message)
        return first

    def clear_warnings(
        self,
        code: str | None = None,
        subject: str | None = None,
        *,
        forget_notified: bool = False,
    ) -> int:
        """Withdraw warnings: all, those with a code, or one; returns how many.

        ``forget_notified`` is the operator's manual clear, which is an
        acknowledgement rather than an end: the notifier's memory goes with the
        warning, so no all clear is sent for something that was never over. The
        daemon's own withdrawals leave the memory, which is what lets the
        notifier see that a condition has closed.
        """
        clauses: list[str] = []
        params: list[str] = []
        if code is not None:
            clauses.append("code = ?")
            params.append(code)
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            cursor = self._db.execute(f"DELETE FROM warnings{where}", params)
            if forget_notified:
                self._db.execute(f"DELETE FROM notified{where}", params)
            return int(cursor.rowcount)

    def has_warning(self, code: str, subject: str | None = None) -> bool:
        """Whether a warning with the code, and the subject if given, stands."""
        clauses = ["code = ?"]
        params = [code]
        if subject is not None:
            clauses.append("subject = ?")
            params.append(subject)
        with self._lock:
            row = self._db.execute(
                f"SELECT 1 FROM warnings WHERE {' AND '.join(clauses)} LIMIT 1",
                params,
            ).fetchone()
        return row is not None

    def warnings(self) -> list[dict[str, Any]]:
        """The flat warning list, each entry carrying its code's kind."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM warnings ORDER BY first_seen, code, subject"
            ).fetchall()
        return [{**dict(row), "kind": kind_of(row["code"])} for row in rows]

    # --- the notifier's memory ---------------------------------------------

    def notified(self) -> list[dict[str, Any]]:
        """The conditions the administrator has been told about and not yet off."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM notified ORDER BY since, code, subject"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_notified(self, code: str, subject: str, message: str) -> None:
        """Record that a mail has gone out about this condition."""
        with self._lock:
            self._db.execute(
                "INSERT INTO notified (code, subject, message, since) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(code, subject) DO UPDATE SET "
                "message = excluded.message",
                (code, subject, message, _now()),
            )

    def remove_notified(self, code: str, subject: str) -> None:
        """Forget a condition, once its all clear has gone out."""
        with self._lock:
            self._db.execute(
                "DELETE FROM notified WHERE code = ? AND subject = ?",
                (code, subject),
            )

    def memory(self, key: str) -> str | None:
        """One of the notifier's scalars, such as the health last reported."""
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM notify_memory WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def remember(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO notify_memory (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def forget(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM notify_memory WHERE key = ?", (key,))

    def record_activation(self, version: int) -> None:
        query = "INSERT INTO activations (version, activated_at) VALUES (?, ?)"
        with self._lock:
            self._db.execute(query, (version, _now()))

    def last_activation(self) -> tuple[int, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT version, activated_at FROM activations "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return (int(row["version"]), str(row["activated_at"])) if row else None

    def activations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT version, activated_at FROM activations"
            ).fetchall()
        return [dict(row) for row in rows]

    def increment(self, counter: str, by: int = 1) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO counters (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "value = counters.value + excluded.value",
                (counter, by),
            )

    def counter(self, name: str) -> int:
        query = "SELECT value FROM counters WHERE name = ?"
        with self._lock:
            row = self._db.execute(query, (name,)).fetchone()
        return int(row["value"]) if row else 0

    def counters(self) -> Mapping[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT name, value FROM counters").fetchall()
        return {str(row["name"]): int(row["value"]) for row in rows}
