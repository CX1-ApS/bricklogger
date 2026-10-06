"""The model explorer's projection: kinds, relations, findings and the tree."""

from __future__ import annotations

from typing import Any

import pytest

from bricklogger.daemon.explorer import (
    attach_runtime,
    build_tree,
    empty_document,
    entities_of_class,
    narrow,
    project_entities,
    sort_tree,
)
from bricklogger.daemon.overlay import insert_statement
from bricklogger.model import ModelStore, WorkingGraph, activate_version, parse_model
from bricklogger.model.prefixes import prefixes_of
from bricklogger.ops.tree import render_tree

EX = "https://example.com/bldg#"

# The richest topology in the suite — two space hierarchies, a part chain, a
# zone within a room and one spanning two — with references and an orphan
# added, so every finding has a case.
MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix rec: <https://w3id.org/rec#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <https://example.com/bldg#> .

ex:Building a brick:Building .
ex:Floor_1 a brick:Floor ; brick:isPartOf ex:Building .
ex:Floor_2 a brick:Floor ; brick:isPartOf ex:Building .
ex:Room_1_17 a brick:Room ; brick:isPartOf ex:Floor_1 ;
    rdfs:label "Meeting room 1.17" .
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

ex:AHU_01 a brick:AHU ; brick:hasLocation ex:Plant_Room ;
    brick:feeds ex:Room_1_17 .
ex:AHU_01_Fan a brick:Supply_Fan ; brick:isPartOf ex:AHU_01 .
ex:Main_Meter a brick:Electrical_Meter .

ex:AHU_01_SAT a brick:Supply_Air_Temperature_Sensor ; brick:isPointOf ex:AHU_01 ;
    brick:hasUnit <http://qudt.org/vocab/unit/DEG_C> ;
    ref:hasExternalReference [ a ref:BACnetReference ;
        bacnet:object-identifier "analog-input,1" ] .
ex:AHU_01_Fan_Speed a brick:Speed_Sensor ; brick:isPointOf ex:AHU_01_Fan .
ex:Room_1_17_ZAT a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_17 ;
    ref:hasExternalReference [ a ref:BACnetReference ;
        bacnet:object-identifier "analog-input,2" ] .
ex:Room_1_18_ZAT a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room_1_18 .
ex:Room_2_01_CO2 a brick:CO2_Sensor ; brick:isPointOf ex:Room_2_01 .
ex:Meter_kWh a brick:Energy_Sensor ; brick:isPointOf ex:Main_Meter .
ex:Orphan a brick:Temperature_Sensor .
"""


@pytest.fixture(scope="module")
def prefixes() -> dict[str, str]:
    return prefixes_of(parse_model(MODEL.encode(), "turtle"))


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> WorkingGraph:
    data_dir = tmp_path_factory.mktemp("explorer")
    store = ModelStore(data_dir)
    store.store(MODEL.encode(), "turtle")
    working = WorkingGraph(data_dir / "graph")
    activate_version(store, working, 1)
    return working


@pytest.fixture(scope="module")
def document(graph: WorkingGraph, prefixes: dict[str, str]) -> dict[str, Any]:
    return project_entities(graph, prefixes)


def by_uri(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entity["uri"]: entity for entity in document["entities"]}


def names(nodes: list[Any]) -> list[str | None]:
    return [node.uri for node in nodes]


def child_of(node: Any, uri: str) -> Any:
    for child in node.children:
        if child.uri == uri:
            return child
    raise AssertionError(f"{uri} is not a child of {node.uri}")


def find(nodes: list[Any], uri: str | None) -> Any:
    for node in nodes:
        if node.uri == uri:
            return node
    raise AssertionError(f"{uri} is not among {names(nodes)}")


def heading_of(nodes: list[Any], label: str) -> Any:
    for node in nodes:
        if node.entity is None and node.label == label:
            return node
    raise AssertionError(f"no heading {label} among {[n.label for n in nodes]}")


def element(uri: str, kind: str, cls: str, grouping: str | None = None) -> Any:
    """A bare element, for the cases the fixture's model cannot express."""
    return {
        "uri": uri,
        "kind": kind,
        "class": cls,
        "types": [cls],
        "grouping": grouping,
        "name": uri.split(":")[-1],
        "unit": None,
        "references": [],
        "last_known_value": None,
        "findings": [],
        "runtime": None,
        "warnings": [],
        "context": False,
    }


