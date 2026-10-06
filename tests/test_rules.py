"""Rule evaluation over an activated model: selectors, regex, SPARQL, order."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from bricklogger.config import ConfigIssue, validate_configuration
from bricklogger.config.schema import Rule
from bricklogger.daemon.metadata import graph_metadata
from bricklogger.daemon.rules import collect_points, evaluate_rules
from bricklogger.model import ModelStore, WorkingGraph, activate_version, parse_model
from bricklogger.model.prefixes import compact, expand, prefixes_of, with_prefixes
from bricklogger.plugins.bacnet_ip.declaration import SOURCE
from bricklogger.plugins.timescaledb.declaration import DESTINATION
from bricklogger.sdk.registry import PluginRegistry

EX = "https://example.com/bldg#"

MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix rec: <https://w3id.org/rec#> .
@prefix ex: <https://example.com/bldg#> .

ex:Building a brick:Building .
ex:Floor_1 a brick:Floor ; brick:isPartOf ex:Building .
ex:Floor_2 a brick:Floor ; brick:isPartOf ex:Building .
ex:Room_1_17 a brick:Room ; brick:isPartOf ex:Floor_1 .
ex:Room_1_18 a brick:Room ; brick:isPartOf ex:Floor_1 .
ex:Room_2_01 a brick:Room ; brick:isPartOf ex:Floor_2 .
ex:Plant_Room a brick:Room ; brick:isPartOf ex:Floor_2 .

ex:Campus a rec:Building .
ex:Level_3 a rec:Level ; rec:isPartOf ex:Campus .
ex:Room_3_01 a rec:Classroom ; rec:isPartOf ex:Level_3 .
ex:Room_3_02 a rec:Classroom ; rec:isPartOf ex:Level_3 .
ex:Zone_3_01 a rec:HVACZone ; rec:isPartOf ex:Room_3_01 ;
    brick:hasPoint ex:Zone_3_01_CO2 .
ex:Zone_3_01_CO2 a brick:CO2_Level_Sensor .
ex:Zone_3_AB a rec:HVACZone ; rec:hasPart ex:Room_3_01 , ex:Room_3_02 ;
    brick:hasPoint ex:Zone_3_AB_CO2 .
ex:Zone_3_AB_CO2 a brick:CO2_Level_Sensor .

ex:AHU_01 a brick:AHU ; brick:hasLocation ex:Plant_Room .
ex:AHU_01_Fan a brick:Supply_Fan ; brick:isPartOf ex:AHU_01 .
ex:Main_Meter a brick:Electrical_Meter .

ex:AHU_01_SAT a brick:Supply_Air_Temperature_Sensor ; brick:isPointOf ex:AHU_01 .
ex:AHU_01_Fan_Speed a brick:Speed_Sensor ; brick:isPointOf ex:AHU_01_Fan .
ex:Room_1_17_ZAT a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_17 .
ex:Room_1_18_ZAT a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_18 .
ex:Room_2_01_CO2 a brick:CO2_Sensor ; brick:isPointOf ex:Room_2_01 .
ex:Meter_kWh a brick:Energy_Sensor ; brick:isPointOf ex:Main_Meter .
"""

ALL_POINTS = {
    f"{EX}AHU_01_SAT",
    f"{EX}AHU_01_Fan_Speed",
    f"{EX}Room_1_17_ZAT",
    f"{EX}Room_1_18_ZAT",
    f"{EX}Room_2_01_CO2",
    f"{EX}Meter_kWh",
    f"{EX}Zone_3_01_CO2",
    f"{EX}Zone_3_AB_CO2",
}
TEMPERATURES = {f"{EX}AHU_01_SAT", f"{EX}Room_1_17_ZAT", f"{EX}Room_1_18_ZAT"}
FLOOR_1 = {f"{EX}Room_1_17_ZAT", f"{EX}Room_1_18_ZAT"}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> WorkingGraph:
    data_dir = tmp_path_factory.mktemp("data")
    store = ModelStore(data_dir)
    store.store(MODEL.encode(), "turtle")
    working = WorkingGraph(data_dir / "graph")
    activate_version(store, working, 1)
    return working


