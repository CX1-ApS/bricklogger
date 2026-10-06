"""Evaluating the rule set over the working graph.

Rules are evaluated top down, the first match wins, and points no rule matches
are not logged. ``match`` and ``match_regex`` are decided on facts gathered
with a few bulk queries over the working graph; a ``sparql`` rule runs its own
query. See ``docs/features/daemon.md``, "Point selection: selectors".
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pyoxigraph import NamedNode, QuerySolutions

from bricklogger.config.schema import Fallback, Rule, Selector, rule_label
from bricklogger.model.prefixes import UnknownPrefix, compact, expand, with_prefixes
from bricklogger.model.working_graph import MODEL_GRAPH, WorkingGraph

BRICK = "https://brickschema.org/schema/Brick#"
REC = "https://w3id.org/rec#"
PART_OF = f"<{BRICK}isPartOf>|<{REC}isPartOf>"
HAS_PART = f"<{BRICK}hasPart>|<{REC}hasPart>"
LOCATED_IN = f"<{BRICK}hasLocation>|<{REC}locatedIn>"


def location_anchors(location: str) -> str:
    """The SPARQL group pattern for the locations a point ``?p`` has of its
    own, bound to ``location``: the Brick Location or RealEstateCore space it
    is a point of, the location of its equipment or of anything that
    equipment is part of, and a location the point is located in itself.

    These are the point's nearest locations, which its metadata carries; the
    closure upward over the part hierarchy is ``collect_points``' business.
    """
    return (
        "{ "
        f"?p <{BRICK}isPointOf> {location} . "
        f"{{ {location} a <{BRICK}Location> }} UNION "
        f"{{ {location} a <{REC}Space> }} "
        "} UNION { "
        f"?p <{BRICK}isPointOf> ?e . ?e a <{BRICK}Equipment> . "
        f"?e ({PART_OF})* ?a . ?a ({LOCATED_IN}) {location} "
        "} UNION { "
        f"?p ({LOCATED_IN}) {location} "
        "} "
    )


class RuleError(Exception):
    """A rule could not be evaluated as written."""


@dataclass(frozen=True)
class PointFacts:
    """What the graph says about one point, as far as the selectors look."""

    uri: str
    types: frozenset[str]
    asserted_types: frozenset[str]
    equipment: frozenset[str]
    locations: frozenset[str]


@dataclass(frozen=True)
class Accepted:
    """A point an accept rule selected, with what the rule asks for."""

    point: str
    rule_index: int
    rule: str
    method: str
    interval: timedelta | None
    fallback: Fallback | None


@dataclass(frozen=True)
class RuleIssue:
    """A rule that was skipped because it could not be evaluated."""

    rule_index: int
    rule: str
    message: str


@dataclass
class RuleEvaluation:
    """The outcome of evaluating the whole rule set."""

    accepted: dict[str, Accepted] = field(default_factory=dict)
    denied: dict[str, int] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)
    matched_per_rule: list[int] = field(default_factory=list)
    issues: list[RuleIssue] = field(default_factory=list)


def collect_points(graph: WorkingGraph) -> dict[str, PointFacts]:
    """Every point in the model with its types, equipment and locations.

    Types come from the union of the graphs, so subclasses are included;
    asserted types from the model graph alone. Equipment is what the point is a
    point of, and everything that is part of, transitively. A location is a
    Brick Location or a RealEstateCore space. A point's location is the
    location it is a point of — Brick models a zone sensor as a point of the
    zone or room — or the location of its equipment or of anything that
    equipment is part of, and from there everything the location is part of or
    located in; Brick's and RealEstateCore's part and location relations count
    alike. A zone is expanded into the rooms it has as parts, so a point of a
    zone that spans rooms is in each of them. See ``docs/features/daemon.md``,
    "Point selection".
    """
    types: dict[str, set[str]] = defaultdict(set)
    asserted: dict[str, set[str]] = defaultdict(set)
    equipment: dict[str, set[str]] = defaultdict(set)
    locations: dict[str, set[str]] = defaultdict(set)
    for point, value in _pairs(graph, "?p a ?x"):
        types[point].add(value)
    for point, value in _pairs(graph, f"GRAPH <{MODEL_GRAPH}> {{ ?p a ?x }}"):
        asserted[point].add(value)
    for point, value in _pairs(
        graph,
        f"?p <{BRICK}isPointOf> ?d . ?d a <{BRICK}Equipment> . "
        f"?d <{BRICK}isPartOf>* ?x",
    ):
        equipment[point].add(value)
    upward = f"({PART_OF}|{LOCATED_IN})*"
    for point, value in _pairs(
        graph,
        location_anchors("?d") + "{ "
        f"?d {upward} ?x "
        "} UNION { "
        f"{{ ?d a <{REC}Zone> }} UNION {{ ?d a <{BRICK}Zone> }} "
        f"?d ({HAS_PART}) ?r . "
        f"{{ ?r a <{REC}Space> }} UNION {{ ?r a <{BRICK}Location> }} "
        f"?r {upward} ?x "
        "}",
    ):
        locations[point].add(value)
    return {
        point: PointFacts(
            uri=point,
            types=frozenset(types[point]),
            asserted_types=frozenset(asserted[point]),
            equipment=frozenset(equipment[point]),
            locations=frozenset(locations[point]),
        )
        for point in types
    }


def _pairs(graph: WorkingGraph, pattern: str) -> Iterable[tuple[str, str]]:
    query = f"SELECT ?p ?x WHERE {{ ?p a <{BRICK}Point> . {pattern} }}"
    result = graph.query(query)
    if not isinstance(result, QuerySolutions):
        return
    for solution in result:
        point, value = solution["p"], solution["x"]
        if isinstance(point, NamedNode) and isinstance(value, NamedNode):
            yield point.value, value.value


def evaluate_rules(
    graph: WorkingGraph, rules: list[Rule], prefixes: Mapping[str, str]
) -> RuleEvaluation:
    """Evaluate the rule set: top down, first match wins, implicit deny."""
    facts = collect_points(graph)
    remaining = set(facts)
    evaluation = RuleEvaluation()
    for index, rule in enumerate(rules):
        label = rule_label(index, rule.name)
        try:
            matched = match_rule(rule, facts, graph, prefixes)
        except RuleError as exc:
            evaluation.issues.append(RuleIssue(index, label, str(exc)))
            evaluation.matched_per_rule.append(0)
            continue
        hits = matched & remaining
        remaining -= hits
        evaluation.matched_per_rule.append(len(hits))
        for point in hits:
            if rule.action == "accept":
                assert rule.method is not None  # guaranteed by the schema
                evaluation.accepted[point] = Accepted(
                    point=point,
                    rule_index=index,
                    rule=label,
                    method=rule.method,
                    interval=rule.interval,
                    fallback=rule.fallback,
                )
            else:
                evaluation.denied[point] = index
    evaluation.unmatched = sorted(remaining)
    return evaluation


def match_rule(
    rule: Rule,
    facts: Mapping[str, PointFacts],
    graph: WorkingGraph,
    prefixes: Mapping[str, str],
) -> set[str]:
    """The points one rule selects, before earlier rules take theirs."""
    if rule.match is not None:
        return _match_exact(rule.match, facts, prefixes)
    if rule.match_regex is not None:
        return _match_regex(rule.match_regex, facts, prefixes)
    assert rule.sparql is not None  # guaranteed by the schema
    return _match_sparql(rule.sparql, facts, graph, prefixes)


def _values(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _match_exact(
    selector: Selector, facts: Mapping[str, PointFacts], prefixes: Mapping[str, str]
) -> set[str]:
    try:
        classes = {expand(v, prefixes) for v in _values(selector.class_)}
        equipment = {expand(v, prefixes) for v in _values(selector.equipment)}
        locations = {expand(v, prefixes) for v in _values(selector.location)}
        points = {expand(v, prefixes) for v in _values(selector.point)}
    except UnknownPrefix as exc:
        raise RuleError(str(exc)) from exc
    selected: set[str] = set()
    for uri, fact in facts.items():
        if classes and not (fact.types & classes):
            continue
        if equipment and not (fact.equipment & equipment):
            continue
        if locations and not (fact.locations & locations):
            continue
        if points and uri not in points:
            continue
        selected.add(uri)
    return selected


def _match_regex(
    selector: Selector, facts: Mapping[str, PointFacts], prefixes: Mapping[str, str]
) -> set[str]:
    patterns: dict[str, list[re.Pattern[str]]] = {}
    for key in ("class_", "equipment", "location", "point"):
        expressions = _values(getattr(selector, key))
        try:
            patterns[key] = [re.compile(expression) for expression in expressions]
        except re.error as exc:
            raise RuleError(
                f"invalid regular expression in {key.rstrip('_')}: {exc}"
            ) from exc

    def any_matches(compiled: list[re.Pattern[str]], uris: Iterable[str]) -> bool:
        return any(
            pattern.fullmatch(compact(uri, prefixes))
            for uri in uris
            for pattern in compiled
        )

    selected: set[str] = set()
    for uri, fact in facts.items():
        if patterns["class_"] and not any_matches(
            patterns["class_"], fact.asserted_types
        ):
            continue
        if patterns["equipment"] and not any_matches(
            patterns["equipment"], fact.equipment
        ):
            continue
        if patterns["location"] and not any_matches(
            patterns["location"], fact.locations
        ):
            continue
        if patterns["point"] and not any_matches(patterns["point"], [uri]):
            continue
        selected.add(uri)
    return selected


def _match_sparql(
    sparql: str,
    facts: Mapping[str, PointFacts],
    graph: WorkingGraph,
    prefixes: Mapping[str, str],
) -> set[str]:
    try:
        result: Any = graph.query(with_prefixes(sparql, prefixes))
    except (SyntaxError, ValueError) as exc:
        raise RuleError(f"the SPARQL query failed: {exc}") from exc
    if not isinstance(result, QuerySolutions) or not result.variables:
        raise RuleError(
            "the SPARQL query must be a SELECT whose first variable is the point"
        )
    variable = result.variables[0]
    selected: set[str] = set()
    for solution in result:
        term = solution[variable]
        if isinstance(term, NamedNode) and term.value in facts:
            selected.add(term.value)
    return selected
