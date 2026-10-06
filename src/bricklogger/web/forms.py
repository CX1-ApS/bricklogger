"""Forms from schemas: the fields of a tool's parameters as the API exposes them
in JSON Schema, and the way a submitted form becomes the tool's JSON body."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Field:
    """One input of a generated form."""

    name: str
    kind: str  # string, integer, number, boolean
    required: bool = False
    default: Any = None
    description: str = ""
    options: list[str] = field(default_factory=list)


def fields_of(schema: Mapping[str, Any]) -> list[Field]:
    """The inputs a JSON Schema object asks for, in the schema's order."""
    required = set(schema.get("required", []))
    fields: list[Field] = []
    for name, spec in (schema.get("properties") or {}).items():
        kind, options = _kind(spec, schema.get("$defs") or {})
        fields.append(
            Field(
                name=name,
                kind=kind,
                required=name in required,
                default=spec.get("default"),
                description=str(spec.get("description", "")),
                options=options,
            )
        )
    return fields


def _kind(
    spec: Mapping[str, Any], definitions: Mapping[str, Any]
) -> tuple[str, list[str]]:
    if "$ref" in spec:
        target = definitions.get(str(spec["$ref"]).rsplit("/", 1)[-1], {})
        return _kind(target, definitions)
    if "enum" in spec:
        return "string", [str(option) for option in spec["enum"]]
    kind = spec.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"]
        kind = kinds[0] if kinds else "string"
    if kind is None and "anyOf" in spec:
        for option in spec["anyOf"]:
            if option.get("type") not in (None, "null"):
                return _kind(option, definitions)
    if kind in ("integer", "number", "boolean", "string"):
        return str(kind), []
    return "string", []


def values_from_form(fields: list[Field], form: Mapping[str, Any]) -> dict[str, Any]:
    """The tool's JSON body from what the browser sent; empty inputs are left out."""
    values: dict[str, Any] = {}
    for item in fields:
        raw = form.get(item.name)
        if raw is None:
            continue
        text = str(raw).strip()
        if text == "":
            continue
        if item.kind == "boolean":
            values[item.name] = text.lower() in ("true", "1", "yes", "on")
        elif item.kind == "integer":
            try:
                values[item.name] = int(text)
            except ValueError:
                values[item.name] = text
        elif item.kind == "number":
            try:
                values[item.name] = float(text)
            except ValueError:
                values[item.name] = text
        else:
            values[item.name] = text
    return values


def cell(value: Any) -> str:
    """A tool result value as one table cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict | list):
        return json.dumps(value, default=str)
    return str(value)


def shape(result: Any) -> dict[str, Any]:
    """How a tool result is rendered: rows and columns, a mapping, or raw JSON."""
    if isinstance(result, list) and result and all(isinstance(r, dict) for r in result):
        columns: list[str] = []
        for row in result:
            for key in row:
                if key not in columns:
                    columns.append(str(key))
        return {"rows": result, "columns": columns, "mapping": None, "raw": None}
    if isinstance(result, list) and not result:
        return {"rows": [], "columns": [], "mapping": None, "raw": None}
    if isinstance(result, dict):
        return {"rows": None, "columns": [], "mapping": result, "raw": None}
    return {
        "rows": None,
        "columns": [],
        "mapping": None,
        "raw": json.dumps(result, indent=2, default=str),
    }


def document_filename(
    instance: str, tool: str, values: Mapping[str, Any], day: date
) -> str:
    """The name of a document tool's download: the instance, the tool, the
    values given for its parameters — a flag by its name — and the day."""
    parts = [instance, tool]
    for key, value in values.items():
        if value is None or value is False or value == "":
            continue
        parts.append(key if value is True else str(value))
    parts.append(day.isoformat())
    return UNSAFE.sub("_", "-".join(parts)) + ".json"


def offers_on(tools: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    """The document tools offered on the rows of ``tool``: for each, the tool's
    name, which parameter a row fills from which column, and whether the whole
    document can be had without a parameter."""
    offers: list[dict[str, Any]] = []
    for candidate in tools:
        offer = candidate.get("offered_on")
        if not candidate.get("document") or not offer or offer.get("tool") != tool:
            continue
        schema = candidate.get("parameters") or {}
        offers.append(
            {
                "tool": candidate["name"],
                "columns": dict(offer.get("parameters") or {}),
                "whole": not schema.get("required"),
            }
        )
    return offers
