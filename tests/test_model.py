"""The model layer: versions, validation, inference, the working graph and the diff.

Validation and inference load the Brick ontology and take a few seconds each,
so the expensive results are computed once per module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.model import (
    INFERRED_GRAPH,
    MODEL_GRAPH,
    ONTOLOGY_GRAPH,
    ModelInvalid,
    ModelNotFound,
    ModelStore,
    ModelUnreadable,
    WorkingGraph,
    activate_version,
    diff_versions,
    parse_model,
    upload_model,
    validate_model,
)

PREFIXES = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix unit: <http://qudt.org/vocab/unit/> .
@prefix bacnet2020: <http://data.ashrae.org/bacnet/2020#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/> .
@prefix ex: <https://example.com/bldg#> .
"""

MODEL_V1 = (
    PREFIXES
    + """\
ex:AHU_01 a brick:AHU .
ex:AHU_02 a brick:AHU .
ex:Ctrl a bacnet2020:BACnetDevice ; bacnet2020:device-instance 1201 .
ex:SAT a brick:Supply_Air_Temperature_Sensor ;
    brick:isPointOf ex:AHU_01 ;
    brick:hasUnit unit:DEG_C ;
    ref:hasExternalReference [
        bacnet2020:object-identifier "analog-input,3" ;
        bacnet2020:objectOf ex:Ctrl ] .
ex:ZAT a brick:Zone_Air_Temperature_Sensor ;
    brick:isPointOf ex:AHU_01 ;
    ref:hasExternalReference [
        bacnet2020:object-identifier "analog-input,7" ;
        bacnet2020:objectOf ex:Ctrl ] .
"""
)

# Version 2: SAT moves to AHU_02, a new point uses the other BACnet vocabulary.
MODEL_V2 = (
    PREFIXES
    + """\
ex:AHU_01 a brick:AHU .
ex:AHU_02 a brick:AHU .
ex:Ctrl a bacnet2020:BACnetDevice ; bacnet2020:device-instance 1201 .
ex:SAT a brick:Supply_Air_Temperature_Sensor ;
    brick:isPointOf ex:AHU_02 ;
    brick:hasUnit unit:DEG_C ;
    ref:hasExternalReference [
        bacnet2020:object-identifier "analog-input,3" ;
        bacnet2020:objectOf ex:Ctrl ] .
ex:ZAT a brick:Zone_Air_Temperature_Sensor ;
    brick:isPointOf ex:AHU_01 ;
    ref:hasExternalReference [
        bacnet2020:object-identifier "analog-input,7" ;
        bacnet2020:objectOf ex:Ctrl ] .
ex:RAT a brick:Return_Air_Temperature_Sensor ;
    brick:isPointOf ex:AHU_01 ;
    ref:hasExternalReference [
        bacnet:object-identifier "analog-input,8" ;
        bacnet:contains ex:Ctrl ] .
"""
)

INVALID = (
    PREFIXES
    + """\
ex:Bad a brick:Temperature_Sensor ;
    brick:hasLocation "not a location" .
"""
)


