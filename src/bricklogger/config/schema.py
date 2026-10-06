"""The schema of the four configuration files, as ``docs/features/configuration.md``
defines it. Each model is one file's content or one entry in it.

The models check shape and types; the checks that need more than one file or
the installed plugins live in :mod:`bricklogger.config.validation`. Every
setting carries a one-line description, which is what the CLI's ``plugins``
command, the ``init`` wizard, the web interface's forms and the MCP server show.
"""

from __future__ import annotations

import ipaddress
from datetime import time, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bricklogger.config.values import Duration, Size, TimeOfDay

Action = Literal["accept", "deny"]
LogLevel = Literal["debug", "info", "warning", "error"]
LogFormat = Literal["text", "json"]
SmtpSecurity = Literal["starttls", "tls", "none"]
SelectorValue = str | list[str]

POLL = "poll"
"""The collection method every source supports; it is defined here, not by plugins."""

SECRET = "a secret, given as ${VARIABLE} with the value in the env file"


def is_loopback(host: str) -> bool:
    """Whether a binding host stays on this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiSettings(_Strict):
    """Where the daemon's HTTP API binds."""

    host: str = Field(
        default="127.0.0.1",
        description="Where the API binds; localhost by default, and binding it "
        "elsewhere is a deliberate choice",
    )
    port: int = Field(default=8420, ge=1, le=65535, description="The API's port")
    token: str | None = Field(
        default=None,
        description="The bearer token every request carries; required when host "
        f"is not a loopback address, and {SECRET}",
    )


class LogSettings(_Strict):
    """The daemon's own logging."""

    level: LogLevel = Field(
        default="info",
        description="debug, info, warning or error; debug adds the plugins' "
        "protocol traffic",
    )
    format: LogFormat = Field(
        default="text", description="text for people, json for log shippers"
    )
    file: Path | None = Field(
        default=None,
        description="The log file of `daemon start`; default bricklogger.log in the "
        "data directory. `daemon run` logs to stdout",
    )
    max_size: Size = Field(
        default=10 * 1024**2,
        description="The size at which the log file is rotated, e.g. 10MB",
    )
    keep: int = Field(
        default=5, ge=0, description="How many rotated log files are kept"
    )


class WebSettings(_Strict):
    """Where `bricklogger serve` binds."""

    host: str = Field(
        default="127.0.0.1",
        description="Where the web interface binds; localhost by default",
    )
    port: int = Field(
        default=8421, ge=1, le=65535, description="The web interface's port"
    )
    password: str | None = Field(
        default=None,
        description="The one password the web interface asks for; required when "
        f"host is not a loopback address, and {SECRET}",
    )


class McpSettings(_Strict):
    """Where `bricklogger mcp serve --http` binds."""

    host: str = Field(
        default="127.0.0.1",
        description="Where the MCP server binds over HTTP; localhost by default",
    )
    port: int = Field(
        default=8422, ge=1, le=65535, description="The MCP server's port over HTTP"
    )
    token: str | None = Field(
        default=None,
        description="The bearer token an MCP client sends over HTTP; enforced "
        "whenever it is set and required when host is not a loopback "
        f"address, and {SECRET}",
    )


class SmtpSettings(_Strict):
    """Where notification mail is submitted; credentials are optional."""

    host: str | None = Field(
        default=None,
        description="The mail server to submit to; required when notifications "
        "are enabled",
    )
    port: int = Field(default=587, ge=1, le=65535, description="The mail server's port")
    security: SmtpSecurity = Field(
        default="starttls",
        description="starttls for the submission port, tls for an implicit-TLS "
        "port, none for a relay on the local network",
    )
    username: str | None = Field(
        default=None,
        description="The login name; left out for a relay that takes "
        "unauthenticated mail from its own hosts",
    )
    password: str | None = Field(
        default=None, description=f"The login password, {SECRET}"
    )


