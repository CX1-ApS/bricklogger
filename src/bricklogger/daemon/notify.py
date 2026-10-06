"""Mail to the administrator: alarms, all clears and the daily summary.

What is sent and when is defined in ``docs/features/notifications.md``. The
policy lives here; the transport is SMTP from the standard library, driven on
a thread of the notifier's own, so a slow or unreachable mail server never
touches collection.

The two ends of a mail are possible because a warning is a condition with a
defined beginning and a defined end. The notifier keeps, in the runtime state,
the set of conditions the administrator has been told about; each pass compares
that set with what stands now, and the difference is what a mail says.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from socket import gethostname
from typing import Any, Protocol

from pyoxigraph import NamedNode, QuerySolutions

from bricklogger.config.schema import NotificationSettings, SmtpSettings
from bricklogger.daemon.codes import (
    NOTIFICATIONS_DORMANT,
    NOTIFY_FAILED,
    notifiable,
)
from bricklogger.daemon.rules import BRICK, REC
from bricklogger.daemon.state import RuntimeState
from bricklogger.model.working_graph import MODEL_GRAPH, WorkingGraph
from bricklogger.ops.updates import available_line

log = logging.getLogger("bricklogger.notify")


class DaemonView(Protocol):
    """The little of the daemon the notifier reads.

    A protocol rather than the class itself, so the policy can be exercised
    without starting a daemon, and so this module does not import the one that
    imports it.
    """

    state: RuntimeState | None
    active_version: int | None
    graph: WorkingGraph | None

    def health(self) -> str: ...

    def status(self) -> dict[str, Any]: ...


class Mailer(Protocol):
    """Whatever can put a message on its way and say what came back."""

    def send(self, message: EmailMessage) -> str: ...


INTERVAL = 15.0
"""How often the notifier looks; the window and the floor do the real pacing."""

ATTEMPTS = 3
"""Tries per mail before it is dropped and left to the next window."""

BACKOFF = (2.0, 8.0)
"""Waits between those tries."""

MAX_EVENTS = 20
"""Lifecycle events held at once; a restart loop cannot grow the memory."""

NAMED_SUBJECTS = 5
"""Subjects named in a mail per code before the rest are merely counted."""

# Keys in the runtime state's notifier memory.
HEALTH = "health"
PENDING_SINCE = "pending_since"
LAST_ATTEMPT = "last_attempt"
LAST_MAIL = "last_mail"
LAST_ANSWER = "last_answer"
LAST_ERROR = "last_error"
LAST_DIGEST = "last_digest"
EVENTS = "events"

DORMANT_MESSAGE = (
    "notifications are switched on, but no model is active, so nothing can be sent"
)

_BUILDINGS = """
SELECT DISTINCT ?b ?label WHERE {
  GRAPH <%(model)s> { ?b a ?any }
  { ?b a <%(brick)sBuilding> } UNION { ?b a <%(rec)sBuilding> }
  OPTIONAL { ?b <http://www.w3.org/2000/01/rdf-schema#label> ?label }
}
"""


def building_names(graph: WorkingGraph) -> list[str]:
    """The names of the model's buildings, for the subject line.

    A model that names none gives an empty list, and the subject then carries
    the machine alone.
    """
    query = _BUILDINGS % {"model": MODEL_GRAPH, "brick": BRICK, "rec": REC}
    result = graph.query(query)
    if not isinstance(result, QuerySolutions):
        return []
    names: dict[str, str] = {}
    for solution in result:
        uri = solution["b"]
        if not isinstance(uri, NamedNode):
            continue
        label = solution["label"]
        text = getattr(label, "value", None) or _local_name(uri.value)
        names.setdefault(uri.value, text)
    return sorted(names.values())


def _local_name(uri: str) -> str:
    return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Mail:
    """One mail, in the two forms every mail carries."""

    subject: str
    text: str
    html: str


@dataclass
class Report:
    """What one alarm or all clear has to say."""

    opened: list[dict[str, Any]] = field(default_factory=list)
    closed: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    health: str = "ok"
    was: str | None = None

    @property
    def empty(self) -> bool:
        return not (self.opened or self.closed or self.events or self.changed)

    @property
    def changed(self) -> bool:
        return self.was is not None and self.was != self.health


class _Recording(smtplib.SMTP):
    """An SMTP client that keeps the server's last reply, to report it."""

    last_reply = ""

    def getreply(self) -> tuple[int, bytes]:
        code, message = super().getreply()
        self.last_reply = f"{code} {_text(message)}"
        return code, message


class _RecordingSSL(smtplib.SMTP_SSL):
    """The same, on a connection that is TLS from the first byte."""

    last_reply = ""

    def getreply(self) -> tuple[int, bytes]:
        code, message = super().getreply()
        self.last_reply = f"{code} {_text(message)}"
        return code, message


class SmtpMailer:
    """The transport: one connection per mail, closed when it is gone."""

    def __init__(
        self,
        settings: SmtpSettings,
        timeout: float = 30.0,
        context: ssl.SSLContext | None = None,
    ) -> None:
        self.settings = settings
        self.timeout = timeout
        self.context = context

    def send(self, message: EmailMessage) -> str:
        """Submit one message; answers what the server said, or raises.

        The client is built **with** the host, not connected afterwards: TLS
        verifies the certificate against the name it was asked to reach, and a
        client that does not know that name cannot start TLS at all.
        """
        settings = self.settings
        if not settings.host:
            raise ValueError("no SMTP server is configured")
        context = self.context if self.context is not None else _default_context()
        smtp: _Recording | _RecordingSSL
        if settings.security == "tls":
            smtp = _RecordingSSL(
                settings.host, settings.port, timeout=self.timeout, context=context
            )
        else:
            smtp = _Recording(settings.host, settings.port, timeout=self.timeout)
        with smtp:
            smtp.ehlo()
            if settings.security == "starttls":
                smtp.starttls(context=context)
                smtp.ehlo()
            if settings.username:
                smtp.login(settings.username, settings.password or "")
            refused = smtp.send_message(message)
            answer = smtp.last_reply
        if refused:
            return f"{answer}; refused: {', '.join(sorted(refused))}"
        return answer


def _default_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _text(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return value.strip()


class Notifier:
    """The policy: what has opened, what has closed, and when to say so."""

    def __init__(
        self,
        daemon: DaemonView,
        settings: NotificationSettings,
        *,
        mailer: Mailer | None = None,
        interval: float = INTERVAL,
        attempts: int = ATTEMPTS,
        backoff: Sequence[float] = BACKOFF,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.daemon = daemon
        self.settings = settings
        self._own_mailer = mailer is None
        self.mailer: Mailer = (
            mailer if mailer is not None else SmtpMailer(settings.smtp)
        )
        self.interval = interval
        self.attempts = attempts
        self.backoff = tuple(backoff) or (0.0,)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._wakeup = threading.Event()

    def reconfigure(self, settings: NotificationSettings) -> None:
        """Take a new configuration, mail server and all.

        The mailer holds the SMTP settings it was built with, so a
        reload that names another server has to build a new one, or the
        change would wait for a restart while status already showed it.
        A mailer handed in from outside is left alone.
        """
        self.settings = settings
        if self._own_mailer:
            self.mailer = SmtpMailer(settings.smtp)

    # --- the thread ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="notify", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        thread = self._thread
        if thread is not None:
            thread.join(10)
        self._thread = None

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wakeup.wait(self.interval)
            self._wakeup.clear()
            if self._stopping.is_set():
                return
            try:
                self.tick()
            except Exception:  # pragma: no cover - defensive
                log.exception("the notifier could not complete a pass")

    # --- state helpers ------------------------------------------------------

    @property
    def state(self) -> RuntimeState:
        state = self.daemon.state
        assert state is not None
        return state

    def _remember_time(self, key: str, moment: datetime) -> None:
        self.state.remember(key, moment.isoformat())

    def _recall_time(self, key: str) -> datetime | None:
        raw = self.state.memory(key)
        if raw is None:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:  # pragma: no cover - a hand-edited state file
            return None

    def _events(self) -> list[dict[str, Any]]:
        raw = self.state.memory(EVENTS)
        if not raw:
            return []
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover
            return []
        return list(loaded) if isinstance(loaded, list) else []

    def record_event(self, event: str) -> None:
        """Note that the daemon started or stopped, to travel in the next mail.

        A stop is held rather than sent, so an upgrade that stops and starts
        gives one mail that says both. A stop that is never followed by a start
        is what the missing daily summary reports.
        """
        if not self.settings.enabled:
            return
        events = self._events()
        events.append({"event": event, "at": self._clock().isoformat()})
        self.state.remember(EVENTS, json.dumps(events[-MAX_EVENTS:]))

    # --- one pass -----------------------------------------------------------

    def tick(self) -> None:
        """One look at the world; safe to call directly, which tests do."""
        if not self.settings.enabled:
            return
        if self.daemon.active_version is None:
            self.state.warn(NOTIFICATIONS_DORMANT, "notifications", DORMANT_MESSAGE)
            return
        self.state.clear_warnings(NOTIFICATIONS_DORMANT)
        now = self._clock()
        self._report(now)
        self._digest(now)

    def _collect(self) -> Report:
        standing = {
            (row["code"], row["subject"]): row
            for row in self.state.warnings()
            if notifiable(row["code"])
        }
        believed = {(row["code"], row["subject"]): row for row in self.state.notified()}
        return Report(
            opened=[standing[key] for key in sorted(standing.keys() - believed.keys())],
            closed=[believed[key] for key in sorted(believed.keys() - standing.keys())],
            events=self._events(),
            health=self.daemon.health(),
            was=self.state.memory(HEALTH),
        )

    def _report(self, now: datetime) -> None:
        report = self._collect()
        if report.empty:
            self.state.forget(PENDING_SINCE)
            if report.was is None:
                self.state.remember(HEALTH, report.health)
            return
        since = self._recall_time(PENDING_SINCE)
        if since is None:
            since = now
            self._remember_time(PENDING_SINCE, since)
        if now - since < self.settings.window:
            return
        attempted = self._recall_time(LAST_ATTEMPT)
        if attempted is not None and now - attempted < self.settings.min_interval:
            return
        if not self._deliver(self.compose_report(report), now):
            return
        for row in report.opened:
            self.state.add_notified(row["code"], row["subject"], row["message"])
        for row in report.closed:
            self.state.remove_notified(row["code"], row["subject"])
        self.state.remember(HEALTH, report.health)
        self.state.forget(EVENTS)
        self.state.forget(PENDING_SINCE)

    def _digest(self, now: datetime) -> None:
        due = self.digest_due(now)
        last = self._recall_time(LAST_DIGEST)
        if last is None:
            self._remember_time(LAST_DIGEST, due)
            return
        if last >= due:
            return
        if self._deliver(self.compose_digest(), now):
            self._remember_time(LAST_DIGEST, due)

    def digest_due(self, now: datetime) -> datetime:
        """The most recent moment the summary was due, in the machine's time."""
        local = now.astimezone()
        due = local.replace(
            hour=self.settings.digest.hour,
            minute=self.settings.digest.minute,
            second=0,
            microsecond=0,
        )
        return due if due <= local else due - timedelta(days=1)

    def next_digest(self, now: datetime) -> datetime:
        """When the summary is next due."""
        return self.digest_due(now) + timedelta(days=1)

    # --- delivery -----------------------------------------------------------

    def _deliver(self, mail: Mail, now: datetime) -> bool:
        self._remember_time(LAST_ATTEMPT, now)
        failure: Exception | None = None
        for attempt in range(self.attempts):
            try:
                answer = self.mailer.send(self.message(mail))
            except Exception as exc:
                failure = exc
                if attempt + 1 < self.attempts and not self._stopping.wait(
                    self.backoff[min(attempt, len(self.backoff) - 1)]
                ):
                    continue
                break
            self.state.clear_warnings(NOTIFY_FAILED)
            self.state.remember(LAST_ANSWER, answer)
            self._remember_time(LAST_MAIL, now)
            log.info("notification sent: %s", mail.subject)
            return True
        message = str(failure) or failure.__class__.__name__
        self.state.warn(NOTIFY_FAILED, "notifications", message)
        self.state.remember(LAST_ERROR, f"{now.isoformat()} {message}")
        log.error("the notification could not be sent: %s", message)
        return False

    def message(self, mail: Mail) -> EmailMessage:
        """One mail as a message with a text part and an HTML part."""
        message = EmailMessage()
        message["Subject"] = mail.subject
        message["From"] = self.settings.sender or ""
        message["To"] = ", ".join(self.settings.to)
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="bricklogger")
        message.set_content(mail.text)
        message.add_alternative(mail.html, subtype="html")
        return message

    def send_test(self) -> dict[str, Any]:
        """Send one test mail now, on the configuration as written."""
        mail = self.compose_test()
        try:
            answer = self.mailer.send(self.message(mail))
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            log.error("the test mail could not be sent: %s", message)
            return {"sent": False, "to": list(self.settings.to), "error": message}
        return {"sent": True, "to": list(self.settings.to), "answer": answer}

    # --- what the mails say -------------------------------------------------

    def site(self) -> str:
        """The building and the machine, as the subject line carries them."""
        names: list[str] = []
        graph = self.daemon.graph
        if graph is not None and self.daemon.active_version is not None:
            try:
                names = building_names(graph)
            except Exception:  # pragma: no cover - a query is never fatal here
                log.exception("the model's buildings could not be read")
        host = gethostname()
        return f"{', '.join(names)} / {host}" if names else host

    def subject(self, summary: str) -> str:
        return f"[{self.site()}] Bricklogger: {summary}"

    def compose_report(self, report: Report) -> Mail:
        return _render(
            self.subject(_summary(report)),
            _report_sections(report),
        )

    def compose_digest(self) -> Mail:
        status = self.daemon.status()
        health = str(status.get("health", "ok"))
        warnings = self.state.warnings()
        summary = f"daily summary: {health}"
        if warnings:
            summary += f", {_count(len(warnings), 'warning')}"
        return _render(self.subject(summary), _digest_sections(status, warnings))

    def compose_test(self) -> Mail:
        return _render(
            self.subject("test mail"),
            [
                (
                    "Test",
                    [
                        (
                            "This is a test from Bricklogger. Notifications are "
                            f"{'on' if self.settings.enabled else 'off'}."
                        )
                    ],
                    [],
                )
            ],
        )

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        """What ``notify status`` and the web panel show."""
        now = now if now is not None else self._clock()
        report = self._collect() if self.settings.enabled else Report()
        dormant = self.settings.enabled and self.daemon.active_version is None
        since = self._recall_time(PENDING_SINCE)
        return {
            "enabled": self.settings.enabled,
            "dormant": dormant,
            "to": list(self.settings.to),
            "server": self.settings.smtp.host,
            "window": _seconds(self.settings.window),
            "min_interval": _seconds(self.settings.min_interval),
            "last_mail": self.state.memory(LAST_MAIL),
            "last_answer": self.state.memory(LAST_ANSWER),
            "last_error": self.state.memory(LAST_ERROR),
            "next_digest": self.next_digest(now).isoformat(),
            "waiting": {
                "since": since.isoformat() if since is not None else None,
                "opened": len(report.opened),
                "closed": len(report.closed),
                "events": len(report.events),
            },
        }


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


def _uptime(seconds: Any) -> str:
    """How long the daemon has been up, in the largest units that say it."""
    if not isinstance(seconds, int | float):
        return ""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _summary(report: Report) -> str:
    if report.changed:
        return f"health {report.health}"
    if report.opened and not report.closed:
        if len(report.opened) == 1:
            row = report.opened[0]
            return f"{row['code']} on {row['subject']}"
        return _count(len(report.opened), "condition") + " opened"
    if report.closed and not report.opened:
        if len(report.closed) == 1:
            row = report.closed[0]
            return f"{row['code']} on {row['subject']} is over"
        return _count(len(report.closed), "condition") + " closed"
    if report.opened or report.closed:
        return f"{len(report.opened)} opened, {len(report.closed)} closed"
    events = ", ".join(str(event.get("event", "")) for event in report.events)
    return f"daemon {events}" if events else "nothing to report"


Section = tuple[str, list[str], list[tuple[str, ...]]]
"""A heading, its paragraphs, and a table given as rows of cells."""


def _grouped(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, ...]]:
    """Rows of a table, one line per code, with the subjects named or counted."""
    by_code: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        by_code.setdefault(str(row["code"]), []).append(row)
    table: list[tuple[str, ...]] = []
    for code, group in sorted(by_code.items()):
        subjects = [str(row["subject"]) for row in group]
        named = ", ".join(subjects[:NAMED_SUBJECTS])
        if len(subjects) > NAMED_SUBJECTS:
            named += f" and {len(subjects) - NAMED_SUBJECTS} more"
        table.append((code, str(len(subjects)), named, str(group[0]["message"])))
    return table


def _report_sections(report: Report) -> list[Section]:
    sections: list[Section] = []
    if report.changed:
        sections.append(("Health", [f"{report.was} became {report.health}."], []))
    else:
        sections.append(("Health", [f"The logger is {report.health}."], []))
    if report.opened:
        sections.append(
            (
                "Opened",
                [],
                [("Code", "Count", "Subjects", "Message"), *_grouped(report.opened)],
            )
        )
    if report.closed:
        sections.append(
            (
                "Over",
                [],
                [("Code", "Count", "Subjects", "Message"), *_grouped(report.closed)],
            )
        )
    if report.events:
        sections.append(
            (
                "The daemon",
                [],
                [
                    ("Event", "At"),
                    *(
                        (str(event.get("event", "")), str(event.get("at", "")))
                        for event in report.events
                    ),
                ],
            )
        )
    return sections


def _digest_sections(
    status: Mapping[str, Any], warnings: Sequence[Mapping[str, Any]]
) -> list[Section]:
    active = (status.get("model") or {}).get("active") or {}
    points = status.get("points") or {}
    sections: list[Section] = [
        (
            "Bricklogger",
            [],
            [
                ("", ""),
                ("Health", str(status.get("health", ""))),
                ("Version", str(status.get("version", ""))),
                ("Uptime", _uptime(status.get("uptime_seconds"))),
                ("Model", f"version {active['version']}" if active else "none"),
                ("Points accepted", str(points.get("accepted", 0))),
                ("Points active", str(points.get("active", 0))),
                ("Observations", str(status.get("observations_received", 0))),
            ],
        )
    ]
    instances = status.get("instances") or []
    if instances:
        sections.append(
            (
                "Instances",
                [],
                [
                    ("Name", "Role", "Type", "State"),
                    *(
                        (
                            str(item.get("name", "")),
                            str(item.get("role", "")),
                            str(item.get("type", "")),
                            str(item.get("state", "")),
                        )
                        for item in instances
                    ),
                ],
            )
        )
    if warnings:
        sections.append(
            (
                "Warnings",
                [],
                [
                    ("Code", "Kind", "Subject", "Since", "Count"),
                    *(
                        (
                            str(row["code"]),
                            str(row.get("kind", "")),
                            str(row["subject"]),
                            str(row["first_seen"]),
                            str(row["count"]),
                        )
                        for row in warnings
                    ),
                ],
            )
        )
    else:
        sections.append(("Warnings", ["Nothing stands."], []))
    newer = available_line(status.get("updates"))
    if newer is not None:
        sections.append(("Updates", [newer], []))
    return sections


def _render(subject: str, sections: Iterable[Section]) -> Mail:
    sections = list(sections)
    return Mail(subject, _as_text(subject, sections), _as_html(subject, sections))


def _rows(table: Sequence[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """The table's rows; a first row of empty cells is a key-value table."""
    return list(table[1:]) if table and not any(table[0]) else list(table)


def _as_text(subject: str, sections: Sequence[Section]) -> str:
    lines = [subject, "=" * len(subject), ""]
    for heading, paragraphs, table in sections:
        lines.append(heading)
        lines.append("-" * len(heading))
        lines.extend(paragraphs)
        rows = _rows(table)
        if rows:
            widths = [
                max(len(str(row[column])) for row in rows if column < len(row))
                for column in range(max(len(row) for row in rows))
            ]
            for row in rows:
                lines.append(
                    "  ".join(
                        str(cell).ljust(widths[index]) for index, cell in enumerate(row)
                    ).rstrip()
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "font-size:14px;color:#1a1a1a;"
)
_CELL = "padding:4px 10px 4px 0;border-bottom:1px solid #e4e4e4;text-align:left;"
_SIGNAL = {"ok": "#1a7f37", "idle": "#57606a", "degraded": "#b3261e"}


def _as_html(subject: str, sections: Sequence[Section]) -> str:
    parts = [f'<div style="{_STYLE}">', f"<h2>{escape(subject)}</h2>"]
    for heading, paragraphs, table in sections:
        parts.append(f"<h3>{escape(heading)}</h3>")
        for paragraph in paragraphs:
            parts.append(f"<p>{_coloured(paragraph)}</p>")
        if table:
            parts.append('<table style="border-collapse:collapse;width:100%">')
            head = table[0]
            body = _rows(table)
            if any(head):
                body = body[1:]
                cells = "".join(
                    f'<th style="{_CELL}font-weight:600">{escape(str(cell))}</th>'
                    for cell in head
                )
                parts.append(f"<tr>{cells}</tr>")
            for row in body:
                cells = "".join(
                    f'<td style="{_CELL}">{_coloured(str(cell))}</td>' for cell in row
                )
                parts.append(f"<tr>{cells}</tr>")
            parts.append("</table>")
    parts.append("</div>")
    return "\n".join(parts)


def _coloured(text: str) -> str:
    """Escape text, and give a health word the signal colour."""
    safe = escape(text)
    for word, colour in _SIGNAL.items():
        if safe == word:
            return f'<span style="color:{colour};font-weight:600">{safe}</span>'
    return safe
