"""The daemon: the long-running core.

On start it validates the configuration, opens the runtime state and the
working graph, activates the marked model version if the graph does not hold
it yet, computes the plan, and starts the plugin instances in their own
threads. Without a model it waits in an idle state. See
``docs/architecture.md``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rdflib

from bricklogger import __version__
from bricklogger.config import (
    Configuration,
    DestinationInstance,
    SourceInstance,
    ValidationResult,
    not_installed,
    read_texts,
    validate_configuration,
    write_text,
)
from bricklogger.daemon.explorer import (
    attach_runtime,
    empty_document,
    entities_of_class,
    narrow,
    project_entities,
)
from bricklogger.daemon.instances import DestinationRunner, InstanceStatus, SourceRunner
from bricklogger.daemon.jobs import Job, JobFailed, JobRunner
from bricklogger.daemon.metadata import graph_metadata
from bricklogger.daemon.notify import Notifier
from bricklogger.daemon.overlay import ValueOverlay
from bricklogger.daemon.plan import Plan, PlanError, assigned_point, compute_plan
from bricklogger.daemon.plugins import describe_plugin, summarize_plugins, tool_of
from bricklogger.daemon.sink import DaemonSink
from bricklogger.daemon.sparql import run_query
from bricklogger.daemon.spool import SPOOL_DIR, Spool
from bricklogger.daemon.state import STATE_FILE, RuntimeState
from bricklogger.daemon.updates import Lookup, UpdateWatch
from bricklogger.model import (
    INFERRED_GRAPH,
    MODEL_GRAPH,
    VALUES_GRAPH,
    Activation,
    Inference,
    ModelDiff,
    ModelInvalid,
    ModelNotFound,
    ModelReport,
    ModelStore,
    ModelUnreadable,
    WorkingGraph,
    activate_version,
    diff_versions,
    parse_model,
    validate_and_infer,
    validate_model,
)
from bricklogger.model.prefixes import (
    WELL_KNOWN,
    UnknownPrefix,
    compact,
    declared_prefixes,
    expand,
)
from bricklogger.model.timeseries import (
    REF,
    TIMESERIES_GRAPH_PREFIX,
    reference_triples,
    timeseries_graph,
)
from bricklogger.model.working_graph import GRAPH_DIR
from bricklogger.sdk.contract import AssignedPoint, ModelDocument, Outcome
from bricklogger.sdk.registry import PluginRegistry

log = logging.getLogger(__name__)

PLAN_WARNINGS = (
    "unclaimed",
    "unknown_reference",
    "no_reference",
    "no_fallback",
    "rule_skipped",
    "unit_conflict",
)
"""Withdrawn at every plan; what still holds is asserted again by the plan or
by the sources' acknowledgements."""


class DaemonError(Exception):
    """The daemon cannot start or apply a change."""


class ExportUnavailable(DaemonError):
    """The inferred graph and the values exist only for the active version."""


class NotFound(DaemonError):
    """A plugin type, instance, tool or action the daemon does not have."""


class InstanceNotRunning(DaemonError):
    """A tool needs its instance running, and it is not."""


class ToolFailed(DaemonError):
    """The tool raised; the message is the plugin's."""


def unusable_data_dir(data_dir: Path, exc: Exception) -> str:
    """Why the data directory cannot be used, in one line.

    The owner and the user the daemon runs as are both named: a mismatch
    between the two is nearly always the cause — a directory another user
    created, or a container whose bind mount belongs to the host's user.
    """
    message = f"the data directory {data_dir} cannot be used: {exc}"
    owner = _owner(data_dir)
    if owner is None or not hasattr(os, "getuid"):
        return message
    return (
        f"{message}. It belongs to uid {owner}, "
        f"and the daemon runs as uid {os.getuid()}"
    )


def _owner(path: Path) -> int | None:
    """The owner of the path, or of the nearest parent that exists."""
    for candidate in (path, *path.parents):
        try:
            return candidate.stat().st_uid
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class Unloadable:
    """A configured instance whose plugin could not be loaded: it never starts,
    and status shows it failed with the load error until a later start finds
    the plugin whole. See ``docs/features/plugins.md``, "When a plugin cannot
    load"."""

    role: str
    type_name: str
    error: str


def model_problem(report: ModelReport) -> dict[str, Any]:
    """The problem document for a model that does not conform."""
    count = len(report.violations)
    return {
        "type": "urn:bricklogger:problem:model-is-invalid",
        "title": "Model is invalid",
        "status": 422,
        "detail": f"{count} violation{'' if count == 1 else 's'}",
        "errors": [violation.as_dict() for violation in report.violations],
    }


def unreadable_problem(exc: ModelUnreadable) -> dict[str, Any]:
    return {
        "type": "urn:bricklogger:problem:model-is-unreadable",
        "title": "Model is unreadable",
        "status": 422,
        "detail": str(exc),
    }


class GraphAccess:
    """The read-only graph access a source receives."""

    def __init__(self, graph: WorkingGraph, prefixes: Mapping[str, str]) -> None:
        self._graph = graph
        self._prefixes = dict(prefixes)

    @property
    def prefixes(self) -> Mapping[str, str]:
        return self._prefixes

    def query(self, sparql: str) -> Any:
        return self._graph.query(sparql)