def holds(subject: str, obj: str, role: str, child: str) -> Any:
    return {
        "subject": subject,
        "predicate": "brick:hasPart",
        "object": obj,
        "role": role,
        "child": child,
    }


# --- the projection --------------------------------------------------------


def test_every_typed_node_is_an_element_and_references_are_not(
    document: dict[str, Any],
) -> None:
    elements = by_uri(document)
    assert "ex:AHU_01" in elements and "ex:Room_1_17" in elements
    # The reference nodes are blank, and nothing about them is an element.
    assert all(":" in uri for uri in elements)
    assert not any("BACnetReference" in uri for uri in elements)
    assert document["counts"]["entities"] == len(elements)


def test_kinds_follow_the_subclass_hierarchy(document: dict[str, Any]) -> None:
    elements = by_uri(document)
    assert elements["ex:AHU_01_SAT"]["kind"] == "point"
    assert elements["ex:AHU_01"]["kind"] == "equipment"
    assert elements["ex:AHU_01_Fan"]["kind"] == "equipment"
    assert elements["ex:Room_1_17"]["kind"] == "location"
    assert elements["ex:Campus"]["kind"] == "location"  # a RealEstateCore space
    assert elements["ex:Level_3"]["kind"] == "location"
    assert elements["ex:Zone_3_01"]["kind"] == "location"
    assert document["counts"]["kinds"]["point"] == 9


def test_the_class_name_and_unit_come_from_the_model(
    document: dict[str, Any],
) -> None:
    elements = by_uri(document)
    sat = elements["ex:AHU_01_SAT"]
    assert sat["class"] == "brick:Supply_Air_Temperature_Sensor"
    assert sat["types"] == ["brick:Supply_Air_Temperature_Sensor"]
    assert sat["unit"] == "unit:DEG_C"
    assert sat["references"] == ["ref:BACnetReference"]
    assert elements["ex:Room_1_17"]["name"] == "Meeting room 1.17"
    assert elements["ex:Room_1_18"]["name"] == "Room_1_18"  # the local part
    assert elements["ex:Meter_kWh"]["references"] == []


def test_classes_carry_their_ancestors(document: dict[str, Any]) -> None:
    ancestors = document["classes"]["brick:Zone_Air_Temperature_Sensor"]
    assert "brick:Temperature_Sensor" in ancestors
    assert "brick:Point" in ancestors
    assert "brick:Zone_Air_Temperature_Sensor" not in ancestors


def test_relations_say_what_contains_what(document: dict[str, Any]) -> None:
    relations = {
        (r["subject"], r["predicate"], r["object"]): r for r in document["relations"]
    }
    point_of = relations[("ex:AHU_01_SAT", "brick:isPointOf", "ex:AHU_01")]
    assert point_of["role"] == "point" and point_of["child"] == "subject"
    has_point = relations[("ex:Zone_3_01", "brick:hasPoint", "ex:Zone_3_01_CO2")]
    assert has_point["role"] == "point" and has_point["child"] == "object"
    located = relations[("ex:AHU_01", "brick:hasLocation", "ex:Plant_Room")]
    assert located["role"] == "location" and located["child"] == "subject"
    feeds = relations[("ex:AHU_01", "brick:feeds", "ex:Room_1_17")]
    assert feeds["role"] is None and feeds["child"] is None
    # The inference's inverses are not in the document.
    assert ("ex:AHU_01", "brick:hasPoint", "ex:AHU_01_SAT") not in relations


