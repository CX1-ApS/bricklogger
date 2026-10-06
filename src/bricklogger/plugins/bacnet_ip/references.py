"""Resolving a point's BACnet reference from the graph.

The source accepts the property form of ``ref:BACnetReference`` in either
BACnet vocabulary found in models — ``http://data.ashrae.org/bacnet/2020#``
(the bundled Brick) and ``http://data.ashrae.org/bacnet/`` (Brick's current
schema) — with the device reached through ``objectOf`` or ``contains``. A
reference the source recognises but cannot use gives the point the outcome
``rejected`` with a reason. See ``docs/features/sources.md``, "The reference".
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from bacpypes3.basetypes import ObjectType, PropertyIdentifier
from pyoxigraph import BlankNode, Literal, NamedNode, QuerySolutions

from bricklogger.sdk.contract import GraphReader

REF = "https://brickschema.org/schema/Brick/ref#"
BACNET_NAMESPACES = (
    "http://data.ashrae.org/bacnet/2020#",
    "http://data.ashrae.org/bacnet/",
)
DEFAULT_PROPERTY = "present-value"

_IDENTIFIER = re.compile(r"^\s*([a-z][a-z0-9-]*|\d+)\s*[,:]\s*(\d+)\s*$")
_LINK_PROPERTIES = ("objectOf", "contains")


@dataclass(frozen=True)
class BACnetReference:
    """A resolved reference: enough to find the device and read the property."""

    point: str
    device_instance: int
    device_ip: str | None
    object_type: str
    object_instance: int
    property_name: str

    @property
    def object_identifier(self) -> str:
        return f"{self.object_type},{self.object_instance}"


@dataclass(frozen=True)
class ReferenceProblem:
    """Why a point's reference cannot be used."""

    point: str
    reason: str
    device_instance: int | None = None
    device_ip: str | None = None


@dataclass
class ResolvedReferences:
    references: dict[str, BACnetReference]
    problems: dict[str, ReferenceProblem]


def _local(iri: str) -> str | None:
    """The local name of an IRI in either BACnet namespace, or ``None``."""
    for namespace in BACNET_NAMESPACES:
        if iri.startswith(namespace):
            return iri[len(namespace) :]
    return None


def _term_key(term: Any) -> str:
    if isinstance(term, NamedNode):
        return term.value
    if isinstance(term, BlankNode):
        return f"_:{term.value}"
    return str(term)


def _literal_text(term: Any) -> str | None:
    if isinstance(term, Literal):
        return term.value
    if isinstance(term, NamedNode):
        return term.value
    return None


def resolve_references(graph: GraphReader) -> ResolvedReferences:
    """Read every BACnet reference in the graph and resolve or reject it."""
    nodes = _describe_reference_nodes(graph)
    devices = _describe_devices(graph)
    by_point: dict[str, list[dict[str, list[Any]]]] = defaultdict(list)
    for (point, _), description in nodes.items():
        by_point[point].append(description)

    resolved = ResolvedReferences({}, {})
    for point, candidates in by_point.items():
        bacnet = [c for c in candidates if _first(c, "object-identifier") is not None]
        uri_only = [
            c for c in candidates if c not in bacnet and (REF + "BACnetURI") in c
        ]
        typed_without_identifier = [
            c
            for c in candidates
            if c not in bacnet
            and c not in uri_only
            and any(
                isinstance(t, NamedNode) and t.value == REF + "BACnetReference"
                for t in c.get("http://www.w3.org/1999/02/22-rdf-syntax-ns#type", [])
            )
        ]
        if not bacnet:
            if uri_only:
                resolved.problems[point] = ReferenceProblem(
                    point, "the reference is in the URI form, which is not supported"
                )
            elif typed_without_identifier:
                resolved.problems[point] = ReferenceProblem(
                    point, "the reference has no object-identifier"
                )
            continue
        if len(bacnet) > 1:
            preferred = [
                c for c in bacnet if _is_true(_first(c, REF + "preferred", raw=True))
            ]
            if len(preferred) != 1:
                resolved.problems[point] = ReferenceProblem(
                    point,
                    "the point has several BACnet references and none is preferred",
                )
                continue
            bacnet = preferred
        outcome = _resolve_one(point, bacnet[0], devices)
        if isinstance(outcome, BACnetReference):
            resolved.references[point] = outcome
        else:
            resolved.problems[point] = outcome
    return resolved