@pytest.fixture(scope="module")
def prefixes() -> dict[str, str]:
    return prefixes_of(parse_model(MODEL.encode(), "turtle"))


def rules(*specs: dict[str, object]) -> list[Rule]:
    return [Rule.model_validate(spec) for spec in specs]


def accept(**selector: object) -> dict[str, object]:
    return {"match": selector, "action": "accept", "method": "poll", "interval": "5m"}


def test_prefixes_expand_and_compact(prefixes: dict[str, str]) -> None:
    assert prefixes["ex"] == EX
    assert expand("ex:AHU_01", prefixes) == f"{EX}AHU_01"
    assert expand("brick:Point", prefixes).endswith("Brick#Point")
    assert expand("<https://x.example/y>", prefixes) == "https://x.example/y"
    assert expand("https://x.example/y", prefixes) == "https://x.example/y"
    assert compact(f"{EX}Room_1_17", prefixes) == "ex:Room_1_17"
    assert compact("https://x.example/y", prefixes) == "https://x.example/y"
    query = with_prefixes(
        "PREFIX ex: <urn:other#> SELECT ?p WHERE { ?p a brick:Point }", prefixes
    )
    assert query.count("PREFIX ex:") == 1
    assert "PREFIX brick:" in query


def test_facts_include_subclasses_and_part_hierarchies(graph: WorkingGraph) -> None:
    facts = collect_points(graph)
    assert set(facts) == ALL_POINTS
    zat = facts[f"{EX}Room_1_17_ZAT"]
    assert "https://brickschema.org/schema/Brick#Temperature_Sensor" in zat.types
    assert zat.asserted_types == {
        "https://brickschema.org/schema/Brick#Zone_Air_Temperature_Sensor"
    }
    assert zat.locations == {f"{EX}Room_1_17", f"{EX}Floor_1", f"{EX}Building"}
    assert zat.equipment == set()
    sat = facts[f"{EX}AHU_01_SAT"]
    assert sat.equipment == {f"{EX}AHU_01"}
    assert sat.locations == {f"{EX}Plant_Room", f"{EX}Floor_2", f"{EX}Building"}
    fan_speed = facts[f"{EX}AHU_01_Fan_Speed"]
    assert fan_speed.equipment == {f"{EX}AHU_01_Fan", f"{EX}AHU_01"}
    assert fan_speed.locations == sat.locations, "through the equipment it is part of"


