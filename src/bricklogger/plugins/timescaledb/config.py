"""The TimescaleDB instance configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TimescaleDBConfig(BaseModel):
    """One ``timescaledb`` instance in ``destinations.yaml``.

    ``spool`` and ``batch`` never reach the plugin; the daemon handles them.
    """

    model_config = ConfigDict(extra="forbid")

    dsn: str = Field(
        min_length=1,
        description="The connection string without the password, e.g. "
        "postgres://bricklogger@db.example.com:5432/brick",
    )
    password: str | None = Field(
        default=None,
        json_schema_extra={"format": "password"},
        description="The database password, given as ${VARIABLE} with the "
        "value in the env file",
    )
