"""From what BACnet delivers to the closed value-type vocabulary. The object
types and the unit map are the SDK's, in ``bricklogger.sdk.bacnet``. See
``docs/features/sources.md``, "Value types and units"."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from bacpypes3.basetypes import DateTime
from bacpypes3.primitivedata import (
    Boolean,
    CharacterString,
    Double,
    Enumerated,
    Integer,
    Real,
    Unsigned,
)

from bricklogger.sdk.bacnet import (
    ANALOG_TYPES,
    BINARY_TYPES,
    DATETIME_TYPES,
    INTEGER_TYPES,
    MULTISTATE_TYPES,
    QUDT,
    STRING_TYPES,
    UNIT_MAP,
    UNSUPPORTED,
    protocol_unit,
)
from bricklogger.sdk.contract import ValueType

PRESENT_VALUE = "present-value"

__all__ = [
    "ANALOG_TYPES",
    "BINARY_TYPES",
    "DATETIME_TYPES",
    "INTEGER_TYPES",
    "MULTISTATE_TYPES",
    "PRESENT_VALUE",
    "QUDT",
    "STRING_TYPES",
    "UNIT_MAP",
    "UNSUPPORTED",
    "classify",
    "enumeration_texts",
    "protocol_unit",
]


def classify(
    object_type: str, property_name: str, value: Any
) -> tuple[ValueType, Any] | None:
    """The value type and Python value of a read; ``None`` when it cannot be logged.

    Present_Value follows the object type; any other property follows the
    datatype as read, with BACnet's own enumeration values as the ordinals.
    """
    if isinstance(value, DateTime):
        return "datetime", _datetime(value)
    if property_name == PRESENT_VALUE:
        if object_type in ANALOG_TYPES:
            return _number(value)
        if object_type in BINARY_TYPES:
            return "boolean", int(value) == 1
        if object_type in MULTISTATE_TYPES:
            return "enum", int(value)
        if object_type in INTEGER_TYPES:
            return "integer", int(value)
        if object_type in STRING_TYPES:
            return "string", str(value)
    if isinstance(value, Boolean):
        return "boolean", bool(int(value))
    if isinstance(value, Enumerated):
        return "enum", int(value)
    if isinstance(value, Real | Double):
        return _number(value)
    if isinstance(value, Unsigned | Integer):
        return "integer", int(value)
    if isinstance(value, CharacterString):
        return "string", str(value)
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, int):
        return "integer", value
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return "string", value
    return None


def _number(value: Any) -> tuple[ValueType, Any] | None:
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return "null", None
    return "number", number


def _datetime(value: DateTime) -> datetime:
    """A BACnet DateTime carries no zone: read as the machine's local time, in UTC."""
    year, month, day, _ = value.date
    hour, minute, second, hundredths = value.time
    local = datetime(1900 + year, month, day, hour, minute, second, hundredths * 10_000)
    return local.astimezone().astimezone(UTC)


def enumeration_texts(value: Any) -> dict[int, str] | None:
    """The standard's names for an enumerated property, as ``ordinal: text``."""
    enumeration_class = type(value)
    enumeration = getattr(enumeration_class, "_enum_map", None)
    if not isinstance(enumeration, dict):
        return None
    texts: dict[int, str] = {}
    for name, number in enumeration.items():
        if not isinstance(number, int):
            continue
        try:
            texts[number] = str(enumeration_class(number))
        except Exception:
            texts[number] = str(name)
    return texts or None
