"""The web interface against a daemon that really serves its API: the shell,
the overview, the points and daemon screens, the forwarded API, and the login."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bricklogger.web.app import create_web_app, format_time
from bricklogger.web.forms import document_filename, offers_on
from tests.support import (
    EX,
    INVALID_MODEL,
    MODEL_WITH_RAT,
    RULES_ALL,
    Served,
    free_port,
    wait_for,
)


@pytest.fixture
def web(served: Served) -> Iterator[TestClient]:
    with TestClient(create_web_app(served.url, config_dir=served.config_dir)) as client:
        yield client


def test_overview_shows_health_summary_and_warnings(
    web: TestClient, served: Served
) -> None:
    wait_for(lambda: served.daemon.status()["observations_received"] >= 1)
    wait_for(
        lambda: any(
            w["code"] == "unit_conflict" for w in served.daemon.status_warnings()
        )
    )
    page = web.get("/")
    assert page.status_code == 200
    html = page.text
    assert "bricklogger_" in html and "daemon: ok" in html
    assert "unit_conflict" in html and "ex:SAT" in html, "warnings with short URIs"
    assert 'aria-current="page"' in html and "/static/vendor/htmx.min.js" in html
    partial = web.get("/partials/overview", headers={"HX-Request": "true"})
    assert partial.status_code == 200 and "Observations received" in partial.text
    assert "Last seen" in html and ">Clear all<" in html and ">Clear<" in html
    assert "Kind" in html, "the warning table names each code kind"
    assert served.daemon.state is not None
    served.daemon.state.warn("stop_timeout", "ghost", "left over")
    cleared = web.post(
        "/actions/warnings/clear", data={"code": "stop_timeout", "subject": "ghost"}
    )
    assert cleared.status_code == 200 and 'id="overview"' in cleared.text
    assert "stop_timeout" not in cleared.text and "unit_conflict" in cleared.text
    line = web.get("/partials/statusline")
    assert 'hx-swap-oob="true"' in line.text and "&gt; daemon: ok" in line.text
    assert web.get("/static/css/app.css").status_code == 200


def test_points_screen_filters_and_pages(web: TestClient, served: Served) -> None:
    wait_for(
        lambda: any(
            w["code"] == "unit_conflict" for w in served.daemon.status_warnings()
        )
    )
    page = web.get("/points", params={"warning": "unit_conflict"})
    assert page.status_code == 200
    assert "ex:SAT" in page.text and "1 points, showing 1 from 0" in page.text
    assert "ex:ZAT_1_17" not in page.text
    by_class = web.get("/points", params={"class": "brick:CO2_Sensor"})
    assert "no points match" in by_class.text
    # The two text filters search, and the form says so.
    searched = web.get("/points", params={"class": "temperature"})
    assert "ex:SAT" in searched.text and "ex:ZAT_1_17" in searched.text
    assert "ex:SAT" in web.get("/points", params={"warning": "unit"}).text
    assert 'placeholder="Temperature"' in page.text
    assert 'placeholder="unit"' in page.text
    # The class is shown, not only filtered on, so a match can be read.
    assert ">Class<" in page.text
    assert "brick:Supply_Air_Temperature_Sensor" in page.text
    paged = web.get("/points", params={"limit": 1})
    assert "3 points, showing 1 from 0" in paged.text and ">next<" in paged.text
    second = web.get("/partials/points", params={"limit": 1, "offset": 1})
    assert "showing 1 from 1" in second.text and ">previous<" in second.text


def test_daemon_screen_reloads_and_shows_rejections(
    web: TestClient, served: Served
) -> None:
    page = web.get("/daemon")
    assert page.status_code == 200 and "Reload the configuration" in page.text
    assert str(served.config_dir) in page.text
    reloaded = web.post("/actions/daemon/reload", headers={"HX-Request": "true"})
    assert reloaded.status_code == 200 and "&gt; reloaded" in reloaded.text
    (served.config_dir / "rules.yaml").write_text(
        "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"
    )
    try:
        rejected = web.post("/actions/daemon/reload")
        assert "rejected" in rejected.text and "interval" in rejected.text
        assert 'class="terminal signal"' in rejected.text
    finally:
        (served.config_dir / "rules.yaml").write_text(RULES_ALL)


def test_the_api_is_forwarded_under_api(web: TestClient) -> None:
    status = web.get("/api/v1/status")
    assert status.status_code == 200 and status.json()["health"] in ("ok", "idle")
    sparql = web.post(
        "/api/v1/sparql",
        content="ASK { ex:SAT a brick:Point }",
        headers={"Content-Type": "application/sparql-query"},
    )
    assert sparql.status_code == 200 and sparql.json()["boolean"] is True
    missing = web.get("/api/v1/nothing")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == "application/problem+json"
    assert web.get("/api/health/live").json()["live"] == "ok"


def test_health_live_answers_while_the_web_runs(web: TestClient) -> None:
    answer = web.get("/health/live")
    assert answer.status_code == 200
    assert set(answer.json()) == {"live", "pid", "started_at"}, "and nothing more"


def test_a_password_requires_a_login(served: Served) -> None:
    with TestClient(create_web_app(served.url, password="secret")) as web:
        assert web.get("/", follow_redirects=False).status_code == 303
        unauthorised = web.get("/partials/overview", headers={"HX-Request": "true"})
        assert unauthorised.status_code == 401
        assert unauthorised.headers["hx-redirect"] == "/login"
        assert web.get("/login").status_code == 200
        wrong = web.post("/login", data={"password": "nope"})
        assert "not the password" in wrong.text
        right = web.post("/login", data={"password": "secret"}, follow_redirects=False)
        assert (
            right.status_code == 303
            and "bricklogger_session" in right.headers["set-cookie"]
        )
        assert web.get("/").status_code == 200, "the cookie lets the page through"
        assert "log out" in web.get("/").text
        web.post("/logout", follow_redirects=False)
        assert web.get("/", follow_redirects=False).status_code == 303


def test_an_unreachable_daemon_is_shown_plainly() -> None:
    with TestClient(create_web_app(f"http://127.0.0.1:{free_port()}")) as web:
        page = web.get("/")
        assert page.status_code == 200 and "does not answer" in page.text
        assert "daemon not reachable" in page.text
        assert web.get("/points").status_code == 200
        assert web.get("/daemon").status_code == 200
        assert "does not answer" in web.get("/explorer").text
        forwarded = web.get("/api/v1/status")
        assert forwarded.status_code == 503
        assert forwarded.headers["content-type"] == "application/problem+json"


def test_format_time_normalises_to_utc() -> None:
    assert format_time("2026-09-05T14:00:00+02:00") == "2026-09-05 12:00:00"
    assert format_time("2026-09-05T12:00:00Z") == "2026-09-05 12:00:00"
    assert format_time(None) == "" and format_time("not a time") == "not a time"
    assert EX  # the module's constant is used by the other tests through fixtures


def finished_web_job(web: TestClient, job_id: str, timeout: float = 90.0) -> str:
    """Poll the job partial until it stops asking to be polled."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        partial = web.get(f"/partials/job/{job_id}")
        assert partial.status_code == 200, partial.text
        if "hx-trigger" not in partial.text:
            return str(partial.text)
        time.sleep(0.5)
    raise AssertionError("the job did not finish in time")


