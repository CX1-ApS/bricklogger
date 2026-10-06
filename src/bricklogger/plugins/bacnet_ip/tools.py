"""The BACnet/IP protocol tools: ``discover``, ``read``, ``objects`` and
``resolve``. They run inside the source's loop when the daemon runs it, and on
a temporary application otherwise. See ``docs/features/sources.md``,
"Protocol tools".
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from bacpypes3.apdu import AbortPDU, ErrorPDU, ErrorRejectAbortNack, RejectPDU
from bacpypes3.app import Application
from bacpypes3.basetypes import ErrorType
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Enumerated, ObjectIdentifier
from pydantic import BaseModel, ConfigDict, Field

from bricklogger.config.values import Duration
from bricklogger.model.prefixes import UnknownPrefix, expand
from bricklogger.plugins.bacnet_ip.config import MAX_DEVICE_INSTANCE, BACnetIPConfig
from bricklogger.plugins.bacnet_ip.references import resolve_references
from bricklogger.plugins.bacnet_ip.values import classify, protocol_unit
from bricklogger.sdk.contract import GraphReader

WILDCARD_DEVICE = "device,4194303"
RPM_CHUNK = 20
STATUS_FLAGS = "status-flags"
FLAG_NAMES = ("in_alarm", "fault", "overridden", "out_of_service")
TOOL_NAMES = ("discover", "read", "objects", "resolve")


class ToolError(RuntimeError):
    """The tool cannot complete; the message is for the operator."""


class DiscoverParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: int = Field(
        default=0,
        ge=0,
        le=MAX_DEVICE_INSTANCE,
        description="The lowest device instance asked for.",
    )
    high: int = Field(
        default=MAX_DEVICE_INSTANCE,
        ge=0,
        le=MAX_DEVICE_INSTANCE,
        description="The highest device instance asked for.",
    )
    duration: Duration = Field(
        default=timedelta(seconds=3),
        description="How long to listen for answers, e.g. 3s.",
    )
    address: str | None = Field(
        default=None,
        description="Send the Who-Is to this address instead of broadcasting.",
    )


class ReadParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str = Field(description="The device: an instance number, or its address.")
    object: str = Field(description="The object identifier, e.g. analog-input,3.")
    property: str = Field(default="present-value", description="The property to read.")
    index: int | None = Field(default=None, ge=0, description="An array element.")


class ObjectsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str = Field(description="The device: an instance number, or its address.")
    values: bool = Field(default=False, description="Add Present_Value and Units.")


class PointListParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str | None = Field(
        default=None,
        description="One device by instance number or address; "
        "default every device the instance claims.",
    )
    low: int | None = Field(
        default=None,
        ge=0,
        le=MAX_DEVICE_INSTANCE,
        description="The lowest device instance asked for; "
        "default the instance's own scope.",
    )
    high: int | None = Field(
        default=None,
        ge=0,
        le=MAX_DEVICE_INSTANCE,
        description="The highest device instance asked for; "
        "default the instance's own scope.",
    )
    duration: Duration = Field(
        default=timedelta(seconds=3),
        description="How long to listen for answers, e.g. 3s.",
    )
    values: bool = Field(default=False, description="Add each object's Present_Value.")


class ResolveParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: str = Field(description="The point URI, full or prefixed.")


def timeout_for(name: str, parameters: Mapping[str, Any]) -> float:
    """How long the caller waits for the tool, with room for the protocol."""
    if name == "discover":
        duration = parameters.get("duration")
        seconds = duration.total_seconds() if isinstance(duration, timedelta) else 3.0
        return seconds + 30.0
    if name == "pointlist":
        # A whole site, one request at a time: slow by design.
        return 900.0
    return 60.0


async def run(
    name: str,
    app: Application,
    settings: BACnetIPConfig,
    graph: GraphReader | None,
    parameters: Mapping[str, Any],
    instance: str | None = None,
) -> Any:
    """Run one tool by name; raises ToolError when it cannot complete."""
    if name == "discover":
        return await discover(
            app, settings, DiscoverParameters.model_validate(parameters)
        )
    if name == "read":
        return await read(app, settings, ReadParameters.model_validate(parameters))
    if name == "objects":
        return await objects(
            app, settings, ObjectsParameters.model_validate(parameters)
        )
    if name == "pointlist":
        return await pointlist(
            app,
            settings,
            instance,
            PointListParameters.model_validate(parameters),
        )
    if name == "resolve":
        return await resolve(
            app, settings, graph, ResolveParameters.model_validate(parameters)
        )
    raise ToolError(f"the BACnet/IP source has no tool {name!r}")


# --- finding devices ---------------------------------------------------------


def can_broadcast(settings: BACnetIPConfig) -> bool:
    """A broadcast needs a broadcast address, or a BBMD to forward it."""
    return settings.prefix_length < 31 or settings.bbmd is not None


async def locate(
    app: Application,
    settings: BACnetIPConfig,
    instance: int,
    hint: str | None,
    timeout: float,
) -> Address | None:
    """A device's address by instance: a directed Who-Is to the hint, else broadcast."""
    targets: list[Address | None] = []
    if hint:
        targets.append(Address(hint))
    if can_broadcast(settings):
        targets.append(None)
    for target in targets:
        try:
            iams = await app.who_is(instance, instance, target, timeout=timeout)
        except (ErrorRejectAbortNack, Exception):
            iams = []
        for iam in iams:
            if int(iam.iAmDeviceIdentifier[1]) == instance:
                return iam.pduSource
    return None


