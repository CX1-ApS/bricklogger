"""The test kit: what a plugin's own tests need from Bricklogger.

A plugin lives outside Bricklogger's repository, so the doubles its tests need
ship in the SDK: a graph built from Turtle text exactly as the daemon builds
its working graph, a collector that is sink and status channel in one, a run
helper that drives a source through assign, start and stop in a thread, and a
shorthand for an assigned point. See ``docs/features/plugins.md``, "Testing a
plugin".
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from bricklogger.config.values import parse_duration
from bricklogger.model import ModelStore, WorkingGraph, activate_version, parse_model
from bricklogger.model.prefixes import WELL_KNOWN, declared_prefixes
from bricklogger.model.working_graph import GRAPH_DIR
from bricklogger.sdk.contract import (
    AssignedPoint,
    GraphReader,
    InstanceState,
    Observation,
    Outcome,
    PointMetadata,
    Source,
)
from bricklogger.sdk.declaration import Vocabulary
from bricklogger.sdk.registry import PluginRegistry

T = TypeVar("T")


class _Reader:
    """Read-only SPARQL over a working graph, with the model's prefixes."""

    def __init__(self, graph: WorkingGraph, prefixes: Mapping[str, str]) -> None:
        self._graph = graph
        self._prefixes = dict(prefixes)

    @property
    def prefixes(self) -> Mapping[str, str]:
        return self._prefixes

    def query(self, sparql: str) -> Any:
        return self._graph.query(sparql)


def graph_from_turtle(
    text: str,
    directory: Path | str | None = None,
    *,
    vocabularies: Iterable[Vocabulary] | None = None,
) -> GraphReader:
    """A graph reader over the model in ``text``, built as the daemon builds it.

    The model is stored, validated, inferred and loaded with Brick's ontology
    and the vocabularies of the installed sources, or the ones given, so the
    plugin sees in its tests what it sees in operation. The graph lives in
    ``directory``, or in a temporary directory. A model that does not conform
    raises :class:`bricklogger.model.ModelInvalid`, as an upload would be
    rejected.
    """
    data_dir = (
        Path(directory)
        if directory is not None
        else Path(tempfile.mkdtemp(prefix="bricklogger-graph-"))
    )
    known = (
        tuple(vocabularies)
        if vocabularies is not None
        else PluginRegistry.from_entry_points().vocabularies()
    )
    data = text.encode("utf-8")
    store = ModelStore(data_dir)
    version = store.store(data, "turtle")
    graph = WorkingGraph(data_dir / GRAPH_DIR)
    activate_version(store, graph, version.number, known)
    prefixes = {
        **WELL_KNOWN,
        **{vocabulary.prefix: vocabulary.namespace for vocabulary in known},
        **declared_prefixes(parse_model(data, "turtle")),
    }
    return _Reader(graph, prefixes)


