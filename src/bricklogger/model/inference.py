"""Validating a model and deriving the inferred graph.

The model is validated against Brick's shapes with the ontology as background,
and the SHACL rules run in place: Brick's own, which type a BACnet reference
written in the vocabulary the bundled Brick knows, and Bricklogger's
supplementary ones in ``shapes.ttl``, which type it in the other vocabulary in
the wild. The source plugins' own vocabularies join both the ontology and the
rules, so a reference type of a plugin's own is typed the same way. Then the
OWL-RL closure of model plus ontology is derived with the Rust reasoner
``reasonable``. See ``docs/architecture.md``, "Inference".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any

import pyshacl
import rdflib
import reasonable
from rdflib import RDF, Namespace, URIRef

from bricklogger.model.ontology import brick_ontology
from bricklogger.sdk.declaration import Vocabulary

log = logging.getLogger(__name__)

SH = Namespace("http://www.w3.org/ns/shacl#")
REF = Namespace("https://brickschema.org/schema/Brick/ref#")
QUDT_VOCABULARY = "http://qudt.org/vocab/"

Triple = tuple[Any, Any, Any]


class ModelUnreadable(Exception):
    """The upload could not be parsed as RDF in the given format."""


@dataclass(frozen=True)
class Violation:
    """One result of the SHACL validation that concerns the model."""

    focus: str
    message: str
    path: str | None = None
    severity: str = "Violation"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "focus": self.focus,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


@dataclass
class ModelReport:
    """What validation found: violations reject the model, warnings do not."""

    violations: list[Violation] = field(default_factory=list)
    warnings: list[Violation] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "violations": [v.as_dict() for v in self.violations],
            "warnings": [w.as_dict() for w in self.warnings],
        }


class ModelInvalid(Exception):
    """The model does not conform to Brick's shapes."""

    def __init__(self, report: ModelReport) -> None:
        self.report = report
        super().__init__("the model does not conform to Brick's shapes")


@dataclass
class Inference:
    """The three graphs an activation loads, as sets of triples."""

    model: set[Triple]
    ontology: set[Triple]
    inferred: set[Triple]
    report: ModelReport


Vocabularies = tuple[Vocabulary, ...]


@lru_cache(maxsize=4)
def vocabulary_graph(vocabularies: Vocabularies) -> rdflib.Graph:
    """The source plugins' vocabularies, parsed once per set."""
    graph = rdflib.Graph()
    for vocabulary in vocabularies:
        graph.parse(data=vocabulary.text(), format="turtle")
    return graph


@lru_cache(maxsize=4)
def ontology_graph(vocabularies: Vocabularies = ()) -> rdflib.Graph:
    """Brick's ontology plus the source plugins' vocabularies."""
    if not vocabularies:
        return brick_ontology()
    graph = rdflib.Graph()
    graph += brick_ontology()
    graph += vocabulary_graph(vocabularies)
    return graph


@lru_cache(maxsize=4)
def shapes_graph(vocabularies: Vocabularies = ()) -> rdflib.Graph:
    """Brick's shapes, Bricklogger's supplementary rules and the vocabularies' rules."""
    shapes = rdflib.Graph()
    shapes += ontology_graph(vocabularies)
    supplementary = files("bricklogger.model").joinpath("shapes.ttl")
    shapes.parse(data=supplementary.read_text(encoding="utf-8"), format="turtle")
    return shapes


@lru_cache(maxsize=1)
def installed_vocabularies() -> Vocabularies:
    """The vocabularies of the installed source plugins, found through entry points."""
    from bricklogger.sdk.registry import PluginError, PluginRegistry

    try:
        return PluginRegistry.from_entry_points().vocabularies()
    except PluginError as exc:
        log.warning("the installed plugins could not be loaded: %s", exc)
        return ()


def _resolve(vocabularies: Iterable[Vocabulary] | None) -> Vocabularies:
    """The given vocabularies as a cache key; ``None`` means the installed ones."""
    return installed_vocabularies() if vocabularies is None else tuple(vocabularies)