async def instance_at(app: Application, address: Address, timeout: float) -> int | None:
    """The instance of the device at an address, from its answer to a Who-Is."""
    try:
        iams = await app.who_is(None, None, address, timeout=timeout)
    except (ErrorRejectAbortNack, Exception):
        return None
    for iam in iams:
        return int(iam.iAmDeviceIdentifier[1])
    return None


def parse_device(text: str) -> tuple[int | None, Address | None]:
    """``1201`` is an instance; anything else must be an address."""
    text = text.strip()
    if text.isdigit():
        return int(text), None
    try:
        return None, Address(text)
    except Exception as exc:
        raise ToolError(
            f"{text!r} is neither a device instance nor an address: {exc}"
        ) from exc


async def resolve_device(
    app: Application, settings: BACnetIPConfig, text: str
) -> tuple[int | None, Address]:
    instance, address = parse_device(text)
    if address is not None:
        return None, address
    assert instance is not None
    found = await locate(
        app, settings, instance, None, settings.timeout.total_seconds()
    )
    if found is None:
        hint = "" if can_broadcast(settings) else "; the interface cannot broadcast"
        raise ToolError(
            f"device {instance} did not answer a Who-Is{hint}; give its address"
        )
    return instance, found


def object_identifier(text: str) -> ObjectIdentifier:
    try:
        return ObjectIdentifier(text.strip())
    except Exception as exc:
        raise ToolError(
            f"{text!r} is not an object identifier such as analog-input,3: {exc}"
        ) from exc


# --- the tools ---------------------------------------------------------------


async def discover(
    app: Application, settings: BACnetIPConfig, p: DiscoverParameters
) -> list[dict[str, Any]]:
    target = Address(p.address) if p.address else None
    if target is None and not can_broadcast(settings):
        raise ToolError(
            "the interface has no broadcast address; give --address to send the "
            "Who-Is to a device or a BBMD"
        )
    try:
        iams = await app.who_is(
            p.low, p.high, target, timeout=p.duration.total_seconds()
        )
    except (ErrorRejectAbortNack, Exception) as exc:
        raise ToolError(f"Who-Is failed: {exc}") from exc
    devices: list[dict[str, Any]] = []
    for iam in iams:
        instance = int(iam.iAmDeviceIdentifier[1])
        address = iam.pduSource
        names = await _device_names(app, address, instance)
        devices.append(
            {
                "instance": instance,
                "address": str(address),
                "name": names.get("object-name"),
                "vendor": names.get("vendor-name"),
                "vendor_id": int(iam.vendorID),
                "model": names.get("model-name"),
                "max_apdu": int(iam.maxAPDULengthAccepted),
                "segmentation": str(iam.segmentationSupported),
            }
        )
    devices.sort(key=lambda device: int(device["instance"]))
    return devices


async def _device_names(
    app: Application, address: Address, instance: int
) -> dict[str, str]:
    try:
        results = await app.read_property_multiple(
            address,
            [
                ObjectIdentifier(f"device,{instance}"),
                ["object-name", "vendor-name", "model-name"],
            ],
        )
    except (ErrorRejectAbortNack, Exception):
        return {}
    return {
        str(prop): str(value)
        for _, prop, _, value in results
        if not isinstance(value, ErrorType)
    }