def test_model_screen_uploads_activates_and_diffs(
    web: TestClient, served: Served
) -> None:
    page = web.get("/model")
    assert page.status_code == 200 and "Upload a model" in page.text
    assert ">1<" in page.text and ">active<" in page.text
    accepted = web.post(
        "/actions/model/upload",
        files={"file": ("with_rat.ttl", MODEL_WITH_RAT.encode(), "text/turtle")},
        data={"activate": "true"},
    )
    assert accepted.status_code == 200, accepted.text
    assert "upload job" in accepted.text and 'hx-trigger="every 1s"' in accepted.text
    job_id = accepted.text.split('id="job-')[1].split('"')[0]
    finished = finished_web_job(web, job_id)
    assert "done" in finished and "stored version 2" in finished, finished
    assert "activated version 2" in finished and "added    ex:RAT" in finished
    assert served.daemon.active_version == 2

    versions = web.get("/partials/model/versions")
    assert ">2<" in versions.text and "/api/v1/models/2?inferred=true" in versions.text
    diff = web.get("/model/diff", params={"a": 1, "b": 2})
    assert diff.status_code == 200 and "ex:RAT" in diff.text and ">added<" in diff.text
    unknown = web.get("/model/diff", params={"a": 1, "b": 9})
    assert "does not exist" in unknown.text

    back = web.post("/actions/model/1/activate")
    assert back.status_code == 200 and "activate job" in back.text
    job_id = back.text.split('id="job-')[1].split('"')[0]
    finished = finished_web_job(web, job_id)
    assert "activated version 1" in finished and "removed  ex:RAT" in finished
    assert served.daemon.active_version == 1

    invalid = web.post(
        "/actions/model/upload",
        files={"file": ("bad.ttl", INVALID_MODEL.encode(), "text/turtle")},
        data={"activate": "true"},
    )
    job_id = invalid.text.split('id="job-')[1].split('"')[0]
    finished = finished_web_job(web, job_id)
    assert "failed" in finished and "ex:Bad" in finished, finished


