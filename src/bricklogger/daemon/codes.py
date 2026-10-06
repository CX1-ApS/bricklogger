"""What each warning code is about.

A warning's *kind* says whether it means the logger is not doing its job right
now (``operation``) or that the model and the rule set leave something
unresolved (``model``). Status, the points view and the manual clear treat the
two alike; notifications do not, as ``docs/features/notifications.md``
describes: an ``operation`` warning mails as soon as it appears, a ``model``
warning waits for the daily summary, because it stands unchanged until the
model or the rules change and is work for a working day.
"""

from __future__ import annotations

from typing import Literal

Kind = Literal["operation", "model"]

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