class NotificationSettings(_Strict):
    """``notifications`` in ``daemon.yaml``.

    Off until it is switched on, and switching it on needs an active model,
    which :mod:`bricklogger.config.validation` checks because this model
    cannot see the data directory. See ``docs/features/notifications.md``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = Field(
        default=False,
        description="Whether mail is sent at all; switching it on requires an "
        "active model",
    )
    smtp: SmtpSettings = Field(
        default_factory=SmtpSettings, description="The mail server to submit to"
    )
    sender: str | None = Field(
        default=None,
        alias="from",
        description="The sender address; required when enabled",
    )
    to: list[str] = Field(
        default_factory=list,
        description="The recipients, at least one when enabled",
    )
    window: Duration = Field(
        default=timedelta(minutes=2),
        description="How long the daemon gathers events before sending one mail",
    )
    min_interval: Duration = Field(
        default=timedelta(minutes=15), description="The floor between two mails"
    )
    digest: TimeOfDay = Field(
        default=time(7, 0),
        description="When the daily summary is sent, in the machine's local time, "
        'quoted: "07:00"',
    )


class DaemonSettings(_Strict):
    """``daemon.yaml``."""

    api: ApiSettings = Field(
        default_factory=ApiSettings, description="Where the HTTP API binds"
    )
    data_dir: Path = Field(
        default=Path("/var/lib/bricklogger"),
        description="The data directory: runtime state, model versions, the "
        "working graph and the spools",
    )
    stop_timeout: Duration = Field(
        default=timedelta(seconds=10),
        description="How long a plugin instance gets to stop gracefully before it "
        "is abandoned",
    )
    log: LogSettings = Field(
        default_factory=LogSettings, description="The daemon's own logging"
    )
    web: WebSettings = Field(
        default_factory=WebSettings,
        description="Where `bricklogger serve` binds; the daemon ignores it",
    )
    mcp: McpSettings = Field(
        default_factory=McpSettings,
        description="Where `bricklogger mcp serve --http` binds; the daemon ignores it",
    )
    notifications: NotificationSettings = Field(
        default_factory=NotificationSettings,
        description="Mail to the administrator, off by default",
    )

    @property
    def log_file(self) -> Path:
        """``log.file``, or ``bricklogger.log`` in the data directory."""
        return self.log.file or self.data_dir / "bricklogger.log"


class SpoolSettings(_Strict):
    """The persistent spool between the daemon and one destination."""

    max_size: Size = Field(
        default=1024**3,
        description="The spool's size cap, e.g. 1GB; the oldest observations are "
        "dropped beyond it",
    )
    max_age: Duration = Field(
        default=timedelta(days=7),
        description="The spool's age cap, e.g. 7d; older observations are dropped",
    )


class BatchSettings(_Strict):
    """How observations are handed to one destination."""

    size: int = Field(
        default=1000, ge=1, description="How many observations go into one write"
    )
    interval: Duration = Field(
        default=timedelta(seconds=5),
        description="How long the daemon waits at most before writing a partial batch",
    )


class SourceInstance(BaseModel):
    """An entry in ``sources.yaml``: ``type`` picks the plugin, the rest is its own."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="The plugin type, e.g. bacnet-ip")

    @property
    def settings(self) -> dict[str, Any]:
        """The instance's own keys, validated later against the plugin's schema."""
        return dict(self.model_extra or {})


class DestinationInstance(BaseModel):
    """One entry in ``destinations.yaml``; ``spool`` and ``batch`` are the daemon's."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(description="The plugin type, e.g. timescaledb")
    spool: SpoolSettings = Field(
        default_factory=SpoolSettings,
        description="The persistent spool in front of this destination",
    )
    batch: BatchSettings = Field(
        default_factory=BatchSettings,
        description="How observations are batched for this destination",
    )

    @property
    def settings(self) -> dict[str, Any]:
        """The instance's own keys, without the reserved ``spool`` and ``batch``."""
        return dict(self.model_extra or {})


