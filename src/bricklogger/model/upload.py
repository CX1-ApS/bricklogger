"""Upload, activation and diff on top of the version store and the working graph."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from bricklogger.model.diff import ModelDiff, diff_models
from bricklogger.model.inference import (
    ModelReport,
    parse_model,
    validate_and_infer,
    validate_model,
)
from bricklogger.model.versions import ModelStore, ModelVersion
from bricklogger.model.working_graph import WorkingGraph
from bricklogger.sdk.declaration import Vocabulary


@dataclass
class UploadResult:
    """A stored upload, its report, and the diff to the previously active version."""

    version: ModelVersion
    report: ModelReport
    activated: bool
    previous: int | None
    diff: ModelDiff | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version.as_dict(),
            "report": self.report.as_dict(),
            "activated": self.activated,
            "previous": self.previous,
            "diff": self.diff.as_dict() if self.diff is not None else None,
        }


def upload_model(
    store: ModelStore, data: bytes, fmt: str, *, activate: bool = True
) -> UploadResult:
    """Validate and store an upload; mark it active unless told not to.

    Marking is what the daemon acts on: it builds the working graph for the
    active version at start, or, when running, in the activation job.
    """
    report = validate_model(data, fmt)
    if not report.valid:
        from bricklogger.model.inference import ModelInvalid

        raise ModelInvalid(report)
    version = store.store(data, fmt)
    previous = store.active()
    diff: ModelDiff | None = None
    if activate:
        if previous is not None and previous != version.number:
            diff = diff_versions(store, previous, version.number)
        store.set_active(version.number)
    return UploadResult(version, report, activate, previous, diff)


def diff_versions(store: ModelStore, a: int, b: int) -> ModelDiff:
    """The diff between two stored versions, computed on the models as uploaded."""
    old = parse_model(store.read(a), store.get(a).format)
    new = parse_model(store.read(b), store.get(b).format)
    return diff_models(old, new)


@dataclass
class Activation:
    """What an activation loaded into the working graph, and how long it took."""

    version: int
    model_triples: int
    ontology_triples: int
    inferred_triples: int
    seconds: float
    report: ModelReport

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "model_triples": self.model_triples,
            "ontology_triples": self.ontology_triples,
            "inferred_triples": self.inferred_triples,
            "seconds": round(self.seconds, 1),
            "warnings": [w.as_dict() for w in self.report.warnings],
        }


def activate_version(
    store: ModelStore,
    graph: WorkingGraph,
    number: int,
    vocabularies: Iterable[Vocabulary] | None = None,
) -> Activation:
    """Validate, infer and load a stored version, then mark it active.

    Nothing in the working graph changes until the inference is complete, so a
    version that fails validation leaves the running model untouched.
    ``vocabularies`` are the source plugins' own; ``None`` means the installed
    plugins'.
    """
    started = time.monotonic()
    stored = store.get(number)
    inference = validate_and_infer(
        store.read(number), stored.format, vocabularies=vocabularies
    )
    graph.replace_model(inference.model, inference.ontology, inference.inferred)
    store.set_active(number)
    return Activation(
        version=number,
        model_triples=len(inference.model),
        ontology_triples=len(inference.ontology),
        inferred_triples=len(inference.inferred),
        seconds=time.monotonic() - started,
        report=inference.report,
    )
