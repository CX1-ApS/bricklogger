"""A source's own vocabulary: typing at activation, the ontology graph, the
plan's view of a point with the declared reference type, and what the
declaration and the registry say about it."""

from __future__ import annotations

import pytest

from bricklogger.config.schema import Rule
from bricklogger.daemon.core import GraphAccess
from bricklogger.daemon.plan import compute_plan
from bricklogger.daemon.plugins import describe_plugin
from bricklogger.model import ModelStore, WorkingGraph, activate_version
from bricklogger.model.prefixes import WELL_KNOWN
from bricklogger.model.working_graph import ONTOLOGY_GRAPH
from bricklogger.sdk.declaration import SourceDeclaration, Vocabulary
from bricklogger.sdk.registry import PluginRegistry
from tests.fakes import FakeSource, FakeSourceConfig

EX = "https://example.com/bldg#"
REF = "https://brickschema.org/schema/Brick/ref#"
NAMESPACE = "https://example.com/cloud#"

VOCABULARY = Vocabulary(
    prefix="cloud", namespace=NAMESPACE, package="tests", resource="cloud.ttl"
)
CLOUD = SourceDeclaration(
    type_name="cloud",
    description="A test source with a vocabulary of its own.",
    config_schema=FakeSourceConfig,
    reference_types=("cloud:Reference",),
    vocabulary=VOCABULARY,
    factory=FakeSource,
)

MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix cloud: <https://example.com/cloud#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix ex: <https://example.com/bldg#> .

ex:Ctrl a bacnet:BACnetDevice ; bacnet:device-instance 1201 .

ex:Typed a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [ a cloud:Reference ; cloud:object-id "a1" ] .
ex:Untyped a brick:Zone_Air_Temperature_Sensor ;
    ref:hasExternalReference [ cloud:object-id "a2" ] .
ex:BACnetOnly a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        bacnet:object-identifier "analog-input,4" ; bacnet:objectOf ex:Ctrl ] .
"""

CLOUD_POINTS = {f"{EX}Typed", f"{EX}Untyped"}


@pytest.fixture(scope="module")
def activated(tmp_path_factory: pytest.TempPathFactory) -> WorkingGraph:
    """The model activated with the test vocabulary loaded."""
    data_dir = tmp_path_factory.mktemp("data")
    store = ModelStore(data_dir)
    store.store(MODEL.encode(), "turtle")
    graph = WorkingGraph(data_dir / "graph")
    activation = activate_version(store, graph, 1, vocabularies=(VOCABULARY,))
    assert activation.report.valid
    return graph


def _values(graph: WorkingGraph, sparql: str, variable: str) -> set[str]:
    return {str(row[variable].value) for row in graph.query(sparql)}


def test_the_vocabulary_types_references_at_activation(
    activated: WorkingGraph,
) -> None:
    typed = _values(
        activated,
        f"SELECT ?p WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        f"?r a <{NAMESPACE}Reference> }}",
        "p",
    )
    assert typed == CLOUD_POINTS, "typed or not, every node with an object-id"
    external = _values(
        activated,
        f"SELECT ?p WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        f"?r a <{NAMESPACE}Reference> . ?r a <{REF}ExternalReference> }}",
        "p",
    )
    assert external == CLOUD_POINTS, "the subclass axiom reaches the inferred graph"
    definitions = _values(
        activated,
        f"SELECT ?c WHERE {{ GRAPH <{ONTOLOGY_GRAPH}> {{ ?c "
        f"<http://www.w3.org/2000/01/rdf-schema#subClassOf> "
        f"<{REF}ExternalReference> }} }}",
        "c",
    )
    assert f"{NAMESPACE}Reference" in definitions, "the vocabulary is ontology too"


def test_the_plan_understands_the_declared_reference_type(
    activated: WorkingGraph,
) -> None:
    prefixes = {**WELL_KNOWN, **PluginRegistry.of(CLOUD).prefixes(), "ex": EX}
    access = GraphAccess(activated, prefixes)
    claims_nothing = FakeSource("cloud", FakeSourceConfig(claims=[]), access)
    rule = Rule.model_validate(
        {
            "match": {"class": "brick:Point"},
            "action": "accept",
            "method": "poll",
            "interval": "5m",
        }
    )
    plan = compute_plan(
        activated,
        prefixes,
        [rule],
        {"cloud": claims_nothing},
        {"cloud": CLOUD.reference_types},
    )
    codes = {warning.subject: warning.code for warning in plan.warnings}
    assert {codes[point] for point in CLOUD_POINTS} == {"unclaimed"}
    assert codes[f"{EX}BACnetOnly"] == "unknown_reference"


def test_the_declaration_carries_the_vocabulary() -> None:
    assert "cloud:Reference a owl:Class" in VOCABULARY.text()
    registry = PluginRegistry.of(CLOUD)
    assert registry.vocabularies() == (VOCABULARY,)
    assert registry.prefixes() == {"cloud": NAMESPACE}
    described = describe_plugin(registry, "cloud", instances=None)
    assert described["vocabulary"] == {"prefix": "cloud", "namespace": NAMESPACE}
    assert described["reference_types"] == ["cloud:Reference"]


def test_a_vocabulary_is_checked_when_declared() -> None:
    with pytest.raises(ValueError, match="not a valid prefix"):
        Vocabulary("1bad", NAMESPACE, "tests", "cloud.ttl")
    with pytest.raises(ValueError, match="ends with"):
        Vocabulary("ok", "https://example.com/vocab", "x", "y.ttl")
