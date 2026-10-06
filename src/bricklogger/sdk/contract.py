"""The contract between the daemon and its plugins.

Sources and destinations meet only in the daemon, and they share only two
things: observations and point metadata, both in closed vocabularies. A source
runs its own loop in its own thread, receives a sink and a status channel, and
gets its assignment as full desired state; a destination receives batches and
writes them idempotently. See ``docs/architecture.md``, "Plugins".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel

ValueType = Literal[
    "number", "integer", "boolean", "enum", "string", "datetime", "null"
]
NullReason = Literal[
    "fault", "out_of_service", "overridden", "no_value", "unreachable", "read_error"
]
OutcomeState = Literal["active", "unsupported", "rejected"]
InstanceState = Literal["starting", "running", "failed", "stopped"]

VALUE_TYPES: frozenset[str] = frozenset(
    {"number", "integer", "boolean", "enum", "string", "datetime", "null"}
)
NULL_REASONS: frozenset[str] = frozenset(
    {"fault", "out_of_service", "overridden", "no_value", "unreachable", "read_error"}
)


@dataclass(frozen=True, slots=True)
class Observation:
    """Point, timestamp, type and value — and nothing else.

    The timestamp is the source's, timezone-aware and in UTC. A ``null``
    observation carries its reason as its value.
    """

    point: str
    timestamp: datetime
    type: ValueType
    value: float | int | bool | str | datetime | None = None
    reason: NullReason | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("an observation's timestamp must be timezone-aware")
        if self.type not in VALUE_TYPES:
            raise ValueError(f"unknown value type {self.type!r}")
        if self.type == "null":
            if self.value is not None:
                raise ValueError("a null observation carries no value")
            if self.reason not in NULL_REASONS:
                raise ValueError(
                    "a null observation needs a reason from the vocabulary"
                )
        elif self.value is None:
            raise ValueError(f"a {self.type} observation needs a value")
        elif self.reason is not None:
            raise ValueError("only null observations carry a reason")

    def as_dict(self) -> dict[str, Any]:
        value: Any = self.value
        if isinstance(value, datetime):
            value = value.isoformat()
        return {
            "point": self.point,
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "type": self.type,
            "value": value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Observation:
        value: Any = data.get("value")
        if data["type"] == "datetime" and isinstance(value, str):
            value = datetime.fromisoformat(value)
        return cls(
            point=data["point"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            type=data["type"],
            value=value,
            reason=data.get("reason"),
        )


@dataclass(frozen=True)
class PointMetadata:
    """What describes a point, delivered beside the measurements.

    The Brick graph supplies ``name``, ``brick_class``, ``equipment``,
    ``location`` and ``graph_unit``; the source plugin supplies ``value_type``,
    the protocol's ``unit`` and the texts for ``enum`` and ``boolean`` points.
    Either side sends what it knows, and the daemon merges the two.
    """

    point: str
    value_type: ValueType | None = None
    unit: str | None = None
    enum_texts: Mapping[int, str] | None = None
    boolean_texts: tuple[str, str] | None = None
    name: str | None = None
    brick_class: str | None = None
    equipment: str | None = None
    location: str | None = None
    graph_unit: str | None = None

    def merged_with(self, newer: PointMetadata) -> PointMetadata:
        """This entry with every field the newer one knows taken from it."""
        updates = {
            key: value
            for key, value in newer.as_fields().items()
            if value is not None and key != "point"
        }
        return replace(self, **updates)

    def as_fields(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "value_type": self.value_type,
            "unit": self.unit,
            "enum_texts": self.enum_texts,
            "boolean_texts": self.boolean_texts,
            "name": self.name,
            "brick_class": self.brick_class,
            "equipment": self.equipment,
            "location": self.location,
            "graph_unit": self.graph_unit,
        }

    def as_dict(self) -> dict[str, Any]:
        data = self.as_fields()
        data["enum_texts"] = (
            {str(k): v for k, v in self.enum_texts.items()}
            if self.enum_texts is not None
            else None
        )
        data["boolean_texts"] = list(self.boolean_texts) if self.boolean_texts else None
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PointMetadata:
        enum_texts = data.get("enum_texts")
        boolean_texts = data.get("boolean_texts")
        return cls(
            point=data["point"],
            value_type=data.get("value_type"),
            unit=data.get("unit"),
            enum_texts=(
                {int(k): str(v) for k, v in enum_texts.items()}
                if enum_texts is not None
                else None
            ),
            boolean_texts=(
                (str(boolean_texts[0]), str(boolean_texts[1]))
                if boolean_texts
                else None
            ),
            name=data.get("name"),
            brick_class=data.get("brick_class"),
            equipment=data.get("equipment"),
            location=data.get("location"),
            graph_unit=data.get("graph_unit"),
        )


@dataclass(frozen=True)
class Outcome:
    """A source's verdict on one assigned point, reported initially and on change."""

    point: str
    state: OutcomeState
    reason: str | None = None