def test_findings_name_what_the_model_lacks(document: dict[str, Any]) -> None:
    elements = by_uri(document)
    assert elements["ex:Orphan"]["findings"] == [
        "no_owner",
        "no_reference",
        "no_relations",
    ]
    assert "no_location" in elements["ex:Main_Meter"]["findings"]
    assert "no_location" not in elements["ex:AHU_01"]["findings"]
    # The fan has no location of its own but is part of a unit that has one.
    assert "no_location" not in elements["ex:AHU_01_Fan"]["findings"]
    assert elements["ex:AHU_01_SAT"]["findings"] == []
    assert "no_reference" in elements["ex:Meter_kWh"]["findings"]
    assert "no_owner" not in elements["ex:Meter_kWh"]["findings"]
    # Brick 1.5 marks the spatial classes RealEstateCore took over.
    assert "deprecated_class" in elements["ex:Room_1_17"]["findings"]
    assert "deprecated_class" in elements["ex:Building"]["findings"]
    assert "deprecated_class" not in elements["ex:Campus"]["findings"]
    assert "deprecated_class" not in elements["ex:Level_3"]["findings"]
    assert document["counts"]["findings"]["no_owner"] == 1


def test_the_runtime_is_attached_to_points_only(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    document = project_entities(graph, prefixes)
    attach_runtime(
        document,
        accepted=[f"{EX}AHU_01_SAT", f"{EX}Meter_kWh"],
        states={
            f"{EX}AHU_01_SAT": {
                "instance": "bacnet_main",
                "method": "poll",
                "fallback_active": False,
                "outcome": "active",
            }
        },
        warnings=[
            {"code": "no_reference", "subject": f"{EX}Meter_kWh"},
            {"code": "unclaimed", "subject": f"{EX}Room_2_01_CO2"},
        ],
        prefixes=prefixes,
    )
    elements = by_uri(document)
    assert elements["ex:AHU_01_SAT"]["runtime"] == {
        "accepted": True,
        "instance": "bacnet_main",
        "method": "poll",
        "fallback_active": False,
        "outcome": "active",
    }
    assert elements["ex:Meter_kWh"]["runtime"]["accepted"] is True
    assert elements["ex:Meter_kWh"]["runtime"]["outcome"] is None
    assert elements["ex:Meter_kWh"]["warnings"] == ["no_reference"]
    assert elements["ex:Room_2_01_CO2"]["warnings"] == ["unclaimed"]
    assert elements["ex:AHU_01"]["runtime"] is None


def test_the_last_known_value_comes_from_the_graphs_overlay(
    tmp_path_factory: pytest.TempPathFactory, prefixes: dict[str, str]
) -> None:
    """The projection reads what the daemon's own overlay writes, texts and all."""
    data_dir = tmp_path_factory.mktemp("overlay")
    store = ModelStore(data_dir)
    store.store(MODEL.encode(), "turtle")
    working = WorkingGraph(data_dir / "graph")
    activate_version(store, working, 1)
    working.update(
        insert_statement(
            [
                {
                    "uri": f"{EX}Room_1_17_ZAT",
                    "last_valid_value": 21.5,
                    "last_valid_time": "2026-09-11T09:12:30Z",
                    "metadata": {},
                },
                {
                    "uri": f"{EX}Zone_3_01_CO2",
                    "last_valid_value": 1,
                    "last_valid_time": "2026-09-11T09:12:31Z",
                    "metadata": {"value_type": "enum", "enum_texts": {"1": "occupied"}},
                },
            ]
        )
    )
    points = by_uri(project_entities(working, prefixes))

    reading = points["ex:Room_1_17_ZAT"]["last_known_value"]
    assert reading == {"value": 21.5, "time": "2026-09-11T09:12:30Z"}
    assert isinstance(reading["value"], float), "a double arrives as a number"
    assert points["ex:Zone_3_01_CO2"]["last_known_value"]["value"] == "occupied", (
        "an enumeration keeps the text the overlay wrote"
    )
    assert points["ex:Room_2_01_CO2"]["last_known_value"] is None, "nothing delivered"
    assert points["ex:Building"]["last_known_value"] is None, "points only"


def test_entities_of_class_follows_subclasses(
    graph: WorkingGraph,
) -> None:
    found = entities_of_class(graph, "https://brickschema.org/schema/Brick#Sensor")
    assert f"{EX}AHU_01_SAT" in found and f"{EX}Orphan" in found
    assert f"{EX}AHU_01" not in found


def test_an_empty_document_has_the_same_shape() -> None:
    document = empty_document({"ex": EX})
    assert document["version"] is None
    assert document["entities"] == [] and document["relations"] == []
    assert document["counts"]["entities"] == 0
    assert document["counts"]["kinds"]["point"] == 0


def test_a_grouping_carries_the_family_that_makes_it_one(
    document: dict[str, Any],
) -> None:
    elements = by_uri(document)
    assert elements["ex:Zone_3_AB"]["grouping"] == "rec:Zone"
    assert elements["ex:Zone_3_01"]["grouping"] == "rec:Zone"
    for uri in ("ex:Room_3_01", "ex:AHU_01", "ex:Building", "ex:AHU_01_SAT"):
        assert elements[uri]["grouping"] is None


# --- the tree --------------------------------------------------------------


def test_the_tree_follows_the_building(document: dict[str, Any]) -> None:
    roots = build_tree(document["entities"], document["relations"])
    building = find(roots, "ex:Building")
    floor = child_of(building, "ex:Floor_1")
    room = child_of(floor, "ex:Room_1_17")
    assert child_of(room, "ex:Room_1_17_ZAT").uri == "ex:Room_1_17_ZAT"
    # Equipment sits where it is located; its parts sit under it.
    plant = child_of(child_of(building, "ex:Floor_2"), "ex:Plant_Room")
    ahu = child_of(plant, "ex:AHU_01")
    assert child_of(ahu, "ex:AHU_01_SAT").uri == "ex:AHU_01_SAT"
    fan = child_of(ahu, "ex:AHU_01_Fan")
    assert child_of(fan, "ex:AHU_01_Fan_Speed").uri == "ex:AHU_01_Fan_Speed"


def test_an_element_appears_once_and_the_rest_stay_relations(
    document: dict[str, Any],
) -> None:
    roots = build_tree(document["entities"], document["relations"])
    level = find(roots, "ex:Campus").children[0]
    assert level.uri == "ex:Level_3"
    # Room_3_01 is part of Level_3 and a part of the spanning zone; the first
    # by URI wins, and the zone keeps the relation without the room under it.
    room = child_of(level, "ex:Room_3_01")
    assert names(room.children) == []
    assert "ex:Zone_3_01" not in names(level.children)
    for node in roots:
        assert node.uri not in ("ex:Zone_3_01", "ex:Zone_3_AB")


def test_what_the_model_forgot_ends_up_unplaced(document: dict[str, Any]) -> None:
    roots = build_tree(document["entities"], document["relations"])
    assert roots[-1].uri is None  # the Unplaced root comes last
    unplaced = names(roots[-1].children)
    assert "ex:Main_Meter" in unplaced and "ex:Orphan" in unplaced
    meter = child_of(roots[-1], "ex:Main_Meter")
    assert names(meter.children) == ["ex:Meter_kWh"]


def test_points_beneath_are_counted(document: dict[str, Any]) -> None:
    roots = build_tree(document["entities"], document["relations"])
    building = find(roots, "ex:Building")
    # AHU_01_SAT, AHU_01_Fan_Speed, Room_1_17_ZAT, Room_1_18_ZAT, Room_2_01_CO2
    assert building.descendants == 5
    # A grouping holds nothing, so nothing is counted beneath its band.
    assert heading_of(roots, "rec:Zone").descendants == 0


def test_sorting_orders_siblings(document: dict[str, Any]) -> None:
    roots = build_tree(document["entities"], document["relations"])
    sort_tree(roots, "count")
    building = find(roots, "ex:Building")
    assert building.children[0].descendants >= building.children[-1].descendants
    assert roots[-1].uri is None  # Unplaced stays last whatever the sort
    sort_tree(roots, "name")
    floor = child_of(building, "ex:Floor_1")
    assert names(floor.children) == ["ex:Room_1_17", "ex:Room_1_18"]


def test_the_groupings_stand_in_a_band_of_their_own(
    document: dict[str, Any],
) -> None:
    roots = build_tree(document["entities"], document["relations"])
    family = heading_of(roots, "rec:Zone")
    assert roots.index(family) > roots.index(find(roots, "ex:Campus"))
    assert roots[-1].uri is None and roots[-1].label is None  # Unplaced last
    assert family.members == 2
    klass = heading_of(family.children, "rec:HVACZone")
    assert klass.members == 2
    zones = {node.uri: node for node in klass.children}
    assert sorted(zones) == ["ex:Zone_3_01", "ex:Zone_3_AB"]
    # What a grouping gathers is counted on its line, not placed beneath it.
    assert zones["ex:Zone_3_AB"].members == 3
    assert zones["ex:Zone_3_AB"].children == []


def test_a_point_whose_only_owner_is_a_grouping_is_unplaced(
    document: dict[str, Any],
) -> None:
    roots = build_tree(document["entities"], document["relations"])
    unplaced = names(roots[-1].children)
    assert "ex:Zone_3_01_CO2" in unplaced and "ex:Zone_3_AB_CO2" in unplaced


def test_a_zones_name_cannot_take_a_room_from_its_storey() -> None:
    """The URI decided once, so a zone named early took the room. Not now."""
    entities = [
        element("ex:AAA_Zone", "location", "rec:HVACZone", grouping="rec:Zone"),
        element("ex:Floor_1", "location", "brick:Floor"),
        element("ex:Room_1", "location", "rec:Room"),
    ]
    relations = [
        holds("ex:Floor_1", "ex:Room_1", "part", "object"),
        holds("ex:AAA_Zone", "ex:Room_1", "part", "object"),
    ]
    roots = build_tree(entities, relations)
    assert child_of(find(roots, "ex:Floor_1"), "ex:Room_1").uri == "ex:Room_1"
    zone = heading_of(heading_of(roots, "rec:Zone").children, "rec:HVACZone")
    assert names(zone.children) == ["ex:AAA_Zone"]
    assert zone.children[0].members == 1


def test_a_system_is_a_grouping_and_its_own_class_needs_no_heading() -> None:
    entities = [
        element("ex:AHU", "equipment", "brick:AHU"),
        element("ex:Plant", "system", "brick:System", grouping="brick:System"),
        element("ex:Room", "location", "rec:Room"),
        element(
            "ex:Vent",
            "system",
            "brick:Ventilation_Air_System",
            grouping="brick:System",
        ),
    ]
    relations = [
        {
            "subject": "ex:AHU",
            "predicate": "brick:hasLocation",
            "object": "ex:Room",
            "role": "location",
            "child": "subject",
        },
        holds("ex:Vent", "ex:AHU", "part", "object"),
    ]
    roots = build_tree(entities, relations)
    # The unit sits where it is located, not in the system that gathers it.
    assert child_of(find(roots, "ex:Room"), "ex:AHU").uri == "ex:AHU"
    family = heading_of(roots, "brick:System")
    assert find(family.children, "ex:Plant").uri == "ex:Plant"
    headings = [node.label for node in family.children if node.entity is None]
    assert headings == ["brick:Ventilation_Air_System"]


def test_a_grouping_asked_for_opens_to_what_it_gathers(
    document: dict[str, Any],
) -> None:
    narrowed = narrow(document, root="ex:Zone_3_AB")
    assert sorted(by_uri(narrowed)) == [
        "ex:Room_3_01",
        "ex:Room_3_02",
        "ex:Zone_3_AB",
        "ex:Zone_3_AB_CO2",
    ]
    roots = build_tree(
        narrowed["entities"], narrowed["relations"], grouping_root="ex:Zone_3_AB"
    )
    zone = heading_of(heading_of(roots, "rec:Zone").children, "rec:HVACZone")
    # the order a grouping gathers in is not promised; key=str also keeps a
    # stray heading in the list, as None, rather than dropping it
    assert sorted(names(zone.children[0].children), key=str) == [
        "ex:Room_3_01",
        "ex:Room_3_02",
        "ex:Zone_3_AB_CO2",
    ]


def test_the_rendered_tree_names_the_band_and_what_it_gathers(
    document: dict[str, Any],
) -> None:
    printed = render_tree({**document, "version": 1})
    lines = printed.splitlines()
    assert "rec:Zone  (2)" in lines
    assert "  rec:HVACZone  (2)" in lines
    assert any(
        line.strip().startswith("ex:Zone_3_AB ") and "gathers 3" in line
        for line in lines
    )
    assert lines.index("rec:Zone  (2)") > lines.index("ex:Campus  rec:Building")
    assert "Unplaced" in lines


# --- narrowing -------------------------------------------------------------


def test_narrowing_keeps_the_ancestors_as_context(document: dict[str, Any]) -> None:
    narrowed = narrow(document, kind="point")
    elements = by_uri(narrowed)
    assert elements["ex:Room_1_17_ZAT"]["context"] is False
    assert elements["ex:Room_1_17"]["context"] is True
    assert elements["ex:Building"]["context"] is True
    assert narrowed["counts"]["entities"] == 9  # the matches, not the context
    roots = build_tree(narrowed["entities"], narrowed["relations"])
    assert child_of(child_of(find(roots, "ex:Building"), "ex:Floor_1"), "ex:Room_1_17")


def test_narrowing_on_a_finding_and_a_search(document: dict[str, Any]) -> None:
    missing = narrow(document, finding="no_reference")
    matched = [e["uri"] for e in missing["entities"] if not e["context"]]
    assert "ex:Meter_kWh" in matched and "ex:AHU_01_SAT" not in matched
    found = narrow(document, search="FAN")
    assert {e["uri"] for e in found["entities"] if not e["context"]} == {
        "ex:AHU_01_Fan",
        "ex:AHU_01_Fan_Speed",
    }


def test_a_root_and_a_depth_cut_the_tree(document: dict[str, Any]) -> None:
    under = narrow(document, root="ex:Floor_1", depth=1)
    assert {e["uri"] for e in under["entities"]} == {
        "ex:Floor_1",
        "ex:Room_1_17",
        "ex:Room_1_18",
    }
    deeper = narrow(document, root="ex:Floor_1")
    assert "ex:Room_1_17_ZAT" in {e["uri"] for e in deeper["entities"]}
    with pytest.raises(KeyError):
        narrow(document, root="ex:Nothing")


def test_narrowing_on_the_runtime(
    graph: WorkingGraph, prefixes: dict[str, str]
) -> None:
    document = project_entities(graph, prefixes)
    attach_runtime(
        document,
        accepted=[f"{EX}AHU_01_SAT"],
        states={
            f"{EX}AHU_01_SAT": {
                "instance": "bacnet_main",
                "method": "poll",
                "fallback_active": False,
                "outcome": "active",
            }
        },
        warnings=[{"code": "unclaimed", "subject": f"{EX}Room_2_01_CO2"}],
        prefixes=prefixes,
    )
    active = narrow(document, outcome="active")
    assert [e["uri"] for e in active["entities"] if not e["context"]] == [
        "ex:AHU_01_SAT"
    ]
    held = narrow(document, warning="unclaimed")
    assert [e["uri"] for e in held["entities"] if not e["context"]] == [
        "ex:Room_2_01_CO2"
    ]
    mine = narrow(document, instance="bacnet_main")
    assert [e["uri"] for e in mine["entities"] if not e["context"]] == ["ex:AHU_01_SAT"]