async def read(
    app: Application, settings: BACnetIPConfig, p: ReadParameters
) -> dict[str, Any]:
    instance, address = await resolve_device(app, settings, p.device)
    objid = object_identifier(p.object)
    result: dict[str, Any] = {
        "device": instance,
        "address": str(address),
        "object": str(objid),
        "property": p.property,
        "index": p.index,
    }
    try:
        value = await app.read_property(address, objid, p.property, p.index)
    except ErrorPDU as exc:
        result["error"] = error_text(exc)
        return result
    except RejectPDU as exc:
        result["error"] = f"rejected: {exc}"
        return result
    except (AbortPDU, TimeoutError) as exc:
        raise ToolError(f"no answer from {address}: {exc or 'timeout'}") from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    result.update(describe_value(str(objid).split(",")[0], p.property, value))
    result["status_flags"] = await read_flags(app, address, objid)
    return result


async def objects(
    app: Application, settings: BACnetIPConfig, p: ObjectsParameters
) -> list[dict[str, Any]]:
    instance, address = await resolve_device(app, settings, p.device)
    if instance is None:
        instance = await instance_at(app, address, settings.timeout.total_seconds())
    device_id = (
        ObjectIdentifier(f"device,{instance}")
        if instance is not None
        else ObjectIdentifier(WILDCARD_DEVICE)
    )
    try:
        object_list = await app.read_property(address, device_id, "object-list")
    except (ErrorRejectAbortNack, TimeoutError) as exc:
        raise ToolError(
            f"the object list of {address} could not be read: {exc}"
        ) from exc
    identifiers = [
        item if isinstance(item, ObjectIdentifier) else ObjectIdentifier(str(item))
        for item in object_list
    ]
    properties = ["object-name"] + (["present-value", "units"] if p.values else [])
    rows: dict[str, dict[str, Any]] = {
        str(oid): {"identifier": str(oid), "type": str(oid).split(",")[0], "name": None}
        for oid in identifiers
    }
    for start in range(0, len(identifiers), RPM_CHUNK):
        chunk = identifiers[start : start + RPM_CHUNK]
        parameters: list[Any] = []
        for oid in chunk:
            parameters += [oid, properties]
        results: list[tuple[Any, Any, Any, Any]]
        try:
            results = list(await app.read_property_multiple(address, parameters))
        except (ErrorRejectAbortNack, TimeoutError):
            results = []
            for oid in chunk:
                try:
                    name = await app.read_property(address, oid, "object-name")
                except (ErrorRejectAbortNack, TimeoutError):
                    continue
                results.append((oid, "object-name", None, name))
        for oid, prop, _, value in results:
            row = rows.get(str(oid))
            if row is None or isinstance(value, ErrorType):
                continue
            key = str(prop)
            if key == "object-name":
                row["name"] = str(value)
            elif key == "present-value":
                classified = classify(row["type"], "present-value", value)
                row["value"] = (
                    json_value(classified[1]) if classified is not None else raw(value)
                )
            elif key == "units":
                row["unit"] = str(value)
                row["qudt"] = protocol_unit(value)
    return list(rows.values())


async def pointlist(
    app: Application,
    settings: BACnetIPConfig,
    instance: str | None,
    p: PointListParameters,
) -> dict[str, Any]:
    """The devices in scope and the objects on each, as one document.

    One request at a time, device after device: a whole site takes a while and
    never puts more on the network at once than a single poll round does, which
    is why this tool needs no ceiling of its own.
    """
    found = await _pointlist_devices(app, settings, p)
    devices: list[dict[str, Any]] = []
    for record in found:
        entry: dict[str, Any] = dict(record)
        # By address, which the Who-Is already gave: asking by instance number
        # would mean a second broadcast, and would fail on an interface that
        # cannot broadcast at all.
        target = record.get("address") or record.get("instance")
        try:
            listed = await objects(
                app, settings, ObjectsParameters(device=str(target), values=p.values)
            )
        except Exception as exc:  # a device that stops answering halfway
            entry["objects"] = []
            entry["error"] = error_text(exc)
        else:
            entry["objects"] = listed
        devices.append(entry)
    return {
        "instance": instance,
        "address": f"{settings.address}",
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "counts": {
            "devices": len(devices),
            "objects": sum(len(entry["objects"]) for entry in devices),
        },
        "devices": devices,
    }