def test_configuration_screen_validates_and_saves(
    web: TestClient, served: Served
) -> None:
    page = web.get("/config", params={"file": "rules"})
    assert page.status_code == 200
    assert "codemirror.min.js" in page.text and "name: Everything" in page.text
    assert 'class="button quiet" href="/config?file=daemon"' in page.text

    broken = "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"
    validated = web.post(
        "/actions/config/validate", data={"file": "rules", "text": broken}
    )
    assert validated.status_code == 200
    assert "nothing written" in validated.text and "interval" in validated.text
    assert 'data-subject="rule 1"' in validated.text, "errors carry their subject"
    assert (served.config_dir / "rules.yaml").read_text() == RULES_ALL

    valid = web.post(
        "/actions/config/validate", data={"file": "rules", "text": RULES_ALL}
    )
    assert "&gt; valid" in valid.text

    deny = "- match: { class: brick:Point }\n  action: deny\n"
    saved = web.post("/actions/config/save", data={"file": "rules", "text": deny})
    assert saved.status_code == 200 and "written and applied" in saved.text
    assert (served.config_dir / "rules.yaml").read_text() == deny
    assert served.daemon.assignment_of("fake_a") == []
    restored = web.post(
        "/actions/config/save", data={"file": "rules", "text": RULES_ALL}
    )
    assert "written and applied" in restored.text

    rejected = web.post("/actions/config/save", data={"file": "rules", "text": broken})
    assert "nothing written" in rejected.text
    assert (served.config_dir / "rules.yaml").read_text() == RULES_ALL

    exists = web.post("/actions/config/init", follow_redirects=False)
    assert exists.status_code == 303
    assert web.get("/config", params={"file": "web"}).status_code == 404