def _resolve_one(
    point: str,
    description: dict[str, list[Any]],
    devices: dict[str, dict[str, list[Any]]],
) -> BACnetReference | ReferenceProblem:
    identifier = _literal_text(_first(description, "object-identifier", raw=True))
    match = _IDENTIFIER.match(identifier or "")
    if match is None:
        return ReferenceProblem(point, f"malformed object identifier {identifier!r}")
    object_type, object_instance = match.group(1), int(match.group(2))
    if not object_type.isdigit():
        try:
            ObjectType(object_type)
        except ValueError:
            return ReferenceProblem(point, f"unknown object type {object_type!r}")
    declared_type = _literal_text(_first(description, "object-type", raw=True))
    if declared_type is not None and declared_type.strip() != object_type:
        return ReferenceProblem(
            point,
            f"object-type {declared_type!r} contradicts the identifier {object_type!r}",
        )
    device_node = None
    for link in _LINK_PROPERTIES:
        device_node = _first(description, link, raw=True)
        if device_node is not None:
            break
    if device_node is None:
        return ReferenceProblem(point, "the reference names no device")
    device = devices.get(_term_key(device_node))
    instance_text = _literal_text(_first(device or {}, "device-instance", raw=True))
    if instance_text is None or not instance_text.strip().isdigit():
        return ReferenceProblem(point, "the device has no device-instance")
    device_instance = int(instance_text)
    device_ip = _device_ip(device or {})
    property_name = _property_name(_first(description, REF + "read-property", raw=True))
    if property_name is None or not _known_property(property_name):
        return ReferenceProblem(
            point,
            f"unknown property {property_name!r}",
            device_instance,
            device_ip,
        )
    return BACnetReference(
        point=point,
        device_instance=device_instance,
        device_ip=device_ip,
        object_type=object_type,
        object_instance=object_instance,
        property_name=property_name,
    )


def _known_property(name: str) -> bool:
    """Whether the standard, as bacpypes3 knows it, has a property of this name."""
    try:
        PropertyIdentifier(name)
    except ValueError:
        return False
    return True


def _property_name(term: Any) -> str | None:
    """``present-value`` from a literal or from the ASHRAE IRI ``…#Present_Value``."""
    if term is None:
        return DEFAULT_PROPERTY
    if isinstance(term, Literal):
        text = term.value.strip()
    elif isinstance(term, NamedNode):
        local = _local(term.value)
        if local is None:
            return None
        text = local
    else:
        return None
    if not text:
        return None
    return text.lower().replace("_", "-")


def _device_ip(device: dict[str, list[Any]]) -> str | None:
    for port in device.get("__ports__", []):
        text = _literal_text(_first(port, "ip-address", raw=True))
        if text is None:
            continue
        text = text.strip()
        if re.fullmatch(r"[0-9A-Fa-f]{8}", text):
            octets = [str(int(text[i : i + 2], 16)) for i in range(0, 8, 2)]
            return ".".join(octets)
        if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text):
            return text
    return None


def _first(
    description: dict[str, list[Any]], local_or_iri: str, raw: bool = False
) -> Any:
    """The first value of a property given by BACnet local name or by full IRI."""
    candidates = (
        [local_or_iri]
        if local_or_iri.startswith("http")
        else [namespace + local_or_iri for namespace in BACNET_NAMESPACES]
    )
    for iri in candidates:
        values = description.get(iri)
        if values:
            return values[0] if raw else _literal_text(values[0])
    return None


def _is_true(term: Any) -> bool:
    return isinstance(term, Literal) and term.value.strip().lower() in ("true", "1")


def _describe_reference_nodes(
    graph: GraphReader,
) -> dict[tuple[str, str], dict[str, list[Any]]]:
    """Every reference node's triples, keyed by (point, reference node)."""
    result = graph.query(
        f"SELECT ?p ?r ?prop ?val WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        "?r ?prop ?val }"
    )
    nodes: dict[tuple[str, str], dict[str, list[Any]]] = {}
    if not isinstance(result, QuerySolutions):
        return nodes
    for solution in result:
        point, node, prop = solution["p"], solution["r"], solution["prop"]
        if not isinstance(point, NamedNode) or not isinstance(prop, NamedNode):
            continue
        description = nodes.setdefault((point.value, _term_key(node)), {})
        description.setdefault(prop.value, []).append(solution["val"])
    return nodes


def _describe_devices(graph: GraphReader) -> dict[str, dict[str, list[Any]]]:
    """Every device node a reference links to, with its triples and its ports'."""
    links = " | ".join(
        f"<{namespace}{link}>"
        for namespace in BACNET_NAMESPACES
        for link in _LINK_PROPERTIES
    )
    ports = " | ".join(f"<{namespace}hasPort>" for namespace in BACNET_NAMESPACES)
    result = graph.query(
        f"SELECT ?dev ?prop ?val WHERE {{ ?r ({links}) ?dev . ?dev ?prop ?val }}"
    )
    devices: dict[str, dict[str, list[Any]]] = {}
    if isinstance(result, QuerySolutions):
        for solution in result:
            device, prop = solution["dev"], solution["prop"]
            if not isinstance(prop, NamedNode):
                continue
            description = devices.setdefault(_term_key(device), {})
            description.setdefault(prop.value, []).append(solution["val"])
    result = graph.query(
        f"SELECT ?dev ?port ?prop ?val WHERE {{ ?r ({links}) ?dev . "
        f"?dev ({ports}) ?port . ?port ?prop ?val }}"
    )
    if isinstance(result, QuerySolutions):
        port_descriptions: dict[tuple[str, str], dict[str, list[Any]]] = {}
        for solution in result:
            device, port, prop = solution["dev"], solution["port"], solution["prop"]
            if not isinstance(prop, NamedNode):
                continue
            key = (_term_key(device), _term_key(port))
            port_descriptions.setdefault(key, {}).setdefault(prop.value, []).append(
                solution["val"]
            )
        for (device_key, _), description in port_descriptions.items():
            devices.setdefault(device_key, {}).setdefault("__ports__", []).append(
                description
            )
    return devices