def parse_model(data: bytes, fmt: str) -> rdflib.Graph:
    """Parse an upload; a model that is not readable RDF is reported as such.

    Only the prefixes the document declares are bound, so the model\'s
    prefixes stay the model\'s own and not rdflib\'s defaults.
    """
    graph = rdflib.Graph(bind_namespaces="none")
    try:
        graph.parse(data=data, format=fmt)
    except Exception as exc:
        raise ModelUnreadable(f"not readable as {fmt}: {exc}") from exc
    return graph


def validate_with_rules(
    model: rdflib.Graph, vocabularies: Vocabularies = ()
) -> tuple[ModelReport, set[Triple]]:
    """Validate the model in place and run the SHACL rules on it.

    Only results about the model's own nodes count. Reference nodes are left to
    the source plugins, which reject what they cannot use with a reason, and
    values in QUDT's vocabulary are not verified, because the bundled Brick
    ontology does not carry QUDT's own type assertions.
    """
    before = set(model)
    subjects = {s for s, _, _ in before}
    reference_nodes = set(model.objects(None, REF.hasExternalReference))
    _, results, _ = pyshacl.validate(
        model,
        shacl_graph=shapes_graph(vocabularies),
        ont_graph=ontology_graph(vocabularies),
        advanced=True,
        inplace=True,
        allow_warnings=True,
        inference="none",
    )
    report = ModelReport()
    for result in results.subjects(RDF.type, SH.ValidationResult):
        focus = results.value(result, SH.focusNode)
        if focus not in subjects or focus in reference_nodes:
            continue
        value = results.value(result, SH.value)
        if isinstance(value, URIRef) and str(value).startswith(QUDT_VOCABULARY):
            continue
        severity = results.value(result, SH.resultSeverity)
        path = results.value(result, SH.resultPath)
        violation = Violation(
            focus=str(focus),
            message=str(results.value(result, SH.resultMessage) or "does not conform"),
            path=str(path) if path is not None else None,
            severity=str(severity).rsplit("#", 1)[-1] if severity else "Violation",
        )
        if severity == SH.Violation:
            report.violations.append(violation)
        else:
            report.warnings.append(violation)
    report.violations.sort(key=lambda v: (v.focus, v.message))
    report.warnings.sort(key=lambda v: (v.focus, v.message))
    return report, set(model) - before


def infer_owlrl(model: rdflib.Graph, vocabularies: Vocabularies = ()) -> set[Triple]:
    """The OWL-RL closure of the model plus the ontology and the vocabularies."""
    combined = rdflib.Graph()
    combined += ontology_graph(vocabularies)
    combined += model
    reasoner = reasonable.PyReasoner()
    reasoner.from_graph(combined)
    return {(s, p, o) for s, p, o in reasoner.reason()}


def validate_model(
    data: bytes, fmt: str, vocabularies: Iterable[Vocabulary] | None = None
) -> ModelReport:
    """Validate an upload without deriving anything.

    ``vocabularies`` are the source plugins' own; ``None`` means those of the
    installed plugins.
    """
    report, _ = validate_with_rules(parse_model(data, fmt), _resolve(vocabularies))
    return report


def validate_and_infer(
    data: bytes,
    fmt: str,
    on_step: Callable[[str], None] | None = None,
    vocabularies: Iterable[Vocabulary] | None = None,
) -> Inference:
    """Everything an activation needs; raises :class:`ModelInvalid` on violations.

    ``on_step`` hears ``validating`` and ``inferring`` as the work moves on.
    ``vocabularies`` are the source plugins' own, loaded with Brick as ontology
    and as rules; ``None`` means those of the installed plugins.
    """
    loaded = _resolve(vocabularies)
    model = parse_model(data, fmt)
    model_triples = set(model)
    if on_step is not None:
        on_step("validating")
    report, rule_added = validate_with_rules(model, loaded)
    if not report.valid:
        raise ModelInvalid(report)
    if on_step is not None:
        on_step("inferring")
    owl = infer_owlrl(model, loaded)
    ontology = set(ontology_graph(loaded))
    inferred = (rule_added | owl) - model_triples - ontology
    log.info("inferred %d triples for a model of %d", len(inferred), len(model_triples))
    return Inference(model_triples, ontology, inferred, report)
