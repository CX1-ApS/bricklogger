"""Prefixes: the model's own and the well-known ones, for reading and writing
URIs in prefixed form, the form the configuration uses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import rdflib

WELL_KNOWN: dict[str, str] = {
    "brick": "https://brickschema.org/schema/Brick#",
    "ref": "https://brickschema.org/schema/Brick/ref#",
    "rec": "https://w3id.org/rec#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "unit": "http://qudt.org/vocab/unit/",
    "qudt": "http://qudt.org/schema/qudt/",
    "bacnet": "http://data.ashrae.org/bacnet/",
}

_PREFIXED = re.compile(r"^([A-Za-z_][\w.-]*):([^\s/#<>]*)$")
_DECLARED = re.compile(
    r"^\s*PREFIX\s+([A-Za-z_][\w.-]*):", re.IGNORECASE | re.MULTILINE
)


class UnknownPrefix(ValueError):
    """A prefixed name uses a prefix neither the model nor Bricklogger knows."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        super().__init__(f"unknown prefix {prefix!r}")


def declared_prefixes(graph: rdflib.Graph) -> dict[str, str]:
    """The prefixes the model itself declares."""
    return {
        str(prefix): str(namespace)
        for prefix, namespace in graph.namespaces()
        if prefix
    }


def prefixes_of(graph: rdflib.Graph) -> dict[str, str]:
    """The well-known prefixes, overlaid with the ones the model declares."""
    return {**WELL_KNOWN, **declared_prefixes(graph)}


def expand(term: str, prefixes: Mapping[str, str]) -> str:
    """A full IRI from ``ex:AHU_01``, ``<https://…>`` or a bare IRI."""
    text = term.strip()
    if text.startswith("<") and text.endswith(">"):
        return text[1:-1]
    match = _PREFIXED.match(text)
    if match is None:
        return text
    prefix, local = match.groups()
    namespace = prefixes.get(prefix)
    if namespace is None:
        raise UnknownPrefix(prefix)
    return namespace + local


def compact(uri: str, prefixes: Mapping[str, str]) -> str:
    """The prefixed form under the longest matching namespace, else the IRI itself."""
    best_prefix: str | None = None
    best_length = -1
    for prefix, namespace in prefixes.items():
        if len(namespace) > best_length and uri.startswith(namespace):
            local = uri[len(namespace) :]
            if local and "/" not in local and "#" not in local:
                best_prefix, best_length = prefix, len(namespace)
    if best_prefix is None:
        return uri
    return f"{best_prefix}:{uri[best_length:]}"


def with_prefixes(query: str, prefixes: Mapping[str, str]) -> str:
    """Prepend ``PREFIX`` lines for every known prefix the query does not declare."""
    declared = {match.lower() for match in _DECLARED.findall(query)}
    header = "".join(
        f"PREFIX {prefix}: <{namespace}>\n"
        for prefix, namespace in prefixes.items()
        if prefix.lower() not in declared
    )
    return header + query
