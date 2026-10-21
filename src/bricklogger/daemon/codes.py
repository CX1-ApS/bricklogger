"""What each warning code is about, and what it does to health.

A warning's *kind* says whether it means the logger is not doing its job right
now (``operation``) or that the model and the rule set leave something
unresolved (``model``). Status, the points view and the manual clear treat the
two alike; notifications do not, as ``docs/features/notifications.md``
describes: an ``operation`` warning mails as soon as it appears, a ``model``
warning waits for the daily summary, because it stands unchanged until the
model or the rules change and is work for a working day.

A warning's *severity* says what it does to its subject's health:
``degraded``, ``failed`` or none. An instance's health is the worst severity
among the warnings standing on it, its devices and its points; see
``docs/features/daemon.md``, "Health".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from bricklogger.sdk.declaration import Severity

Kind = Literal["operation", "model"]
Health = Literal["ok", "degraded", "failed"]

NOTIFY_FAILED = "notify_failed"
"""Raised when a notification mail could not be sent."""

NOTIFICATIONS_DORMANT = "notifications_dormant"
"""Raised when notifications are switched on but no model is active."""

MODEL_WARNINGS: frozenset[str] = frozenset(
    {
        "unclaimed",
        "unknown_reference",
        "no_reference",
        "no_fallback",
        "rejected",
        "rule_skipped",
        "unit_conflict",
    }
)
"""The codes the model and the rule set raise, as the warning table lists them."""

NEVER_NOTIFY: frozenset[str] = frozenset({NOTIFY_FAILED, NOTIFICATIONS_DORMANT})
"""Codes about the mail itself: one would loop, and the other cannot be sent."""


def kind_of(code: str) -> Kind:
    """The kind of a warning code; an unknown code counts as ``operation``.

    A plugin may raise a code of its own through its status channel, and a
    plugin that raises a warning is saying collection is in trouble, so the
    default is the one that reaches someone.
    """
    return "model" if code in MODEL_WARNINGS else "operation"


def notifiable(code: str) -> bool:
    """Whether opening or closing this code is worth a mail of its own."""
    return kind_of(code) == "operation" and code not in NEVER_NOTIFY


DEVICE_UNREACHABLE = "device_unreachable"
"""Raised on ``instance/device`` while the source reports the device unreachable."""

STALE = "stale"
"""Raised on a point that has delivered nothing for longer than its limit."""

SEVERITIES: Mapping[str, Severity] = {
    "read_error": "degraded",
    "poll_overrun": "degraded",
    "rate_limited": "degraded",
    "future_timestamp": "degraded",
    DEVICE_UNREACHABLE: "degraded",
    STALE: "degraded",
    "spool_drop": "failed",
    "instance_failed": "failed",
    "stop_timeout": "failed",
}
"""The daemon's own codes that harm health, as the warning table lists them."""

NO_SEVERITY: frozenset[str] = MODEL_WARNINGS | {
    "instance_stopped",
    NOTIFY_FAILED,
    NOTIFICATIONS_DORMANT,
}
"""Codes about the model, the mail or the operator's own stop, not collection."""


def severity_of(
    code: str, declared: Mapping[str, Severity] | None = None
) -> Severity | None:
    """The severity of a code: the daemon's own, else as a plugin declared it.

    A code no one declared counts as ``degraded``: a plugin that raises a
    warning is saying collection is in trouble, as with the kind.
    """
    if code in NO_SEVERITY:
        return None
    if code in SEVERITIES:
        return SEVERITIES[code]
    return (declared or {}).get(code, "degraded")


def worst(severities: Iterable[Severity | None]) -> Health:
    """The health a set of severities gives: the worst of them, ``ok`` for none."""
    found = set(severities)
    if "failed" in found:
        return "failed"
    return "degraded" if "degraded" in found else "ok"
