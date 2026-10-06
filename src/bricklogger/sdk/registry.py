"""Discovery of the installed plugins through entry points.

Sources register in the group ``bricklogger.sources`` and destinations in
``bricklogger.destinations``; the entry point's name is the type name written
in the configuration, and it must load to a declaration of the group's role
carrying that same type name. A plugin that does not load is **recorded with
its error** rather than ending the discovery: the daemon and the CLI run on,
and the failure is shown where the operator looks. See
``docs/features/plugins.md``, "When a plugin cannot load".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import TypeVar

from bricklogger.sdk.declaration import (
    DestinationDeclaration,
    SourceDeclaration,
    Vocabulary,
)

SOURCES_GROUP = "bricklogger.sources"
DESTINATIONS_GROUP = "bricklogger.destinations"
BUILT_IN_DISTRIBUTION = "bricklogger"
"""The distribution the built-in plugins come from; it cannot be removed."""

RESERVED_TYPE_NAMES = frozenset({"all", "core", "status"})
"""Words of ``bricklogger update`` that therefore cannot name a type."""

Declaration = TypeVar("Declaration", SourceDeclaration, DestinationDeclaration)


class PluginError(Exception):
    """A plugin could not be loaded or is declared inconsistently."""


@dataclass(frozen=True)
class PluginFailure:
    """A plugin that could not be loaded: what its entry point says, and why."""

    type_name: str
    role: str
    version: str
    error: str
    distribution: str | None = None


@dataclass(frozen=True)
class PluginRegistry:
    """The declarations of the installed plugins, by type name, and the
    plugins that could not be loaded, by the type name their entry point
    promised."""

    sources: Mapping[str, SourceDeclaration] = field(default_factory=dict)
    destinations: Mapping[str, DestinationDeclaration] = field(default_factory=dict)
    versions: Mapping[str, str] = field(default_factory=dict)
    failures: Mapping[str, PluginFailure] = field(default_factory=dict)
    distributions: Mapping[str, str] = field(default_factory=dict)
    """Type name to the distribution that provides it, when known."""

    def version_of(self, type_name: str) -> str | None:
        """The version of the distribution a plugin came from, when known."""
        return self.versions.get(type_name) or None

    def failure_of(self, type_name: str) -> PluginFailure | None:
        """Why a plugin could not be loaded, or ``None`` for one that was."""
        return self.failures.get(type_name)

    def role_of(self, type_name: str) -> str | None:
        """``source`` or ``destination`` for any installed type, loaded or not."""
        if type_name in self.sources:
            return "source"
        if type_name in self.destinations:
            return "destination"
        failure = self.failures.get(type_name)
        return failure.role if failure is not None else None

    def distribution_of(self, type_name: str) -> str | None:
        """The distribution that provides a type, when the entry point says."""
        return self.distributions.get(type_name)

    def types_of(self, distribution: str) -> list[str]:
        """Every type the distribution provides, loaded or not."""
        return sorted(
            name for name, dist in self.distributions.items() if dist == distribution
        )

    def source_methods(self) -> set[str]:
        """Every source-specific collection method any installed source offers."""
        names: set[str] = set()
        for declaration in self.sources.values():
            names |= declaration.method_names()
        return names

    def vocabularies(self) -> tuple[Vocabulary, ...]:
        """The vocabularies the installed sources declare, in a stable order."""
        found = {
            declaration.vocabulary
            for declaration in self.sources.values()
            if declaration.vocabulary is not None
        }
        return tuple(sorted(found, key=lambda v: (v.prefix, v.namespace)))

    def prefixes(self) -> dict[str, str]:
        """The prefixes of the installed sources' vocabularies."""
        return {v.prefix: v.namespace for v in self.vocabularies()}

    @classmethod
    def of(
        cls, *declarations: SourceDeclaration | DestinationDeclaration
    ) -> PluginRegistry:
        """A registry of the given declarations, for tests and tools."""
        sources: dict[str, SourceDeclaration] = {}
        destinations: dict[str, DestinationDeclaration] = {}
        for declaration in declarations:
            if isinstance(declaration, SourceDeclaration):
                sources[declaration.type_name] = declaration
            else:
                destinations[declaration.type_name] = declaration
        return cls(sources, destinations)

    @classmethod
    def from_entry_points(cls) -> PluginRegistry:
        """Load every installed plugin's declaration; a plugin that does not
        load is recorded in ``failures`` and stops nothing else."""
        sources: dict[str, SourceDeclaration] = {}
        destinations: dict[str, DestinationDeclaration] = {}
        versions: dict[str, str] = {}
        failures: dict[str, PluginFailure] = {}
        distributions: dict[str, str] = {}
        for point in entry_points(group=SOURCES_GROUP):
            _record(point, "source", versions, distributions)
            try:
                sources[point.name] = _load(point.name, point.value, SourceDeclaration)
            except PluginError as exc:
                failures[point.name] = _failure(point, "source", exc)
        for point in entry_points(group=DESTINATIONS_GROUP):
            _record(point, "destination", versions, distributions)
            try:
                destinations[point.name] = _load(
                    point.name, point.value, DestinationDeclaration
                )
            except PluginError as exc:
                failures[point.name] = _failure(point, "destination", exc)
        return cls(sources, destinations, versions, failures, distributions)


def _record(
    point: EntryPoint,
    role: str,
    versions: dict[str, str],
    distributions: dict[str, str],
) -> None:
    versions[point.name] = _version(point)
    distribution = _distribution(point)
    if distribution is not None:
        distributions[point.name] = distribution


def _failure(point: EntryPoint, role: str, error: PluginError) -> PluginFailure:
    return PluginFailure(
        type_name=point.name,
        role=role,
        version=_version(point),
        error=str(error),
        distribution=_distribution(point),
    )


def _version(point: EntryPoint) -> str:
    distribution = point.dist
    return str(distribution.version) if distribution is not None else ""


def _distribution(point: EntryPoint) -> str | None:
    distribution = point.dist
    if distribution is None:
        return None
    name = distribution.name
    return str(name) if name else None


def _load(name: str, value: str, expected: type[Declaration]) -> Declaration:
    if name in RESERVED_TYPE_NAMES:
        raise PluginError(
            f"plugin {name!r} cannot use that type name: "
            f"{', '.join(sorted(RESERVED_TYPE_NAMES))} are words of "
            "`bricklogger update`"
        )
    try:
        loaded = EntryPoint(name=name, value=value, group="").load()
    except Exception as exc:
        raise PluginError(f"plugin {name!r} could not be loaded: {exc}") from exc
    if not isinstance(loaded, expected):
        raise PluginError(
            f"plugin {name!r} must declare a {expected.__name__}, "
            f"not {type(loaded).__name__}"
        )
    if loaded.type_name != name:
        raise PluginError(
            f"plugin {name!r} declares the type name {loaded.type_name!r}; "
            "the entry point name and the type name must match"
        )
    return loaded
