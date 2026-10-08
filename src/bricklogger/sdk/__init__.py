"""The plugin SDK: the one public surface a plugin is built on.

Everything a source or destination plugin needs is importable from here — the
contract, the declaration, the configuration value types, prefixed names,
the BACnet tables in :mod:`bricklogger.sdk.bacnet` and the test kit in
:mod:`bricklogger.sdk.testing` — and nothing else in the package is public.
The contract is stable within a minor version of Bricklogger: a change that
breaks plugins raises the minor version, an addition they do not need raises
the patch version, and a plugin pins the minor version it was built for and
requires the version that brought an addition it uses. See
``docs/features/plugins.md``, "The SDK".
"""

from bricklogger.config.values import (
    DURATION_HELP,
    Duration,
    format_duration,
    parse_duration,
)
from bricklogger.model.prefixes import UnknownPrefix, expand
from bricklogger.sdk.contract import (
    NULL_REASONS,
    VALUE_TYPES,
    AssignedPoint,
    Destination,
    GraphReader,
    InstanceState,
    ModelDocument,
    NullReason,
    Observation,
    Outcome,
    OutcomeState,
    PointMetadata,
    Sink,
    Source,
    StatusChannel,
    ValueType,
)
from bricklogger.sdk.declaration import (
    CollectionMethod,
    DestinationDeclaration,
    SourceDeclaration,
    ToolDeclaration,
    ToolOffer,
    Vocabulary,
)

__all__ = [
    "DURATION_HELP",
    "NULL_REASONS",
    "VALUE_TYPES",
    "AssignedPoint",
    "CollectionMethod",
    "Destination",
    "DestinationDeclaration",
    "Duration",
    "GraphReader",
    "InstanceState",
    "ModelDocument",
    "NullReason",
    "Observation",
    "Outcome",
    "OutcomeState",
    "PointMetadata",
    "Sink",
    "Source",
    "SourceDeclaration",
    "StatusChannel",
    "ToolDeclaration",
    "ToolOffer",
    "UnknownPrefix",
    "ValueType",
    "Vocabulary",
    "expand",
    "format_duration",
    "parse_duration",
]
