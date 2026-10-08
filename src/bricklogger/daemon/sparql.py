"""The SPARQL endpoint's engine: read-only queries over the union of the working
graph's four graphs, with the model's prefixes and Brick's pre-declared, and
the result in the serialisation the client asked for. See
``docs/features/api.md``, "SPARQL".
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pyoxigraph import QueryResultsFormat, QueryTriples, RdfFormat

from bricklogger.model.prefixes import with_prefixes
from bricklogger.model.working_graph import WorkingGraph

RESULTS_JSON = "application/sparql-results+json"
TURTLE = "text/turtle"

RESULT_FORMATS: dict[str, QueryResultsFormat] = {
    RESULTS_JSON: QueryResultsFormat.JSON,
    "application/json": QueryResultsFormat.JSON,
    "text/csv": QueryResultsFormat.CSV,
    "text/tab-separated-values": QueryResultsFormat.TSV,
    "application/sparql-results+xml": QueryResultsFormat.XML,
}
GRAPH_FORMATS: dict[str, RdfFormat] = {
    TURTLE: RdfFormat.TURTLE,
    "application/n-triples": RdfFormat.N_TRIPLES,
    "application/rdf+xml": RdfFormat.RDF_XML,
}

_UPDATE_KEYWORDS = frozenset(
    {
        "insert",
        "delete",
        "load",
        "clear",
        "create",
        "drop",
        "copy",
        "move",
        "add",
        "with",
    }
)
_COMMENT = re.compile(r"#[^\n]*")
_DECLARATION = re.compile(r"^\s*(?:prefix\s+\S+\s*<[^>]*>|base\s*<[^>]*>)\s*", re.I)


class QueryError(ValueError):
    """The query cannot be run: it does not parse, or it is an update."""


def is_update(query: str) -> bool:
    """Whether the request is an update, judged by its first keyword."""
    text = _COMMENT.sub("", query)
    while True:
        match = _DECLARATION.match(text)
        if match is None:
            break
        text = text[match.end() :]
    first = text.split(None, 1)[0].lower() if text.split() else ""
    return first in _UPDATE_KEYWORDS


def negotiate(accept: str, offered: Mapping[str, object], default: str) -> str:
    """The first media type of the Accept header that is offered, else the default."""
    for part in accept.split(","):
        media = part.split(";")[0].strip().lower()
        if media in offered:
            return media
    return default


def run_query(
    graph: WorkingGraph, prefixes: Mapping[str, str], query: str, accept: str = ""
) -> tuple[bytes, str]:
    """Run a read-only query; returns the serialised result and its media type."""
    if is_update(query):
        raise QueryError("update requests are refused: the graph is read-only")
    try:
        result = graph.query(with_prefixes(query, prefixes), references=True)
    except SyntaxError as exc:
        raise QueryError(f"the query does not parse: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise QueryError(f"the query failed: {exc}") from exc
    if isinstance(result, QueryTriples):
        media = negotiate(accept, GRAPH_FORMATS, TURTLE)
        return bytes(result.serialize(format=GRAPH_FORMATS[media]) or b""), media
    media = negotiate(accept, RESULT_FORMATS, RESULTS_JSON)
    return bytes(result.serialize(format=RESULT_FORMATS[media]) or b""), media