@dataclass(frozen=True)
class AssignedPoint:
    """One point of an assignment: the URI, the method and its typed parameters,
    and the latest observation the daemon has recorded for the point, if any —
    where a history source resumes. See ``docs/architecture.md``, "Operation"."""

    uri: str
    method: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    last_observation: datetime | None = None


class Sink(Protocol):
    """Where a source pushes observations and metadata; thread-safe."""

    def observations(self, batch: Iterable[Observation]) -> None: ...

    def metadata(self, entries: Iterable[PointMetadata]) -> None: ...


class StatusChannel(Protocol):
    """Where a source reports state per instance, device and point; thread-safe."""

    def instance_state(
        self, state: InstanceState, error: str | None = None
    ) -> None: ...

    def device(
        self,
        device: str,
        *,
        reachable: bool,
        error: str | None = None,
        skipped_rounds: int | None = None,
    ) -> None: ...

    def outcomes(self, outcomes: Iterable[Outcome]) -> None: ...

    def warn(self, code: str, message: str, subject: str | None = None) -> None:
        """Raise a warning with a code of the source's own; the subject is a
        device or a point, or the instance itself when ``None``."""
        ...

    def clear_warning(self, code: str, subject: str | None = None) -> None:
        """Withdraw a warning raised with ``warn``."""
        ...


class GraphReader(Protocol):
    """Read-only SPARQL over the working graph, with the model's prefixes."""

    @property
    def prefixes(self) -> Mapping[str, str]: ...

    def query(self, sparql: str) -> Any: ...


class Source(ABC):
    """A source instance. The daemon owns the plan; the source owns the execution.

    ``start`` runs the source's own loop in the thread the daemon gives it and
    returns when ``stop`` has been called from another thread. ``assign`` may
    be called from another thread at any time, before or after ``start``.
    """

    def __init__(self, name: str, config: BaseModel, graph: GraphReader) -> None:
        self.name = name
        self.config = config
        self.graph = graph

    @abstractmethod
    def resources(self) -> Iterable[str]:
        """The exclusive resources this instance claims, as opaque strings."""

    @abstractmethod
    def claim(self) -> set[str]:
        """The point URIs this instance serves, from the graph and its configuration."""

    @abstractmethod
    def start(self, sink: Sink, status: StatusChannel) -> None:
        """Run the source until stopped; observations go into the sink."""

    @abstractmethod
    def assign(self, points: Sequence[AssignedPoint]) -> None:
        """Hand over full desired state; the source works out what changes."""

    @abstractmethod
    def stop(self) -> None:
        """Ask the source to finish; ``start`` returns when it has."""

    def run_tool(self, name: str, parameters: Mapping[str, Any]) -> Any:
        """Run a declared protocol tool inside the source's own loop."""
        raise NotImplementedError(f"{self.name} offers no tool {name!r}")


class Destination(ABC):
    """A destination instance; writes arrive in batches, at least once."""

    def __init__(self, name: str, config: BaseModel) -> None:
        self.name = name
        self.config = config

    @abstractmethod
    def start(self) -> None:
        """Connect and prepare storage; raise if that fails."""

    @abstractmethod
    def write(self, batch: Sequence[Observation]) -> None:
        """Write one batch idempotently; raise on failure and it is retried."""

    def write_metadata(self, entries: Sequence[PointMetadata]) -> None:
        """Store point metadata; the default keeps none."""
        return None

    @abstractmethod
    def stop(self) -> None:
        """Flush what is in flight and close."""
