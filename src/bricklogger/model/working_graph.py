"""The working graph: an Oxigraph store in the data directory with four named
graphs, where every query runs as SPARQL. See ``docs/architecture.md``, "The
working graph"."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import rdflib
from pyoxigraph import NamedNode, QuerySolutions, RdfFormat, Store

GRAPH_DIR = "graph"

MODEL_GRAPH = "urn:bricklogger:model"
ONTOLOGY_GRAPH = "urn:bricklogger:ontology"
INFERRED_GRAPH = "urn:bricklogger:inferred"
VALUES_GRAPH = "urn:bricklogger:values"

Triple = tuple[rdflib.term.Node, rdflib.term.Node, rdflib.term.Node]


class WorkingGraph:
    """The store; queries run over the union of its graphs."""

    def __init__(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.store = Store(str(path))

    def replace_model(
        self,
        model: Iterable[Triple],
        ontology: Iterable[Triple],
        inferred: Iterable[Triple],
    ) -> None:
        """Swap the model, ontology and inferred graphs for a newly activated version.

        The three graphs are loaded from one N-Quads document, so blank nodes
        keep one identity across them; the value overlay is left alone.
        """
        document = _nquads(
            {MODEL_GRAPH: model, ONTOLOGY_GRAPH: ontology, INFERRED_GRAPH: inferred}
        )
        for iri in (MODEL_GRAPH, ONTOLOGY_GRAPH, INFERRED_GRAPH):
            graph = NamedNode(iri)
            if self.store.contains_named_graph(graph):
                self.store.clear_graph(graph)
        self.store.load(document.encode("utf-8"), RdfFormat.N_QUADS)
        self.store.flush()

    def query(self, sparql: str) -> Any:
        """A read-only query over the union of all graphs."""
        return self.store.query(sparql, use_default_graph_as_union=True)

    def update(self, sparql: str) -> None:
        """A SPARQL update; the daemon uses it for the value overlay only."""
        self.store.update(sparql)

    def clear_graph(self, graph_iri: str) -> None:
        """Empty one named graph, if it exists."""
        graph = NamedNode(graph_iri)
        if self.store.contains_named_graph(graph):
            self.store.clear_graph(graph)

    def count(self, graph_iri: str) -> int:
        """The number of triples in one named graph."""
        result = self.store.query(
            f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph_iri}> {{ ?s ?p ?o }} }}"
        )
        if not isinstance(result, QuerySolutions):
            return 0
        for solution in result:
            return int(solution["n"].value)
        return 0

    def dump(self, graph_iri: str) -> bytes:
        """One named graph as N-Triples."""
        data = self.store.dump(
            format=RdfFormat.N_TRIPLES, from_graph=NamedNode(graph_iri)
        )
        return bytes(data) if data is not None else b""


def _nquads(graphs: dict[str, Iterable[Triple]]) -> str:
    dataset = rdflib.Dataset()
    for iri, triples in graphs.items():
        graph = dataset.graph(rdflib.URIRef(iri))
        for triple in triples:
            graph.add(triple)
    serialized = dataset.serialize(format="nquads")
    return serialized if isinstance(serialized, str) else serialized.decode("utf-8")
