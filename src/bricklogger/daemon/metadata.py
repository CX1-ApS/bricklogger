"""The graph's part of a point's metadata: name, class, equipment, location and
the modelled unit, read from the working graph for the points in the plan.

Equipment is what the point is a point of. Location is the point's nearest
location: the Brick Location or RealEstateCore space it is a point of, or the
location of its equipment, found with the same anchors as the rule set's
location selector, without the selector's closure over floors and buildings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pyoxigraph import Literal, NamedNode, QuerySolutions

from bricklogger.daemon.rules import BRICK, location_anchors
from bricklogger.model.prefixes import compact
from bricklogger.model.working_graph import MODEL_GRAPH, WorkingGraph
from bricklogger.sdk.contract import PointMetadata

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
CHUNK = 500


def graph_metadata(
    graph: WorkingGraph, prefixes: Mapping[str, str], points: Iterable[str]
) -> dict[str, PointMetadata]:
    """What the graph says about each point, with URIs in prefixed form."""
    uris = sorted(set(points))
    result: dict[str, PointMetadata] = {}
    for start in range(0, len(uris), CHUNK):
        chunk = uris[start : start + CHUNK]
        values = " ".join(f"<{uri}>" for uri in chunk)
        labels = _values(graph, values, f"?p <{RDFS_LABEL}> ?x")
        classes = _values(graph, values, f"GRAPH <{MODEL_GRAPH}> {{ ?p a ?x }}")
        equipment = _values(
            graph, values, f"?p <{BRICK}isPointOf> ?x . ?x a <{BRICK}Equipment>"
        )
        locations = _values(graph, values, location_anchors("?x"))
        units = _values(graph, values, f"?p <{BRICK}hasUnit> ?x")
        for uri in chunk:
            brick_classes = sorted(
                c for c in classes.get(uri, []) if c.startswith(BRICK)
            )
            result[uri] = PointMetadata(
                point=uri,
                name=_first(labels.get(uri))
                or uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1],
                brick_class=compact(brick_classes[0], prefixes)
                if brick_classes
                else None,
                equipment=_compact_first(equipment.get(uri), prefixes),
                location=_compact_first(locations.get(uri), prefixes),
                graph_unit=_compact_first(units.get(uri), prefixes),
            )
    return result


def _values(graph: WorkingGraph, values: str, pattern: str) -> dict[str, list[str]]:
    query = f"SELECT ?p ?x WHERE {{ VALUES ?p {{ {values} }} {pattern} }}"
    result = graph.query(query)
    found: dict[str, list[str]] = {}
    if not isinstance(result, QuerySolutions):
        return found
    for solution in result:
        point, value = solution["p"], solution["x"]
        if not isinstance(point, NamedNode):
            continue
        if isinstance(value, NamedNode | Literal):
            found.setdefault(point.value, []).append(value.value)
    return found


def _first(values: list[str] | None) -> str | None:
    return sorted(values)[0] if values else None


def _compact_first(values: list[str] | None, prefixes: Mapping[str, str]) -> str | None:
    first = _first(values)
    return compact(first, prefixes) if first is not None else None
