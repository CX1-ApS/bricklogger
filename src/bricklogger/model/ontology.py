"""The Brick ontology Bricklogger bundles: Brick 1.5.0-rc1 with its
RealEstateCore alignment and the reference schema, loaded once per process.
See ``docs/architecture.md``, "The Brick model"."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

import rdflib

BRICK_VERSION = "1.5.0-rc1"
"""The bundled Brick's version, as its ``owl:versionInfo`` states."""


@lru_cache(maxsize=1)
def brick_ontology() -> rdflib.Graph:
    """The bundled Brick ontology, parsed once."""
    graph = rdflib.Graph()
    document = files("bricklogger.model").joinpath("Brick.ttl")
    graph.parse(data=document.read_text(encoding="utf-8"), format="turtle")
    return graph
