"""One instance in ``sources.yaml`` or ``destinations.yaml``, edited as text.

The two files are written by hand and carry comments — ``config init`` writes
them commented — so an instance is spliced into the text rather than the file
being dumped again from a parsed mapping: everything outside the block that
changes is left exactly as it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml


class InstanceNotFound(LookupError):
    """The file holds no instance of that name."""


def instances_in(text: str | None) -> dict[str, Any]:
    """The file's instances by name; empty for a missing or empty file."""
    if not text or not text.strip():
        return {}
    data = yaml.safe_load(text)
    return dict(data) if isinstance(data, dict) else {}


def set_instance(text: str | None, name: str, data: Mapping[str, Any]) -> str:
    """The text with the instance written: replaced where it stood, or appended."""
    block = yaml.safe_dump(
        {name: dict(data)},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    original = text or ""
    line = key_line(original, name)
    if line is None:
        head = original.rstrip("\n")
        return f"{head}\n\n{block}" if head else block
    lines = original.splitlines(keepends=True)
    start, end = block_span(lines, line)
    return "".join([*lines[:start], block, *lines[end:]])


def remove_instance(text: str | None, name: str) -> str:
    """The text without the instance; raises when it is not there."""
    original = text or ""
    line = key_line(original, name)
    if line is None:
        raise InstanceNotFound(name)
    lines = original.splitlines(keepends=True)
    start, end = block_span(lines, line)
    return "".join([*lines[:start], *lines[end:]])


def key_line(text: str, name: str) -> int | None:
    """The line a top-level key stands on, counted from zero."""
    if not text.strip():
        return None
    node = yaml.compose(text)
    if not isinstance(node, yaml.MappingNode):
        return None
    for key, _value in node.value:
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            return int(key.start_mark.line)
    return None


def block_span(lines: list[str], start: int) -> tuple[int, int]:
    """The block's lines, from its key to the last line that belongs to it.

    A line belongs to the block while it is blank or indented deeper than the
    key, so a comment at the key's own indentation begins what follows and
    stays out of the block. Trailing blank lines are left to the file.
    """
    base = indent_of(lines[start])
    end = last = start + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and indent_of(line) <= base:
            break
        end += 1
        if line.strip():
            last = end
    return start, last


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())
