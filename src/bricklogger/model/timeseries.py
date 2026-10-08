"""Brick's time-series references: where a point's time series is stored.

Bricklogger states them beside the model, never in it: in one graph of the
working graph per destination instance that stores the model, and in the
copy of each model version the destination stores. A reference carries the
destination's own key as ``ref:hasTimeseriesId`` and no ``ref:storedAt``.
See ``docs/features/destinations.md``, "The model beside the data".
"""

from __future__ import annotations

from collections.abc import Mapping

import rdflib
from rdflib.namespace import RDF

from bricklogger.model.working_graph import Triple

REF = rdflib.Namespace("https://brickschema.org/schema/Brick/ref#")

TIMESERIES_GRAPH_PREFIX = "urn:bricklogger:timeseries:"


def timeseries_graph(instance: str) -> str:
    """The named graph holding one destination instance's references."""
    return TIMESERIES_GRAPH_PREFIX + instance


def referenced(model: rdflib.Graph, ids: Mapping[str, str]) -> dict[str, str]:
    """The keys of the points that are in the model: URIs it has as subjects."""
    subjects = {str(s) for s in model.subjects() if isinstance(s, rdflib.URIRef)}
    return {uri: key for uri, key in ids.items() if uri in subjects}


def reference_triples(ids: Mapping[str, str]) -> list[Triple]:
    """A ``ref:TimeseriesReference`` per point, as a blank node under it."""
    triples: list[Triple] = []
    for uri, key in sorted(ids.items()):
        node = rdflib.BNode()
        triples.append((rdflib.URIRef(uri), REF.hasExternalReference, node))
        triples.append((node, RDF.type, REF.TimeseriesReference))
        triples.append((node, REF.hasTimeseriesId, rdflib.Literal(key)))
    return triples


def with_references(turtle: str, ids: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """A model in Turtle with the references of its points added, and the keys
    that were used — those of the points the model has."""
    model = rdflib.Graph()
    model.parse(data=turtle, format="turtle")
    used = referenced(model, ids)
    for triple in reference_triples(used):
        model.add(triple)
    model.bind("ref", REF, override=False)
    return model.serialize(format="turtle"), used