def test_class_matches_subclasses(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    result = evaluate_rules(
        graph, rules(accept(**{"class": "brick:Temperature_Sensor"})), prefixes
    )
    assert set(result.accepted) == TEMPERATURES
    assert result.matched_per_rule == [3]
    assert set(result.unmatched) == ALL_POINTS - TEMPERATURES
    accepted = result.accepted[f"{EX}AHU_01_SAT"]
    assert (accepted.method, accepted.interval, accepted.rule) == (
        "poll",
        timedelta(minutes=5),
        "rule 1",
    )


def test_equipment_and_location_are_transitive(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    by_equipment = evaluate_rules(graph, rules(accept(equipment="ex:AHU_01")), prefixes)
    assert set(by_equipment.accepted) == {f"{EX}AHU_01_SAT", f"{EX}AHU_01_Fan_Speed"}
    by_location = evaluate_rules(graph, rules(accept(location="ex:Floor_1")), prefixes)
    assert set(by_location.accepted) == FLOOR_1


def test_keys_are_and_and_lists_are_or(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    building = evaluate_rules(
        graph,
        rules(
            accept(**{"class": "brick:Temperature_Sensor", "location": "ex:Building"})
        ),
        prefixes,
    )
    assert set(building.accepted) == TEMPERATURES, (
        "the AHU's plant room is in the building"
    )
    both = evaluate_rules(
        graph,
        rules(
            accept(**{"class": "brick:Temperature_Sensor", "location": "ex:Floor_1"})
        ),
        prefixes,
    )
    assert set(both.accepted) == FLOOR_1
    either = evaluate_rules(
        graph, rules(accept(location=["ex:Room_1_17", "ex:Room_2_01"])), prefixes
    )
    assert set(either.accepted) == {f"{EX}Room_1_17_ZAT", f"{EX}Room_2_01_CO2"}
    one = evaluate_rules(graph, rules(accept(point="ex:Meter_kWh")), prefixes)
    assert set(one.accepted) == {f"{EX}Meter_kWh"}


def test_first_match_wins_and_the_rest_is_implicitly_denied(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    result = evaluate_rules(
        graph,
        rules(
            {
                "name": "Exclude test room",
                "match": {"location": "ex:Room_1_17"},
                "action": "deny",
            },
            {
                "name": "Temperatures",
                "match": {"class": "brick:Temperature_Sensor"},
                "action": "accept",
                "method": "poll",
                "interval": "1m",
            },
        ),
        prefixes,
    )
    assert result.denied == {f"{EX}Room_1_17_ZAT": 0}
    assert set(result.accepted) == {f"{EX}AHU_01_SAT", f"{EX}Room_1_18_ZAT"}
    assert result.accepted[f"{EX}Room_1_18_ZAT"].rule == "Temperatures"
    assert result.matched_per_rule == [1, 2]
    assert set(result.unmatched) == {
        f"{EX}AHU_01_Fan_Speed",
        f"{EX}Room_2_01_CO2",
        f"{EX}Meter_kWh",
        f"{EX}Zone_3_01_CO2",
        f"{EX}Zone_3_AB_CO2",
    }


def test_regex_matches_the_whole_prefixed_form(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    rooms = evaluate_rules(
        graph,
        rules(
            {
                "match_regex": {"location": "ex:Room_1_.*"},
                "action": "accept",
                "method": "poll",
                "interval": "5m",
            }
        ),
        prefixes,
    )
    assert set(rooms.accepted) == FLOOR_1
    partial = evaluate_rules(
        graph,
        rules(
            {
                "match_regex": {"location": "Room_1"},
                "action": "accept",
                "method": "poll",
                "interval": "5m",
            }
        ),
        prefixes,
    )
    assert partial.accepted == {}, "the whole prefixed form must match"
    exact_class = evaluate_rules(
        graph,
        rules(
            {
                "match_regex": {"class": "brick:Zone_Air_.*"},
                "action": "accept",
                "method": "poll",
                "interval": "5m",
            },
            {
                "match_regex": {"class": "brick:Temperature_Sensor"},
                "action": "accept",
                "method": "poll",
                "interval": "5m",
            },
        ),
        prefixes,
    )
    assert set(exact_class.accepted) == FLOOR_1
    assert exact_class.matched_per_rule == [2, 0], (
        "no subclass hierarchy under match_regex"
    )


def test_sparql_selects_by_its_first_variable(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    result = evaluate_rules(
        graph,
        rules(
            {
                "sparql": "SELECT ?point WHERE { ?point a brick:Energy_Sensor }",
                "action": "accept",
                "method": "poll",
                "interval": "15m",
            }
        ),
        prefixes,
    )
    assert set(result.accepted) == {f"{EX}Meter_kWh"}


def test_rules_that_cannot_be_evaluated_are_skipped_with_an_issue(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    result = evaluate_rules(
        graph,
        rules(
            {"name": "bad prefix", "match": {"class": "foo:Thing"}, "action": "deny"},
            {"name": "bad query", "sparql": "SELECT ?p WHERE { ?p a", "action": "deny"},
            accept(**{"class": "brick:Point"}),
        ),
        prefixes,
    )
    assert [(issue.rule, issue.message.split(":")[0]) for issue in result.issues] == [
        ("bad prefix", "unknown prefix 'foo'"),
        ("bad query", "the SPARQL query failed"),
    ]
    assert result.matched_per_rule == [0, 0, len(ALL_POINTS)]
    assert set(result.accepted) == ALL_POINTS


def test_invalid_regexes_fail_configuration_validation(tmp_path: Path) -> None:
    (tmp_path / "rules.yaml").write_text(
        "- name: broken\n  match_regex: { location: 'ex:Room_(' }\n  action: deny\n"
    )
    result = validate_configuration(
        tmp_path, PluginRegistry.of(SOURCE, DESTINATION), {}
    )
    assert len(result.errors) == 1
    issue: ConfigIssue = result.errors[0]
    assert (issue.subject, issue.key) == ("broken", "match_regex.location")
    assert "invalid regular expression" in issue.message


def test_realestatecore_spaces_are_locations(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    """A zone within a room is part of it; a zone spanning two rooms has them as
    parts. The points of both are reached from their rooms, the level and the
    building. The REC hierarchy stands on its own, because Brick 1.5 rejects a
    REC level as part of a deprecated brick:Building."""
    facts = collect_points(graph)
    within = facts[f"{EX}Zone_3_01_CO2"]
    assert within.locations >= {
        f"{EX}Zone_3_01",
        f"{EX}Room_3_01",
        f"{EX}Level_3",
        f"{EX}Campus",
    }
    assert f"{EX}Room_3_02" not in within.locations
    spanning = facts[f"{EX}Zone_3_AB_CO2"]
    assert spanning.locations == {
        f"{EX}Zone_3_AB",
        f"{EX}Room_3_01",
        f"{EX}Room_3_02",
        f"{EX}Level_3",
        f"{EX}Campus",
    }, "a zone's points are in every room the zone has as a part"
    both = {f"{EX}Zone_3_01_CO2", f"{EX}Zone_3_AB_CO2"}
    by_level = evaluate_rules(graph, rules(accept(location="ex:Level_3")), prefixes)
    assert set(by_level.accepted) == both
    room_01 = evaluate_rules(graph, rules(accept(location="ex:Room_3_01")), prefixes)
    assert set(room_01.accepted) == both
    room_02 = evaluate_rules(graph, rules(accept(location="ex:Room_3_02")), prefixes)
    assert set(room_02.accepted) == {f"{EX}Zone_3_AB_CO2"}
    by_zone = evaluate_rules(graph, rules(accept(location="ex:Zone_3_01")), prefixes)
    assert set(by_zone.accepted) == {f"{EX}Zone_3_01_CO2"}


def test_metadata_location_is_the_nearest_space(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    """Point metadata carries the point's nearest location by the same anchors
    as the location selector: the space it is a point of, or where its
    equipment is, in Brick or RealEstateCore, without the closure over floors
    and buildings."""
    entries = graph_metadata(graph, prefixes, ALL_POINTS)
    by_name = {compact(uri, prefixes): entry for uri, entry in entries.items()}
    assert by_name["ex:Zone_3_01_CO2"].location == "ex:Zone_3_01"
    assert by_name["ex:Zone_3_01_CO2"].equipment is None
    assert by_name["ex:Zone_3_AB_CO2"].location == "ex:Zone_3_AB"
    assert by_name["ex:Room_1_17_ZAT"].location == "ex:Room_1_17"
    assert by_name["ex:AHU_01_SAT"].equipment == "ex:AHU_01"
    assert by_name["ex:AHU_01_SAT"].location == "ex:Plant_Room"
    assert by_name["ex:AHU_01_Fan_Speed"].equipment == "ex:AHU_01_Fan"
    assert by_name["ex:AHU_01_Fan_Speed"].location == "ex:Plant_Room"
    assert by_name["ex:Meter_kWh"].equipment == "ex:Main_Meter"
    assert by_name["ex:Meter_kWh"].location is None
