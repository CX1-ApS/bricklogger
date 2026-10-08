"""Brick's time-series references beside the model: building them, carrying a
model version through the spool, and a destination receiving it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import rdflib

from bricklogger.daemon.spool import Spool
from bricklogger.model.timeseries import REF, timeseries_graph, with_references
from bricklogger.sdk.contract import ModelDocument, Observation

EX = "https://example.com/bldg#"
TURTLE = f"""\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ex: <{EX}> .
ex:SAT a brick:Supply_Air_Temperature_Sensor .
ex:RAT a brick:Return_Air_Temperature_Sensor .
"""
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_references_are_added_for_the_points_the_model_has() -> None:
    turtle, used = with_references(TURTLE, {f"{EX}SAT": "17", f"{EX}Gone": "4"})
    assert used == {f"{EX}SAT": "17"}, "a key for a point not in the model is left"
    graph = rdflib.Graph().parse(data=turtle, format="turtle")
    (reference,) = graph.objects(rdflib.URIRef(f"{EX}SAT"), REF.hasExternalReference)
    assert (reference, rdflib.RDF.type, REF.TimeseriesReference) in graph
    assert graph.value(reference, REF.hasTimeseriesId) == rdflib.Literal("17")
    assert graph.value(reference, REF.storedAt) is None
    assert not list(
        graph.objects(rdflib.URIRef(f"{EX}RAT"), REF.hasExternalReference)
    ), "a point the destination has not met gets no reference"
    assert "ex:SAT" in turtle, "the model's own prefixes are kept"


def test_a_model_version_travels_through_the_spool_on_its_own(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool.sqlite")
    try:
        document = ModelDocument(3, NOW, (NOW, NOW.replace(hour=13)), TURTLE)
        spool.append_observations([Observation(f"{EX}SAT", NOW, "number", 1.0)])
        spool.append_model(document)
        spool.append_observations([Observation(f"{EX}SAT", NOW, "number", 2.0)])
        first = spool.next_entries(100)
        assert [entry.kind for entry in first] == ["observations"]
        spool.ack(entry.id for entry in first)
        (model,) = spool.next_entries(100)
        assert model.kind == "model" and model.model() == document
    finally:
        spool.close()


def test_each_instance_has_a_graph_of_its_own() -> None:
    assert timeseries_graph("tsdb") == "urn:bricklogger:timeseries:tsdb"
