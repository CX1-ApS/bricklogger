"""The BACnet/IP source's declaration: what the daemon and the CLI know about it
before an instance exists."""

from __future__ import annotations

from bricklogger.plugins.bacnet_ip.config import (
    DEFAULT_PORT,
    DEFAULT_VENDOR_ID,
    MAX_DEVICE_INSTANCE,
    BACnetIPConfig,
    BBMDSettings,
    DeviceRange,
    parse_address,
)
from bricklogger.plugins.bacnet_ip.source import BACnetIPSource
from bricklogger.plugins.bacnet_ip.tools import (
    DiscoverParameters,
    ObjectsParameters,
    PointListParameters,
    ReadParameters,
    ResolveParameters,
)
from bricklogger.sdk.declaration import (
    SourceDeclaration,
    ToolDeclaration,
    ToolOffer,
)

SOURCE = SourceDeclaration(
    type_name="bacnet-ip",
    description="BACnet/IP over UDP: polling with ReadPropertyMultiple, "
    "devices found by instance with Who-Is.",
    config_schema=BACnetIPConfig,
    reference_types=("ref:BACnetReference",),
    tools=(
        ToolDeclaration(
            "discover",
            "Find the devices that answer a Who-Is: instance, address, name, "
            "vendor and model.",
            DiscoverParameters,
        ),
        ToolDeclaration(
            "read",
            "Read one property of one object, with its datatype and status flags.",
            ReadParameters,
        ),
        ToolDeclaration(
            "objects",
            "List a device's objects: identifier, name and type, and with "
            "--values their Present_Value and Units.",
            ObjectsParameters,
        ),
        ToolDeclaration(
            "resolve",
            "Show how a point's reference resolves, and what it reads.",
            ResolveParameters,
        ),
        ToolDeclaration(
            "pointlist",
            "The point list: the devices the instance claims, or one device, "
            "with the objects on each, as one JSON document to keep as a file.",
            PointListParameters,
            document=True,
            offered_on=ToolOffer("discover", {"device": "instance"}),
        ),
    ),
    factory=BACnetIPSource,
)

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_VENDOR_ID",
    "MAX_DEVICE_INSTANCE",
    "SOURCE",
    "BACnetIPConfig",
    "BACnetIPSource",
    "BBMDSettings",
    "DeviceRange",
    "parse_address",
]
