"""The value overlay on its own: literals, and writing into a working graph."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bricklogger.daemon.overlay import ValueOverlay, literal
from bricklogger.daemon.state import RuntimeState
from bricklogger.model import WorkingGraph
from bricklogger.sdk.contract import Observation, PointMetadata

EX = "https://example.com/bldg#"
XSD = "http://www.w3.org/2001/XMLSchema#"


def test_literals_show_texts_for_enums_and_booleans() -> None:
    assert literal(2, "enum", {"enum_texts": {"1": "Off", "2": "Auto"}}) == '"Auto"'
    assert literal(3, "enum", {"enum_texts": {"1": "Off"}}) == f'"3"^^<{XSD}integer>'
    assert literal(True, "boolean", {"boolean_texts": ["Stopped", "Running"]}) == (
        '"Running"'
    )
    assert literal(False, "boolean", {}) == '"false"'
    assert literal(21.5, "number", {}) == f'"21.5"^^<{XSD}double>'
    assert literal(7, "integer", {}) == f'"7"^^<{XSD}integer>'
    assert literal("2026-09-01T12:00:00+00:00", "datetime", {}) == (
        f'"2026-09-01T12:00:00+00:00"^^<{XSD}dateTime>'
    )
    assert literal('a "quoted" text', "string", {}) == '"a \\"quoted\\" text"'


def test_write_and_rebuild_against_a_working_graph(tmp_path: Path) -> None:
    state = RuntimeState(tmp_path / "state.sqlite")
    graph = WorkingGraph(tmp_path / "graph")
    overlay = ValueOverlay(graph, state, interval=60.0)
    mode, sat = f"{EX}Mode", f"{EX}SAT"
    state.replace_plan([(mode, "fake_a", "poll"), (sat, "fake_a", "poll")])
    state.merge_metadata(PointMetadata(mode, "enum", None, {1: "Off", 2: "Auto"}, None))
    stamp = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    state.record_observations(
        [
            Observation(mode, stamp, "enum", 2),
            Observation(sat, stamp, "number", 21.5),
            Observation(sat, stamp, "null", reason="fault"),
        ]
    )
    overlay.mark([mode, sat])
    overlay.flush()

    def values() -> dict[str, str]:
        result = graph.query(
            "PREFIX brick: <https://brickschema.org/schema/Brick#> "
            "SELECT ?p ?v WHERE { ?p brick:lastKnownValue ?n . ?n brick:value ?v }"
        )
        return {str(row["p"].value): str(row["v"].value) for row in result}

    found = values()
    assert found[mode] == "Auto" and float(found[sat]) == 21.5, "no null in the way"

    state.record_observations([Observation(sat, stamp, "number", 22.0)])
    overlay.mark([sat])
    overlay.flush()
    assert float(values()[sat]) == 22.0
    assert graph.count("urn:bricklogger:values") == 6, "one node per point, replaced"

    state.replace_plan([(sat, "fake_a", "poll")])
    overlay.request_rebuild()
    overlay.flush()
    remaining = values()
    assert set(remaining) == {sat} and float(remaining[sat]) == 22.0, "Mode left"
    state.close()