async def _pointlist_devices(
    app: Application, settings: BACnetIPConfig, p: PointListParameters
) -> list[dict[str, Any]]:
    """The devices the list covers: one named, or those the instance claims."""
    if p.device is not None:
        number, address = await resolve_device(app, settings, p.device)
        asked = DiscoverParameters(
            low=number if number is not None else 0,
            high=number if number is not None else MAX_DEVICE_INSTANCE,
            duration=p.duration,
            address=None if number is not None else str(address),
        )
        return await discover(app, settings, asked)
    low, high = _scope_bounds(settings, p.low, p.high)
    found = await discover(
        app, settings, DiscoverParameters(low=low, high=high, duration=p.duration)
    )
    return [
        record
        for record in found
        if record.get("instance") is None
        or settings.claims_device(int(record["instance"]), _host(record.get("address")))
    ]


def _scope_bounds(
    settings: BACnetIPConfig, low: int | None, high: int | None
) -> tuple[int, int]:
    """The band the Who-Is asks for: what was given, else the instance's scope."""
    if settings.devices:
        scope_low = min(bound[0] for bound in settings.devices)
        scope_high = max(bound[1] for bound in settings.devices)
    else:
        scope_low, scope_high = 0, MAX_DEVICE_INSTANCE
    return (scope_low if low is None else low, scope_high if high is None else high)


def _host(address: object) -> str | None:
    """The IP of a BACnet address, without its port."""
    if not isinstance(address, str) or not address:
        return None
    return address.split(":", 1)[0]


async def resolve(
    app: Application,
    settings: BACnetIPConfig,
    graph: GraphReader | None,
    p: ResolveParameters,
) -> dict[str, Any]:
    if graph is None:
        raise ToolError("resolve needs the running daemon's working graph")
    try:
        uri = expand(p.point, graph.prefixes)
    except UnknownPrefix:
        uri = p.point
    resolved = resolve_references(graph)
    result: dict[str, Any] = {"point": uri}
    reference = resolved.references.get(uri)
    if reference is None:
        problem = resolved.problems.get(uri)
        result["problem"] = (
            problem.reason
            if problem is not None
            else "the point has no BACnet reference"
        )
        if problem is not None:
            result["device"] = problem.device_instance
        return result
    result.update(
        {
            "device": reference.device_instance,
            "device_ip": reference.device_ip,
            "object": reference.object_identifier,
            "property": reference.property_name,
            "in_scope": settings.claims_device(
                reference.device_instance, reference.device_ip
            ),
        }
    )
    address = await locate(
        app,
        settings,
        reference.device_instance,
        reference.device_ip,
        settings.timeout.total_seconds(),
    )
    if address is None:
        result["address"] = None
        result["error"] = "the device did not answer a Who-Is"
        return result
    result["address"] = str(address)
    objid = ObjectIdentifier(reference.object_identifier)
    try:
        value = await app.read_property(address, objid, reference.property_name)
    except ErrorPDU as exc:
        result["error"] = error_text(exc)
        return result
    except (AbortPDU, RejectPDU, TimeoutError) as exc:
        result["error"] = f"no value: {exc or 'timeout'}"
        return result
    result.update(describe_value(reference.object_type, reference.property_name, value))
    result["status_flags"] = await read_flags(app, address, objid)
    return result


# --- rendering ---------------------------------------------------------------


def error_text(exc: BaseException) -> str:
    error_class = getattr(exc, "errorClass", None)
    error_code = getattr(exc, "errorCode", None)
    if error_class is not None and error_code is not None:
        return f"{error_class}: {error_code}"
    return str(exc) or type(exc).__name__


def describe_value(object_type: str, property_name: str, value: Any) -> dict[str, Any]:
    """The value as the vocabulary sees it, beside BACnet's own datatype."""
    data: dict[str, Any] = {"datatype": type(value).__name__}
    classified = classify(object_type, property_name, value)
    if classified is not None:
        value_type, python_value = classified
        data["type"] = value_type
        data["value"] = json_value(python_value)
    else:
        data["type"] = None
        data["value"] = raw(value)
    if isinstance(value, Enumerated):
        data["text"] = str(value)
    return data


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def raw(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return [raw(item) for item in value]
    if isinstance(value, Enumerated):
        return str(value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


async def read_flags(
    app: Application, address: Address, objid: ObjectIdentifier
) -> dict[str, bool] | None:
    try:
        flags = await app.read_property(address, objid, STATUS_FLAGS)
    except (ErrorRejectAbortNack, Exception):
        return None
    try:
        bits = [bool(bit) for bit in list(flags)]
    except TypeError:
        return None
    if len(bits) < 4:
        return None
    return dict(zip(FLAG_NAMES, bits[:4], strict=False))