class Selector(BaseModel):
    """The ``match`` and ``match_regex`` keys: several keys are AND, a list is OR."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    class_: SelectorValue | None = Field(
        default=None,
        alias="class",
        description="The point's Brick class, subclasses included, e.g. "
        "brick:Temperature_Sensor",
    )
    equipment: SelectorValue | None = Field(
        default=None,
        description="The equipment the point belongs to, sub-components included",
    )
    location: SelectorValue | None = Field(
        default=None,
        description="The point's location: a zone, room, floor or building, and "
        "everything within it",
    )
    point: SelectorValue | None = Field(
        default=None, description="One point by its URI, e.g. ex:TT_1_17"
    )

    @model_validator(mode="after")
    def _at_least_one_key(self) -> Self:
        if all(
            value is None
            for value in (self.class_, self.equipment, self.location, self.point)
        ):
            raise ValueError(
                "a selector needs at least one of class, equipment, location or point"
            )
        return self


class Fallback(_Strict):
    """The alternative collection method of a rule with a source-specific method."""

    method: str = Field(description="The method to fall back to, e.g. poll")
    interval: Duration | None = Field(
        default=None, description="The polling interval when the fallback is poll"
    )

    @model_validator(mode="after")
    def _interval_belongs_to_poll(self) -> Self:
        _check_interval(self.method, self.interval)
        return self


class Rule(_Strict):
    """One rule in ``rules.yaml``."""

    name: str | None = Field(
        default=None, description="A readable name for status and troubleshooting"
    )
    match: Selector | None = Field(
        default=None,
        description="Selectors that must all hold; a list as a value matches any "
        "of its elements",
    )
    match_regex: Selector | None = Field(
        default=None,
        description="The same four keys as regular expressions over the whole "
        "prefixed URI",
    )
    sparql: str | None = Field(
        default=None,
        description="A SPARQL query whose first selected variable is the point",
    )
    action: Action = Field(description="accept or deny")
    method: str | None = Field(
        default=None,
        description="The collection method of an accept rule: poll, or one a "
        "source declares",
    )
    interval: Duration | None = Field(
        default=None,
        description="The polling interval, required with poll: 30s, 5m, 1h",
    )
    fallback: Fallback | None = Field(
        default=None,
        description="The method to use when a device cannot do the primary one",
    )

    @model_validator(mode="after")
    def _shape(self) -> Self:
        selectors = [
            key
            for key in ("match", "match_regex", "sparql")
            if getattr(self, key) is not None
        ]
        if len(selectors) != 1:
            raise ValueError("exactly one of match, match_regex or sparql is required")
        if self.action == "deny":
            for key in ("method", "interval", "fallback"):
                if getattr(self, key) is not None:
                    raise ValueError(f"{key} is not allowed on a deny rule")
            return self
        if self.method is None:
            raise ValueError("method is required with action accept")
        _check_interval(self.method, self.interval)
        if self.fallback is not None:
            if self.method == POLL:
                raise ValueError("fallback is not allowed with method poll")
            if self.fallback.method == self.method:
                raise ValueError("the fallback must name another method than the rule")
        return self


def _check_interval(method: str, interval: timedelta | None) -> None:
    if method == POLL:
        if interval is None:
            raise ValueError("interval is required with method poll")
        if interval <= timedelta(0):
            raise ValueError("interval must be longer than zero")
    elif interval is not None:
        raise ValueError("interval is only allowed with method poll")


class Configuration(BaseModel):
    """The whole config directory, loaded and validated."""

    model_config = ConfigDict(frozen=True)

    daemon: DaemonSettings
    sources: dict[str, SourceInstance]
    destinations: dict[str, DestinationInstance]
    rules: list[Rule]

    def rule_label(self, index: int) -> str:
        """The rule's name, or its position for status and error messages."""
        return rule_label(index, self.rules[index].name)


def rule_label(index: int, name: object) -> str:
    """The name a rule is reported under: ``name`` if set, else ``rule N``."""
    if isinstance(name, str) and name.strip():
        return name
    return f"rule {index + 1}"
