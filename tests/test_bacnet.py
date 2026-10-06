"""The BACnet/IP source against a simulated device on localhost, and the
reference resolution and value mapping on their own."""

from __future__ import annotations

import asyncio
import functools
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from bacpypes3.app import Application
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, Reliability
from bacpypes3.local.analog import AnalogInputObject
from bacpypes3.local.binary import BinaryInputObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.multistate import MultiStateValueObject
from bacpypes3.local.networkport import NetworkPortObject
from bacpypes3.primitivedata import ObjectIdentifier, Real, Unsigned
from pydantic import ValidationError
from typer.testing import CliRunner

from bricklogger.cli import app as cli_app
from bricklogger.daemon.core import Daemon
from bricklogger.plugins.bacnet_ip.declaration import SOURCE, BACnetIPConfig
from bricklogger.plugins.bacnet_ip.references import resolve_references
from bricklogger.plugins.bacnet_ip.source import (
    SPREAD_MAX,
    BACnetIPSource,
    DevicePoller,
    search_delay,
    spread_offset,
)
from bricklogger.plugins.bacnet_ip.values import classify, protocol_unit
from bricklogger.sdk.contract import (
    AssignedPoint,
    GraphReader,
)
from bricklogger.sdk.testing import Collector, graph_from_turtle
from bricklogger.sdk.testing import assigned as assigned_point
from tests.support import free_port

EX = "https://example.com/bldg#"
QUDT = "http://qudt.org/vocab/unit/"

MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix b20: <http://data.ashrae.org/bacnet/2020#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <https://example.com/bldg#> .

ex:Ctrl a b20:BACnetDevice ; b20:device-instance 1201 ;
    b20:hasPort [ a b20:Port ; b20:ip-address "7F000001"^^xsd:hexBinary ] .
ex:Ghost a bacnet:BACnetDevice ; bacnet:device-instance 1299 .
ex:NoInstance a bacnet:BACnetDevice .

ex:SAT a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,3" ; b20:objectOf ex:Ctrl ] .
ex:Fan a brick:Fan_Status ;
    ref:hasExternalReference [
        bacnet:object-identifier "binary-input,1" ; bacnet:contains ex:Ctrl ] .
ex:Mode a brick:Mode_Status ;
    ref:hasExternalReference [
        b20:object-identifier "multi-state-value,12" ; b20:objectOf ex:Ctrl ] .
ex:Faulty a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,4" ; b20:objectOf ex:Ctrl ] .
ex:Missing a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,99" ; b20:objectOf ex:Ctrl ] .
ex:Reliab a brick:Status ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,4" ; b20:objectOf ex:Ctrl ;
        ref:read-property "reliability" ] .
ex:Ghosted a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        bacnet:object-identifier "analog-input,1" ; bacnet:contains ex:Ghost ] .
ex:Malformed a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog input 3" ; b20:objectOf ex:Ctrl ] .
ex:UriForm a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ref:BACnetURI "bacnet://1201/analog-input,3" ] .
ex:NameOnly a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        a ref:BACnetReference ; b20:object-name "SAT" ; b20:objectOf ex:Ctrl ] .
ex:Twice a brick:Temperature_Sensor ;
    ref:hasExternalReference
        [ b20:object-identifier "analog-input,3" ; b20:objectOf ex:Ctrl ] ,
        [ b20:object-identifier "analog-input,4" ; b20:objectOf ex:Ctrl ] .
ex:Preferred a brick:Temperature_Sensor ;
    ref:hasExternalReference
        [ b20:object-identifier "analog-input,3" ; b20:objectOf ex:Ctrl ;
          ref:preferred true ] ,
        [ b20:object-identifier "analog-input,4" ; b20:objectOf ex:Ctrl ] .
ex:Deviceless a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,3" ; b20:objectOf ex:NoInstance ] .
ex:BadProp a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        b20:object-identifier "analog-input,3" ; b20:objectOf ex:Ctrl ;
        ref:read-property "no-such-property" ] .