def test_versions_are_numbered_and_the_marker_is_separate(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    assert store.versions() == []
    assert store.active() is None
    first = store.store(MODEL_V1.encode(), "turtle")
    second = store.store(MODEL_V2.encode(), "turtle")
    assert (first.number, second.number) == (1, 2)
    assert first.path.name == "0001.ttl"
    assert store.read(2) == MODEL_V2.encode()
    assert [v.number for v in store.versions()] == [1, 2]
    store.set_active(1)
    assert store.active() == 1
    with pytest.raises(ModelNotFound):
        store.set_active(9)
    assert store.get(2).sha256 == second.sha256


def test_unreadable_uploads_are_reported() -> None:
    with pytest.raises(ModelUnreadable, match="not readable as turtle"):
        parse_model(b"this is not turtle {", "turtle")


def test_validation_reports_only_the_models_violations() -> None:
    report = validate_model(INVALID.encode(), "turtle")
    assert not report.valid
    assert {v.focus for v in report.violations} == {"https://example.com/bldg#Bad"}
    messages = " ".join(v.message for v in report.violations)
    assert "isPointOf" in messages or "Location" in messages

    valid = validate_model(MODEL_V1.encode(), "turtle")
    assert valid.valid, [v.as_dict() for v in valid.violations]


@pytest.fixture(scope="module")
def activated(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[ModelStore, WorkingGraph]:
    data_dir = tmp_path_factory.mktemp("data")
    store = ModelStore(data_dir)
    store.store(MODEL_V1.encode(), "turtle")
    store.store(MODEL_V2.encode(), "turtle")
    graph = WorkingGraph(data_dir / "graph")
    activation = activate_version(store, graph, 2)
    assert activation.version == 2
    assert activation.inferred_triples > 0
    return store, graph


def test_activation_loads_the_four_graphs(
    activated: tuple[ModelStore, WorkingGraph],
) -> None:
    store, graph = activated
    assert store.active() == 2
    uploaded = parse_model(MODEL_V2.encode(), "turtle")
    assert graph.count(MODEL_GRAPH) == len(uploaded)
    assert graph.count(ONTOLOGY_GRAPH) > 50_000
    assert graph.count(INFERRED_GRAPH) > 0


def test_inferred_graph_carries_subclasses_inverses_and_reference_types(
    activated: tuple[ModelStore, WorkingGraph],
) -> None:
    _, graph = activated
    points = {
        str(row["p"].value)
        for row in graph.query(
            "PREFIX brick: <https://brickschema.org/schema/Brick#> "
            "SELECT ?p WHERE { ?p a brick:Point }"
        )
    }
    assert points == {
        "https://example.com/bldg#SAT",
        "https://example.com/bldg#ZAT",
        "https://example.com/bldg#RAT",
    }
    has_point = {
        (str(row["e"].value), str(row["p"].value))
        for row in graph.query(
            "PREFIX brick: <https://brickschema.org/schema/Brick#> "
            "SELECT ?e ?p WHERE { ?e brick:hasPoint ?p }"
        )
    }
    moved = ("https://example.com/bldg#AHU_02", "https://example.com/bldg#SAT")
    assert moved in has_point
    typed = {
        str(row["p"].value)
        for row in graph.query(
            "PREFIX ref: <https://brickschema.org/schema/Brick/ref#> "
            "SELECT ?p WHERE { ?p ref:hasExternalReference ?r . "
            "?r a ref:BACnetReference }"
        )
    }
    assert typed == points, "both BACnet vocabularies get their reference typed"


def test_upload_flow_marks_active_and_diffs(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    first = upload_model(store, MODEL_V1.encode(), "turtle")
    assert first.activated and first.previous is None and first.diff is None
    assert store.active() == 1
    second = upload_model(store, MODEL_V2.encode(), "turtle")
    assert second.previous == 1 and second.diff is not None
    assert second.diff.added == ["https://example.com/bldg#RAT"]
    assert second.diff.changed == ["https://example.com/bldg#SAT"]
    assert second.diff.removed == []
    assert store.active() == 2
    staged = upload_model(store, MODEL_V1.encode(), "turtle", activate=False)
    assert staged.version.number == 3 and not staged.activated
    assert store.active() == 2
    assert diff_versions(store, 2, 3).removed == ["https://example.com/bldg#RAT"]
    with pytest.raises(ModelInvalid) as excinfo:
        upload_model(store, INVALID.encode(), "turtle")
    assert not excinfo.value.report.valid
    assert [v.number for v in store.versions()] == [1, 2, 3], "nothing stored"


def test_cli_model_commands(tmp_path: Path) -> None:
    config_dir = tmp_path / "etc"
    data_dir = tmp_path / "var"
    config_dir.mkdir()
    (config_dir / "daemon.yaml").write_text(f"data_dir: {data_dir}\n")
    model_file = tmp_path / "building.ttl"
    model_file.write_text(MODEL_V1)
    runner = CliRunner()
    base = ["--config-dir", str(config_dir), "model"]

    result = runner.invoke(app, [*base, "upload", str(model_file)])
    assert result.exit_code == 0, result.output
    assert "stored version 1" in result.output
    assert "marked version 1 active" in result.output

    model_file.write_text(MODEL_V2)
    result = runner.invoke(app, [*base, "upload", str(model_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version"]["version"] == 2
    assert payload["diff"]["added"] == ["https://example.com/bldg#RAT"]

    listing = runner.invoke(app, [*base, "list", "--json"])
    assert json.loads(listing.output)["active"] == 2

    diff = runner.invoke(app, [*base, "diff", "1", "2"])
    assert "changed  https://example.com/bldg#SAT" in diff.output

    exported = tmp_path / "export.ttl"
    result = runner.invoke(
        app, [*base, "export", "--version", "1", "-o", str(exported)]
    )
    assert result.exit_code == 0, result.output
    assert exported.read_text() == MODEL_V1

    refused = runner.invoke(app, [*base, "export", "--inferred"])
    assert refused.exit_code == 1
    assert "running daemon" in refused.output

    model_file.write_text(INVALID)
    invalid = runner.invoke(app, [*base, "upload", str(model_file)])
    assert invalid.exit_code == 1
    assert "does not conform" in invalid.output
