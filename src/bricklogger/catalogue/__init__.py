"""The catalogue: the approved plugins Bricklogger ships with.

The list is kept in the repository ``CX1-ApS/bricklogger-marketplace`` and
copied into the package at every release as ``catalogue.json``, so it is there
on a machine with no network. It names plugins, not releases: installing one
is ``plugins add`` with the package's name. The **origin** of an installed
plugin — built-in, catalogue or custom — is decided by its package name alone.
See ``docs/features/plugins.md``, "The catalogue".
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any, Literal

from packaging.utils import canonicalize_name
from pydantic import BaseModel, ConfigDict, Field

from bricklogger.sdk.registry import BUILT_IN_DISTRIBUTION, PluginRegistry

Origin = Literal["built-in", "catalogue", "custom"]


class CatalogueType(BaseModel):
    """One type a catalogue plugin provides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    role: Literal["source", "destination"]


class CatalogueEntry(BaseModel):
    """One approved plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package: str = Field(min_length=1)
    types: tuple[CatalogueType, ...] = Field(min_length=1)
    description: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    link: str = Field(min_length=1)


class Catalogue(BaseModel):
    """The file as shipped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugins: tuple[CatalogueEntry, ...] = ()


@cache
def catalogue() -> tuple[CatalogueEntry, ...]:
    """The catalogue this release of Bricklogger ships with, by package name."""
    text = resources.files(__name__).joinpath("catalogue.json").read_text("utf-8")
    entries = Catalogue.model_validate(json.loads(text)).plugins
    return tuple(sorted(entries, key=lambda entry: canonicalize_name(entry.package)))


def packages() -> frozenset[str]:
    """The catalogue's package names, normalised as package names are compared."""
    return frozenset(canonicalize_name(entry.package) for entry in catalogue())


def origin_of(distribution: str | None) -> Origin:
    """Where a plugin from this distribution stands against the catalogue.

    A type whose distribution is unknown came with no package of its own, and
    is counted as built-in.
    """
    if distribution is None:
        return "built-in"
    name = canonicalize_name(distribution)
    if name == canonicalize_name(BUILT_IN_DISTRIBUTION):
        return "built-in"
    return "catalogue" if name in packages() else "custom"


def catalogue_rows(registry: PluginRegistry) -> list[dict[str, Any]]:
    """The catalogue with the version installed of each entry, ``None`` when
    the package is not installed, as ``GET /v1/plugins/catalogue`` gives it."""
    installed: dict[str, str | None] = {}
    for type_name, distribution in registry.distributions.items():
        installed[canonicalize_name(distribution)] = registry.version_of(type_name)
    return [
        {
            "package": entry.package,
            "types": [{"type": t.type, "role": t.role} for t in entry.types],
            "description": entry.description,
            "publisher": entry.publisher,
            "link": entry.link,
            "installed": installed.get(canonicalize_name(entry.package)),
        }
        for entry in catalogue()
    ]