def test_plugins_screen_lists_installs_and_removes(
    web: TestClient, served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue is the daemon's; install and remove run in this process,
    faked here, and both end with the restart note."""
    from bricklogger.ops.environment import Added, InstancesConfigured, Removed
    from bricklogger.ops.errors import OperationError
    from bricklogger.web import app as web_app

    page = web.get("/plugins")
    assert page.status_code == 200
    assert "fake-source" in page.text and "fake_a" in page.text
    assert 'aria-current="page"' in page.text and ">Remove<" in page.text

    calls: list[Any] = []

    def fake_add(packages: Sequence[str], **_: Any) -> Added:
        calls.append(("add", list(packages)))
        return Added(tuple(packages), ("uv",), "")

    def fake_remove(
        type_name: str, *, config_dir: Path, force: bool = False, **_: Any
    ) -> Removed:
        calls.append(("remove", type_name, force, config_dir))
        if not force:
            raise InstancesConfigured(
                "instances of fake-source are configured: fake_a in sources.yaml"
            )
        return Removed(
            "bricklogger-fake",
            "1.0",
            ("fake-source", "history"),
            ("uv",),
            "",
            ("websockets",),
        )

    monkeypatch.setattr(web_app, "add_plugins", fake_add)
    monkeypatch.setattr(web_app, "remove_plugin", fake_remove)

    added = web.post("/actions/plugins/add", data={"packages": "bricklogger-x ./y.whl"})
    assert added.status_code == 200
    assert "installed bricklogger-x, ./y.whl" in added.text
    assert "restart it to see this" in added.text and "daemon restart" in added.text

    refused = web.post("/actions/plugins/fake-source/remove")
    assert "refused: instances of fake-source" in refused.text
    assert ">Remove anyway<" in refused.text
    forced = web.post("/actions/plugins/fake-source/remove", data={"force": "yes"})
    assert "removed bricklogger-fake 1.0, which provided fake-source" in forced.text
    assert "with it went websockets, which nothing else needed" in forced.text
    assert (
        "with it went history" in forced.text and ">Remove anyway<" not in forced.text
    )
    assert calls == [
        ("add", ["bricklogger-x", "./y.whl"]),
        ("remove", "fake-source", False, served.config_dir),
        ("remove", "fake-source", True, served.config_dir),
    ]

    def deny(packages: Sequence[str], **_: Any) -> Added:
        raise OperationError("no right to write to /opt; run the command with sudo")

    monkeypatch.setattr(web_app, "add_plugins", deny)
    denied = web.post("/actions/plugins/add", data={"packages": "bricklogger-x"})
    assert "run the command with sudo" in denied.text
    assert ">Remove anyway<" not in denied.text and "restart it" not in denied.text


def test_query_screen_carries_yasgui_and_the_prefixes(web: TestClient) -> None:
    page = web.get("/query")
    assert page.status_code == 200
    assert "yasgui.min.js" in page.text and "/api/v1/sparql" in page.text
    assert "PREFIX ex:" in page.text and "PREFIX brick:" in page.text


def test_instances_screen_controls_instances_and_runs_tools(
    web: TestClient, served: Served
) -> None:
    wait_for(lambda: served.daemon.status_sources()[0]["state"] == "running")
    page = web.get("/instances")
    assert page.status_code == 200
    assert "fake_a" in page.text and "sink_a" in page.text and ">running<" in page.text
    assert "fake_a · echo" in page.text and 'name="text"' in page.text
    assert 'name="times"' in page.text and 'value="1"' in page.text, "the default"

    result = web.post(
        "/actions/instances/fake-source/fake_a/tools/echo",
        data={"text": "hi", "times": "2"},
    )
    assert result.status_code == 200 and "hihi" in result.text, result.text
    failed = web.post("/actions/instances/fake-source/fake_a/tools/fail", data={})
    assert "broke" in failed.text and "terminal signal" in failed.text
    invalid = web.post(
        "/actions/instances/fake-source/fake_a/tools/echo",
        data={"text": "hi", "times": "x"},
    )
    assert "times" in invalid.text and "422" in invalid.text

    assert (
        "fake_a · dump" not in page.text and "tools/dump/document" not in page.text
    ), "offered on the rows of claims, so no form of its own"
    listed = web.post("/actions/instances/fake-source/fake_a/tools/claims", data={})
    assert listed.status_code == 200 and "ZAT_1_17" in listed.text, listed.text
    assert listed.text.count("tools/dump/document") == 4, "one per row and the whole"
    assert 'name="scope" value="ZAT_1_17"' in listed.text
    assert ">Download dump<" in listed.text and ">Download<" in listed.text
    document = web.post(
        "/actions/instances/fake-source/fake_a/tools/dump/document",
        data={"scope": "all"},
    )
    assert document.status_code == 200, document.text
    assert document.headers["content-type"].startswith("application/json")
    disposition = document.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="fake_a-dump-all-')
    assert disposition.endswith('.json"'), disposition
    assert document.json()["instance"] == "fake_a" and document.json()["scope"] == "all"
    not_a_document = web.post(
        "/actions/instances/fake-source/fake_a/tools/echo/document", data={"text": "x"}
    )
    assert not_a_document.status_code == 404
    broken = web.post("/actions/instances/fake-source/nope/tools/dump/document")
    assert broken.status_code == 200 and "404" in broken.text, broken.text
    assert broken.headers["content-type"].startswith("text/html"), "the screen"

    stopped = web.post("/actions/instances/fake-source/fake_a/stop")
    assert stopped.status_code == 200 and "fake_a: stopped" in stopped.text
    assert "stopped by the operator" in stopped.text
    assert served.daemon.health() == "idle"
    started = web.post("/actions/instances/fake-source/fake_a/start")
    assert "fake_a:" in started.text
    wait_for(lambda: served.daemon.health() == "ok")
    missing = web.post("/actions/instances/fake-source/nope/restart")
    assert "404" in missing.text
    assert web.post("/actions/instances/fake-source/fake_a/dance").status_code == 404
    partial = web.get("/partials/instances")
    assert partial.status_code == 200 and "sink_a" in partial.text


def test_cross_site_posts_are_refused(web: TestClient) -> None:
    foreign = web.post(
        "/actions/daemon/reload", headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403 and "cross-site" in foreign.text
    fetched = web.post(
        "/api/v1/daemon/reload", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert fetched.status_code == 403
    own = web.post(
        "/actions/daemon/reload",
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )
    assert own.status_code == 200
    assert web.get("/", headers={"Origin": "https://evil.example"}).status_code == 200


def test_document_filename_names_instance_tool_values_and_day() -> None:
    day = date(2026, 9, 9)
    assert (
        document_filename("ibos_main", "pointlist", {}, day)
        == "ibos_main-pointlist-2026-09-09.json"
    )
    assert (
        document_filename("ibos_main", "pointlist", {"project": "4711"}, day)
        == "ibos_main-pointlist-4711-2026-09-09.json"
    )
    assert (
        document_filename("a b", "t", {"values": True, "skip": False, "u": "x/y"}, day)
        == "a_b-t-values-x_y-2026-09-09.json"
    )


def test_offers_on_finds_the_documents_offered_on_a_tool() -> None:
    tools = [
        {"name": "projects", "document": False, "offered_on": None, "parameters": {}},
        {
            "name": "pointlist",
            "document": True,
            "offered_on": {"tool": "projects", "parameters": {"project": "number"}},
            "parameters": {"properties": {"project": {}}},
        },
        {
            "name": "strict",
            "document": True,
            "offered_on": {"tool": "projects", "parameters": {"project": "number"}},
            "parameters": {"properties": {"project": {}}, "required": ["project"]},
        },
    ]
    assert offers_on(tools, "projects") == [
        {"tool": "pointlist", "columns": {"project": "number"}, "whole": True},
        {"tool": "strict", "columns": {"project": "number"}, "whole": False},
    ]
    assert offers_on(tools, "devices") == []


def test_the_explorer_panes_can_be_resized(web: TestClient) -> None:
    html = web.get("/explorer").text
    assert html.count('class="divider"') == 2
    assert 'role="separator"' in html and 'aria-orientation="vertical"' in html
    assert 'tabindex="0"' in html


def test_explorer_screen_draws_the_active_model(web: TestClient) -> None:
    page = web.get("/explorer")
    assert page.status_code == 200
    html = page.text
    assert 'aria-current="page"' in html and ">Explorer<" in html
    assert "/static/vendor/cytoscape.min.js" in html
    assert "/static/js/explorer.js" in html and "/static/css/explorer.css" in html
    assert "Hierarchy" in html and "Neighbourhood" in html
    assert "window.EXPLORER" in html and '"/api/v1/entities"' in html
    assert "no external reference" in html, "the findings are explained on the screen"

    assert web.get("/static/js/explorer.js").status_code == 200
    assert web.get("/static/css/explorer.css").status_code == 200
    assert web.get("/static/vendor/cytoscape.min.js").status_code == 200

    document = web.get("/api/v1/entities").json()
    assert document["version"] == 1
    assert any(e["uri"] == "ex:AHU_01" for e in document["entities"])
    assert any(r["role"] == "point" for r in document["relations"])
    # The screen has what it needs to draw the tree without knowing Brick.
    assert document["classes"]["brick:Zone_Air_Temperature_Sensor"]
    meter = next(e for e in document["entities"] if e["uri"] == "ex:Meter_kWh")
    assert meter["findings"] == ["no_reference"] and meter["warnings"] == [
        "no_reference"
    ], "the same name on both lenses; the screen shows it once"

    # The status line carries the active version, which is how the screen
    # notices that another one has been activated.
    assert 'data-model="1"' in web.get("/partials/statusline").text


def test_the_query_screen_accepts_a_query(web: TestClient) -> None:
    page = web.get("/query", params={"query": "DESCRIBE ex:AHU_01"})
    assert page.status_code == 200
    assert "DESCRIBE ex:AHU_01" in page.text
    assert "PREFIX brick:" in page.text


def test_notifications_have_a_screen_of_their_own(web: TestClient) -> None:
    page = web.get("/notifications")
    assert page.status_code == 200
    html = page.text
    assert 'href="/notifications" aria-current="page"' in html, "in the sidebar"
    assert "Send test mail" in html and "/actions/notifications/test" in html
    assert '<td class="dim">state</td><td>off</td>' in html, (
        "the state row says what it shows, not the daemon's health"
    )
    assert ">none</td>" in html, "an unset recipient list is not blank"
    partial = web.get("/partials/notifications", headers={"HX-Request": "true"})
    assert partial.status_code == 200 and 'id="notifications"' in partial.text
    assert "next summary" in partial.text
    overview = web.get("/").text
    assert "/actions/notifications/test" not in overview, (
        "the overview keeps to health and warnings"
    )
    assert "next summary" not in overview


def test_a_test_mail_without_a_server_says_so_under_the_button(
    web: TestClient,
) -> None:
    """Notifications are off in the fixture, so the test mail has nowhere to go."""
    page = web.post("/actions/notifications/test")
    assert page.status_code == 200
    assert 'id="notify-test"' in page.text and "could not be sent" in page.text
