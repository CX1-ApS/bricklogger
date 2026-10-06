"""Notifications: what opens, what closes, and when a mail says so.

The policy is exercised against a small stand-in for the daemon and a mailer
that keeps what it is given, so a pass costs nothing; the transport is proved
once against a mail server that really speaks SMTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from socket import gethostname
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pyoxigraph import RdfFormat

from bricklogger.config.schema import NotificationSettings, SmtpSettings
from bricklogger.daemon.api import create_app
from bricklogger.daemon.codes import NOTIFICATIONS_DORMANT, NOTIFY_FAILED, kind_of
from bricklogger.daemon.core import Daemon
from bricklogger.daemon.explorer import RDF_TYPE, RDFS_LABEL
from bricklogger.daemon.notify import Notifier, SmtpMailer, building_names
from bricklogger.daemon.rules import BRICK
from bricklogger.daemon.state import STATE_FILE, RuntimeState
from bricklogger.model.working_graph import MODEL_GRAPH, WorkingGraph
from tests.fakes import FakeSmtpServer
from tests.support import DESTINATIONS, REGISTRY, SOURCES, make_config

START = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


class Clock:
    """A clock the tests move by hand; the project keeps no fake clock."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **amount: float) -> None:
        self.now += timedelta(**amount)


class FakeDaemon:
    """The little of the daemon the notifier reads."""

    def __init__(self, state: RuntimeState) -> None:
        self.state: RuntimeState | None = state
        self.active_version: int | None = 1
        self.graph: WorkingGraph | None = None
        self.current_health = "ok"

    def health(self) -> str:
        return self.current_health

    def status(self) -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "health": self.current_health,
            "uptime_seconds": 3600,
            "model": {"active": {"version": 1}},
            "points": {"accepted": 3, "active": 3},
            "instances": [
                {
                    "name": "bacnet_main",
                    "role": "source",
                    "type": "bacnet-ip",
                    "state": "running",
                }
            ],
            "observations_received": 42,
        }


