"""Health read off the warnings, and the staleness of points.

An instance's health is the worst severity among the warnings standing on the
instance, on its devices and on the points assigned to it; its reason is that
warning's message, the oldest among equals, with the number of others. Point
warnings of one code count as one condition, told as how many points it
holds for. A point is stale when its newest observation is older than its
limit. See ``docs/features/daemon.md``, "Health" and "Stale points".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bricklogger.daemon.codes import Health, severity_of
from bricklogger.sdk.contract import AssignedPoint
from bricklogger.sdk.declaration import Severity

STALE_FACTOR = 3
"""A point is stale after this many of its intervals without an observation."""

POINT_PHRASES: Mapping[str, str] = {
    "stale": "stale",
    "read_error": "answering reads with an error",
    "future_timestamp": "delivering timestamps in the future",
}
"""How a point condition reads after "12 of 57 points"."""

_RANK: Mapping[Health, int] = {"ok": 0, "degraded": 1, "failed": 2}


@dataclass(frozen=True)
class InstanceHealth:
    """One instance's health with the condition that decides it."""

    health: Health = "ok"
    reason: str | None = None
    since: str | None = None
    more: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "reason": self.reason,
            "since": self.since,
            "more": self.more,
        }


@dataclass
class _Condition:
    severity: Severity
    since: str
    message: str
    points: int = 0


def assess(
    instances: Iterable[str],
    warnings: Iterable[Mapping[str, Any]],
    owners: Mapping[str, str | None],
    declared: Mapping[str, Severity] | None = None,
) -> dict[str, InstanceHealth]:
    """The health of each named instance.

    ``owners`` maps every planned point to the instance it is assigned to;
    ``declared`` holds the severities the installed plugins declare.
    """
    names = list(instances)
    totals: dict[str, int] = {}
    for owner in owners.values():
        if owner is not None:
            totals[owner] = totals.get(owner, 0) + 1
    conditions: dict[str, dict[tuple[str, str], _Condition]] = {n: {} for n in names}
    for warning in warnings:
        code = str(warning["code"])
        severity = severity_of(code, declared)
        if severity is None:
            continue
        subject = str(warning["subject"])
        owner, key, message = _place(subject, owners, names, str(warning["message"]))
        if owner is None:
            continue
        found = conditions[owner].get((code, key))
        first_seen = str(warning["first_seen"])
        if found is None:
            found = conditions[owner][(code, key)] = _Condition(
                severity, first_seen, message
            )
        found.since = min(found.since, first_seen)
        if key == "":
            found.points += 1
            found.message = point_reason(code, found.points, totals.get(owner, 0))
    result: dict[str, InstanceHealth] = {}
    for name in names:
        standing = list(conditions[name].values())
        if not standing:
            result[name] = InstanceHealth()
            continue
        chosen = min(standing, key=lambda c: (-_RANK[c.severity], c.since))
        result[name] = InstanceHealth(
            health=chosen.severity,
            reason=chosen.message,
            since=chosen.since,
            more=len(standing) - 1,
        )
    return result


def _place(
    subject: str,
    owners: Mapping[str, str | None],
    names: list[str],
    message: str,
) -> tuple[str | None, str, str]:
    """The instance a warning's subject belongs to, the condition's key within
    it, and the message as an instance's reason; a point's key is empty."""
    if subject in owners:
        return owners[subject], "", message
    for name in names:
        if subject == name:
            return name, subject, message
        if subject.startswith(name + "/"):
            return name, subject, f"{subject[len(name) + 1 :]}: {message}"
    return None, "", message


def point_reason(code: str, points: int, total: int) -> str:
    """A point condition as an instance's reason, e.g. ``12 of 57 points stale``."""
    phrase = POINT_PHRASES.get(code)
    counted = f"{points} of {total} points"
    return f"{counted} {phrase}" if phrase else f"{code} on {counted}"


def told(item: Mapping[str, Any]) -> str:
    """An instance's reason as one line shows it: the worst, and how many more."""
    reason = item.get("reason")
    if not reason:
        return ""
    more = int(item.get("more") or 0)
    return f"{reason}, and {more} more" if more else str(reason)


def overall(healths: Iterable[InstanceHealth]) -> Health:
    """The worst of the instances' health."""
    return max((h.health for h in healths), key=lambda h: _RANK[h], default="ok")


def staleness_limit(
    point: AssignedPoint,
    stale_after: timedelta | None,
    default: timedelta | None,
) -> timedelta | None:
    """How long the point may deliver nothing: the instance's ``stale_after``,
    the plugin's default, or three times its interval; ``None`` without any."""
    if stale_after is not None:
        return stale_after
    if default is not None:
        return default
    interval = point.parameters.get("interval")
    if isinstance(interval, timedelta):
        return interval * STALE_FACTOR
    return None


def stale_since(
    last_observation: datetime | None,
    running_since: datetime,
    limit: timedelta,
    now: datetime,
) -> datetime | None:
    """Since when the point counts as delivering nothing, when it is stale."""
    since = (
        running_since
        if last_observation is None
        else max(last_observation, running_since)
    )
    return since if now - since > limit else None
