"""The value overlay of the working graph: every planned point with a valid
value carries ``brick:lastKnownValue``, a node with ``brick:value`` and
``brick:timestamp``. The overlay is rebuilt from runtime state whenever the
plan changes and updated in batches while observations arrive. See
``docs/architecture.md``, "The working graph".
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from bricklogger.daemon.state import RuntimeState
from bricklogger.model.working_graph import VALUES_GRAPH, WorkingGraph

log = logging.getLogger(__name__)

BRICK = "https://brickschema.org/schema/Brick#"
XSD = "http://www.w3.org/2001/XMLSchema#"
LAST_KNOWN_VALUE = f"<{BRICK}lastKnownValue>"
VALUE = f"<{BRICK}value>"
TIMESTAMP = f"<{BRICK}timestamp>"
FLUSH_INTERVAL = 5.0
CHUNK = 500


class ValueOverlay:
    """Keeps the values graph in step with the runtime state, a batch at a time."""

    def __init__(
        self, graph: WorkingGraph, state: RuntimeState, interval: float = FLUSH_INTERVAL
    ) -> None:
        self.graph = graph
        self.state = state
        self.interval = interval
        self._dirty: set[str] = set()
        self._rebuild = False
        self._lock = threading.Lock()
        self._wakeup = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        thread = self._thread
        if thread is not None:
            thread.join(10)
        self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wakeup.wait(self.interval)
            self._wakeup.clear()
            try:
                self.flush()
            except Exception:
                log.exception("the value overlay could not be updated")

    # --- what to write -------------------------------------------------------

    def mark(self, points: Iterable[str]) -> None:
        """Note points whose value changed; written at the next flush."""
        with self._lock:
            self._dirty.update(points)

    def request_rebuild(self) -> None:
        """Rebuild the whole overlay at the next flush, e.g. after a new plan."""
        with self._lock:
            self._rebuild = True
        self._wakeup.set()

    def flush(self) -> None:
        """Write what is pending now."""
        with self._lock:
            rebuild, dirty = self._rebuild, set(self._dirty)
            self._rebuild = False
            self._dirty.clear()
        if rebuild:
            self.rebuild()
        elif dirty:
            self.write(dirty)

    # --- writing -------------------------------------------------------------

    def rebuild(self) -> None:
        """Replace the overlay with every planned point's last valid value."""
        rows = self.state.points_with_values()
        self.graph.clear_graph(VALUES_GRAPH)
        for start in range(0, len(rows), CHUNK):
            self.graph.update(insert_statement(rows[start : start + CHUNK]))
        log.debug("value overlay rebuilt for %d points", len(rows))

    def write(self, points: set[str]) -> None:
        """Replace the value nodes of the given points."""
        uris = sorted(points)
        for start in range(0, len(uris), CHUNK):
            chunk = uris[start : start + CHUNK]
            rows = self.state.points_with_values(chunk)
            self.graph.update(delete_statement(chunk))
            if rows:
                self.graph.update(insert_statement(rows))


# --- SPARQL ------------------------------------------------------------------


def value_node(uri: str) -> str:
    """A stable IRI for a point's value node, so a replacement is a plain swap."""
    digest = hashlib.sha1(uri.encode("utf-8")).hexdigest()[:20]
    return f"<urn:bricklogger:value:{digest}>"


def delete_statement(uris: Iterable[str]) -> str:
    values = " ".join(f"<{uri}>" for uri in uris)
    return (
        f"DELETE {{ GRAPH <{VALUES_GRAPH}> "
        f"{{ ?p {LAST_KNOWN_VALUE} ?n . ?n ?x ?y }} }} "
        f"WHERE {{ VALUES ?p {{ {values} }} "
        f"GRAPH <{VALUES_GRAPH}> {{ ?p {LAST_KNOWN_VALUE} ?n . ?n ?x ?y }} }}"
    )


def insert_statement(rows: Iterable[Mapping[str, Any]]) -> str:
    triples: list[str] = []
    for row in rows:
        uri = str(row["uri"])
        node = value_node(uri)
        metadata = row.get("metadata") or {}
        value = literal(row["last_valid_value"], metadata.get("value_type"), metadata)
        stamp = json.dumps(str(row["last_valid_time"]))
        triples.append(f"<{uri}> {LAST_KNOWN_VALUE} {node} .")
        triples.append(f"{node} {VALUE} {value} .")
        triples.append(f"{node} {TIMESTAMP} {stamp}^^<{XSD}dateTime> .")
    body = "\n".join(triples)
    return f"INSERT DATA {{ GRAPH <{VALUES_GRAPH}> {{\n{body}\n}} }}"


def literal(value: Any, value_type: str | None, metadata: Mapping[str, Any]) -> str:
    """The value as a SPARQL literal: enum and boolean as text where texts are known."""
    if value_type == "boolean" or isinstance(value, bool):
        texts = metadata.get("boolean_texts")
        flag = bool(value)
        if isinstance(texts, list | tuple) and len(texts) == 2:
            return json.dumps(str(texts[1] if flag else texts[0]))
        return json.dumps("true" if flag else "false")
    if value_type == "enum" and isinstance(value, int):
        texts = metadata.get("enum_texts")
        if isinstance(texts, dict) and str(value) in texts:
            return json.dumps(str(texts[str(value)]))
        return f'"{value}"^^<{XSD}integer>'
    if isinstance(value, int):
        return f'"{value}"^^<{XSD}integer>'
    if isinstance(value, float):
        return f'"{value!r}"^^<{XSD}double>'
    if value_type == "datetime" and isinstance(value, str):
        return f"{json.dumps(value)}^^<{XSD}dateTime>"
    return json.dumps(str(value))
