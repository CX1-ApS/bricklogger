"""Computing the plan: the rule set's accepted points, the instances' claims,
the arbitration between them, and the warnings for what nobody collects.

The daemon owns the plan and the plugins own the execution. Each source
instance claims the points it serves from the graph itself; the daemon
intersects the claims with the accepted points, refuses a double claim, and
warns about points nobody claims. See ``docs/architecture.md``, "Which source
owns which point".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pyoxigraph import NamedNode, QuerySolutions

from bricklogger.config.schema import POLL, Rule
from bricklogger.daemon.rules import Accepted, RuleIssue, evaluate_rules
from bricklogger.model.prefixes import WELL_KNOWN, UnknownPrefix, expand
from bricklogger.model.working_graph import WorkingGraph
from bricklogger.sdk.contract import AssignedPoint, Source

REF_HAS_EXTERNAL_REFERENCE = (
    "https://brickschema.org/schema/Brick/ref#hasExternalReference"
)


class PlanError(Exception):
    """The configuration makes no plan: a double claim or a resource collision."""


@dataclass(frozen=True)
class PlanWarning:
    code: str
    subject: str
    message: str


@dataclass
class Plan:
    """What every source instance is to collect, and what is left out and why."""

    assignments: dict[str, list[AssignedPoint]] = field(default_factory=dict)
    accepted: dict[str, Accepted] = field(default_factory=dict)
    owner: dict[str, str] = field(default_factory=dict)
    warnings: list[PlanWarning] = field(default_factory=list)
    rule_issues: list[RuleIssue] = field(default_factory=list)
    matched_per_rule: list[int] = field(default_factory=list)

    @property
    def assigned(self) -> int:
        return sum(len(points) for points in self.assignments.values())


def assigned_point(accepted: Accepted, *, fallback: bool = False) -> AssignedPoint:
    """The assignment of one accepted point, with the rule's method or its fallback."""
    if fallback:
        assert accepted.fallback is not None
        method, interval = accepted.fallback.method, accepted.fallback.interval
    else:
        method, interval = accepted.method, accepted.interval
    parameters: dict[str, Any] = {}
    if method == POLL and interval is not None:
        parameters["interval"] = interval
    return AssignedPoint(uri=accepted.point, method=method, parameters=parameters)


def compute_plan(
    graph: WorkingGraph,
    prefixes: Mapping[str, str],
    rules: list[Rule],
    sources: Mapping[str, Source],
    reference_types: Mapping[str, Iterable[str]],
) -> Plan:
    """Evaluate the rules, gather the claims and arbitrate.

    ``reference_types`` maps each instance to the reference types its plugin
    declares, in prefixed or full form; it decides whether an unclaimed point
    is one no installed source understands.
    """
    evaluation = evaluate_rules(graph, rules, prefixes)
    plan = Plan(
        accepted=evaluation.accepted,
        rule_issues=evaluation.issues,
        matched_per_rule=evaluation.matched_per_rule,
    )
    for issue in evaluation.issues:
        plan.warnings.append(
            PlanWarning("rule_skipped", issue.rule, f"rule skipped: {issue.message}")
        )

    _check_resources(sources)
    claims: dict[str, set[str]] = {}
    for name, source in sources.items():
        try:
            claims[name] = set(source.claim())
        except Exception as exc:
            raise PlanError(f"source {name!r} failed while claiming: {exc}") from exc

    understood = {
        _expand_type(reference_type, prefixes)
        for types in reference_types.values()
        for reference_type in types
    }
    references = _reference_types_by_point(graph)

    for name in sources:
        plan.assignments[name] = []
    for point, accepted in sorted(evaluation.accepted.items()):
        claimants = sorted(name for name, claimed in claims.items() if point in claimed)
        if len(claimants) > 1:
            raise PlanError(
                f"point {point} is claimed by several source instances: "
                + ", ".join(claimants)
            )
        if claimants:
            plan.owner[point] = claimants[0]
            plan.assignments[claimants[0]].append(assigned_point(accepted))
            continue
        types = references.get(point)
        if types is None:
            plan.warnings.append(
                PlanWarning(
                    "no_reference", point, "the point has no external reference"
                )
            )
        elif not (types & understood):
            plan.warnings.append(
                PlanWarning(
                    "unknown_reference",
                    point,
                    "no installed source understands any of the point's references",
                )
            )
        else:
            plan.warnings.append(
                PlanWarning("unclaimed", point, "no source instance claims the point")
            )
    return plan


def _expand_type(reference_type: str, prefixes: Mapping[str, str]) -> str:
    """A declared reference type as a full IRI, through the well-known prefixes
    and the plugins' and the model's own; an unknown prefix is left as written."""
    try:
        return expand(reference_type, {**WELL_KNOWN, **prefixes})
    except UnknownPrefix:
        return reference_type


def _check_resources(sources: Mapping[str, Source]) -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for name, source in sources.items():
        for resource in source.resources():
            owners[resource].append(name)
    for resource, names in owners.items():
        if len(names) > 1:
            raise PlanError(
                f"resource {resource!r} is claimed by several instances: "
                + ", ".join(names)
            )


def _reference_types_by_point(graph: WorkingGraph) -> dict[str, set[str]]:
    """Each point's reference types; an untyped reference gives an empty set."""
    result = graph.query(
        f"SELECT ?p ?t WHERE {{ ?p <{REF_HAS_EXTERNAL_REFERENCE}> ?r . "
        "OPTIONAL { ?r a ?t } }"
    )
    types: dict[str, set[str]] = {}
    if not isinstance(result, QuerySolutions):
        return types
    for solution in result:
        point = solution["p"]
        if not isinstance(point, NamedNode):
            continue
        entry = types.setdefault(point.value, set())
        reference_type = solution["t"]
        if isinstance(reference_type, NamedNode):
            entry.add(reference_type.value)
    return types