class RecordingMailer:
    """Keeps every message, and fails on demand."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.failure = failure

    def send(self, message: EmailMessage) -> str:
        if self.failure is not None:
            raise self.failure
        self.sent.append(message)
        return "250 fake.example.com"


@pytest.fixture
def state(tmp_path: Path) -> Iterator[RuntimeState]:
    runtime = RuntimeState(tmp_path / STATE_FILE)
    yield runtime
    runtime.close()


@pytest.fixture
def daemon(state: RuntimeState) -> FakeDaemon:
    return FakeDaemon(state)


def make_settings(
    *,
    enabled: bool = True,
    window: timedelta = timedelta(minutes=2),
    min_interval: timedelta = timedelta(minutes=15),
    digest: time = time(7, 0),
) -> NotificationSettings:
    return NotificationSettings(
        enabled=enabled,
        smtp=SmtpSettings(host="smtp.example.com"),
        sender="bricklogger@example.com",
        to=["drift@example.com"],
        window=window,
        min_interval=min_interval,
        digest=digest,
    )


def make_notifier(
    daemon: FakeDaemon,
    mailer: RecordingMailer,
    clock: Clock,
    settings: NotificationSettings | None = None,
) -> Notifier:
    return Notifier(
        daemon,
        settings if settings is not None else make_settings(),
        mailer=mailer,
        attempts=1,
        backoff=(0.0,),
        clock=clock,
    )


def parts(message: EmailMessage) -> dict[str, str]:
    """Each non-multipart part of a message, by content type."""
    found: dict[str, str] = {}
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            found[part.get_content_type()] = payload.decode("utf-8")
    return found


def settle(notifier: Notifier, clock: Clock) -> None:
    """Open the window with one pass, then let it close with another."""
    notifier.tick()
    clock.advance(minutes=3)
    notifier.tick()


# --- what opens and what closes --------------------------------------------


def test_nothing_is_sent_while_notifications_are_off(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock, make_settings(enabled=False))
    state.warn("instance_failed", "tsdb", "the database refused the connection")
    settle(notifier, clock)
    assert mailer.sent == []


def test_a_condition_that_opens_is_mailed_after_the_window(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "the database refused the connection")

    notifier.tick()
    assert mailer.sent == [], "the window has not passed yet"

    clock.advance(minutes=3)
    notifier.tick()
    assert len(mailer.sent) == 1
    assert "instance_failed on tsdb" in str(mailer.sent[0]["Subject"])
    assert "the database refused the connection" in parts(mailer.sent[0])["text/plain"]


def test_one_outage_is_one_mail_with_the_subjects_counted(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    for number in range(142):
        state.warn("read_error", f"ex:P{number}", "no response")

    settle(notifier, clock)
    assert len(mailer.sent) == 1
    text = parts(mailer.sent[0])["text/plain"]
    assert "142" in text
    assert "and 137 more" in text
    assert "142 conditions opened" in str(mailer.sent[0]["Subject"])


def test_a_condition_that_ends_gives_an_all_clear(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "the database refused the connection")
    settle(notifier, clock)

    state.clear_warnings("instance_failed", "tsdb")
    clock.advance(minutes=20)
    settle(notifier, clock)

    assert len(mailer.sent) == 2
    assert "is over" in str(mailer.sent[1]["Subject"])
    assert "Over" in parts(mailer.sent[1])["text/plain"]


def test_a_manual_clear_sends_no_all_clear(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    """An acknowledgement is not an end, so nothing is declared over."""
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "the database refused the connection")
    settle(notifier, clock)

    state.clear_warnings("instance_failed", "tsdb", forget_notified=True)
    clock.advance(minutes=20)
    settle(notifier, clock)

    assert len(mailer.sent) == 1


def test_a_model_warning_waits_for_the_summary(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("no_reference", "ex:SAT", "the point has no external reference")

    assert kind_of("no_reference") == "model"
    settle(notifier, clock)
    assert mailer.sent == []

    notifier.tick()
    clock.advance(days=1)
    notifier.tick()
    assert len(mailer.sent) == 1
    text = parts(mailer.sent[0])["text/plain"]
    assert "daily summary" in str(mailer.sent[0]["Subject"])
    assert "no_reference" in text


def test_the_floor_holds_the_next_mail_back(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "down")
    settle(notifier, clock)
    assert len(mailer.sent) == 1

    state.warn("read_error", "ex:SAT", "no response")
    clock.advance(minutes=5)
    settle(notifier, clock)
    assert len(mailer.sent) == 1, "under the floor, so it waits"

    clock.advance(minutes=20)
    notifier.tick()
    assert len(mailer.sent) == 2


def test_a_restart_does_not_mail_what_was_already_reported(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    make_and_send = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "down")
    settle(make_and_send, clock)
    assert len(mailer.sent) == 1

    clock.advance(hours=2)
    restarted = make_notifier(daemon, mailer, clock)
    settle(restarted, clock)
    assert len(mailer.sent) == 1


# --- the daily summary ------------------------------------------------------


def test_the_summary_is_sent_at_its_hour_even_when_all_is_green(
    daemon: FakeDaemon,
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)

    notifier.tick()
    assert mailer.sent == [], "the first pass only arms the summary"

    clock.advance(days=1)
    notifier.tick()
    assert len(mailer.sent) == 1
    assert "daily summary: ok" in str(mailer.sent[0]["Subject"])
    text = parts(mailer.sent[0])["text/plain"]
    assert "Nothing stands." in text
    assert "bacnet_main" in text


def test_the_summary_comes_once_a_day(daemon: FakeDaemon) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    notifier.tick()
    for _ in range(6):
        clock.advance(hours=4)
        notifier.tick()
    assert len(mailer.sent) == 1


# --- the model requirement and failures -------------------------------------


def test_notifications_are_dormant_without_a_model(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    daemon.active_version = None
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "down")

    settle(notifier, clock)
    assert mailer.sent == []
    assert state.has_warning(NOTIFICATIONS_DORMANT)

    daemon.active_version = 1
    settle(notifier, clock)
    assert not state.has_warning(NOTIFICATIONS_DORMANT)
    assert len(mailer.sent) == 1


def test_a_failure_is_recorded_and_never_mails_about_itself(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer(failure=OSError("connection refused"))
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "down")

    settle(notifier, clock)
    assert mailer.sent == []
    assert state.has_warning(NOTIFY_FAILED)

    # The failure stands, but it is never itself a reason to send.
    mailer.failure = None
    clock.advance(minutes=20)
    notifier.tick()
    assert len(mailer.sent) == 1
    assert NOTIFY_FAILED not in parts(mailer.sent[0])["text/plain"]
    assert not state.has_warning(NOTIFY_FAILED)


# --- what a mail looks like -------------------------------------------------


def test_every_mail_carries_a_text_part_and_an_html_part(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, clock)
    state.warn("instance_failed", "tsdb", "down")
    settle(notifier, clock)

    content = parts(mailer.sent[0])
    assert set(content) == {"text/plain", "text/html"}
    assert "<table" in content["text/html"]
    assert "instance_failed" in content["text/html"]


def test_the_subject_names_the_building_and_the_machine(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    graph = WorkingGraph(tmp_path / "graph")
    subject = "<https://example.com/bldg#B1>"
    quads = (
        f"{subject} <{RDF_TYPE}> <{BRICK}Building> <{MODEL_GRAPH}> .\n"
        f'{subject} <{RDFS_LABEL}> "Baltorpvej 20" <{MODEL_GRAPH}> .\n'
    )
    graph.store.load(quads.encode("utf-8"), RdfFormat.N_QUADS)
    daemon.graph = graph

    assert building_names(graph) == ["Baltorpvej 20"]
    notifier = make_notifier(daemon, RecordingMailer(), Clock())
    assert notifier.site() == f"Baltorpvej 20 / {gethostname()}"
    assert notifier.subject("2 instances failed").startswith("[Baltorpvej 20 / ")


def test_a_model_without_a_building_leaves_the_machine_alone(
    daemon: FakeDaemon,
) -> None:
    notifier = make_notifier(daemon, RecordingMailer(), Clock())
    assert notifier.site() == gethostname()


# --- the transport ----------------------------------------------------------


def test_the_transport_really_speaks_smtp(daemon: FakeDaemon) -> None:
    server = FakeSmtpServer()
    try:
        settings = SmtpSettings(host="127.0.0.1", port=server.port, security="none")
        notifier = Notifier(
            daemon,
            make_settings(),
            mailer=SmtpMailer(settings, timeout=5),
            clock=Clock(),
        )
        answer = notifier.send_test()
    finally:
        server.stop()

    assert answer["sent"] is True
    assert answer["answer"].startswith("250 queued"), (
        "the answer is the server's last word, not its greeting"
    )
    assert len(server.received) == 1
    raw = server.received[0].decode("utf-8")
    assert "Subject: " in raw
    assert "test mail" in raw
    assert "multipart/alternative" in raw


def test_starttls_knows_which_host_it_is_verifying(daemon: FakeDaemon) -> None:
    """A client built without the host cannot start TLS at all.

    The fake answers 220 to STARTTLS and then speaks plain text, so the
    handshake fails either way. What must not appear is the failure that comes
    *before* the handshake, when the client has no name to verify against.
    """
    server = FakeSmtpServer(starttls=True)
    try:
        settings = SmtpSettings(host="127.0.0.1", port=server.port, security="starttls")
        notifier = Notifier(
            daemon,
            make_settings(),
            mailer=SmtpMailer(settings, timeout=5),
            clock=Clock(),
        )
        answer = notifier.send_test()
    finally:
        server.stop()

    assert answer["sent"] is False, "the fake server does not really speak TLS"
    assert "server_hostname" not in answer["error"], (
        "the client reached TLS without knowing which host it verifies"
    )


def test_a_refused_recipient_is_reported(daemon: FakeDaemon) -> None:
    server = FakeSmtpServer(reject="drift@example.com")
    try:
        settings = SmtpSettings(host="127.0.0.1", port=server.port, security="none")
        notifier = Notifier(
            daemon,
            make_settings(),
            mailer=SmtpMailer(settings, timeout=5),
            clock=Clock(),
        )
        answer = notifier.send_test()
    finally:
        server.stop()

    assert answer["sent"] is False
    assert "drift@example.com" in answer["error"]


def test_a_server_that_is_not_there_is_reported_not_raised(
    daemon: FakeDaemon,
) -> None:
    settings = SmtpSettings(host="127.0.0.1", port=9, security="none")
    notifier = Notifier(
        daemon, make_settings(), mailer=SmtpMailer(settings, timeout=2), clock=Clock()
    )
    answer = notifier.send_test()
    assert answer["sent"] is False
    assert answer["error"]


# --- what status says -------------------------------------------------------


def test_status_says_what_waits_in_the_window(
    daemon: FakeDaemon, state: RuntimeState
) -> None:
    clock = Clock()
    notifier = make_notifier(daemon, RecordingMailer(), clock)
    state.warn("instance_failed", "tsdb", "down")
    notifier.tick()

    status = notifier.status(clock.now)
    assert status["enabled"] is True
    assert status["dormant"] is False
    assert status["to"] == ["drift@example.com"]
    assert status["waiting"]["opened"] == 1
    assert status["waiting"]["since"] is not None
    assert status["next_digest"] > clock.now.isoformat()


# --- the daemon, the API and the CLI ----------------------------------------


def notifications_yaml(host: str, port: int) -> str:
    return (
        "notifications:\n"
        "  enabled: true\n"
        "  smtp:\n"
        f"    host: {host}\n"
        f"    port: {port}\n"
        "    security: none\n"
        "  from: bricklogger@example.com\n"
        "  to:\n"
        "    - drift@example.com\n"
    )


def configured(tmp_path: Path, template: Path, host: str, port: int) -> Path:
    return make_config(
        tmp_path,
        template,
        sources=SOURCES,
        destinations=DESTINATIONS,
        daemon_extra=notifications_yaml(host, port),
    )


def test_the_daemon_holds_the_start_and_the_stop_for_one_mail(
    tmp_path: Path, template: Path
) -> None:
    """A stop is recorded, not sent, so an upgrade is one mail that says both."""
    server = FakeSmtpServer()
    daemon = Daemon(
        configured(tmp_path, template, "127.0.0.1", server.port),
        registry=REGISTRY,
        env={},
    )
    daemon.start()
    try:
        assert daemon.notifier is not None
        assert daemon.state is not None
        events = json.loads(daemon.state.memory("events") or "[]")
        assert [event["event"] for event in events] == ["started"]
    finally:
        daemon.stop()
        server.stop()

    reopened = RuntimeState(tmp_path / "var" / STATE_FILE)
    try:
        events = json.loads(reopened.memory("events") or "[]")
        assert [event["event"] for event in events] == ["started", "stopped"]
    finally:
        reopened.close()
    assert server.received == [], "nothing is sent on the way down"


def test_the_api_answers_for_notifications_and_sends_a_test_mail(
    tmp_path: Path, template: Path
) -> None:
    server = FakeSmtpServer()
    daemon = Daemon(
        configured(tmp_path, template, "127.0.0.1", server.port),
        registry=REGISTRY,
        env={},
    )
    daemon.start()
    try:
        client = TestClient(create_app(daemon))
        status = client.get("/v1/notifications").json()
        assert status["enabled"] is True
        assert status["dormant"] is False
        assert status["to"] == ["drift@example.com"]

        sent = client.post("/v1/notifications/test")
        assert sent.status_code == 200, sent.text
        assert sent.json()["sent"] is True
        assert len(server.received) == 1
        assert "test mail" in server.received[0].decode("utf-8")
    finally:
        daemon.stop()
        server.stop()


def test_a_test_mail_that_fails_answers_with_a_problem(
    tmp_path: Path, template: Path
) -> None:
    daemon = Daemon(
        configured(tmp_path, template, "127.0.0.1", 9), registry=REGISTRY, env={}
    )
    daemon.start()
    try:
        response = TestClient(create_app(daemon)).post("/v1/notifications/test")
        assert response.status_code == 502
        body = response.json()
        assert body["title"] == "The test mail could not be sent"
        assert body["to"] == ["drift@example.com"]
    finally:
        daemon.stop()


def test_a_reload_points_the_mailer_at_the_new_server(daemon: FakeDaemon) -> None:
    """The mailer holds the settings it was built with, so a reload rebuilds it.

    Without this, naming a mail server and reloading changed only what status
    showed, and the mail itself waited for a restart.
    """
    notifier = Notifier(daemon, make_settings(), clock=Clock())
    assert isinstance(notifier.mailer, SmtpMailer)
    assert notifier.mailer.settings.host == "smtp.example.com"

    moved = make_settings().model_copy(
        update={"smtp": SmtpSettings(host="mail.example.net")}
    )
    notifier.reconfigure(moved)
    assert isinstance(notifier.mailer, SmtpMailer)
    assert notifier.mailer.settings.host == "mail.example.net"


def test_a_reload_leaves_a_mailer_it_was_handed_alone(daemon: FakeDaemon) -> None:
    mailer = RecordingMailer()
    notifier = make_notifier(daemon, mailer, Clock())
    notifier.reconfigure(make_settings(enabled=False))
    assert notifier.mailer is mailer
    assert notifier.settings.enabled is False
