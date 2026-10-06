"""The model explorer's projection: the active model as elements, the relations
between them, what the model lacks, and the hierarchy both interfaces draw.

The document is described in ``docs/features/api.md``, "Entities"; the findings
in ``docs/features/daemon.md``, "Model findings"; and the hierarchy rules in
``docs/features/web.md``, "Model explorer". The tree is built here so that the
CLI and the web interface cannot draw two different buildings out of one model.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pyoxigraph import Literal, NamedNode, QuerySolutions

from bricklogger.daemon.rules import BRICK, LOCATED_IN, PART_OF, REC
from bricklogger.model.prefixes import compact
from bricklogger.model.working_graph import (
    MODEL_GRAPH,
    ONTOLOGY_GRAPH,
    VALUES_GRAPH,
    WorkingGraph,
)

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASS_OF = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
OWL_DEPRECATED = "http://www.w3.org/2002/07/owl#deprecated"
REF = "https://brickschema.org/schema/Brick/ref#"
REF_HAS_EXTERNAL_REFERENCE = f"{REF}hasExternalReference"
REF_EXTERNAL_REFERENCE = f"{REF}ExternalReference"

KINDS = ("location", "equipment", "point", "system", "other")
FINDINGS = (
    "no_owner",
    "no_reference",
    "no_location",
    "no_relations",
    "deprecated_class",
)
OUTCOMES = ("active", "unsupported", "rejected", "pending")

#: Which classes decide a kind, in the order they are tried. A model that gives
#: a node two of them is broken; the order only settles what to show.
KIND_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("point", (f"{BRICK}Point",)),
    ("equipment", (f"{BRICK}Equipment",)),
    ("location", (f"{BRICK}Location", f"{REC}Space")),
    ("system", (f"{BRICK}System", f"{REC}Collection")),
)

#: The families that make an element a grouping: it gathers what is placed
#: elsewhere instead of holding what is inside it, so the hierarchy leaves it
#: out. The document carries the family it belongs to, both because the tree
#: is drawn from it and so that no client needs Brick's vocabulary; the order
#: only settles what to show for a class that sits under two of them.
GROUPING_FAMILIES: tuple[str, ...] = (
    f"{BRICK}System",
    f"{REC}Collection",
    f"{BRICK}Zone",
    f"{REC}Zone",
)

#: Predicate -> (role, which end is the contained one). The document carries
#: this so that no client needs to know Brick's vocabulary.
CONTAINMENT: dict[str, tuple[str, str]] = {
    f"{BRICK}isPartOf": ("part", "subject"),
    f"{REC}isPartOf": ("part", "subject"),
    f"{BRICK}hasPart": ("part", "object"),
    f"{REC}hasPart": ("part", "object"),
    f"{REC}includes": ("part", "object"),
    f"{BRICK}hasLocation": ("location", "subject"),
    f"{REC}locatedIn": ("location", "subject"),
    f"{BRICK}isLocationOf": ("location", "object"),
    f"{REC}isLocationOf": ("location", "object"),
    f"{BRICK}isPointOf": ("point", "subject"),
    f"{REC}isPointOf": ("point", "subject"),
    f"{BRICK}hasPoint": ("point", "object"),
    f"{REC}hasPoint": ("point", "object"),
}

#: Which container a kind prefers, most telling first.
CONTAINER_ORDER: dict[str, tuple[str, ...]] = {
    "point": ("point", "location"),
    "equipment": ("location", "part"),
    "location": ("part", "location"),
    "system": ("part", "location"),
    "other": ("part", "location"),
}

#: The kinds that are roots of the building rather than of what was forgotten.
ROOT_KINDS = ("location", "system")

UNPLACED = "Unplaced"


def empty_document(prefixes: Mapping[str, str]) -> dict[str, Any]:
    """The answer when no model is active."""
    return {
        "version": None,
        "prefixes": dict(prefixes),
        "classes": {},
        "counts": {
            "entities": 0,
            "relations": 0,
            "kinds": dict.fromkeys(KINDS, 0),
            "findings": dict.fromkeys(FINDINGS, 0),
        },
        "entities": [],
        "relations": [],
    }


def project_entities(
    graph: WorkingGraph, prefixes: Mapping[str, str]
) -> dict[str, Any]:
    """The active model's elements and relations with their findings, URIs in
    prefixed form. The runtime is attached afterwards by ``attach_runtime``."""
    asserted = _asserted_types(graph)
    entities = set(asserted)
    kinds = _kinds(graph, entities)
    families = _groupings(graph, entities)
    labels = _one_per_subject(
        graph, f"GRAPH <{MODEL_GRAPH}> {{ ?s <{RDFS_LABEL}> ?o }}"
    )
    units = _one_per_subject(
        graph, f"GRAPH <{MODEL_GRAPH}> {{ ?s <{BRICK}hasUnit> ?o }}"
    )
    references = _reference_types(graph)
    values = _last_known_values(graph)
    relations = _relations(graph, entities, prefixes)
    deprecated = _deprecated_classes(graph)

    related: set[str] = set()
    for relation in relations:
        related.add(relation["_subject"])
        related.add(relation["_object"])

    no_owner = _matching(
        graph,
        f"?s a <{BRICK}Point> . "
        f"FILTER NOT EXISTS {{ ?s <{BRICK}isPointOf> ?x }} "
        f"FILTER NOT EXISTS {{ ?s ({LOCATED_IN}) ?l }}",
    )
    no_reference = _matching(
        graph,
        f"?s a <{BRICK}Point> . "
        f"FILTER NOT EXISTS {{ ?s <{REF_HAS_EXTERNAL_REFERENCE}> ?r }}",
    )
    no_location = _matching(
        graph,
        f"?s a <{BRICK}Equipment> . "
        f"FILTER NOT EXISTS {{ ?s ({PART_OF})* ?a . ?a ({LOCATED_IN}) ?l }}",
    )

    documented: list[dict[str, Any]] = []
    for uri in sorted(entities):
        kind = kinds.get(uri, "other")
        types = sorted(asserted[uri])
        findings: list[str] = []
        if kind == "point":
            if uri in no_owner:
                findings.append("no_owner")
            if uri in no_reference:
                findings.append("no_reference")
        if kind == "equipment" and uri in no_location:
            findings.append("no_location")
        if kind != "other" and uri not in related:
            findings.append("no_relations")
        if deprecated & set(types):
            findings.append("deprecated_class")
        documented.append(
            {
                "uri": compact(uri, prefixes),
                "kind": kind,
                "class": _class_of(types, prefixes),
                "types": [compact(t, prefixes) for t in types],
                "grouping": compact(families[uri], prefixes)
                if uri in families
                else None,
                "name": _label(labels.get(uri), uri),
                "unit": compact(units[uri], prefixes) if uri in units else None,
                "references": sorted(
                    compact(t, prefixes) for t in references.get(uri, set())
                )
                if uri in references
                else [],
                "last_known_value": values.get(uri) if kind == "point" else None,
                "findings": sorted(findings),
                "runtime": None,
                "warnings": [],
                "context": False,
            }
        )

    document = {
        "version": None,
        "prefixes": dict(prefixes),
        "classes": _class_ancestors(graph, asserted, prefixes),
        "counts": {},
        "entities": documented,
        "relations": [
            {key: value for key, value in relation.items() if not key.startswith("_")}
            for relation in relations
        ],
    }
    _recount(document)
    return document


def attach_runtime(
    document: dict[str, Any],
    *,
    accepted: Collection[str],
    states: Mapping[str, Mapping[str, Any]],
    warnings: Iterable[Mapping[str, Any]],
    prefixes: Mapping[str, str],
) -> None:
    """Fill in what the daemon has made of each point, and the warnings it holds
    against any element. Both arrive with full URIs and are compacted here."""
    accepted_short = {compact(uri, prefixes) for uri in accepted}
    states_short = {compact(uri, prefixes): value for uri, value in states.items()}
    by_subject: dict[str, list[str]] = {}
    for warning in warnings:
        subject = warning.get("subject")
        if subject:
            by_subject.setdefault(compact(str(subject), prefixes), []).append(
                str(warning["code"])
            )
    for entity in document["entities"]:
        uri = entity["uri"]
        entity["warnings"] = sorted(set(by_subject.get(uri, [])))
        if entity["kind"] != "point":
            continue
        state = states_short.get(uri)
        entity["runtime"] = {
            "accepted": uri in accepted_short,
            "instance": state["instance"] if state else None,
            "method": state["method"] if state else None,
            "fallback_active": bool(state["fallback_active"]) if state else False,
            "outcome": state["outcome"] if state else None,
        }


def entities_of_class(graph: WorkingGraph, class_iri: str) -> set[str]:
    """The model's elements of the class or one of its subclasses, full URIs."""
    return _matching(graph, f"GRAPH <{MODEL_GRAPH}> {{ ?s a ?any }} ?s a <{class_iri}>")