"""


@functools.cache
def model_graph() -> GraphReader:
    """The model as the daemon holds it: validated and inferred, built once for
    the module."""
    return graph_from_turtle(MODEL)


def wait_for(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture(scope="module")
def simulated_device() -> Iterable[None]:
    """A BACnet device on 127.0.0.1:47808 with a few objects, in its own thread."""
    ready = threading.Event()
    stop = threading.Event()

    def serve() -> None:
        async def main() -> None:
            app = Application.from_object_list(
                [
                    DeviceObject(
                        objectIdentifier=("device", 1201),
                        objectName="Ctrl",
                        vendorIdentifier=999,
                    ),
                    NetworkPortObject(
                        "127.0.0.1/32:47808",
                        objectIdentifier=("network-port", 1),
                        objectName="NP",
                    ),
                ]
            )
            app.add_object(
                AnalogInputObject(
                    objectIdentifier=("analog-input", 3),
                    objectName="SAT",
                    presentValue=21.5,
                    units=EngineeringUnits("degrees-celsius"),
                    statusFlags=[0, 0, 0, 0],
                )
            )
            app.add_object(
                AnalogInputObject(
                    objectIdentifier=("analog-input", 4),
                    objectName="Faulty",
                    presentValue=99.0,
                    units=EngineeringUnits("degrees-celsius"),
                    reliability="unreliable-other",
                )
            )
            app.add_object(
                BinaryInputObject(
                    objectIdentifier=("binary-input", 1),
                    objectName="Fan",
                    presentValue="active",
                    statusFlags=[0, 0, 0, 0],
                    activeText="Running",
                    inactiveText="Stopped",
                )
            )
            app.add_object(
                MultiStateValueObject(
                    objectIdentifier=("multi-state-value", 12),
                    objectName="Mode",
                    presentValue=2,
                    numberOfStates=3,
                    stateText=["Off", "Auto", "Manual"],
                    statusFlags=[0, 0, 0, 0],
                )
            )
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
            app.close()

        asyncio.run(main())

    thread = threading.Thread(target=serve, name="simulated-bacnet", daemon=True)
    thread.start()
    assert ready.wait(10)
    yield None
    stop.set()
    thread.join(5)


def assigned(uri: str, interval: float = 0.3) -> AssignedPoint:
    return assigned_point(f"{EX}{uri}", interval=interval)


def test_reference_resolution_covers_both_vocabularies_and_the_rejections() -> None:
    resolved = resolve_references(model_graph())
    sat = resolved.references[f"{EX}SAT"]
    assert (sat.device_instance, sat.device_ip) == (1201, "127.0.0.1")
    assert (sat.object_identifier, sat.property_name) == (
        "analog-input,3",
        "present-value",
    )
    fan = resolved.references[f"{EX}Fan"]
    assert (fan.device_instance, fan.object_type, fan.object_instance) == (
        1201,
        "binary-input",
        1,
    )
    assert resolved.references[f"{EX}Reliab"].property_name == "reliability"
    assert resolved.references[f"{EX}Ghosted"].device_ip is None
    assert resolved.references[f"{EX}Preferred"].object_identifier == "analog-input,3"
    problems = {uri.split("#")[1]: p.reason for uri, p in resolved.problems.items()}
    assert "malformed object identifier" in problems["Malformed"]
    assert "URI form" in problems["UriForm"]
    assert "no object-identifier" in problems["NameOnly"]
    assert "none is preferred" in problems["Twice"]
    assert "no device-instance" in problems["Deviceless"]
    assert "unknown property" in problems["BadProp"]


def test_value_and_unit_mapping() -> None:
    assert classify("analog-input", "present-value", Real(21.5)) == ("number", 21.5)
    assert classify("analog-input", "present-value", Real(float("nan"))) == (
        "null",
        None,
    )
    assert classify("binary-input", "present-value", BinaryPV("active")) == (
        "boolean",
        True,
    )
    assert classify("multi-state-value", "present-value", Unsigned(2)) == ("enum", 2)
    reliability = Reliability("no-fault-detected")
    assert classify("analog-input", "reliability", reliability) == ("enum", 0)
    assert classify("analog-input", "status-flags", [0, 1, 0, 0]) is None
    assert protocol_unit(EngineeringUnits("degrees-celsius")) == QUDT + "DEG_C"
    assert protocol_unit(EngineeringUnits("no-units")) is None
    assert protocol_unit(EngineeringUnits(500)) == str(EngineeringUnits(500))


def test_source_polls_a_simulated_device(simulated_device: None) -> None:
    reader = model_graph()
    config = BACnetIPConfig.model_validate(
        {
            "address": "127.0.0.1/32:47809",
            "device_instance": 1200,
            "timeout": "1s",
            "retries": 0,
        }
    )
    source = BACnetIPSource("bacnet_test", config, reader)
    assert list(source.resources()) == ["udp:127.0.0.1:47809"]
    claimed = source.claim()
    assert f"{EX}SAT" in claimed and f"{EX}Fan" in claimed
    assert f"{EX}Malformed" in claimed, "in scope, so claimed and then rejected"
    assert f"{EX}UriForm" in claimed, "no scope restriction, so claimed"

    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        source.assign(
            [
                assigned("SAT"),
                assigned("Fan"),
                assigned("Mode"),
                assigned("Faulty"),
                assigned("Missing"),
                assigned("Reliab"),
                assigned("Malformed"),
                assigned("BadProp"),
                assigned("Ghosted", interval=0.5),
                AssignedPoint(f"{EX}Fan", "subscribe", {}),
            ]
        )
        wait_for(lambda: len(collector.observations_for(f"{EX}SAT")) >= 2)
        wait_for(lambda: len(collector.observations_for(f"{EX}Ghosted")) >= 1, 20.0)
        wait_for(lambda: len(collector.observations_for(f"{EX}Reliab")) >= 1)
        wait_for(lambda: len(collector.observations_for(f"{EX}Missing")) >= 1)

        sat = collector.observations_for(f"{EX}SAT")[0]
        assert (sat.type, sat.value) == ("number", 21.5)
        mode = collector.observations_for(f"{EX}Mode")[0]
        assert (mode.type, mode.value) == ("enum", 2)
        faulty = collector.observations_for(f"{EX}Faulty")[0]
        assert (faulty.type, faulty.reason) == ("null", "fault")
        missing = collector.observations_for(f"{EX}Missing")[0]
        assert (missing.type, missing.reason) == ("null", "read_error")
        reliab = collector.observations_for(f"{EX}Reliab")[0]
        assert (reliab.type, reliab.value) == ("enum", 7), "read despite the fault flag"
        ghosted = collector.observations_for(f"{EX}Ghosted")[0]
        assert (ghosted.type, ghosted.reason) == ("null", "unreachable")
        assert collector.observations_for(f"{EX}BadProp") == []

        with collector.lock:
            outcomes = dict(collector.seen_outcomes)
        assert outcomes[f"{EX}SAT"].state == "active"
        assert outcomes[f"{EX}Malformed"].state == "rejected"
        assert "malformed" in (outcomes[f"{EX}Malformed"].reason or "")
        assert outcomes[f"{EX}BadProp"].state == "rejected"
        assert outcomes[f"{EX}Fan"].state == "unsupported", "the later assignment wins"

        with collector.lock:
            metadata = {m.point.split("#")[1]: m for m in collector.seen_metadata}
        assert metadata["SAT"].unit == QUDT + "DEG_C"
        assert metadata["Mode"].enum_texts == {1: "Off", 2: "Auto", 3: "Manual"}
        assert metadata["Reliab"].value_type == "enum"
        texts = metadata["Reliab"].enum_texts
        assert texts is not None and texts[7] == "unreliable-other"

        with collector.lock:
            devices = list(collector.devices)
        assert any(d["device"] == "1201" and d["reachable"] for d in devices)
        assert any(d["device"] == "1299" and not d["reachable"] for d in devices)
    finally:
        source.stop()
        thread.join(10)
    assert not thread.is_alive(), "the source's loop ends when stopped"


def test_declaration_has_a_factory() -> None:
    assert SOURCE.factory is BACnetIPSource


def tool_source(port: int) -> BACnetIPSource:
    config = BACnetIPConfig.model_validate(
        {
            "address": f"127.0.0.1/32:{port}",
            "device_instance": 1200,
            "timeout": "1s",
            "retries": 0,
        }
    )
    return BACnetIPSource("bacnet_tools", config, model_graph())


def test_tools_run_without_a_started_source(simulated_device: None) -> None:
    source = tool_source(47811)
    devices = source.run_tool(
        "discover", {"address": "127.0.0.1:47808", "duration": "1s"}
    )
    assert [d["instance"] for d in devices] == [1201]
    assert devices[0]["name"] == "Ctrl" and devices[0]["address"] == "127.0.0.1"
    assert devices[0]["vendor_id"] == 999

    read = source.run_tool("read", {"device": "127.0.0.1", "object": "analog-input,3"})
    assert (read["value"], read["type"], read["datatype"]) == (21.5, "number", "Real")
    assert read["status_flags"] == {
        "in_alarm": False,
        "fault": False,
        "overridden": False,
        "out_of_service": False,
    }
    units = source.run_tool(
        "read", {"device": "127.0.0.1", "object": "analog-input,3", "property": "units"}
    )
    assert units["text"] == "degrees-celsius" and units["type"] == "enum"
    faulty = source.run_tool(
        "read", {"device": "127.0.0.1", "object": "analog-input,4"}
    )
    assert faulty["status_flags"]["fault"] is True
    missing = source.run_tool(
        "read", {"device": "127.0.0.1", "object": "analog-input,99"}
    )
    assert "unknown-object" in missing["error"]

    listed = source.run_tool("objects", {"device": "127.0.0.1", "values": True})
    by_id = {row["identifier"]: row for row in listed}
    assert by_id["analog-input,3"]["name"] == "SAT"
    assert by_id["analog-input,3"]["value"] == 21.5
    assert by_id["analog-input,3"]["unit"] == "degrees-celsius"
    assert by_id["analog-input,3"]["qudt"] == QUDT + "DEG_C"
    assert by_id["multi-state-value,12"]["value"] == 2
    assert by_id["device,1201"]["type"] == "device"

    with pytest.raises(RuntimeError, match="running daemon"):
        source.run_tool("resolve", {"point": f"{EX}SAT"})
    with pytest.raises(RuntimeError, match="broadcast"):
        source.run_tool("discover", {})
    with pytest.raises(RuntimeError, match="cannot broadcast"):
        source.run_tool("read", {"device": "1201", "object": "analog-input,3"})
    with pytest.raises(RuntimeError, match="object identifier"):
        source.run_tool("read", {"device": "127.0.0.1", "object": "analog input 3"})


def test_tools_run_inside_a_started_source(simulated_device: None) -> None:
    source = tool_source(47812)
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        wait_for(lambda: source.app is not None)
        resolved = source.run_tool("resolve", {"point": "ex:SAT"})
        assert (resolved["device"], resolved["object"]) == (1201, "analog-input,3")
        assert (resolved["address"], resolved["value"]) == ("127.0.0.1", 21.5)
        assert (
            resolved["in_scope"] is True and resolved["status_flags"]["fault"] is False
        )
        malformed = source.run_tool("resolve", {"point": f"{EX}Malformed"})
        assert "malformed" in malformed["problem"]
        ghost = source.run_tool("resolve", {"point": f"{EX}Ghosted"})
        assert ghost["address"] is None and "Who-Is" in ghost["error"]
        nothing = source.run_tool("resolve", {"point": f"{EX}Nothing"})
        assert "no BACnet reference" in nothing["problem"]
        read = source.run_tool(
            "read", {"device": "127.0.0.1", "object": "binary-input,1"}
        )
        assert read["value"] is True and read["type"] == "boolean"
    finally:
        source.stop()
        thread.join(10)


def test_tools_through_the_daemon(
    simulated_device: None, tmp_path: Path, template: Path
) -> None:
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    shutil.copytree(template, tmp_path / "var")
    (config_dir / "daemon.yaml").write_text(f"data_dir: {tmp_path / 'var'}\n")
    (config_dir / "sources.yaml").write_text(
        "bacnet_main:\n  type: bacnet-ip\n  address: 127.0.0.1/32:47813\n"
        "  device_instance: 1200\n  timeout: 1s\n  retries: 0\n"
    )
    (config_dir / "rules.yaml").write_text(
        "- match: { class: brick:Point }\n  action: deny\n"
    )
    daemon = Daemon(config_dir, env={})
    daemon.start()
    try:
        wait_for(lambda: daemon.status_sources()[0]["state"] == "running")
        read = daemon.run_tool(
            "bacnet-ip",
            "bacnet_main",
            "read",
            {"device": "127.0.0.1", "object": "analog-input,3"},
        )
        assert read["value"] == 21.5
        resolved = daemon.run_tool(
            "bacnet-ip", "bacnet_main", "resolve", {"point": f"{EX}SAT"}
        )
        assert (resolved["device"], resolved["object"]) == (1201, "analog-input,3")
        assert resolved["address"] is None, "no address in the model, no broadcast"
        declared = daemon.plugin("bacnet-ip")
        assert [t["name"] for t in declared["tools"]] == [
            "discover",
            "read",
            "objects",
            "resolve",
            "pointlist",
        ]
        assert declared["tools"][0]["parameters"]["properties"]["duration"]
    finally:
        daemon.stop()


def test_tools_through_the_cli_without_a_daemon(
    simulated_device: None, tmp_path: Path
) -> None:
    config_dir = tmp_path / "etc"
    config_dir.mkdir()
    (config_dir / "daemon.yaml").write_text(
        f"data_dir: {tmp_path / 'var'}\napi:\n  port: {free_port()}\n"
    )
    (config_dir / "sources.yaml").write_text(
        "bacnet_main:\n  type: bacnet-ip\n  address: 127.0.0.1/32:47814\n"
        "  device_instance: 1200\n  timeout: 1s\n  retries: 0\n"
    )
    runner = CliRunner()
    base = ["--config-dir", str(config_dir), "sources", "bacnet_main"]
    read = runner.invoke(
        cli_app,
        [
            *base,
            "read",
            "--device",
            "127.0.0.1",
            "--object",
            "analog-input,3",
            "--json",
        ],
    )
    assert read.exit_code == 0, read.output
    assert json.loads(read.output)["value"] == 21.5

    listed = runner.invoke(cli_app, [*base, "objects", "--device", "127.0.0.1"])
    assert listed.exit_code == 0 and "SAT" in listed.output, listed.output

    declared = runner.invoke(
        cli_app, ["--config-dir", str(config_dir), "plugins", "bacnet-ip"]
    )
    assert declared.exit_code == 0 and "--duration" in declared.output, declared.output

    resolve = runner.invoke(cli_app, [*base, "resolve", "--point", f"{EX}SAT"])
    assert resolve.exit_code == 1 and "running daemon" in resolve.output, resolve.output

    unknown = runner.invoke(cli_app, [*base, "read", "--device", "127.0.0.1"])
    assert unknown.exit_code == 1 and "object" in unknown.output, unknown.output


def test_the_vendor_identifier_is_a_setting() -> None:
    default = BACnetIPConfig.model_validate(
        {"address": "127.0.0.1/32:47815", "device_instance": 1200}
    )
    assert default.vendor_id == 999
    configured = BACnetIPConfig.model_validate(
        {"address": "127.0.0.1/32:47815", "device_instance": 1200, "vendor_id": 7}
    )
    source = BACnetIPSource("bacnet_vendor", configured, model_graph())

    async def vendor_of_the_device_object() -> int:
        app = source._make_application()
        try:
            device = app.get_object_id(ObjectIdentifier("device,1200"))
            return int(device.vendorIdentifier)
        finally:
            app.close()

    assert asyncio.run(vendor_of_the_device_object()) == 7
    with pytest.raises(ValueError):
        BACnetIPConfig.model_validate(
            {
                "address": "127.0.0.1/32:47815",
                "device_instance": 1200,
                "vendor_id": 70000,
            }
        )


# --- what the source puts on the network ------------------------------------


def test_the_request_ceiling_is_a_setting() -> None:
    """A fragile network, or one behind a single router, wants it lower."""
    assert BACnetIPConfig(address="10.0.0.2/24", device_instance=1).max_in_flight == 8
    lowered = BACnetIPConfig(address="10.0.0.2/24", device_instance=1, max_in_flight=2)
    assert lowered.max_in_flight == 2
    with pytest.raises(ValidationError):
        BACnetIPConfig(address="10.0.0.2/24", device_instance=1, max_in_flight=0)


def test_first_rounds_are_spread_over_the_interval() -> None:
    """Hundreds of devices must not all begin in the same instant."""
    offsets = [spread_offset(f"{n}:300.0", 300.0) for n in range(400)]
    assert all(0.0 <= offset < SPREAD_MAX for offset in offsets), (
        "the spread never exceeds a minute, however long the interval"
    )
    assert {int(offset // 10) for offset in offsets} == set(range(6)), (
        "every ten-second bucket of the minute is used"
    )
    assert all(spread_offset(f"{n}:10.0", 10.0) < 10.0 for n in range(400)), (
        "a short interval spreads over itself, not beyond"
    )
    assert spread_offset("42:300.0", 300.0) == spread_offset("42:300.0", 300.0), (
        "the place is stable, so a restart does not reshuffle the network"
    )


def test_a_device_that_does_not_answer_is_searched_for_less_and_less() -> None:
    """A failed search broadcasts, and every device on the net must read it."""
    assert [search_delay(n) for n in range(1, 7)] == [
        30.0,
        60.0,
        120.0,
        240.0,
        300.0,
        300.0,
    ]


def test_the_backoff_holds_the_next_search_back() -> None:
    """Without a started application the search fails without touching a net."""
    config = BACnetIPConfig(address="127.0.0.1/32:47899", device_instance=9001)
    instance = BACnetIPSource("bacnet_test", config, model_graph())
    poller = DevicePoller(instance, 4711, None)

    assert asyncio.run(poller._search()) is False
    assert poller.search_failures == 1
    first = poller.next_search
    assert first > 0.0

    assert asyncio.run(poller._search()) is False
    assert poller.search_failures == 1, "the second call did not search at all"
    assert poller.next_search == first

    poller.next_search = 0.0
    assert asyncio.run(poller._search()) is False
    assert poller.search_failures == 2, "once the backoff is over it searches again"


def test_the_point_list_nests_the_devices_and_their_objects(
    simulated_device: None,
) -> None:
    """One document to hand to whoever builds the Brick model."""
    source = tool_source(47816)
    document = source.run_tool(
        "pointlist", {"device": "127.0.0.1", "values": True, "duration": "1s"}
    )
    assert document["instance"] == "bacnet_tools", "the document names its instance"
    assert document["counts"]["devices"] == 1
    assert document["exported_at"].endswith("+00:00")

    (device,) = document["devices"]
    assert device["instance"] == 1201 and device["name"] == "Ctrl"
    assert document["counts"]["objects"] == len(device["objects"])
    by_id = {row["identifier"]: row for row in device["objects"]}
    assert by_id["analog-input,3"]["name"] == "SAT"
    assert by_id["analog-input,3"]["value"] == 21.5
    assert by_id["analog-input,3"]["qudt"] == QUDT + "DEG_C"

    plain = source.run_tool("pointlist", {"device": "127.0.0.1", "duration": "1s"})
    assert "value" not in plain["devices"][0]["objects"][0], (
        "a value costs a reading each, so it is only there when asked for"
    )

    with pytest.raises(RuntimeError, match="broadcast"):
        source.run_tool("pointlist", {})
