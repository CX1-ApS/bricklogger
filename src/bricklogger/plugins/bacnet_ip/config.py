"""The BACnet/IP instance configuration.

``docs/features/sources.md`` defines the keys: ``address`` and
``device_instance`` are required, ``devices`` and ``subnet`` narrow the claim
scope, ``bbmd`` registers the instance as a foreign device, and ``timeout``
and ``retries`` are the only communication settings.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import timedelta
from ipaddress import IPv4Network
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from bricklogger.config.values import Duration

MAX_DEVICE_INSTANCE = 4_194_302
"""The largest device instance number BACnet allows; 4194303 means unassigned."""

DEFAULT_PORT = 47808
DEFAULT_VENDOR_ID = 999
"""The vendor identifier of the instance's own device object until one is assigned."""

_ADDRESS = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})(?::(\d{1,5}))?$")
_RANGE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_HOST_PORT = re.compile(r"^([A-Za-z0-9.-]+)(?::(\d{1,5}))?$")


def parse_address(text: str) -> tuple[str, int, int]:
    """Parse ``192.168.10.5/24`` or ``192.168.10.5/24:47809`` to (ip, prefix, port)."""
    match = _ADDRESS.match(text.strip())
    if match is None:
        raise ValueError(
            "an address is an IPv4 address with prefix length, optionally a port: "
            "192.168.10.5/24 or 192.168.10.5/24:47809"
        )
    ip_text, prefix_text, port_text = match.groups()
    try:
        interface = ipaddress.IPv4Interface(f"{ip_text}/{prefix_text}")
    except ValueError as exc:
        raise ValueError(f"not a valid IPv4 address with prefix length: {exc}") from exc
    port = int(port_text) if port_text else DEFAULT_PORT
    if not 1 <= port <= 65535:
        raise ValueError("the port must be between 1 and 65535")
    return str(interface.ip), interface.network.prefixlen, port


def _coerce_range(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("a device range is a number or 'low-high'")
    if isinstance(value, int):
        return (value, value)
    if isinstance(value, str):
        match = _RANGE.match(value)
        if match is None:
            if value.strip().isdigit():
                number = int(value)
                return (number, number)
            raise ValueError("a device range is a number or 'low-high', e.g. 1000-1999")
        low, high = int(match.group(1)), int(match.group(2))
        if low > high:
            raise ValueError("a device range must run from low to high")
        return (low, high)
    if isinstance(value, tuple) and len(value) == 2:
        return value
    raise ValueError("a device range is a number or 'low-high'")


def _format_range(value: tuple[int, int]) -> str | int:
    low, high = value
    return low if low == high else f"{low}-{high}"


DeviceRange = Annotated[
    tuple[int, int],
    BeforeValidator(_coerce_range),
    PlainSerializer(_format_range, return_type=str | int, when_used="json"),
]
"""One device instance or an inclusive range, written ``2500`` or ``"1000-1999"``."""


class BBMDSettings(BaseModel):
    """Foreign-device registration with a BBMD."""

    model_config = ConfigDict(extra="forbid")

    address: str = Field(
        description="The BBMD to register with, as a host or IP, optionally with :port"
    )
    ttl: Duration = Field(
        default=timedelta(seconds=60),
        description="How often the registration is renewed",
    )

    @field_validator("address")
    @classmethod
    def _host_and_optional_port(cls, value: str) -> str:
        if _HOST_PORT.match(value.strip()) is None:
            raise ValueError("the BBMD address is a host or IP, optionally with :port")
        return value.strip()


class BACnetIPConfig(BaseModel):
    """One ``bacnet-ip`` instance in ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    address: str = Field(
        description="The local IPv4 address with prefix length, e.g. "
        "192.168.10.5/24; a port other than 47808 is appended as :47809"
    )
    device_instance: int = Field(
        ge=0,
        le=MAX_DEVICE_INSTANCE,
        description="The number of the instance's own device object, unique "
        "on the BACnet network",
    )
    device_name: str | None = Field(
        default=None,
        description="The name of the instance's own device object; default "
        "the instance name",
    )
    vendor_id: int = Field(
        default=DEFAULT_VENDOR_ID,
        ge=0,
        le=65535,
        description="The vendor identifier the instance's own device object "
        "reports; 999 until one is assigned",
    )
    devices: list[DeviceRange] = Field(
        default_factory=list,
        description="The device instances the instance claims: numbers and "
        'ranges written "low-high"; all when empty',
    )
    subnet: IPv4Network | None = Field(
        default=None,
        description="Claim only devices whose IP in the graph lies in this subnet",
    )
    bbmd: BBMDSettings | None = Field(
        default=None,
        description="A BBMD to register with as a foreign device, for "
        "discovery across routers",
    )
    timeout: Duration = Field(
        default=timedelta(seconds=3),
        description="How long a request waits for an answer before a retry",
    )
    retries: int = Field(
        default=2,
        ge=0,
        description="Retries per request before the read counts as failed",
    )
    max_in_flight: int = Field(
        default=8,
        ge=1,
        description="How many requests may be on the wire at once, across "
        "every device and every search",
    )

    @field_validator("address")
    @classmethod
    def _valid_address(cls, value: str) -> str:
        parse_address(value)
        return value.strip()

    @field_validator("devices")
    @classmethod
    def _ranges_within_bacnet(
        cls, value: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        for low, high in value:
            if low < 0 or high > MAX_DEVICE_INSTANCE:
                raise ValueError(
                    f"device instances run from 0 to {MAX_DEVICE_INSTANCE}"
                )
        return value

    @property
    def local_ip(self) -> str:
        return parse_address(self.address)[0]

    @property
    def prefix_length(self) -> int:
        return parse_address(self.address)[1]

    @property
    def port(self) -> int:
        return parse_address(self.address)[2]

    def claims_device(self, instance: int, ip: str | None) -> bool:
        """Whether a device is in scope: every restriction given must hold."""
        if self.devices and not any(
            low <= instance <= high for low, high in self.devices
        ):
            return False
        if self.subnet is not None:
            if ip is None:
                return False
            try:
                if ipaddress.IPv4Address(ip) not in self.subnet:
                    return False
            except ValueError:
                return False
        return True