def narrow(
    document: dict[str, Any],
    *,
    kind: str | None = None,
    keep: set[str] | None = None,
    finding: str | None = None,
    warning: str | None = None,
    outcome: str | None = None,
    instance: str | None = None,
    search: str | None = None,
    root: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """The document reduced to what matches, with the ancestors of every match
    kept as context so the result is still a tree.

    Raises ``KeyError`` when ``root`` is not an element of the model.
    """
    entities: list[dict[str, Any]] = document["entities"]
    relations: list[dict[str, Any]] = document["relations"]
    by_uri = {entity["uri"]: entity for entity in entities}
    if root is not None and root not in by_uri:
        raise KeyError(root)

    parents = _parents(entities, relations)
    wanted = set(by_uri)
    if root is not None:
        wanted = _under(root, parents, by_uri, depth, _gathered(entities, relations))
    elif depth is not None:
        wanted = {uri for uri in by_uri if _depth_of(uri, parents, by_uri) <= depth}

    needle = search.lower() if search else None
    matches: set[str] = set()
    for uri in wanted:
        entity = by_uri[uri]
        if kind is not None and entity["kind"] != kind:
            continue
        if keep is not None and uri not in keep:
            continue
        if finding is not None and finding not in entity["findings"]:
            continue
        if warning is not None and warning not in entity["warnings"]:
            continue
        runtime = entity["runtime"] or {}
        if outcome is not None and (runtime.get("outcome") or "pending") != outcome:
            continue
        if outcome is not None and entity["kind"] != "point":
            continue
        if instance is not None and runtime.get("instance") != instance:
            continue
        if needle is not None and needle not in f"{uri} {entity['name']}".lower():
            continue
        matches.add(uri)

    narrowed = not (
        kind is None
        and keep is None
        and finding is None
        and warning is None
        and outcome is None
        and instance is None
        and needle is None
    )
    if not narrowed:
        matches = set(wanted)

    context: set[str] = set()
    for uri in matches:
        walker = parents.get(uri)
        while walker is not None and walker not in matches and walker not in context:
            if root is not None and walker not in wanted:
                break
            context.add(walker)
            walker = parents.get(walker)

    kept = matches | context
    result = dict(document)
    result["entities"] = [
        {**by_uri[uri], "context": uri in context} for uri in sorted(kept)
    ]
    result["relations"] = [
        relation
        for relation in relations
        if relation["subject"] in kept and relation["object"] in kept
    ]
    _recount(result, matches=matches)
    return result


@dataclass
class TreeNode:
    """One line of the hierarchy. ``entity`` is None for a line the model does
    not hold: a class heading in the grouping band, which carries its class in
    ``label``, and the Unplaced root, which carries neither. ``members`` is
    what the line counts — for a grouping the elements it gathers, for a
    heading the groupings beneath it."""

    uri: str | None
    entity: Mapping[str, Any] | None
    children: list[TreeNode] = field(default_factory=list)
    descendants: int = 0
    label: str | None = None
    members: int = 0


def build_tree(
    entities: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    grouping_root: str | None = None,
) -> list[TreeNode]:
    """The hierarchy by the rules of ``docs/features/web.md``, "Model explorer":
    the building out of bodies alone, then the groupings under a heading for
    their family and one for their class, then Unplaced.

    ``grouping_root`` names a grouping that was asked for by itself, whose
    members then hang beneath it. That is the view ``model tree --root`` gives
    of a grouping, not the building, where a grouping holds nothing.
    """
    by_uri = {entity["uri"]: entity for entity in entities}
    parents = _parents(entities, relations)
    gathered = _gathered(entities, relations)
    nodes = {uri: TreeNode(uri=uri, entity=entity) for uri, entity in by_uri.items()}
    for uri, members in gathered.items():
        nodes[uri].members = len(members)
    for member in gathered.get(grouping_root or "", ()):
        parents[member] = str(grouping_root)

    roots: list[TreeNode] = []
    groupings: list[TreeNode] = []
    unplaced: list[TreeNode] = []
    for uri in sorted(by_uri):
        parent = parents.get(uri)
        if parent is not None and parent in nodes:
            nodes[parent].children.append(nodes[uri])
        elif by_uri[uri].get("grouping"):
            groupings.append(nodes[uri])
        elif by_uri[uri]["kind"] in ROOT_KINDS:
            roots.append(nodes[uri])
        else:
            unplaced.append(nodes[uri])

    for node in nodes.values():
        _count_points(node)
    roots.sort(key=lambda node: (ROOT_KINDS.index(_kind_of(node)), node.uri or ""))
    roots.extend(_grouping_band(groupings))
    if unplaced:
        placeholder = TreeNode(uri=None, entity=None, children=unplaced)
        _count_points(placeholder)
        roots.append(placeholder)
    return roots


def _grouping_band(nodes: Sequence[TreeNode]) -> list[TreeNode]:
    """The groupings under a heading for the family that makes each one a
    grouping and, beneath that, one for its own class — two deep in every
    model, so that a whole family folds away with one click. An element whose
    class is the family itself hangs directly under it."""
    families: dict[str, dict[str, list[TreeNode]]] = {}
    for node in nodes:
        entity = node.entity or {}
        family = str(entity.get("grouping") or "")
        name = str(entity.get("class") or family)
        families.setdefault(family, {}).setdefault(name, []).append(node)

    band: list[TreeNode] = []
    for family in sorted(families):
        head = TreeNode(uri=None, entity=None, label=family)
        head.members = sum(len(group) for group in families[family].values())
        for name in sorted(families[family]):
            group = families[family][name]
            if name == family:
                head.children.extend(group)
                continue
            head.children.append(
                TreeNode(
                    uri=None,
                    entity=None,
                    label=name,
                    children=list(group),
                    members=len(group),
                )
            )
        _count_points(head)
        band.append(head)
    return band


def sort_tree(nodes: list[TreeNode], key: str) -> None:
    """Order siblings in place by ``name``, ``class`` or ``count``."""

    def order(node: TreeNode) -> tuple[Any, ...]:
        entity = node.entity
        if entity is None:  # a heading by its class, Unplaced always last
            return (1, node.label or "\uffff", "")
        if key == "count":
            return (0, -node.descendants, entity["name"], node.uri or "")
        if key == "class":
            return (0, entity["class"] or "", entity["name"], node.uri or "")
        return (0, entity["name"], node.uri or "")

    nodes.sort(key=order)
    for node in nodes:
        sort_tree(node.children, key)


# --- the graph -------------------------------------------------------------


def _asserted_types(graph: WorkingGraph) -> dict[str, set[str]]:
    """Every typed IRI of the model graph with its asserted types, reference
    nodes left out: they describe an address, not a thing in the building."""
    result = graph.query(
        f"SELECT ?s ?t WHERE {{ GRAPH <{MODEL_GRAPH}> {{ ?s a ?t }} "
        "FILTER(isIRI(?s)) "
        f"FILTER NOT EXISTS {{ ?x <{REF_HAS_EXTERNAL_REFERENCE}> ?s }} "
        f"FILTER NOT EXISTS {{ ?s a <{REF_EXTERNAL_REFERENCE}> }} }}"
    )
    types: dict[str, set[str]] = {}
    for subject, value in _pairs(result, "s", "t"):
        types.setdefault(subject, set()).add(value)
    return types


def _kinds(graph: WorkingGraph, entities: set[str]) -> dict[str, str]:
    """Each element's kind, decided over the subclass hierarchy."""
    kinds: dict[str, str] = {}
    for kind, classes in KIND_CLASSES:
        pattern = " UNION ".join(f"{{ ?s a <{iri}> }}" for iri in classes)
        for uri in _matching(graph, f"GRAPH <{MODEL_GRAPH}> {{ ?s a ?any }} {pattern}"):
            if uri in entities:
                kinds.setdefault(uri, kind)
    return {uri: kinds.get(uri, "other") for uri in entities}


def _groupings(graph: WorkingGraph, entities: set[str]) -> dict[str, str]:
    """Each grouping's family, decided over the subclass hierarchy, so that a
    plugin's own zone class is a grouping like Brick's own."""
    families: dict[str, str] = {}
    for iri in GROUPING_FAMILIES:
        for uri in _matching(
            graph, f"GRAPH <{MODEL_GRAPH}> {{ ?s a ?any }} ?s a <{iri}>"
        ):
            if uri in entities:
                families.setdefault(uri, iri)
    return families


def _relations(
    graph: WorkingGraph, entities: set[str], prefixes: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The triples the model asserts between two elements, as written."""
    result = graph.query(
        f"SELECT ?s ?p ?o WHERE {{ GRAPH <{MODEL_GRAPH}> {{ ?s ?p ?o }} "
        f"FILTER(isIRI(?s) && isIRI(?o) && ?p != <{RDF_TYPE}>) }}"
    )
    relations: list[dict[str, Any]] = []
    if not isinstance(result, QuerySolutions):
        return relations
    for solution in result:
        subject, predicate, obj = solution["s"], solution["p"], solution["o"]
        if not (
            isinstance(subject, NamedNode)
            and isinstance(predicate, NamedNode)
            and isinstance(obj, NamedNode)
        ):
            continue
        if subject.value not in entities or obj.value not in entities:
            continue
        role, child = CONTAINMENT.get(predicate.value, (None, None))
        relations.append(
            {
                "subject": compact(subject.value, prefixes),
                "predicate": compact(predicate.value, prefixes),
                "object": compact(obj.value, prefixes),
                "role": role,
                "child": child,
                "_subject": subject.value,
                "_object": obj.value,
            }
        )
    relations.sort(key=lambda r: (r["subject"], r["predicate"], r["object"]))
    return relations


def _deprecated_classes(graph: WorkingGraph) -> set[str]:
    """The classes the bundled Brick marks deprecated."""
    return _matching(
        graph,
        f"GRAPH <{ONTOLOGY_GRAPH}> {{ ?s <{OWL_DEPRECATED}> true }}",
    )


def _class_ancestors(
    graph: WorkingGraph,
    asserted: Mapping[str, set[str]],
    prefixes: Mapping[str, str],
) -> dict[str, list[str]]:
    """Each asserted class mapped to its ancestors, so a client can filter by
    class with subclasses without asking again."""
    present = {iri for types in asserted.values() for iri in types}
    if not present:
        return {}
    values = " ".join(f"<{iri}>" for iri in sorted(present))
    result = graph.query(
        f"SELECT ?s ?o WHERE {{ VALUES ?s {{ {values} }} "
        f"GRAPH <{ONTOLOGY_GRAPH}> {{ ?s <{RDFS_SUBCLASS_OF}>+ ?o }} "
        "FILTER(isIRI(?o)) }"
    )
    ancestors: dict[str, set[str]] = {}
    for subject, value in _pairs(result, "s", "o"):
        if value != subject and (value.startswith(BRICK) or value.startswith(REC)):
            ancestors.setdefault(subject, set()).add(value)
    return {
        compact(iri, prefixes): sorted(compact(a, prefixes) for a in found)
        for iri, found in sorted(ancestors.items())
    }


def _reference_types(graph: WorkingGraph) -> dict[str, set[str]]:
    """Each point's reference types as the model graph holds them; an untyped
    reference gives an empty set.

    The model graph rather than the union, because the inference gives every
    reference node the superclasses of its type as well, down to ``owl:Thing``,
    and the document says what the model asserts. Bricklogger's own SHACL rule
    writes into the model graph, so a reference typed by the rule is included.
    """
    result = graph.query(
        f"SELECT ?s ?t WHERE {{ GRAPH <{MODEL_GRAPH}> {{ "
        f"?s <{REF_HAS_EXTERNAL_REFERENCE}> ?r . OPTIONAL {{ ?r a ?t }} }} }}"
    )
    types: dict[str, set[str]] = {}
    if not isinstance(result, QuerySolutions):
        return types
    for solution in result:
        subject = solution["s"]
        if not isinstance(subject, NamedNode):
            continue
        entry = types.setdefault(subject.value, set())
        found = solution["t"]
        if isinstance(found, NamedNode):
            entry.add(found.value)
    return types


def _matching(graph: WorkingGraph, pattern: str) -> set[str]:
    """The IRIs bound to ``?s`` by a pattern."""
    result = graph.query(f"SELECT DISTINCT ?s WHERE {{ {pattern} }}")
    if not isinstance(result, QuerySolutions):
        return set()
    return {
        solution["s"].value
        for solution in result
        if isinstance(solution["s"], NamedNode)
    }


def _last_known_values(graph: WorkingGraph) -> dict[str, dict[str, Any]]:
    """Each point's value and the time it was observed, from the value overlay.

    The overlay is the daemon's own, kept in step with the runtime state, so
    this is the value the SPARQL endpoint and a values export also give — an
    enumeration or a boolean already as its text where the source knows one.
    """
    result = graph.query(
        "SELECT ?s ?v ?t WHERE { "
        f"GRAPH <{VALUES_GRAPH}> {{ "
        f"?s <{BRICK}lastKnownValue> ?n . "
        f"?n <{BRICK}value> ?v ; <{BRICK}timestamp> ?t "
        "} }"
    )
    if not isinstance(result, QuerySolutions):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for solution in result:
        subject, value, stamp = solution["s"], solution["v"], solution["t"]
        if not isinstance(subject, NamedNode):
            continue
        if not isinstance(value, Literal) or not isinstance(stamp, Literal):
            continue
        found[subject.value] = {"value": _typed(value), "time": stamp.value}
    return found


def _typed(literal: Literal) -> Any:
    """A literal as the JSON type it stands for, its lexical form otherwise."""
    datatype = literal.datatype.value if literal.datatype else ""
    try:
        if datatype.endswith(("#integer", "#int", "#long")):
            return int(literal.value)
        if datatype.endswith(("#double", "#decimal", "#float")):
            return float(literal.value)
    except ValueError:
        return literal.value
    if datatype.endswith("#boolean"):
        return literal.value == "true"
    return literal.value


def _one_per_subject(graph: WorkingGraph, pattern: str) -> dict[str, str]:
    """The first value per subject, by sort order, so the answer is stable."""
    result = graph.query(f"SELECT ?s ?o WHERE {{ {pattern} }}")
    found: dict[str, list[str]] = {}
    for subject, value in _pairs(result, "s", "o"):
        found.setdefault(subject, []).append(value)
    return {subject: sorted(values)[0] for subject, values in found.items()}


def _pairs(result: Any, left: str, right: str) -> Iterable[tuple[str, str]]:
    if not isinstance(result, QuerySolutions):
        return
    for solution in result:
        subject, value = solution[left], solution[right]
        if isinstance(subject, NamedNode) and isinstance(value, NamedNode | Literal):
            yield subject.value, value.value


# --- shaping ---------------------------------------------------------------


def _class_of(types: Sequence[str], prefixes: Mapping[str, str]) -> str | None:
    """The first asserted Brick or RealEstateCore class, else the first type."""
    known = [t for t in types if t.startswith(BRICK) or t.startswith(REC)]
    chosen = known[0] if known else (types[0] if types else None)
    return compact(chosen, prefixes) if chosen is not None else None


def _label(label: str | None, uri: str) -> str:
    return label or uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _parents(
    entities: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    """Each element's one container, by the rules of the explorer."""
    by_uri = {entity["uri"]: entity for entity in entities}
    candidates: dict[str, dict[str, list[str]]] = {}
    for relation in relations:
        role, child = relation["role"], relation["child"]
        if role is None or child is None:
            continue
        if child == "subject":
            lower, upper = relation["subject"], relation["object"]
        else:
            lower, upper = relation["object"], relation["subject"]
        if lower not in by_uri or upper not in by_uri or lower == upper:
            continue
        if by_uri[lower].get("grouping") or by_uri[upper].get("grouping"):
            continue  # a grouping neither holds nor is held: it only gathers
        candidates.setdefault(lower, {}).setdefault(role, []).append(upper)

    parents: dict[str, str] = {}
    for uri, found in candidates.items():
        for role in CONTAINER_ORDER.get(by_uri[uri]["kind"], ("part", "location")):
            if found.get(role):
                parents[uri] = sorted(found[role])[0]
                break
    return _without_cycles(parents)


def _gathered(
    entities: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]]
) -> dict[str, set[str]]:
    """What each grouping gathers. The hierarchy leaves it out, the grouping's
    line says how many there are, and asking for the grouping shows them."""
    by_uri = {entity["uri"]: entity for entity in entities}
    members: dict[str, set[str]] = {}
    for relation in relations:
        role, child = relation["role"], relation["child"]
        if role is None or child is None:
            continue
        if child == "subject":
            lower, upper = relation["subject"], relation["object"]
        else:
            lower, upper = relation["object"], relation["subject"]
        if lower not in by_uri or upper not in by_uri or lower == upper:
            continue
        if by_uri[upper].get("grouping"):
            members.setdefault(upper, set()).add(lower)
    return members


