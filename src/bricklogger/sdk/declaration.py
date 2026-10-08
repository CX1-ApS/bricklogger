"""What a plugin declares before any instance exists.

The declaration is static: the type name, the reference types a source
understands, the vocabulary that defines a reference type of its own, its
collection methods, the configuration schema of an instance, the protocol
tools, and the factory that makes an instance. The daemon validates the
configuration against it and the CLI lists it, without starting anything. See
``docs/architecture.md``, "Declaration".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files

from pydantic import BaseModel

from bricklogger.sdk.contract import Destination, GraphReader, Source

RESERVED_TOOL_NAMES = frozenset({"start", "stop", "restart"})

SourceFactory = Callable[[str, BaseModel, GraphReader], Source]
DestinationFactory = Callable[[str, BaseModel], Destination]


@dataclass(frozen=True)
class CollectionMethod:
    """A source-specific collection method a rule can name in ``method``.

    ``poll`` is not declared here: it is defined centrally, with ``interval``
    in the rule schema, and every source opts into it.
    """

    name: str
    description: str
    parameters: type[BaseModel] | None = None

    def __post_init__(self) -> None:
        if self.name == "poll":
            raise ValueError("poll is defined centrally and cannot be declared")


@dataclass(frozen=True)
class ToolOffer:
    """Where the web interface offers a document tool: on the rows of another
    tool of the same plugin, each named parameter filled from a named column
    of the row. See ``docs/architecture.md``, "Protocol tools"."""

    tool: str
    parameters: Mapping[str, str]
    """Parameter name to column name."""


@dataclass(frozen=True)
class ToolDeclaration:
    """A protocol tool: a name, what it does, the schema of its parameters, and
    whether its result is a **document** — one JSON object to keep as a file,
    which the CLI writes as JSON and the web interface offers as a download
    rather than rendering it as a table. A document may say where the web
    interface offers it, ``offered_on`` another tool's rows; it then has no
    form of its own there."""

    name: str
    description: str
    parameters: type[BaseModel]
    document: bool = False
    offered_on: ToolOffer | None = None

    def __post_init__(self) -> None:
        if self.name in RESERVED_TOOL_NAMES:
            raise ValueError(f"{self.name!r} is reserved and cannot be a tool name")
        if self.offered_on is not None:
            if not self.document:
                raise ValueError(
                    f"{self.name!r} is offered on another tool's rows, "
                    "which only a document tool can be"
                )
            if self.offered_on.tool == self.name:
                raise ValueError(f"{self.name!r} cannot be offered on its own rows")


_PREFIX = re.compile(r"^[A-Za-z_][\w.-]*$")


@dataclass(frozen=True)
class Vocabulary:
    """A source's own reference vocabulary, for a system Brick's reference
    schema has no type for: a prefix, its namespace, and a Turtle document in
    the plugin's package with the classes, properties and SHACL rules.

    The daemon loads the vocabularies of every installed source at activation
    together with Brick's ontology and rules and pre-declares the prefix. See
    ``docs/architecture.md``, "Declaration".
    """

    prefix: str
    namespace: str
    package: str
    resource: str

    def __post_init__(self) -> None:
        if _PREFIX.match(self.prefix) is None:
            raise ValueError(f"{self.prefix!r} is not a valid prefix")
        if not self.namespace.endswith(("#", "/")):
            raise ValueError("a namespace ends with '#' or '/'")

    def text(self) -> str:
        """The Turtle document."""
        return files(self.package).joinpath(self.resource).read_text(encoding="utf-8")


@dataclass(frozen=True)
class SourceDeclaration:
    """The static declaration of a source plugin."""

    type_name: str
    description: str
    config_schema: type[BaseModel]
    reference_types: tuple[str, ...]
    vocabulary: Vocabulary | None = None
    methods: tuple[CollectionMethod, ...] = ()
    supports_poll: bool = True
    tools: tuple[ToolDeclaration, ...] = ()
    factory: SourceFactory | None = None

    role = "source"

    def __post_init__(self) -> None:
        names = {tool.name for tool in self.tools}
        for tool in self.tools:
            if tool.offered_on is not None and tool.offered_on.tool not in names:
                raise ValueError(
                    f"{tool.name!r} is offered on the rows of "
                    f"{tool.offered_on.tool!r}, which {self.type_name!r} has no tool of"
                )

    def method_names(self) -> set[str]:
        """The source-specific methods this source offers, by name."""
        return {method.name for method in self.methods}

    def create(self, name: str, config: BaseModel, graph: GraphReader) -> Source:
        """Make an instance; without a factory the plugin has no implementation yet."""
        if self.factory is None:
            raise NotImplementedError(
                f"the {self.type_name!r} source has no implementation yet"
            )
        return self.factory(name, config, graph)


@dataclass(frozen=True)
class DestinationDeclaration:
    """The static declaration of a destination plugin."""

    type_name: str
    description: str
    config_schema: type[BaseModel]
    stores_metadata: bool = True
    factory: DestinationFactory | None = None
    stores_model: bool = False

    role = "destination"

    def create(self, name: str, config: BaseModel) -> Destination:
        if self.factory is None:
            raise NotImplementedError(
                f"the {self.type_name!r} destination has no implementation yet"
            )
        return self.factory(name, config)
