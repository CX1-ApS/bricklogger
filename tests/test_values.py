"""Durations, sizes and times of day as the configuration writes them."""

from datetime import time, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from bricklogger.config.values import (
    Duration,
    Size,
    TimeOfDay,
    format_duration,
    format_size,
    format_time_of_day,
    parse_duration,
    parse_size,
    parse_time_of_day,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("7d", timedelta(days=7)),
        (" 10 s ", timedelta(seconds=10)),
    ],
)
def test_durations_parse(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "5", "5 minutes", "1.5h", "-5m", "5ms", "1w"])
def test_durations_without_a_unit_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="number with a unit"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (timedelta(seconds=90), "90s"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=36), "36h"),
        (timedelta(days=7), "7d"),
        (timedelta(0), "0s"),
    ],
)
def test_durations_format_in_the_largest_exact_unit(
    value: timedelta, text: str
) -> None:
    assert format_duration(value) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [("500MB", 500 * 1024**2), ("1GB", 1024**3), ("10mb", 10 * 1024**2), ("1B", 1)],
)
def test_sizes_parse_with_binary_multiples(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "500", "1.5GB", "1 gigabyte", "1G"])
def test_sizes_without_a_unit_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="number with a unit"):
        parse_size(text)


def test_sizes_format_in_the_largest_exact_unit() -> None:
    assert format_size(1024**3) == "1GB"
    assert format_size(1536 * 1024) == "1536KB"
    assert format_size(7) == "7B"


class _Model(BaseModel):
    interval: Duration
    max_size: Size


def test_annotated_types_parse_and_serialise() -> None:
    model = _Model.model_validate({"interval": "5m", "max_size": "1GB"})
    assert model.interval == timedelta(minutes=5)
    assert model.max_size == 1024**3
    assert model.model_dump(mode="json") == {"interval": "5m", "max_size": "1GB"}


def test_annotated_types_reject_bare_numbers() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _Model.model_validate({"interval": 300, "max_size": 1024})
    messages = [error["msg"] for error in excinfo.value.errors()]
    assert any("30s, 5m, 1h, 7d" in message for message in messages)
    assert any("500MB, 1GB" in message for message in messages)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07:00", time(7, 0)),
        ("7:00", time(7, 0)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        (" 17:30 ", time(17, 30)),
    ],
)
def test_times_of_day_parse(text: str, expected: time) -> None:
    assert parse_time_of_day(text) == expected


@pytest.mark.parametrize("text", ["", "7", "24:00", "07:60", "07:00:00", "7am"])
def test_impossible_times_of_day_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time_of_day(text)


def test_times_of_day_format_with_two_digits() -> None:
    assert format_time_of_day(time(7, 0)) == "07:00"
    assert format_time_of_day(time(23, 5)) == "23:05"


class _TimeModel(BaseModel):
    digest: TimeOfDay


def test_a_time_of_day_parses_and_serialises() -> None:
    model = _TimeModel.model_validate({"digest": "07:00"})
    assert model.digest == time(7, 0)
    assert model.model_dump(mode="json") == {"digest": "07:00"}


def test_an_unquoted_time_says_it_must_be_quoted() -> None:
    """PyYAML reads an unquoted 07:00 as sexagesimal, so 420 arrives here."""
    with pytest.raises(ValidationError) as excinfo:
        _TimeModel.model_validate({"digest": 420})
    assert any("quoted" in error["msg"] for error in excinfo.value.errors())
