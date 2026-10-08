"""The persistent spool between the daemon and one destination.

An on-disk queue that observations, metadata and model versions are appended
to and drained from in order, in batches; it survives a daemon restart and is
bounded by size and age, dropping the oldest entries when a cap is reached. See
``docs/architecture.md``, "Backpressure: the spool".
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from bricklogger.sdk.contract import ModelDocument, Observation, PointMetadata

SPOOL_DIR = "spool"

EntryKind = Literal["observations", "metadata", "model"]


@dataclass(frozen=True)
class Entry:
    """One spooled batch: observations, metadata or one model version, as it
    was appended."""

    id: int
    kind: EntryKind
    payload: list[dict[str, Any]]

    def observations(self) -> list[Observation]:
        return [Observation.from_dict(item) for item in self.payload]

    def metadata(self) -> list[PointMetadata]:
        return [PointMetadata.from_dict(item) for item in self.payload]

    def model(self) -> ModelDocument:
        return ModelDocument.from_dict(self.payload[0])


@dataclass(frozen=True)
class SpoolStats:
    entries: int
    observations: int
    bytes: int
    oldest_age: float | None


class Spool:
    """One destination's queue; safe from the sink's and the drainer's threads."""

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
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS entries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                "payload TEXT NOT NULL, items INTEGER NOT NULL, "
                "bytes INTEGER NOT NULL, created REAL NOT NULL)"
            )
        self.wakeup = threading.Event()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def append_observations(self, batch: Sequence[Observation]) -> None:
        if batch:
            self._append("observations", [o.as_dict() for o in batch])

    def append_metadata(self, entries: Sequence[PointMetadata]) -> None:
        if entries:
            self._append("metadata", [e.as_dict() for e in entries])

    def append_model(self, model: ModelDocument) -> None:
        self._append("model", [model.as_dict()])

    def _append(self, kind: EntryKind, items: list[dict[str, Any]]) -> None:
        payload = json.dumps(items, separators=(",", ":"))
        with self._lock:
            self._db.execute(
                "INSERT INTO entries (kind, payload, items, bytes, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, payload, len(items), len(payload), time.time()),
            )
        self.wakeup.set()

    def next_entries(self, max_observations: int) -> list[Entry]:
        """The oldest entries, in order, up to about ``max_observations`` observations.

        Metadata and model entries break a run, so a destination learns about
        a point before it sees the point's values, and holds its key before
        the model names it, in the order things were appended.
        """
        entries: list[Entry] = []
        collected = 0
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, payload, items FROM entries ORDER BY id LIMIT 500"
            ).fetchall()
        for row in rows:
            kind = row["kind"]
            if entries and kind != entries[0].kind:
                break
            entries.append(Entry(int(row["id"]), kind, json.loads(row["payload"])))
            collected += int(row["items"])
            if kind != "observations" or collected >= max_observations:
                break
        return entries

    def ack(self, ids: Iterable[int]) -> None:
        with self._lock:
            self._db.executemany(
                "DELETE FROM entries WHERE id = ?", [(i,) for i in ids]
            )

    def enforce_caps(self, max_bytes: int, max_age: timedelta) -> int:
        """Drop the oldest entries beyond the caps; returns the observations dropped."""
        dropped = 0
        cutoff = time.time() - max_age.total_seconds()
        total_bytes = "SELECT COALESCE(SUM(bytes), 0) FROM entries"
        with self._lock:
            old = self._db.execute(
                "SELECT COALESCE(SUM(items), 0) FROM entries "
                "WHERE created < ? AND kind = 'observations'",
                (cutoff,),
            ).fetchone()[0]
            self._db.execute("DELETE FROM entries WHERE created < ?", (cutoff,))
            dropped += int(old)
            total = self._db.execute(total_bytes).fetchone()[0]
            while total > max_bytes:
                row = self._db.execute(
                    "SELECT id, kind, items, bytes FROM entries ORDER BY id LIMIT 1"
                ).fetchone()
                if row is None:
                    break
                self._db.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
                if row["kind"] == "observations":
                    dropped += int(row["items"])
                total -= int(row["bytes"])
        return dropped

    def stats(self) -> SpoolStats:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS entries, "
                "COALESCE(SUM(CASE WHEN kind = 'observations' "
                "THEN items ELSE 0 END), 0) AS obs, "
                "COALESCE(SUM(bytes), 0) AS bytes, MIN(created) AS oldest "
                "FROM entries"
            ).fetchone()
        oldest = row["oldest"]
        return SpoolStats(
            entries=int(row["entries"]),
            observations=int(row["obs"]),
            bytes=int(row["bytes"]),
            oldest_age=(time.time() - float(oldest)) if oldest is not None else None,
        )
