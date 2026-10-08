"""The API's view of the installed plugins: the list, one declaration with its
schemas as JSON Schema, and running a tool without a daemon.

See ``docs/features/api.md``, "Plugins", and ``docs/architecture.md``,
"Protocol tools".
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from bricklogger import __version__
from bricklogger.config import Configuration, validate_configuration
from bricklogger.model.prefixes import WELL_KNOWN, declared_prefixes
from bricklogger.model.versions import ModelStore
from bricklogger.sdk.declaration import (
    DestinationDeclaration,
    SourceDeclaration,
    ToolDeclaration,
)
from bricklogger.sdk.registry import PluginRegistry


def summarize_plugins(
    registry: PluginRegistry, configuration: Configuration | None
) -> list[dict[str, Any]]:
    """Every installed plugin: type, role, version, description and instances."""
    rows: list[dict[str, Any]] = []
    declarations: list[tuple[str, SourceDeclaration | DestinationDeclaration]] = sorted(
        [*registry.sources.items(), *registry.destinations.items()],
        key=lambda item: item[0],
    )
    for name, declaration in declarations:
        rows.append(
            {
                "type": name,
                "role": declaration.role,
                "version": registry.version_of(name) or __version__,
                "description": declaration.description,
                "instances": instances_of(configuration, name),
                "error": None,
            }
        )
    for name, failure in registry.failures.items():
        rows.append(
            {
                "type": name,
                "role": failure.role,
                "version": failure.version or None,
                "description": None,
                "instances": instances_of(configuration, name),
                "error": failure.error,
            }
        )
    rows.sort(key=lambda row: str(row["type"]))
    return rows


def instances_of(configuration: Configuration | None, type_name: str) -> list[str]:
    if configuration is None:
        return []
    return sorted(
        [n for n, i in configuration.sources.items() if i.type == type_name]
        + [n for n, i in configuration.destinations.items() if i.type == type_name]
    )


def describe_plugin(
    registry: PluginRegistry, type_name: str, instances: list[str] | None
) -> dict[str, Any]:
    """One declaration, schemas as JSON Schema; raises KeyError for an unknown type.

    A plugin that could not be loaded answers with what its entry point says
    and the error, without schemas.
    """
    failure = registry.failure_of(type_name)
    if failure is not None:
        return {
            "type": type_name,
            "role": failure.role,
            "version": failure.version or None,
            "description": None,
            "instances": instances,
            "error": failure.error,
        }
    source = registry.sources.get(type_name)
    destination = registry.destinations.get(type_name)
    declaration = source or destination
    if declaration is None:
        raise KeyError(type_name)
    data: dict[str, Any] = {
        "type": type_name,
        "role": declaration.role,
        "version": registry.version_of(type_name) or __version__,
        "description": declaration.description,
        "config_schema": declaration.config_schema.model_json_schema(),
        "instances": instances,
        "error": None,
    }
    if source is not None:
        data["reference_types"] = list(source.reference_types)
        data["vocabulary"] = (
            {
                "prefix": source.vocabulary.prefix,
                "namespace": source.vocabulary.namespace,
            }
            if source.vocabulary is not None
            else None
        )
        data["supports_poll"] = source.supports_poll
        data["methods"] = [
            {
                "name": method.name,
                "description": method.description,
                "parameters": (
                    method.parameters.model_json_schema()
                    if method.parameters is not None
                    else None
                ),
            }
            for method in source.methods
        ]
        data["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "document": tool.document,
                "offered_on": (
                    {
                        "tool": tool.offered_on.tool,
                        "parameters": dict(tool.offered_on.parameters),
                    }
                    if tool.offered_on is not None
                    else None
                ),
                "parameters": tool.parameters.model_json_schema(),
            }
            for tool in source.tools
        ]
    elif destination is not None:
        data["stores_metadata"] = destination.stores_metadata
        data["stores_model"] = destination.stores_model
    return data


def tool_of(declaration: SourceDeclaration, tool: str) -> ToolDeclaration:
    """The declared tool by name; raises KeyError when the source has none."""
    for candidate in declaration.tools:
        if candidate.name == tool:
            return candidate
    raise KeyError(tool)


def run_tool_offline(
    config_dir: Path,
    registry: PluginRegistry,
    type_name: str,
    instance: str | None,
    tool: str,
    parameters: Mapping[str, Any],
) -> Any:
    """Bind a configured instance and run one of its tools without a daemon.

    Raises KeyError for an unknown type or tool, LookupError when the instance
    cannot be chosen, RuntimeError for an invalid configuration, and pydantic's
    ValidationError for parameters the tool does not accept.
    """
    from bricklogger.daemon.core import GraphAccess

    declaration = registry.sources.get(type_name)
    if declaration is None:
        raise KeyError(f"unknown source type {type_name!r}")
    tool_declaration = tool_of(declaration, tool)
    result = validate_configuration(config_dir, registry)
    if result.configuration is None:
        raise RuntimeError(
            "the configuration is invalid: "
            + "; ".join(str(issue) for issue in result.errors)
        )
    candidates = {
        name: item
        for name, item in result.configuration.sources.items()
        if item.type == type_name
    }
    if instance is None:
        if len(candidates) != 1:
            names = ", ".join(sorted(candidates)) or "none"
            raise LookupError(
                f"{type_name!r} has {len(candidates)} instances ({names}); "
                "give --instance"
            )
        instance = next(iter(candidates))
    elif instance not in candidates:
        raise LookupError(f"no instance {instance!r} of {type_name!r} is configured")
    config = declaration.config_schema.model_validate(candidates[instance].settings)
    params = tool_declaration.parameters.model_validate(dict(parameters))
    from bricklogger.model.inference import parse_model
    from bricklogger.model.working_graph import GRAPH_DIR, WorkingGraph

    data_dir = result.configuration.daemon.data_dir
    prefixes: dict[str, str] = {**WELL_KNOWN, **registry.prefixes()}
    store = ModelStore(data_dir)
    active = store.active()
    if active is not None:
        version = store.get(active)
        prefixes.update(
            declared_prefixes(parse_model(store.read(active), version.format))
        )
    graph = WorkingGraph(data_dir / GRAPH_DIR)
    source = declaration.create(instance, config, GraphAccess(graph, prefixes))
    return source.run_tool(tool, params.model_dump())