def _without_cycles(parents: dict[str, str]) -> dict[str, str]:
    """Break every cycle at its member first by URI, which becomes unplaced."""
    result = dict(parents)
    for start in sorted(result):
        seen = [start]
        walker = result.get(start)
        while walker is not None:
            if walker in seen:
                del result[sorted(seen[seen.index(walker) :])[0]]
                break
            seen.append(walker)
            walker = result.get(walker)
    return result


def _depth_of(uri: str, parents: Mapping[str, str], by_uri: Mapping[str, Any]) -> int:
    depth = 0
    walker = parents.get(uri)
    while walker is not None and walker in by_uri:
        depth += 1
        walker = parents.get(walker)
    return depth


def _under(
    root: str,
    parents: Mapping[str, str],
    by_uri: Mapping[str, Any],
    depth: int | None,
    gathered: Mapping[str, Collection[str]] | None = None,
) -> set[str]:
    """The root and everything beneath it, no deeper than ``depth`` levels. A
    grouping holds nothing in the tree, so asked for by itself it opens to what
    it gathers, each member with its own subtree."""
    children: dict[str, list[str]] = {}
    for uri, parent in parents.items():
        children.setdefault(parent, []).append(uri)
    for member in (gathered or {}).get(root, ()):
        children.setdefault(root, []).append(member)
    wanted = {root}
    frontier = [root]
    level = 0
    while frontier and (depth is None or level < depth):
        level += 1
        frontier = [
            child
            for uri in frontier
            for child in children.get(uri, [])
            if child in by_uri
        ]
        wanted.update(frontier)
    return wanted


def _count_points(node: TreeNode) -> int:
    """How many points sit beneath a node, itself included."""
    total = sum(_count_points(child) for child in node.children)
    if node.entity is not None and node.entity["kind"] == "point":
        total += 1
    node.descendants = total
    return total


def _kind_of(node: TreeNode) -> str:
    return str(node.entity["kind"]) if node.entity is not None else "other"


def _recount(document: dict[str, Any], matches: set[str] | None = None) -> None:
    counted = [
        entity
        for entity in document["entities"]
        if matches is None or entity["uri"] in matches
    ]
    kinds = dict.fromkeys(KINDS, 0)
    findings = dict.fromkeys(FINDINGS, 0)
    for entity in counted:
        kinds[entity["kind"]] = kinds.get(entity["kind"], 0) + 1
        for code in entity["findings"]:
            findings[code] = findings.get(code, 0) + 1
    document["counts"] = {
        "entities": len(counted),
        "relations": len(document["relations"]),
        "kinds": kinds,
        "findings": findings,
    }
