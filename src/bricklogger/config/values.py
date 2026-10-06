"""Durations, sizes and times of day as the configuration writes them.

Durations are ``30s``, ``5m``, ``1h`` or ``7d``; sizes are ``500MB`` or ``1GB``,
with binary multiples (1 KB = 1024 B); a time of day is ``"07:00"``, quoted,
because YAML would otherwise read it as a number. All three are annotated
types that pydantic parses on input and renders back in the same form on JSON
output.
"""

from __future__ import annotations

import re
from datetime import time, timedelta
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

DURATION_HELP = "a duration is a number with a unit: 30s, 5m, 1h, 7d"
SIZE_HELP = "a size is a number with a unit: 500MB, 1GB"
TIME_HELP = 'a time of day is HH:MM in quotes: "07:00"'
TIME_QUOTE_HELP = "YAML read this as a number; a time of day is quoted: '07:00'"

_DURATION = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$")
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SIZE = re.compile(r"^\s*(\d+)\s*(B|KB|MB|GB|TB)\s*$", re.IGNORECASE)
_SIZE_BYTES = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
_TIME_OF_DAY = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_duration(text: str) -> timedelta:
    """Parse ``30s``, ``5m``, ``1h`` or ``7d``."""
    match = _DURATION.match(text)
    if match is None:
        raise ValueError(DURATION_HELP)
    return timedelta(seconds=int(match.group(1)) * _DURATION_SECONDS[match.group(2)])


def format_duration(value: timedelta) -> str:
    """Render a duration in the largest unit that divides it exactly."""
    seconds = int(value.total_seconds())
    for unit, factor in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds and seconds % factor == 0:
            return f"{seconds // factor}{unit}"
    return f"{seconds}s"


def parse_size(text: str) -> int:
    """Parse ``500MB`` or ``1GB`` into bytes, with binary multiples."""
    match = _SIZE.match(text)
    if match is None:
        raise ValueError(SIZE_HELP)
    return int(match.group(1)) * _SIZE_BYTES[match.group(2).upper()]


def format_size(value: int) -> str:
    """Render a size in the largest unit that divides it exactly."""
    for unit, factor in (
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    ):
        if value and value % factor == 0:
            return f"{value // factor}{unit}"
    return f"{value}B"


def parse_time_of_day(text: str) -> time:
    """Parse ``07:00`` into a time of day."""
    match = _TIME_OF_DAY.match(text)
    if match is None:
        raise ValueError(TIME_HELP)
    return time(int(match.group(1)), int(match.group(2)))


def format_time_of_day(value: time) -> str:
    """Render a time of day as ``HH:MM``."""
    return f"{value.hour:02d}:{value.minute:02d}"


def _coerce_duration(value: Any) -> Any:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        return parse_duration(value)
    raise ValueError(DURATION_HELP)


def _coerce_size(value: Any) -> Any:
    if isinstance(value, str):
        return parse_size(value)
    raise ValueError(SIZE_HELP)


def _coerce_time_of_day(value: Any) -> Any:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        return parse_time_of_day(value)
    if isinstance(value, int):
        # PyYAML reads an unquoted 07:00 as sexagesimal: 7 * 60 + 0.
        raise ValueError(TIME_QUOTE_HELP)
    raise ValueError(TIME_HELP)


Duration = Annotated[
    timedelta,
    BeforeValidator(_coerce_duration),
    PlainSerializer(format_duration, return_type=str, when_used="json"),
]
"""A duration written as a number with a unit, held as a ``timedelta``."""

Size = Annotated[
    int,
    BeforeValidator(_coerce_size),
    PlainSerializer(format_size, return_type=str, when_used="json"),
]
"""A size written as a number with a unit, held as a number of bytes."""

TimeOfDay = Annotated[
    time,
    BeforeValidator(_coerce_time_of_day),
    PlainSerializer(format_time_of_day, return_type=str, when_used="json"),
]
"""A time of day written as ``"07:00"``, held as a ``time``."""
