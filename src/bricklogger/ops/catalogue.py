"""The catalogue: the installed plugins, one plugin's declaration, and the
shape of each configuration file as JSON Schema."""

from __future__ import annotations

from typing import Any

from bricklogger import __version__
from bricklogger.config.schema import (
    BatchSettings,
    DaemonSettings,
    Rule,
    SpoolSettings,
)
from bricklogger.daemon.plugins import describe_plugin
from bricklogger.ops.client import DaemonApi
from bricklogger.ops.context import Operations
from bricklogger.ops.errors import OperationError
from bricklogger.sdk.declaration import DestinationDeclaration, SourceDeclaration

FILE_TITLES = {
    "daemon": "daemon.yaml: the daemon's own settings and the bindings of the web "
    "interface and the MCP server",
    "sources": "sources.yaml: source instances, the name as the key and `type` "
    "picking the plugin",
    "destinations": "destinations.yaml: destination instances, the name as the "
    "key and `type` picking the plugin; `spool` and `batch` are the daemon's",
    "rules": "rules.yaml: an ordered list of rules, evaluated top down; the "
    "first match wins, and points no rule matches are not logged",
}


def list_plugins(ops: Operations, client: DaemonApi | None) -> list[dict[str, Any]]:
    """Every installed plugin: type, role, version, description and instances."""
    if client is not None:
        return [dict(row) for row in client.get("/v1/plugins")]
    registry = ops.plugins()
    declarations: list[tuple[str, SourceDeclaration | DestinationDeclaration]] = [
        *registry.sources.items(),
        *registry.destinations.items(),
    ]
    rows: list[dict[str, Any]] = [
        {
            "type": name,
            "role": declaration.role,
            "version": registry.version_of(name) or __version__,
            "description": declaration.description,
            "instances": None,
            "error": None,
        }
        for name, declaration in declarations
    ]
    rows += [
        {
            "type": name,
            "role": failure.role,
            "version": failure.version or None,
            "description": None,
            "instances": None,
            "error": failure.error,
        }
        for name, failure in registry.failures.items()
    ]
    rows.sort(key=lambda row: str(row["type"]))
    return rows


def describe(
    ops: Operations, client: DaemonApi | None, type_name: str
) -> dict[str, Any]:
    """One plugin's declaration, from the daemon or from what is installed."""
    if client is not None:
        answer: dict[str, Any] = client.get(f"/v1/plugins/{type_name}")
        return answer
    try:
        return describe_plugin(ops.plugins(), type_name, instances=None)
    except KeyError as exc:
        raise OperationError(f"unknown plugin type {type_name!r}") from exc


def file_schema(ops: Operations, client: DaemonApi | None, name: str) -> dict[str, Any]:
    """The JSON Schema of one file: ``daemon`` and ``rules`` from their models,
    the two instance files with one alternative per installed type."""
    if name == "daemon":
        schema = DaemonSettings.model_json_schema()
        schema["title"] = FILE_TITLES[name]
        return schema
    if name == "rules":
        item = Rule.model_json_schema()
        rule_definitions = item.pop("$defs", {})
        return {
            "title": FILE_TITLES[name],
            "type": "array",
            "items": item,
            "$defs": rule_definitions,
        }
    if name not in ("sources", "destinations"):
        raise OperationError(
            f"unknown configuration file {name!r}; one of daemon, sources, "
            "destinations or rules"
        )
    role = "source" if name == "sources" else "destination"
    alternatives: list[dict[str, Any]] = []
    definitions: dict[str, Any] = {}
    for row in list_plugins(ops, client):
        if row["role"] != role or row.get("error"):
            continue  # a plugin that could not be loaded has no schema to offer
        declaration = describe(ops, client, str(row["type"]))
        schema = dict(declaration.get("config_schema") or {})
        definitions.update(schema.pop("$defs", {}))
        properties: dict[str, Any] = {
            "type": {"const": row["type"], "description": "The plugin type"},
            **(schema.get("properties") or {}),
        }
        if role == "destination":
            for key, model in (("spool", SpoolSettings), ("batch", BatchSettings)):
                reserved = model.model_json_schema()
                definitions.update(reserved.pop("$defs", {}))
                properties[key] = reserved
        alternatives.append(
            {
                "title": row["type"],
                "description": row.get("description") or "",
                "type": "object",
                "properties": properties,
                "required": ["type", *(schema.get("required") or [])],
                "additionalProperties": False,
            }
        )
    return {
        "title": FILE_TITLES[name],
        "type": "object",
        "additionalProperties": {"oneOf": alternatives},
        "$defs": definitions,
    }