class Collector:
    """A sink and a status channel in one object that remembers everything.

    Thread-safe, as the daemon's are. The recorded lists are read directly or
    through the lookups per point; ``wait_until`` polls a condition until it
    holds.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen_observations: list[Observation] = []
        self.seen_metadata: list[PointMetadata] = []
        self.seen_outcomes: dict[str, Outcome] = {}
        self.devices: list[dict[str, Any]] = []
        self.states: list[tuple[InstanceState, str | None]] = []
        self.warnings: dict[str, str] = {}
        self.cleared: list[str] = []

    # --- lookups -------------------------------------------------------------

    def observations_for(self, point: str) -> list[Observation]:
        """The point's observations, oldest first."""
        with self.lock:
            return sorted(
                (o for o in self.seen_observations if o.point == point),
                key=lambda o: o.timestamp,
            )

    def metadata_for(self, point: str) -> PointMetadata | None:
        """The latest metadata delivered for the point, if any."""
        with self.lock:
            entries = [m for m in self.seen_metadata if m.point == point]
        return entries[-1] if entries else None

    def outcome_of(self, point: str) -> Outcome | None:
        """The latest outcome reported for the point, if any."""
        with self.lock:
            return self.seen_outcomes.get(point)

    def wait_until(
        self, predicate: Callable[[], T], timeout: float = 5.0, interval: float = 0.05
    ) -> T:
        """Poll until the predicate returns something true, and return it.

        Raises :class:`TimeoutError` when it does not within ``timeout`` seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= deadline:
                raise TimeoutError(f"the condition did not hold within {timeout:g}s")
            time.sleep(interval)

    # --- the sink ------------------------------------------------------------

    def observations(self, batch: Iterable[Observation]) -> None:
        with self.lock:
            self.seen_observations.extend(batch)

    def metadata(self, entries: Iterable[PointMetadata]) -> None:
        with self.lock:
            self.seen_metadata.extend(entries)

    # --- the status channel --------------------------------------------------

    def instance_state(self, state: InstanceState, error: str | None = None) -> None:
        with self.lock:
            self.states.append((state, error))

    def device(
        self,
        device: str,
        *,
        reachable: bool,
        error: str | None = None,
        skipped_rounds: int | None = None,
    ) -> None:
        with self.lock:
            self.devices.append(
                {
                    "device": device,
                    "reachable": reachable,
                    "error": error,
                    "skipped_rounds": skipped_rounds,
                }
            )

    def outcomes(self, outcomes: Iterable[Outcome]) -> None:
        with self.lock:
            for outcome in outcomes:
                self.seen_outcomes[outcome.point] = outcome

    def warn(self, code: str, message: str, subject: str | None = None) -> None:
        with self.lock:
            self.warnings[code] = message

    def clear_warning(self, code: str, subject: str | None = None) -> None:
        with self.lock:
            self.warnings.pop(code, None)
            self.cleared.append(code)


@contextmanager
def run_source(
    source: Source, points: Sequence[AssignedPoint], *, timeout: float = 5.0
) -> Iterator[Collector]:
    """Assign the points, run the source in a thread and stop it afterwards.

    The source runs against a fresh :class:`Collector`, which the block gets.
    On exit ``stop`` is called and the thread given ``timeout`` seconds; a
    source that does not stop in time raises :class:`TimeoutError`, and one
    whose loop raised re-raises that error, so neither goes unnoticed.
    """
    collector = Collector()
    raised: list[BaseException] = []

    def run() -> None:
        try:
            source.start(collector, collector)
        except BaseException as exc:
            raised.append(exc)

    source.assign(list(points))
    thread = threading.Thread(target=run, name=f"source:{source.name}", daemon=True)
    thread.start()
    try:
        yield collector
    except BaseException:
        _finish(source, thread, timeout, raised, report=False)
        raise
    _finish(source, thread, timeout, raised, report=True)


def _finish(
    source: Source,
    thread: threading.Thread,
    timeout: float,
    raised: list[BaseException],
    *,
    report: bool,
) -> None:
    source.stop()
    thread.join(timeout)
    if not report:
        return
    if raised:
        raise RuntimeError(f"the source's loop raised: {raised[0]!r}") from raised[0]
    if thread.is_alive():
        raise TimeoutError(f"the source did not stop within {timeout:g}s")


def assigned(
    uri: str,
    method: str = "poll",
    *,
    last_observation: datetime | None = None,
    **parameters: Any,
) -> AssignedPoint:
    """An assigned point; an ``interval`` given as ``"1s"`` or as seconds
    becomes the ``timedelta`` the daemon hands over."""
    interval = parameters.get("interval")
    if isinstance(interval, str):
        parameters["interval"] = parse_duration(interval)
    elif isinstance(interval, int | float) and not isinstance(interval, bool):
        parameters["interval"] = timedelta(seconds=interval)
    return AssignedPoint(
        uri=uri,
        method=method,
        parameters=parameters,
        last_observation=last_observation,
    )


__all__ = ["Collector", "assigned", "graph_from_turtle", "run_source"]
