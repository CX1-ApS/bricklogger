"""BACnet's object types and engineering units, for a source whose system
exposes BACnet objects — on site, as the BACnet/IP source reads them, or
through a cloud service that carries the same objects. The object types are
grouped by the value type their present value maps to, and the units are
mapped to Brick's unit vocabulary (QUDT). See ``docs/features/plugins.md``,
"The SDK"."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ANALOG_TYPES",
    "BINARY_TYPES",
    "DATETIME_TYPES",
    "INTEGER_TYPES",
    "MULTISTATE_TYPES",
    "QUDT",
    "STRING_TYPES",
    "UNIT_MAP",
    "UNSUPPORTED",
    "protocol_unit",
]

ANALOG_TYPES = frozenset(
    {"analog-input", "analog-output", "analog-value", "large-analog-value", "loop"}
)
BINARY_TYPES = frozenset(
    {"binary-input", "binary-output", "binary-value", "binary-lighting-output"}
)
MULTISTATE_TYPES = frozenset(
    {"multi-state-input", "multi-state-output", "multi-state-value"}
)
INTEGER_TYPES = frozenset(
    {"integer-value", "positive-integer-value", "accumulator", "pulse-converter"}
)
STRING_TYPES = frozenset({"character-string-value"})
DATETIME_TYPES = frozenset({"date-time-value"})

UNSUPPORTED = "the datatype has no counterpart in the value vocabulary"

QUDT = "http://qudt.org/vocab/unit/"
UNIT_MAP: Mapping[str, str] = {
    "degrees-celsius": "DEG_C",
    "degrees-fahrenheit": "DEG_F",
    "degrees-kelvin": "K",
    "percent": "PERCENT",
    "percent-relative-humidity": "PERCENT_RH",
    "pascals": "PA",
    "hectopascals": "HectoPA",
    "kilopascals": "KiloPA",
    "bars": "BAR",
    "millibars": "MilliBAR",
    "liters-per-second": "L-PER-SEC",
    "liters-per-minute": "L-PER-MIN",
    "liters-per-hour": "L-PER-HR",
    "cubic-meters-per-second": "M3-PER-SEC",
    "cubic-meters-per-hour": "M3-PER-HR",
    "cubic-feet-per-minute": "FT3-PER-MIN",
    "cubic-meters": "M3",
    "liters": "L",
    "watts": "W",
    "kilowatts": "KiloW",
    "megawatts": "MegaW",
    "watt-hours": "W-HR",
    "kilowatt-hours": "KiloW-HR",
    "megawatt-hours": "MegaW-HR",
    "joules": "J",
    "kilojoules": "KiloJ",
    "megajoules": "MegaJ",
    "gigajoules": "GigaJ",
    "volts": "V",
    "millivolts": "MilliV",
    "amperes": "A",
    "milliamperes": "MilliA",
    "hertz": "HZ",
    "parts-per-million": "PPM",
    "parts-per-billion": "PPB",
    "lux": "LUX",
    "meters-per-second": "M-PER-SEC",
    "kilometers-per-hour": "KiloM-PER-HR",
    "meters": "M",
    "millimeters": "MilliM",
    "centimeters": "CentiM",
    "square-meters": "M2",
    "kilograms": "KiloGM",
    "grams": "GM",
    "kilograms-per-hour": "KiloGM-PER-HR",
    "seconds": "SEC",
    "minutes": "MIN",
    "hours": "HR",
    "days": "DAY",
    "degrees-angular": "DEG",
    "revolutions-per-minute": "REV-PER-MIN",
    "watts-per-square-meter": "W-PER-M2",
    "power-factor": "UNITLESS",
    "milligrams-per-cubic-meter": "MilliGM-PER-M3",
    "micrograms-per-cubic-meter": "MicroGM-PER-M3",
}


def protocol_unit(units: Any) -> str | None:
    """The unit designation to deliver: QUDT where BACnet's unit has a counterpart."""
    if units is None:
        return None
    name = str(units)
    if name == "no-units":
        return None
    if name in UNIT_MAP:
        return QUDT + UNIT_MAP[name]
    return name
