"""The CLI against a daemon that really serves its API: the commands that go
through HTTP end to end, with the fake plugins behind the daemon."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import bricklogger.cli.config as config_cli
from tests.fakes import FakeDestination
from tests.support import (
    EX,
    INVALID_MODEL,
    MODEL_WITH_RAT,
    RULES_ALL,
    Served,
    wait_for,
)

runner = CliRunner()


def test_config_edit_goes_through_the_api_and_takes_effect(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    deny = "- match: { class: brick:Point }\n  action: deny\n"
    monkeypatch.setattr(config_cli, "edit_text", lambda text: deny)
    code, output = served.invoke("rules", "config", "edit")
    assert code == 0, output
    assert "written and applied" in output
    assert (served.config_dir / "rules.yaml").read_text() == deny
    assert served.daemon.assignment_of("fake_a") == []

    broken = "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"
    monkeypatch.setattr(config_cli, "edit_text", lambda text: broken)
    code, output = served.invoke("rules", "config", "edit")
    assert code == 1
    assert "interval" in output, "the daemon's validation errors are shown"
    assert (served.config_dir / "rules.yaml").read_text() == deny

    monkeypatch.setattr(config_cli, "edit_text", lambda text: None)
    code, output = served.invoke("rules", "config", "edit")
    assert code == 0 and "no change" in output


def test_config_views_and_validate_through_the_api(served: Served) -> None:
    code, output = served.invoke("rules", "config", "show", api=True)
    assert code == 0 and output.endswith(RULES_ALL), output
    assert output.startswith("# http://127.0.0.1:"), "the source of the text is named"

    code, output = served.invoke("sources", "config", "show", "--json", api=True)
    assert code == 0
    assert json.loads(output)["fake_a"]["type"] == "fake-source"

    code, output = served.invoke("validate", api=True)
    assert code == 0 and output.startswith("ok: http://127.0.0.1:")

    code, output = served.invoke("init", "--non-interactive", api=True)
    assert code == 1 and "409" in output

    code, output = served.invoke("init", api=True)
    assert code == 1 and "--non-interactive" in output


def test_points_filters_and_status_through_the_cli(served: Served) -> None:
    wait_for(
        lambda: any(
            w["code"] == "unit_conflict" for w in served.daemon.status_warnings()
        )
    )
    code, output = served.invoke("points", "--warning", "unit_conflict", "--json")
    assert code == 0, output
    assert [item["uri"] for item in json.loads(output)["items"]] == [f"{EX}SAT"]

    code, output = served.invoke(
        "points", "--class", "brick:Zone_Air_Temperature_Sensor", "--json"
    )
    assert code == 0, output
    assert [item["uri"] for item in json.loads(output)["items"]] == [f"{EX}ZAT_1_17"]

    code, output = served.invoke(
        "points", "--class", "brick:Zone_Air_Temperature_Sensor"
    )
    assert code == 0 and "1 points, showing 1 from 0" in output, output
    assert "ex:ZAT_1_17" in output, "URIs are shortened with the model prefixes"

    code, output = served.invoke("status", "warnings")
    assert code == 0 and "unit_conflict" in output and "Last seen" in output

    assert served.daemon.state is not None
    served.daemon.state.warn("stop_timeout", "ghost", "left over")
    code, output = served.invoke("status", "warnings", "clear", "stop_timeout", "nope")
    assert code == 0 and "cleared 0 warnings" in output, output
    code, output = served.invoke(
        "status", "warnings", "clear", "stop_timeout", "ghost", "--json"
    )
    assert code == 0 and json.loads(output) == {"cleared": 1}, output
    code, output = served.invoke("status", "warnings", "tidy")
    assert code == 1 and "clear" in output, output


def test_model_commands_through_the_api(served: Served, tmp_path: Path) -> None:
    model_file = tmp_path / "with_rat.ttl"
    model_file.write_text(MODEL_WITH_RAT)
    code, output = served.invoke("model", "upload", str(model_file))
    assert code == 0, output
    assert "validating..." in output and "activated version 2" in output, output
    assert f"added    {EX}RAT" in output
    assert served.daemon.active_version == 2

    code, output = served.invoke("model", "list")
    assert code == 0 and "active" in output, output

    code, output = served.invoke("model", "export", "--inferred")
    assert code == 0 and "hasPoint" in output, output

    wait_for(lambda: served.daemon.destinations["sink_a"].model_version == 2)
    ours = f'hasTimeseriesId "{FakeDestination.keys["sink_a"][f"{EX}SAT"]}"'
    code, output = served.invoke("model", "export", "--timeseries")
    assert code == 0 and ours in output, output
    code, output = served.invoke(
        "model", "export", "--timeseries", "--destination", "sink_a"
    )
    assert code == 0 and ours in output, output
    code, output = served.invoke("model", "export", "--destination", "sink_a")
    assert code == 1 and "goes with --timeseries" in output, output

    code, output = served.invoke("model", "activate", "1")
    assert code == 0 and "activated version 1" in output, output
    assert served.daemon.active_version == 1

    code, output = served.invoke("model", "diff", "1", "2")
    assert code == 0 and f"added    {EX}RAT" in output, output

    invalid = tmp_path / "invalid.ttl"
    invalid.write_text(INVALID_MODEL)
    code, output = served.invoke("model", "upload", str(invalid))
    assert code == 1 and f"{EX}Bad" in output, output
    assert served.daemon.active_version == 1


def test_query_through_the_cli(served: Served, tmp_path: Path) -> None:
    code, output = served.invoke(
        "query", "SELECT ?p WHERE { ?p a brick:Supply_Air_Temperature_Sensor }"
    )
    assert code == 0, output
    assert "ex:SAT" in output and "1 result" in output, output

    query_file = tmp_path / "ask.rq"
    query_file.write_text("ASK { ex:SAT a brick:Point }")
    code, output = served.invoke("query", f"@{query_file}")
    assert code == 0 and output.strip() == "true", output

    code, output = served.invoke(
        "query",
        "SELECT ?p WHERE { ?p a brick:Supply_Air_Temperature_Sensor }",
        "--json",
    )
    assert (
        code == 0
        and json.loads(output)["results"]["bindings"][0]["p"]["value"] == f"{EX}SAT"
    )

    code, output = served.invoke(
        "query", "SELECT ?p WHERE { ?p a brick:AHU }", "--format", "csv"
    )
    assert (code == 0 and output.startswith("p\r\n")) or output.startswith("p\n"), (
        output
    )

    code, output = served.invoke("query", "INSERT DATA { ex:X a brick:Point }")
    assert code == 1 and "read-only" in output, output


def test_plugins_through_the_cli(served: Served) -> None:
    wait_for(lambda: served.daemon.status_sources()[0]["state"] == "running")
    code, output = served.invoke("plugins")
    assert code == 0 and "fake-source" in output and "fake_a" in output, output

    code, output = served.invoke("plugins", "fake-source")
    assert code == 0 and "echo" in output and "--claims" in output, output

    code, output = served.invoke("plugins", "nope")
    assert code == 1, output


def test_the_role_groups_through_the_cli(served: Served, tmp_path: Path) -> None:
    wait_for(lambda: served.daemon.status_sources()[0]["state"] == "running")
    code, output = served.invoke("sources")
    assert "Usage" in output and "status" in output and "add" in output, output

    code, output = served.invoke("sources", "status")
    assert code == 0 and "fake_a" in output and "running" in output, output

    code, output = served.invoke("destinations", "status")
    assert code == 0 and "sink_a" in output, output

    code, output = served.invoke("sources", "status", "--state", "running", "--json")
    assert code == 0, output
    assert [row["name"] for row in json.loads(output)] == ["fake_a"]

    code, output = served.invoke(
        "sources", "status", "--state", "unconfigured", "--json"
    )
    assert code == 0 and json.loads(output) == [], (
        "every installed type has an instance behind this daemon"
    )

    code, output = served.invoke("sources", "fake_a")
    assert "Usage" in output and "show" in output and "echo" in output, output

    code, output = served.invoke("sources", "fake_a", "show")
    assert code == 0, output
    assert "fake_a (fake-source, source): running" in output
    assert "claims" in output and "tools: echo, fail, dump, claims" in output, output

    code, output = served.invoke(
        "sources", "fake_a", "echo", "--text", "hi", "--times", "2"
    )
    assert code == 0 and "echo: hihi" in output, output

    code, output = served.invoke("sources", "fake_a", "echo", "--text=hi", "--json")
    assert code == 0 and json.loads(output)["echo"] == "hi", output

    code, output = served.invoke("sources", "fake_a", "dump", "--scope", "all")
    assert code == 0 and json.loads(output)["scope"] == "all", "a document is JSON"
    target = tmp_path / "dump.json"
    code, output = served.invoke("sources", "fake_a", "dump", "-o", str(target))
    assert code == 0 and "wrote dump to" in output, output
    assert json.loads(target.read_text())["instance"] == "fake_a"
    code, output = served.invoke("sources", "fake_a", "dump", "--help")
    assert "-o" in output and "document" in output, output

    code, output = served.invoke(
        "sources", "fake_a", "echo", "--text", "hi", "--times", "x"
    )
    assert code == 1 and "times" in output, output

    code, output = served.invoke("destinations", "fake_a", "echo", "--text", "hi")
    assert code == 1 and "no destination named" in output, output

    code, output = served.invoke("sources", "stop", "fake_a")
    assert code == 0 and "fake_a: stopped" in output, output
    assert served.daemon.health() == "idle"

    code, output = served.invoke("sources", "start", "fake_a")
    assert code == 0 and output.startswith("fake_a:"), output
    wait_for(lambda: served.daemon.health() == "ok")

    code, output = served.invoke("sources", "stop", "nope")
    assert code == 1 and "no source named" in output, output


def test_an_instance_is_added_through_the_api(served: Served) -> None:
    code, output = served.invoke(
        "destinations",
        "add",
        "sink_b",
        "--type",
        "fake-destination",
        "--fail-first-start",
        "false",
    )
    assert code == 0, output
    assert "sink_b" in (served.config_dir / "destinations.yaml").read_text()
    wait_for(
        lambda: "sink_b" in {row["name"] for row in served.daemon.status_destinations()}
    )

    code, output = served.invoke("destinations", "remove", "sink_b")
    assert code == 0, output
    wait_for(
        lambda: (
            "sink_b" not in {row["name"] for row in served.daemon.status_destinations()}
        )
    )


def test_status_names_the_instances_and_keeps_only_warnings(served: Served) -> None:
    code, output = served.invoke("status")
    assert code == 0, output
    assert output.startswith("daemon: running"), output
    assert "web:    not running" in output, output
    assert "source fake_a (fake-source)" in output, output
    assert "destination sink_a (fake-destination)" in output, output

    code, output = served.invoke("status", "--json")
    assert code == 0, output
    data = json.loads(output)
    assert data["daemon"]["running"] is True and data["web"]["running"] is False
    assert data["status"]["health"] in ("ok", "idle", "degraded")

    code, output = served.invoke("status", "sources")
    assert code == 1 and "bricklogger sources" in output, output


def test_model_tree_prints_the_hierarchy(served: Served) -> None:
    code, output = served.invoke("model", "tree", "--json")
    assert code == 0
    document = json.loads(output)
    assert document["version"] == 1
    assert document["counts"]["kinds"]["point"] == 6
    assert any(r["role"] == "point" for r in document["relations"])

    code, text = served.invoke("model", "tree")
    assert code == 0
    lines = text.splitlines()
    # Nothing in this model has a location, so the unit is unplaced, and its
    # points hang under it.
    unplaced = lines.index("Unplaced")
    ahu = next(
        i for i, line in enumerate(lines) if line.strip().startswith("ex:AHU_01 ")
    )
    assert ahu > unplaced
    assert lines[ahu] == "  ex:AHU_01  brick:AHU  [no_location]"
    assert lines[ahu + 1].startswith("    ex:"), "its points are indented under it"
    assert "ex:ZAT_1_17  brick:Zone_Air_Temperature_Sensor  active fake_a" in text
    assert "ex:Room_1_17  brick:Room  [deprecated_class]" in text
    # The finding and the daemon's warning share the name, and are printed once.
    meter = next(line for line in lines if "ex:Meter_kWh" in line)
    assert "[no_reference]" in meter and "!no_reference" not in meter
    assert any("!unknown_reference" in line for line in lines)
    assert lines[-1].endswith("with findings")

    code, only_missing = served.invoke("model", "tree", "--finding", "no_reference")
    assert code == 0
    assert "ex:Meter_kWh" in only_missing and "ex:SAT " not in only_missing
    code, rooms = served.invoke("model", "tree", "--kind", "location")
    assert "ex:Room_1_17" in rooms and "ex:SAT" not in rooms
    code, bad = served.invoke("model", "tree", "--sort", "bogus")
    assert code == 1 and "unknown sort" in bad


def test_notify_status_reports_that_notifications_are_off(served: Served) -> None:
    """The standard fixture leaves them off, which the command must say plainly."""
    code, output = served.invoke("notify", "status", "--json")
    assert code == 0, output
    status = json.loads(output)
    assert status["enabled"] is False
    assert status["dormant"] is False

    code, table = served.invoke("notify", "status")
    assert code == 0, table
    assert "off" in table
