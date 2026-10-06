"""The diff between two model versions: points added, removed or changed,
computed on the models as uploaded."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import rdflib
from rdflib import RDF, RDFS, BNode, URIRef
from rdflib.namespace import Namespace

from bricklogger.model.ontology import brick_ontology

BRICK = Namespace("https://brickschema.org/schema/Brick#")

Description = frozenset[tuple[Any, Any]]


@dataclass(frozen=True)
class ModelDiff:
    """Point URIs added, removed or changed between two versions."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def as_dict(self) -> dict[str, list[str]]:
        return {"added": self.added, "removed": self.removed, "changed": self.changed}


def diff_models(old: rdflib.Graph, new: rdflib.Graph) -> ModelDiff:
    """Compare the points of two uploaded models.

    A point is a subject typed with ``brick:Point`` or one of its subclasses,
    according to the Brick ontology. A point counts as changed when anything
    said about it differs, including the content of its blank-node references.
    """
    old_points = {point: describe(old, point) for point in points_of(old)}
    new_points = {point: describe(new, point) for point in points_of(new)}
    added = sorted(str(p) for p in new_points.keys() - old_points.keys())
    removed = sorted(str(p) for p in old_points.keys() - new_points.keys())
    changed = sorted(
        str(p)
        for p in old_points.keys() & new_points.keys()
        if old_points[p] != new_points[p]
    )
    return ModelDiff(added, removed, changed)


def points_of(model: rdflib.Graph) -> set[URIRef]:
    """The URIs typed as a Brick point class in the uploaded model."""
    ontology = brick_ontology()
    point_classes = set(ontology.transitive_subjects(RDFS.subClassOf, BRICK.Point))
    point_classes.add(BRICK.Point)
    return {
        subject
        for subject, _, klass in model.triples((None, RDF.type, None))
        if isinstance(subject, URIRef) and klass in point_classes
    }


def describe(model: rdflib.Graph, node: Any, depth: int = 0) -> Description:
    """Everything the model says about a node, with blank nodes expanded in place."""
    if depth > 8:
        return frozenset()
    entries: set[tuple[Any, Any]] = set()
    for predicate, obj in model.predicate_objects(node):
        if isinstance(obj, BNode):
            entries.add((predicate, describe(model, obj, depth + 1)))
        else:
            entries.add((predicate, obj))
    return frozenset(entries)
