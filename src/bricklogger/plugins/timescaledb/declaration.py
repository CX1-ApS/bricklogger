"""The TimescaleDB destination's declaration."""

from __future__ import annotations

from bricklogger.plugins.timescaledb.config import TimescaleDBConfig
from bricklogger.plugins.timescaledb.destination import TimescaleDBDestination
from bricklogger.sdk.declaration import DestinationDeclaration

DESTINATION = DestinationDeclaration(
    type_name="timescaledb",
    description="TimescaleDB: one narrow numeric hypertable, metadata beside it.",
    config_schema=TimescaleDBConfig,
    stores_metadata=True,
    factory=TimescaleDBDestination,
)

__all__ = ["DESTINATION", "TimescaleDBConfig", "TimescaleDBDestination"]