class Daemon:
    """One daemon process: configuration, model, plan and instances."""

    def __init__(
        self,
        config_dir: Path,
        registry: PluginRegistry | None = None,
        env: Mapping[str, str] | None = None,
        update_lookup: Lookup | None = None,
    ) -> None:
        self.config_dir = config_dir
        if registry is None:
            registry = PluginRegistry.from_entry_points()
        self.registry = registry
        self.env = env
        self.configuration: Configuration | None = None
        self.state: RuntimeState | None = None
        self.model_store: ModelStore | None = None
        self.graph: WorkingGraph | None = None
        self.graph_access: GraphAccess | None = None
        self.sink: DaemonSink | None = None
        self.overlay: ValueOverlay | None = None
        self.sources: dict[str, SourceRunner] = {}
        self.destinations: dict[str, DestinationRunner] = {}
        self.unloadable: dict[str, Unloadable] = {}
        self.spools: dict[str, Spool] = {}
        self.notifier: Notifier | None = None
        self.updates: UpdateWatch | None = None
        self._update_lookup = update_lookup
        self.plan: Plan | None = None
        self.active_version: int | None = None
        self.model_error: str | None = None
        self.started_at: datetime | None = None
        self._fallback_active: set[str] = set()
        self._turtle: dict[int, str] = {}
        self._lock = threading.RLock()
        self.jobs = JobRunner()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        result = validate_configuration(self.config_dir, self.registry, self.env)
        if result.configuration is None:
            raise DaemonError(
                "the configuration is invalid: "
                + "; ".join(str(issue) for issue in result.errors)
            )
        config = result.configuration
        data_dir = config.daemon.data_dir
        self.configuration = config
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            self.state = RuntimeState(data_dir / STATE_FILE)
            self.model_store = ModelStore(data_dir)
            self.graph = WorkingGraph(data_dir / GRAPH_DIR)
        except (OSError, sqlite3.Error) as exc:
            raise DaemonError(unusable_data_dir(data_dir, exc)) from exc
        # an abandoned instance died with the old process
        self.state.clear_warnings(code="stop_timeout")
        self.overlay = ValueOverlay(self.graph, self.state)
        with self._lock:
            self._open_destinations(config.destinations)
            self.sink = DaemonSink(self.state, self.spools, overlay=self.overlay)
            self._activate_marked_version()
            self._create_sources(config.sources)
            self._replan()
            self.overlay.flush()
            self.overlay.start()
            self._start_sources()
        self.started_at = datetime.now(UTC)
        self.notifier = Notifier(self, config.daemon.notifications)
        self.notifier.record_event("started")
        self.notifier.start()
        self.updates = UpdateWatch(config.daemon.updates.check, self._update_lookup)
        self.updates.start()
        log.info("daemon started with config %s", self.config_dir)

    def stop(self) -> None:
        self.jobs.stop()
        if self.updates is not None:
            self.updates.stop()
        if self.notifier is not None:
            # Held rather than sent, so an upgrade gives one mail that says
            # the daemon stopped and started; see docs/features/notifications.md.
            self.notifier.record_event("stopped")
            self.notifier.stop()
        if self.overlay is not None:
            self.overlay.stop()
        with self._lock:
            for source_runner in self.sources.values():
                source_runner.stop()
            for destination_runner in self.destinations.values():
                destination_runner.stop()
            for spool in self.spools.values():
                spool.close()
            if self.state is not None:
                self.state.close()
        log.info("daemon stopped")

    def reload(self) -> ValidationResult:
        """Re-read the configuration; an invalid one is rejected, nothing changes."""
        result = validate_configuration(self.config_dir, self.registry, self.env)
        if result.configuration is None:
            return result
        new = result.configuration
        assert self.configuration is not None
        with self._lock:
            self._reconcile_destinations(
                self.configuration.destinations, new.destinations
            )
            self._reconcile_sources(self.configuration.sources, new.sources)
            self.configuration = new
            self._replan()
            self._start_sources()
            if self.notifier is not None:
                self.notifier.reconfigure(new.daemon.notifications)
            if self.updates is not None:
                self.updates.reconfigure(new.daemon.updates.check)
        log.info("configuration reloaded from %s", self.config_dir)
        return result

    # --- configuration -------------------------------------------------------

    def config_texts(self) -> dict[str, str | None]:
        """The four files as written, ``None`` where one does not exist."""
        return read_texts(self.config_dir)

    def validate_config(
        self, texts: Mapping[str, str | None] | None = None
    ) -> ValidationResult:
        """Validate the files on disk, or the same with ``texts`` replacing some."""
        return validate_configuration(self.config_dir, self.registry, self.env, texts)

    def replace_config_file(self, name: str, text: str) -> ValidationResult:
        """Validate the directory with one file replaced; write and apply it if valid.

        The text is written verbatim and atomically, then the configuration is
        reloaded from disk, so the change takes effect like a reload does.
        """
        with self._lock:
            result = validate_configuration(
                self.config_dir, self.registry, self.env, {name: text}
            )
            if result.configuration is None:
                return result
            write_text(self.config_dir, name, text)
            return self.reload()

    # --- model ---------------------------------------------------------------

    def activate(
        self, version: int, on_step: Callable[[str], None] | None = None
    ) -> Activation:
        """Activate a stored version: validate, infer, swap, re-plan.

        Validation and inference run without the lock; only the swap holds it,
        so the daemon keeps collecting on the running model meanwhile.
        """
        assert self.model_store is not None
        stored = self.model_store.get(version)
        started = time.monotonic()
        inference = validate_and_infer(
            self.model_store.read(version),
            stored.format,
            on_step,
            vocabularies=self.registry.vocabularies(),
        )
        return self._swap(inference, version, started, on_step)

    def _swap(
        self,
        inference: Inference,
        number: int,
        started: float,
        on_step: Callable[[str], None] | None = None,
    ) -> Activation:
        """Put an inferred model into the working graph and re-plan on it."""
        assert self.model_store is not None and self.graph is not None
        assert self.state is not None
        if on_step is not None:
            on_step("activating")
        with self._lock:
            self.graph.replace_model(
                inference.model, inference.ontology, inference.inferred
            )
            if number != self.active_version:
                # another version's references until each destination writes this one
                self._clear_timeseries_graphs()
            self.model_store.set_active(number)
            self.state.record_activation(number)
            self.active_version = number
            self.model_error = None
            self._refresh_graph_access()
            self._replan()
        log.info(
            "model version %d activated: %d triples, %d inferred",
            number,
            len(inference.model),
            len(inference.inferred),
        )
        return Activation(
            version=number,
            model_triples=len(inference.model),
            ontology_triples=len(inference.ontology),
            inferred_triples=len(inference.inferred),
            seconds=time.monotonic() - started,
            report=inference.report,
        )

    # --- jobs ----------------------------------------------------------------

    def upload_model(self, data: bytes, fmt: str, *, activate: bool = True) -> Job:
        """Store an upload as a job: validate, store, and activate unless told not to.

        Nothing is stored when the model does not conform, and the marker moves
        only when the activation has succeeded.
        """
        assert self.model_store is not None
        store = self.model_store

        def work(job: Job) -> dict[str, Any]:
            def step(name: str) -> None:
                job.step = name

            started = time.monotonic()
            previous = self.active_version
            try:
                if not activate:
                    step("validating")
                    report = validate_model(data, fmt, self.registry.vocabularies())
                    if not report.valid:
                        raise JobFailed(model_problem(report))
                    version = store.store(data, fmt)
                    return {
                        "version": version.as_dict(),
                        "report": report.as_dict(),
                        "activated": False,
                        "previous": previous,
                        "diff": None,
                        "activation": None,
                    }
                inference = validate_and_infer(
                    data, fmt, step, vocabularies=self.registry.vocabularies()
                )
            except ModelInvalid as exc:
                raise JobFailed(model_problem(exc.report)) from exc
            except ModelUnreadable as exc:
                raise JobFailed(unreadable_problem(exc)) from exc
            version = store.store(data, fmt)
            activation = self._swap(inference, version.number, started, step)
            return {
                "version": version.as_dict(),
                "report": inference.report.as_dict(),
                "activated": True,
                "previous": previous,
                "diff": self._diff_from(previous, version.number),
                "activation": activation.as_dict(),
            }

        return self.jobs.submit("upload", work)

    def activate_model(self, version: int) -> Job:
        """Activate a stored version as a job; an unknown version is refused at once."""
        assert self.model_store is not None
        stored = self.model_store.get(version)

        def work(job: Job) -> dict[str, Any]:
            def step(name: str) -> None:
                job.step = name

            previous = self.active_version
            try:
                activation = self.activate(version, step)
            except ModelInvalid as exc:
                raise JobFailed(model_problem(exc.report)) from exc
            except ModelUnreadable as exc:
                raise JobFailed(unreadable_problem(exc)) from exc
            return {
                "version": stored.as_dict(),
                "report": activation.report.as_dict(),
                "activated": True,
                "previous": previous,
                "diff": self._diff_from(previous, version),
                "activation": activation.as_dict(),
            }

        return self.jobs.submit("activate", work)

    def _diff_from(self, previous: int | None, number: int) -> dict[str, Any] | None:
        assert self.model_store is not None
        if previous is None or previous == number:
            return None
        return diff_versions(self.model_store, previous, number).as_dict()

    def diff(self, a: int, b: int) -> ModelDiff:
        """The diff between two stored versions, on the models as uploaded."""
        assert self.model_store is not None
        return diff_versions(self.model_store, a, b)

    def models(self) -> dict[str, Any]:
        """The stored versions, which one is active, and when each was activated."""
        assert self.model_store is not None
        history = self._activation_history()
        return {
            "active": self.active_version,
            "prefixes": dict(self.graph_access.prefixes) if self.graph_access else {},
            "versions": [
                {
                    **version.as_dict(),
                    "active": version.number == self.active_version,
                    "activations": history.get(version.number, []),
                }
                for version in self.model_store.versions()
            ],
        }

    def sparql(self, query: str, accept: str = "") -> tuple[bytes, str]:
        """A read-only query over the working graph; raises QueryError."""
        assert self.graph is not None
        prefixes = self.graph_access.prefixes if self.graph_access else WELL_KNOWN
        return run_query(self.graph, prefixes, query, accept)

    def export(
        self,
        version: int,
        *,
        inferred: bool = False,
        values: bool = False,
        timeseries: str | None = None,
    ) -> tuple[bytes, str]:
        """A version as uploaded, with its format name.

        With the inferred graph, the values or a destination's time-series
        references added the result is Turtle built from the working graph,
        which holds them for the active version only. ``timeseries`` names the
        destination instance, or is ``"true"`` for the one that stores the
        model.
        """
        assert self.model_store is not None and self.graph is not None
        stored = self.model_store.get(version)
        if not inferred and not values and timeseries is None:
            return self.model_store.read(version), stored.format
        instance = None if timeseries is None else self._reference_instance(timeseries)
        if version != self.active_version:
            raise ExportUnavailable(
                "the inferred graph, the values and the time-series references "
                "exist for the active version only"
            )
        graph = rdflib.Graph()
        graph.parse(data=self.graph.dump(MODEL_GRAPH), format="nt")
        if inferred:
            graph.parse(data=self.graph.dump(INFERRED_GRAPH), format="nt")
        if values:
            graph.parse(data=self.graph.dump(VALUES_GRAPH), format="nt")
        if instance is not None:
            graph.parse(data=self.graph.dump(timeseries_graph(instance)), format="nt")
            graph.bind("ref", REF)
        prefixes = self.graph_access.prefixes if self.graph_access else WELL_KNOWN
        for prefix, iri in prefixes.items():
            graph.bind(prefix, iri, override=True)
        return graph.serialize(format="turtle").encode("utf-8"), "turtle"

    def _activate_marked_version(self) -> None:
        assert self.model_store is not None and self.graph is not None
        assert self.state is not None
        marker = self.model_store.active()
        if marker is None:
            self.active_version = None
            self._refresh_graph_access()
            return
        last = self.state.last_activation()
        needs_build = (
            last is None or last[0] != marker or self.graph.count(MODEL_GRAPH) == 0
        )
        if needs_build:
            self._clear_timeseries_graphs()
            try:
                activate_version(
                    self.model_store,
                    self.graph,
                    marker,
                    vocabularies=self.registry.vocabularies(),
                )
            except ModelInvalid as exc:
                problems = "; ".join(
                    f"{v.focus}: {v.message}" for v in exc.report.violations[:5]
                )
                self.model_error = f"version {marker} does not conform: {problems}"
                log.error("the marked model version %d is invalid", marker)
                self.active_version = None
                self._refresh_graph_access()
                return
            self.state.record_activation(marker)
        self.active_version = marker
        self._refresh_graph_access()

    # --- the model in destinations -------------------------------------------

    def _reference_instance(self, timeseries: str) -> str:
        """The destination instance whose references an export adds: the one
        named, or with ``"true"`` the only one that stores the model."""
        storing = sorted(
            name for name, runner in self.destinations.items() if runner.stores_model
        )
        if timeseries == "true":
            if not storing:
                raise NotFound("no destination stores the model")
            if len(storing) > 1:
                raise ExportUnavailable(
                    "several destinations store the model; name one: "
                    + ", ".join(storing)
                )
            return storing[0]
        if timeseries not in storing:
            raise NotFound(f"no destination {timeseries!r} stores the model")
        return timeseries

    def _clear_timeseries_graphs(self, instance: str | None = None) -> None:
        assert self.graph is not None
        for iri in self.graph.named_graphs():
            if iri.startswith(TIMESERIES_GRAPH_PREFIX) and (
                instance is None or iri == timeseries_graph(instance)
            ):
                self.graph.clear_graph(iri)

    def _model_document(
        self, version: int, activations: Sequence[str]
    ) -> ModelDocument:
        """A version as Turtle, without references; the runner adds them."""
        assert self.model_store is not None
        stored = self.model_store.get(version)
        turtle = self._turtle.get(version)
        if turtle is None:
            model = parse_model(self.model_store.read(version), stored.format)
            turtle = model.serialize(format="turtle")
            self._turtle[version] = turtle
        return ModelDocument(
            version=version,
            uploaded_at=stored.uploaded_at,
            activations=tuple(datetime.fromisoformat(a) for a in activations),
            turtle=turtle,
        )

    def _activation_history(self) -> dict[int, list[str]]:
        assert self.state is not None
        history: dict[int, list[str]] = {}
        for row in self.state.activations():
            history.setdefault(int(row["version"]), []).append(str(row["activated_at"]))
        return history

    def _offer_models(self, spools: Sequence[Spool], *, every: bool) -> None:
        """Spool the active version, or every version that has been active,
        for the destinations that store the model."""
        assert self.model_store is not None
        if not spools:
            return
        history = self._activation_history()
        if every:
            versions = sorted(history)
        elif self.active_version is not None:
            versions = [self.active_version]
        else:
            versions = []
        for version in versions:
            try:
                document = self._model_document(version, history.get(version, []))
            except (ModelNotFound, ModelUnreadable):
                log.warning("model version %d cannot be offered", version)
                continue
            for spool in spools:
                spool.append_model(document)

    def _model_spools(self) -> list[Spool]:
        return [
            self.spools[name]
            for name, runner in self.destinations.items()
            if runner.stores_model
        ]

    def _on_model_written(
        self, instance: str, version: int, ids: Mapping[str, str]
    ) -> None:
        """Called from a destination's thread once it stored a version; the
        active version's keys become the instance's graph of references."""
        if version != self.active_version or self.graph is None:
            return
        try:
            self.graph.replace_graph(timeseries_graph(instance), reference_triples(ids))
        except Exception:
            log.exception("the time-series references of %s were not stored", instance)

    def _refresh_graph_access(self) -> None:
        assert self.graph is not None and self.model_store is not None
        prefixes = {**WELL_KNOWN, **self.registry.prefixes()}
        if self.active_version is not None:
            version = self.model_store.get(self.active_version)
            prefixes.update(
                declared_prefixes(
                    parse_model(
                        self.model_store.read(self.active_version), version.format
                    )
                )
            )
        self.graph_access = GraphAccess(self.graph, prefixes)
        for runner in self.sources.values():
            runner.source.graph = self.graph_access

    # --- instances -----------------------------------------------------------

    def _open_destinations(self, instances: Mapping[str, DestinationInstance]) -> None:
        for name, instance in instances.items():
            self._open_destination(name, instance)

    def _open_destination(self, name: str, instance: DestinationInstance) -> None:
        assert self.configuration is not None and self.state is not None
        declaration = self.registry.destinations.get(instance.type)
        if declaration is None:
            self._mark_unloadable(name, "destination", instance.type)
            return
        config = declaration.config_schema.model_validate(instance.settings)
        destination = declaration.create(name, config)
        spool = Spool(self.configuration.daemon.data_dir / SPOOL_DIR / f"{name}.sqlite")
        self.spools[name] = spool
        runner = DestinationRunner(
            name,
            instance.type,
            destination,
            spool,
            self.state,
            batch_size=instance.batch.size,
            batch_interval=instance.batch.interval,
            spool_max_size=instance.spool.max_size,
            spool_max_age=instance.spool.max_age,
            stop_timeout=self.configuration.daemon.stop_timeout,
            stores_metadata=declaration.stores_metadata,
            stores_model=declaration.stores_model,
            on_model_written=self._on_model_written,
        )
        self.destinations[name] = runner
        if declaration.stores_model:
            self._offer_models([spool], every=True)
        if self.state.stop_intent(name):
            runner.current_state = "stopped"
            self.state.set_instance(
                name, role="destination", type_name=instance.type, state="stopped"
            )
            self.state.warn("instance_stopped", name, "stopped by the operator")
        else:
            runner.start()

    def _create_sources(self, instances: Mapping[str, SourceInstance]) -> None:
        for name, instance in instances.items():
            self._create_source(name, instance)

    def _create_source(self, name: str, instance: SourceInstance) -> None:
        assert self.configuration is not None and self.state is not None
        assert self.graph_access is not None and self.sink is not None
        declaration = self.registry.sources.get(instance.type)
        if declaration is None:
            self._mark_unloadable(name, "source", instance.type)
            return
        config = declaration.config_schema.model_validate(instance.settings)
        source = declaration.create(name, config, self.graph_access)
        status = InstanceStatus(name, instance.type, self.state, self._on_outcomes)
        self.sources[name] = SourceRunner(
            name,
            instance.type,
            source,
            self.sink,
            status,
            self.state,
            self.configuration.daemon.stop_timeout,
        )

    def _start_sources(self) -> None:
        assert self.state is not None
        for name, runner in self.sources.items():
            if self.state.stop_intent(name):
                runner.current_state = "stopped"
                self.state.set_instance(
                    name, role="source", type_name=runner.type_name, state="stopped"
                )
                self.state.warn("instance_stopped", name, "stopped by the operator")
            else:
                runner.start()

    def _mark_unloadable(self, name: str, role: str, type_name: str) -> None:
        """Record an instance whose plugin did not load: failed, with the error,
        and the same warning a crashed instance raises."""
        assert self.state is not None
        failure = self.registry.failure_of(type_name)
        error = failure.error if failure is not None else not_installed(type_name)
        self.unloadable[name] = Unloadable(role, type_name, error)
        self.state.set_instance(
            name, role=role, type_name=type_name, state="failed", error=error
        )
        self.state.warn("instance_failed", name, f"failed: {error}")
        log.error("%s %s cannot start: %s", role, name, error)

    def _forget_unloadable(self, name: str) -> bool:
        """Drop the record of an unloadable instance; whether there was one."""
        assert self.state is not None
        if self.unloadable.pop(name, None) is None:
            return False
        self.state.clear_warnings(code="instance_failed", subject=name)
        return True

    def _reconcile_destinations(
        self,
        old: Mapping[str, DestinationInstance],
        new: Mapping[str, DestinationInstance],
    ) -> None:
        for name in set(old) - set(new):
            if self._forget_unloadable(name):
                continue
            self.destinations.pop(name).stop()
            self.spools.pop(name).close()
            self._clear_timeseries_graphs(name)
        for name, instance in new.items():
            if name in old and old[name] == instance:
                continue
            self._forget_unloadable(name)
            if name in self.destinations:
                self.destinations.pop(name).stop()
                self.spools.pop(name).close()
            self._open_destination(name, instance)
        assert self.sink is not None
        self.sink.spools = dict(self.spools)

    def _reconcile_sources(
        self, old: Mapping[str, SourceInstance], new: Mapping[str, SourceInstance]
    ) -> None:
        for name in set(old) - set(new):
            if self._forget_unloadable(name):
                continue
            self.sources.pop(name).stop()
        for name, instance in new.items():
            unchanged = name in old and old[name] == instance
            if unchanged and (name in self.sources or name in self.unloadable):
                continue
            self._forget_unloadable(name)
            if name in self.sources:
                self.sources.pop(name).stop()
            self._create_source(name, instance)

    # --- plugins -------------------------------------------------------------

    def plugins(self) -> list[dict[str, Any]]:
        """The installed plugins with their configured instances."""
        return summarize_plugins(self.registry, self.configuration)

    def plugin(self, type_name: str) -> dict[str, Any]:
        """One plugin's declaration; raises NotFound."""
        try:
            return describe_plugin(
                self.registry, type_name, self._instances_of(type_name)
            )
        except KeyError as exc:
            raise NotFound(f"unknown plugin type {type_name!r}") from exc

    def _instances_of(self, type_name: str) -> list[str]:
        if self.configuration is None:
            return []
        return sorted(
            [n for n, i in self.configuration.sources.items() if i.type == type_name]
            + [
                n
                for n, i in self.configuration.destinations.items()
                if i.type == type_name
            ]
        )

    def run_tool(
        self, type_name: str, instance: str, tool: str, parameters: Mapping[str, Any]
    ) -> Any:
        """Run a declared tool on a running source instance.

        Raises NotFound, InstanceNotRunning, ToolFailed, or pydantic\'s
        ValidationError when the parameters do not fit the tool\'s schema.
        """
        declaration = self.registry.sources.get(type_name)
        if declaration is None:
            raise NotFound(f"unknown source type {type_name!r}")
        try:
            tool_declaration = tool_of(declaration, tool)
        except KeyError as exc:
            raise NotFound(f"{type_name!r} has no tool {tool!r}") from exc
        runner = self.sources.get(instance)
        if runner is None or runner.type_name != type_name:
            raise NotFound(f"no instance {instance!r} of {type_name!r}")
        params = tool_declaration.parameters.model_validate(dict(parameters))
        if runner.current_state != "running":
            raise InstanceNotRunning(f"{instance} is {runner.current_state}")
        try:
            return runner.source.run_tool(tool, params.model_dump())
        except Exception as exc:
            raise ToolFailed(f"{tool}: {exc}") from exc

    def control_instance(
        self, type_name: str, instance: str, action: str
    ) -> dict[str, Any]:
        """Start, stop or restart an instance; the operator's stop persists."""
        assert self.state is not None
        if type_name in self.registry.sources:
            role = "source"
            runner: SourceRunner | DestinationRunner | None = self.sources.get(instance)
        elif type_name in self.registry.destinations:
            role = "destination"
            runner = self.destinations.get(instance)
        else:
            entry = self.unloadable.get(instance)
            if entry is not None and entry.type_name == type_name:
                raise InstanceNotRunning(f"{instance} cannot start: {entry.error}")
            raise NotFound(f"unknown plugin type {type_name!r}")
        if runner is None or runner.type_name != type_name:
            raise NotFound(f"no instance {instance!r} of {type_name!r}")
        if action not in ("start", "stop", "restart"):
            raise NotFound(f"unknown action {action!r}; start, stop or restart")
        with self._lock:
            if action == "stop":
                self.state.set_stop_intent(instance, True)
                runner.stop()
                self.state.warn("instance_stopped", instance, "stopped by the operator")
            else:
                self.state.set_stop_intent(instance, False)
                self.state.clear_warnings(code="instance_stopped", subject=instance)
                if action == "restart":
                    runner.stop()
                runner.start()
        rows = self.status_sources() if role == "source" else self.status_destinations()
        return next(row for row in rows if row["name"] == instance)

    # --- plan ----------------------------------------------------------------

    def _replan(self) -> None:
        assert self.configuration is not None and self.state is not None
        assert self.graph is not None and self.graph_access is not None
        self._fallback_active.clear()
        if self.active_version is None:
            self.plan = None
            self.state.replace_plan([])
            self._clear_plan_warnings()
            if self.overlay is not None:
                self.overlay.request_rebuild()
            for runner in self.sources.values():
                runner.assign([])
            return
        try:
            plan = compute_plan(
                self.graph,
                self.graph_access.prefixes,
                self.configuration.rules,
                {name: runner.source for name, runner in self.sources.items()},
                {
                    name: self.registry.sources[runner.type_name].reference_types
                    for name, runner in self.sources.items()
                },
            )
        except PlanError as exc:
            raise DaemonError(str(exc)) from exc
        self.state.replace_plan(
            (point.uri, owner, point.method)
            for owner, points in plan.assignments.items()
            for point in points
        )
        if self.overlay is not None:
            self.overlay.request_rebuild()
        # Cleared after the plan is replaced: a source pushing metadata for a
        # point that just left the plan can then no longer re-raise a warning.
        self._clear_plan_warnings()
        for warning in plan.warnings:
            self.state.warn(warning.code, warning.subject, warning.message)
        self.plan = plan
        assigned = [
            point.uri for points in plan.assignments.values() for point in points
        ]
        if assigned and self.sink is not None:
            entries = graph_metadata(self.graph, self.graph_access.prefixes, assigned)
            self.sink.metadata(entries.values())
        # behind the metadata, so the destinations hold the keys the model needs
        self._offer_models(self._model_spools(), every=False)
        for name, runner in self.sources.items():
            runner.assign(plan.assignments.get(name, []))

    def _clear_plan_warnings(self) -> None:
        assert self.state is not None
        for code in PLAN_WARNINGS:
            self.state.clear_warnings(code=code)

    def _on_outcomes(self, instance: str, outcomes: Sequence[Outcome]) -> None:
        """Apply the rule's fallback when a source reports a point as unsupported."""
        assert self.state is not None
        with self._lock:
            plan = self.plan
            runner = self.sources.get(instance)
            if plan is None or runner is None:
                return
            changed = False
            stale = self.state.has_warning("no_fallback")
            for outcome in outcomes:
                if outcome.state != "unsupported":
                    if stale:
                        self.state.clear_warnings(
                            code="no_fallback", subject=outcome.point
                        )
                    continue
                if outcome.point in self._fallback_active:
                    continue
                accepted = plan.accepted.get(outcome.point)
                if accepted is None:
                    continue
                if accepted.fallback is None:
                    self.state.warn(
                        "no_fallback",
                        outcome.point,
                        f"cannot do {accepted.method!r}; the rule has no fallback",
                    )
                    continue
                replacement = assigned_point(accepted, fallback=True)
                plan.assignments[instance] = [
                    replacement if point.uri == outcome.point else point
                    for point in plan.assignments[instance]
                ]
                self._fallback_active.add(outcome.point)
                self.state.set_fallback(outcome.point, replacement.method, True)
                changed = True
            if changed:
                runner.assign(plan.assignments[instance])

    # --- status --------------------------------------------------------------

    def health(self) -> str:
        """``ok``, ``idle`` or ``degraded``, as the health endpoint reports it."""
        assert self.state is not None
        if self.active_version is None:
            return "idle"
        if self.sources and all(self.state.stop_intent(name) for name in self.sources):
            return "idle"
        failed = any(
            source.current_state == "failed" for source in self.sources.values()
        ) or any(
            destination.current_state == "failed"
            for destination in self.destinations.values()
        )
        dropped = self.state.has_warning("spool_drop")
        return "degraded" if failed or dropped or self.unloadable else "ok"

    def status(self) -> dict[str, Any]:
        assert self.state is not None and self.configuration is not None
        assert self.model_store is not None
        version = (
            self.model_store.get(self.active_version).as_dict()
            if self.active_version is not None
            else None
        )
        activation = self.state.last_activation()
        counts = self.state.point_counts()
        return {
            "version": __version__,
            "health": self.health(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime_seconds": (
                (datetime.now(UTC) - self.started_at).total_seconds()
                if self.started_at
                else None
            ),
            "config_dir": str(self.config_dir),
            "data_dir": str(self.configuration.daemon.data_dir),
            "model": {
                "active": version,
                "activated_at": activation[1] if activation else None,
                "error": self.model_error,
            },
            "points": {
                "accepted": len(self.plan.accepted) if self.plan else 0,
                "assigned": self.plan.assigned if self.plan else 0,
                "active": counts.get("active", 0),
                "unsupported": counts.get("unsupported", 0),
                "rejected": counts.get("rejected", 0),
            },
            "rules": {
                "matched": self.plan.matched_per_rule if self.plan else [],
                "skipped": len(self.plan.rule_issues) if self.plan else 0,
            },
            "instances": [
                {
                    "name": name,
                    "role": "source",
                    "type": runner.type_name,
                    "state": runner.current_state,
                }
                for name, runner in self.sources.items()
            ]
            + [
                {
                    "name": name,
                    "role": "destination",
                    "type": destination.type_name,
                    "state": destination.current_state,
                }
                for name, destination in self.destinations.items()
            ]
            + [
                {
                    "name": name,
                    "role": entry.role,
                    "type": entry.type_name,
                    "state": "failed",
                }
                for name, entry in self.unloadable.items()
            ],
            "warnings": len(self.state.warnings()),
            "observations_received": self.state.counter("observations_received"),
            "updates": self.updates.summary() if self.updates else None,
        }

    def status_sources(self) -> list[dict[str, Any]]:
        assert self.state is not None
        rows = {
            row["name"]: row
            for row in self.state.instances()
            if row["role"] == "source"
        }
        result: list[dict[str, Any]] = []
        for name, runner in self.sources.items():
            row = rows.get(name, {})
            result.append(
                {
                    "name": name,
                    "type": runner.type_name,
                    "state": runner.current_state,
                    "last_error": row.get("last_error"),
                    "restart_count": row.get("restart_count", 0),
                    "stopped_by_operator": bool(row.get("stop_intent", False)),
                    "resources": sorted(runner.source.resources()),
                    "points": self.state.point_counts(instance=name),
                    "devices": self.state.devices(name),
                }
            )
        for name, entry in self.unloadable.items():
            if entry.role != "source":
                continue
            result.append(
                {
                    "name": name,
                    "type": entry.type_name,
                    "state": "failed",
                    "last_error": entry.error,
                    "restart_count": 0,
                    "stopped_by_operator": False,
                    "resources": [],
                    "points": self.state.point_counts(instance=name),
                    "devices": [],
                }
            )
        return result

    def status_destinations(self) -> list[dict[str, Any]]:
        assert self.state is not None
        rows = {
            row["name"]: row
            for row in self.state.instances()
            if row["role"] == "destination"
        }
        result: list[dict[str, Any]] = []
        for name, runner in self.destinations.items():
            row = rows.get(name, {})
            stats = self.spools[name].stats()
            result.append(
                {
                    "name": name,
                    "type": runner.type_name,
                    "state": runner.current_state,
                    "stores_metadata": runner.stores_metadata,
                    "stores_model": runner.stores_model,
                    "model_version": runner.model_version,
                    "last_error": row.get("last_error"),
                    "last_write": runner.last_write,
                    "written": self.state.counter(f"written:{name}"),
                    "spool": {
                        "observations": stats.observations,
                        "bytes": stats.bytes,
                        "oldest_age_seconds": stats.oldest_age,
                        "dropped": self.state.counter(f"spool_drop:{name}"),
                    },
                }
            )
        for name, entry in self.unloadable.items():
            if entry.role != "destination":
                continue
            result.append(
                {
                    "name": name,
                    "type": entry.type_name,
                    "state": "failed",
                    "stores_metadata": None,
                    "stores_model": None,
                    "model_version": None,
                    "last_error": entry.error,
                    "last_write": None,
                    "written": 0,
                    "spool": {
                        "observations": 0,
                        "bytes": 0,
                        "oldest_age_seconds": None,
                        "dropped": 0,
                    },
                }
            )
        return result

    def status_warnings(self) -> list[dict[str, Any]]:
        assert self.state is not None
        return self.state.warnings()

    def clear_warnings(
        self, code: str | None = None, subject: str | None = None
    ) -> int:
        """Clear warnings by hand; one whose condition holds is asserted again.

        A manual clear is an acknowledgement, not an end, so the notifier
        forgets it too rather than sending an all clear for a condition that
        may still hold.
        """
        assert self.state is not None
        return self.state.clear_warnings(
            code=code, subject=subject, forget_notified=True
        )

    def notification_status(self) -> dict[str, Any]:
        """What ``notify status`` and the web panel show."""
        assert self.notifier is not None
        return self.notifier.status()

    def send_test_notification(self) -> dict[str, Any]:
        """Send one test mail on the configuration as written, on or off."""
        assert self.notifier is not None
        return self.notifier.send_test()

    def points(
        self,
        *,
        instance: str | None = None,
        outcome: str | None = None,
        warning: str | None = None,
        brick_class: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        assert self.state is not None
        if brick_class is not None and self.graph_access is not None:
            prefixes = self.graph_access.prefixes
            with contextlib.suppress(UnknownPrefix):
                brick_class = compact(expand(brick_class, prefixes), prefixes)
        total, items = self.state.points(
            instance=instance,
            outcome=outcome,
            warning=warning,
            brick_class=brick_class,
            limit=limit,
            offset=offset,
        )
        warnings_by_point: dict[str, list[str]] = {}
        for entry in self.state.warnings():
            warnings_by_point.setdefault(entry["subject"], []).append(entry["code"])
        for item in items:
            item["warnings"] = warnings_by_point.get(item["uri"], [])
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    def entities(
        self,
        *,
        kind: str | None = None,
        brick_class: str | None = None,
        finding: str | None = None,
        warning: str | None = None,
        outcome: str | None = None,
        instance: str | None = None,
        search: str | None = None,
        root: str | None = None,
        depth: int | None = None,
    ) -> dict[str, Any]:
        """The active model as elements, relations and findings, with the
        runtime per point; narrowed by the filters. See
        ``docs/features/api.md``, "Entities". Raises NotFound for an unknown
        root."""
        assert self.graph is not None and self.state is not None
        prefixes = self.graph_access.prefixes if self.graph_access else WELL_KNOWN
        if self.active_version is None:
            return empty_document(prefixes)
        document = project_entities(self.graph, prefixes)
        document["version"] = self.active_version
        attach_runtime(
            document,
            accepted=self.plan.accepted if self.plan else {},
            states=self.state.point_states(),
            warnings=self.state.warnings(),
            prefixes=prefixes,
        )
        keep: set[str] | None = None
        if brick_class is not None:
            keep = set()
            with contextlib.suppress(UnknownPrefix):
                keep = {
                    compact(uri, prefixes)
                    for uri in entities_of_class(
                        self.graph, expand(brick_class, prefixes)
                    )
                }
        if root is not None:
            with contextlib.suppress(UnknownPrefix):
                root = compact(expand(root, prefixes), prefixes)
        try:
            return narrow(
                document,
                kind=kind,
                keep=keep,
                finding=finding,
                warning=warning,
                outcome=outcome,
                instance=instance,
                search=search,
                root=root,
                depth=depth,
            )
        except KeyError as exc:
            raise NotFound(f"no element {root!r} in the active model") from exc

    def assignment_of(self, instance: str) -> list[AssignedPoint]:
        """What an instance is currently asked to collect (for tests and tools)."""
        if self.plan is None:
            return []
        return list(self.plan.assignments.get(instance, []))
