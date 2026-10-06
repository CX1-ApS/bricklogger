"""The command tree's root, init, validate, the config views, the role groups
and status, all without a daemon."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bricklogger import __version__
from bricklogger.cli import app
from tests.fakes import write_distribution
from tests.support import free_port

runner = CliRunner()


def init(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "init", "--non-interactive"]
    )
    assert result.exit_code == 0, result.output


def test_version_is_printed() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"bricklogger {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


def test_a_group_without_a_subcommand_shows_its_help(tmp_path: Path) -> None:
    for group in ("sources", "destinations", "rules", "daemon"):
        result = runner.invoke(app, ["--config-dir", str(tmp_path), group])
        assert "Usage" in result.output and "config" in result.output, group
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "sources"])
    for word in ("status", "add", "edit", "remove", "start", "stop", "restart"):
        assert word in result.output, word
    bare = runner.invoke(app, ["--config-dir", str(tmp_path), "sources", "config"])
    assert "Usage" in bare.output and "show" in bare.output and "edit" in bare.output


def test_init_writes_the_examples_once(tmp_path: Path) -> None:
    config_dir = tmp_path / "etc" / "bricklogger"
    result = runner.invoke(
        app, ["--config-dir", str(config_dir), "init", "--non-interactive"]
    )
    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in config_dir.iterdir()) == [
        "daemon.yaml",
        "destinations.yaml",
        "rules.yaml",
        "sources.yaml",
    ]
    again = runner.invoke(
        app, ["--config-dir", str(config_dir), "init", "--non-interactive"]
    )
    assert again.exit_code == 1
    assert "refusing to overwrite" in again.output


def test_init_guided_asks_from_the_schemas_and_keeps_the_secret_apart(
    tmp_path: Path,
) -> None:
    answers = [
        "1",  # add a source: bacnet-ip
        "",  # its name: the suggested bacnet_ip_main
        "192.168.10.5/24",  # address, required
        "1200",  # device_instance, required
        "",  # device_name
        "",  # vendor_id
        "",  # devices
        "",  # subnet
        "",  # bbmd
        "",  # timeout
        "",  # retries
        "",  # max_in_flight
        "",  # another source? no
        "1",  # add a destination: timescaledb
        "tsdb",  # its name
        "postgres://bricklogger@db:5432/brick",  # dsn, required
        "hunter2",  # password, a secret
        "",  # another destination? no
    ]
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "init"], input="\n".join(answers) + "\n"
    )
    assert result.exit_code == 0, result.output
    sources = (tmp_path / "sources.yaml").read_text()
    assert "bacnet_ip_main:" in sources
    assert "address: 192.168.10.5/24" in sources
    assert "device_instance: 1200" in sources, "typed by the schema"
    assert "device_name" not in sources, "an optional setting left at its default"
    destinations = (tmp_path / "destinations.yaml").read_text()
    assert "tsdb:" in destinations
    assert "password: ${TSDB_PASSWORD}" in destinations
    assert "hunter2" not in destinations
    assert "TSDB_PASSWORD=hunter2" in (tmp_path / "env").read_text()
    assert "hunter2" not in result.output, "asked for without echo"
    assert "ok:" in result.output, "validated with the secret from the env file"
    assert (tmp_path / "rules.yaml").exists() and (tmp_path / "daemon.yaml").exists()

    again = runner.invoke(app, ["--config-dir", str(tmp_path), "init"], input="\n")
    assert again.exit_code == 1 and "refusing to overwrite" in again.output


def test_validate_after_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init(tmp_path)
    monkeypatch.delenv("TSDB_PASSWORD", raising=False)
    missing = runner.invoke(app, ["--config-dir", str(tmp_path), "validate"])
    assert missing.exit_code == 1
    assert "TSDB_PASSWORD" in missing.output

    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    ok = runner.invoke(app, ["--config-dir", str(tmp_path), "validate"])
    assert ok.exit_code == 0, ok.output
    assert ok.output.startswith("ok:")
    assert "1 source, 1 destination, 1 rule" in ok.output

    as_json = runner.invoke(app, ["--config-dir", str(tmp_path), "validate", "--json"])
    assert as_json.exit_code == 0
    assert json.loads(as_json.output) == {"valid": True, "errors": [], "warnings": []}


def test_validate_uses_the_environment_variable_for_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BRICKLOGGER_CONFIG_DIR", str(tmp_path))
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert str(tmp_path) in result.output


def test_the_config_views_print_the_files_as_written(tmp_path: Path) -> None:
    (tmp_path / "destinations.yaml").write_text(
        "tsdb:\n  type: timescaledb\n  dsn: x\n  password: ${TSDB_PASSWORD}\n"
    )
    shown = runner.invoke(
        app, ["--config-dir", str(tmp_path), "destinations", "config", "show"]
    )
    assert shown.exit_code == 0, shown.output
    assert "${TSDB_PASSWORD}" in shown.output, "secrets stay as written"
    assert str(tmp_path) in shown.output, "the directory it read is named"

    missing = runner.invoke(
        app, ["--config-dir", str(tmp_path), "daemon", "config", "show"]
    )
    assert missing.exit_code == 0, missing.output
    assert "does not exist and is read as empty" in missing.output

    as_json = runner.invoke(
        app, ["--config-dir", str(tmp_path), "destinations", "config", "show", "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["tsdb"]["password"] == "${TSDB_PASSWORD}"

    rules = runner.invoke(
        app, ["--config-dir", str(tmp_path), "rules", "config", "show", "--json"]
    )
    assert rules.exit_code == 0 and json.loads(rules.output) == []


def test_config_edit_offline_validates_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bricklogger.cli.config as config_cli

    init(tmp_path)
    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    base = ["--config-dir", str(tmp_path), "rules", "config", "edit"]
    deny = "- match: { class: brick:Point }\n  action: deny\n"
    monkeypatch.setattr(config_cli, "edit_text", lambda text: deny)
    written = runner.invoke(app, base)
    assert written.exit_code == 0, written.output
    assert (tmp_path / "rules.yaml").read_text() == deny

    broken = "- match: { class: brick:Point }\n  action: accept\n  method: poll\n"
    monkeypatch.setattr(config_cli, "edit_text", lambda text: broken)
    invalid = runner.invoke(app, base)
    assert invalid.exit_code == 1
    assert "not written" in invalid.output and "interval" in invalid.output
    assert (tmp_path / "rules.yaml").read_text() == deny

    monkeypatch.setattr(config_cli, "edit_text", lambda text: None)
    unchanged = runner.invoke(app, base)
    assert unchanged.exit_code == 0 and "no change" in unchanged.output


def test_sources_and_destinations_are_configured_from_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    plugins = tmp_path / "site"
    write_distribution(
        plugins,
        "bricklogger-fake",
        "1.0",
        entry_points={"bricklogger.sources": "fake-source = tests.fakes:FAKE_SOURCE"},
    )
    monkeypatch.syspath_prepend(str(plugins))
    init(tmp_path)
    base = ["--config-dir", str(tmp_path)]

    listed = runner.invoke(app, [*base, "sources", "status"])
    assert listed.exit_code == 0, listed.output
    assert "bacnet_main" in listed.output
    assert "no daemon answers" in listed.output
    assert "fake-source" not in listed.output, (
        "an installed type without an instance is not a source, so it is not a row"
    )

    catalogue = runner.invoke(
        app, [*base, "sources", "status", "--state", "unconfigured", "--json"]
    )
    assert catalogue.exit_code == 0, catalogue.output
    payload = catalogue.output[catalogue.output.index("[") :]
    assert [row["type"] for row in json.loads(payload)] == ["fake-source"], (
        "an installed type without an instance"
    )

    added = runner.invoke(
        app,
        [
            *base,
            "sources",
            "add",
            "bacnet_second",
            "--type",
            "bacnet-ip",
            "--address",
            "10.0.0.5/24",
            "--device-instance",
            "1201",
        ],
    )
    assert added.exit_code == 0, added.output
    text = (tmp_path / "sources.yaml").read_text()
    assert "bacnet_second" in text
    assert "# sources.yaml" in text, "the file's comments survive"
    assert "device_instance: 1201" in text, "the value is typed by the schema"

    again = runner.invoke(
        app, [*base, "sources", "add", "bacnet_second", "--type", "bacnet-ip"]
    )
    assert again.exit_code == 1 and "already configured" in again.output

    unknown = runner.invoke(
        app,
        [*base, "sources", "add", "third", "--type", "bacnet-ip", "--nonsense", "1"],
    )
    assert unknown.exit_code == 1
    assert "no setting --nonsense" in unknown.output
    assert "--address" in unknown.output, "the settings it does take are listed"

    wrong_role = runner.invoke(
        app, [*base, "sources", "add", "oops", "--type", "timescaledb", "--dsn", "x"]
    )
    assert wrong_role.exit_code == 1
    assert "is a destination" in wrong_role.output

    reserved = runner.invoke(
        app, [*base, "sources", "add", "status", "--type", "bacnet-ip"]
    )
    assert reserved.exit_code == 1 and "subcommand" in reserved.output

    edited = runner.invoke(
        app, [*base, "sources", "edit", "bacnet_second", "--device-instance", "1202"]
    )
    assert edited.exit_code == 0, edited.output
    assert "device_instance: 1202" in (tmp_path / "sources.yaml").read_text()
    assert "address: 10.0.0.5/24" in (tmp_path / "sources.yaml").read_text()

    missing = runner.invoke(app, [*base, "sources", "edit", "nope", "--retries", "1"])
    assert missing.exit_code == 1 and "no source named" in missing.output

    secret = runner.invoke(
        app,
        [
            *base,
            "destinations",
            "add",
            "archive",
            "--type",
            "timescaledb",
            "--dsn",
            "postgres://bricklogger@archive:5432/brick",
            "--password-env",
            "TSDB_PASSWORD",
        ],
    )
    assert secret.exit_code == 0, secret.output
    assert "password: ${TSDB_PASSWORD}" in (tmp_path / "destinations.yaml").read_text()

    literal = runner.invoke(
        app,
        [
            *base,
            "destinations",
            "edit",
            "archive",
            "--password",
            "in-the-clear",
        ],
    )
    assert literal.exit_code == 1, literal.output
    assert "is a secret" in literal.output and "--password-env" in literal.output
    assert "in-the-clear" not in (tmp_path / "destinations.yaml").read_text()

    removed = runner.invoke(app, [*base, "destinations", "remove", "archive"])
    assert removed.exit_code == 0, removed.output
    assert "archive" not in (tmp_path / "destinations.yaml").read_text()

    gone = runner.invoke(app, [*base, "destinations", "remove", "archive"])
    assert gone.exit_code == 1 and "no destination named" in gone.output

    ok = runner.invoke(app, [*base, "validate"])
    assert ok.exit_code == 0, ok.output


def test_edit_without_settings_walks_through_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    init(tmp_path)
    base = ["--config-dir", str(tmp_path)]
    answers = [
        "",  # address: Enter keeps 192.168.10.5/24
        "1300",  # device_instance: a new value
        "",  # device_name
        "",  # vendor_id
        "",  # devices
        "",  # subnet
        "",  # bbmd
        "",  # timeout
        "",  # retries
        "",  # max_in_flight
    ]
    edited = runner.invoke(
        app, [*base, "sources", "edit", "bacnet_main"], input="\n".join(answers) + "\n"
    )
    assert edited.exit_code == 0, edited.output
    text = (tmp_path / "sources.yaml").read_text()
    assert "address: 192.168.10.5/24" in text, "kept with Enter"
    assert "device_instance: 1300" in text, "changed, and typed by the schema"
    assert "# sources.yaml" in text, "the file's comments survive"

    kept = runner.invoke(app, [*base, "destinations", "edit", "tsdb"], input="\n\n")
    assert kept.exit_code == 0, kept.output
    assert "kept in the env file as TSDB_PASSWORD" in kept.output
    assert "secret" not in kept.output, "the value is never shown"
    assert "password: ${TSDB_PASSWORD}" in (tmp_path / "destinations.yaml").read_text()
    assert not (tmp_path / "env").exists(), "Enter changes nothing"

    added = runner.invoke(
        app,
        [*base, "destinations", "add", "archive", "--type", "timescaledb"],
        input="postgres://bricklogger@archive:5432/brick\nhunter2\n",
    )
    assert added.exit_code == 0, added.output
    destinations = (tmp_path / "destinations.yaml").read_text()
    assert "password: ${ARCHIVE_PASSWORD}" in destinations
    assert "hunter2" not in destinations
    assert "ARCHIVE_PASSWORD=hunter2" in (tmp_path / "env").read_text()


def test_an_instance_is_shown_by_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    init(tmp_path)
    bare = runner.invoke(app, ["--config-dir", str(tmp_path), "sources", "bacnet_main"])
    assert "Usage" in bare.output and "show" in bare.output, bare.output
    assert "discover" in bare.output, "the type's tools are commands"

    tool_help = runner.invoke(
        app, ["--config-dir", str(tmp_path), "sources", "bacnet_main", "read", "--help"]
    )
    assert tool_help.exit_code == 0, tool_help.output
    assert "--device" in tool_help.output and "--object" in tool_help.output

    shown = runner.invoke(
        app, ["--config-dir", str(tmp_path), "sources", "bacnet_main", "show"]
    )
    assert shown.exit_code == 0, shown.output
    assert "bacnet_main (bacnet-ip, source): unknown" in shown.output
    assert "address: 192.168.10.5/24" in shown.output

    as_json = runner.invoke(
        app, ["--config-dir", str(tmp_path), "destinations", "tsdb", "show", "--json"]
    )
    assert as_json.exit_code == 0, as_json.output
    data = json.loads(as_json.output)
    assert data["type"] == "timescaledb" and data["status"] is None
    assert data["configuration"]["password"] == "${TSDB_PASSWORD}"

    nobody = runner.invoke(app, ["--config-dir", str(tmp_path), "sources", "nobody"])
    assert nobody.exit_code == 1 and "no source named 'nobody'" in nobody.output


def test_a_reserved_instance_name_is_refused_by_validation(tmp_path: Path) -> None:
    (tmp_path / "sources.yaml").write_text(
        "status:\n  type: bacnet-ip\n  address: 10.0.0.5/24\n  device_instance: 1\n"
    )
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "validate"])
    assert result.exit_code == 1
    assert "subcommand" in result.output


def test_an_invalid_instance_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TSDB_PASSWORD", "secret")
    init(tmp_path)
    before = (tmp_path / "sources.yaml").read_text()
    result = runner.invoke(
        app,
        [
            "--config-dir",
            str(tmp_path),
            "sources",
            "add",
            "bacnet_second",
            "--type",
            "bacnet-ip",
            "--address",
            "not an address",
        ],
    )
    assert result.exit_code == 1
    assert "not written" in result.output
    assert (tmp_path / "sources.yaml").read_text() == before


def test_a_state_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "sources", "status", "--state", "nonsense"]
    )
    assert result.exit_code == 1
    assert "unknown state" in result.output and "unconfigured" in result.output


def test_status_answers_when_nothing_runs(tmp_path: Path) -> None:
    (tmp_path / "daemon.yaml").write_text(
        f"data_dir: {tmp_path / 'var'}\napi:\n  port: {free_port()}\n"
        f"web:\n  port: {free_port()}\nmcp:\n  port: {free_port()}\n"
    )
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "status"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("daemon: not running — nothing answers at http://")
    assert lines[1].startswith("web:    not running — nothing answers at http://")
    assert lines[2].startswith("mcp:    not running — nothing answers at http://")
    assert lines[2].endswith("/mcp (stdio needs no server)")

    as_json = runner.invoke(app, ["--config-dir", str(tmp_path), "status", "--json"])
    data = json.loads(as_json.output)
    assert data["daemon"]["running"] is False and data["status"] is None
    assert data["web"]["running"] is False
    assert data["mcp"]["running"] is False and data["mcp"]["url"].endswith("/mcp")

    warnings = runner.invoke(app, ["--config-dir", str(tmp_path), "status", "warnings"])
    assert warnings.exit_code == 1 and "not running" in warnings.output


def test_notify_is_a_group_with_status_and_test(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "notify"])
    assert "Usage" in result.output
    assert "status" in result.output and "test" in result.output


def test_mcp_is_a_group_with_serve_and_auth(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "mcp"])
    assert "Usage" in result.output
    assert "serve" in result.output and "auth" in result.output
    result = runner.invoke(
        app, ["--config-dir", str(tmp_path), "mcp", "serve", "--port", "1"]
    )
    assert result.exit_code == 1 and "--http" in result.output


def test_mcp_auth_writes_a_token_and_hands_it_over(tmp_path: Path) -> None:
    port = free_port()
    (tmp_path / "daemon.yaml").write_text(
        f"data_dir: {tmp_path / 'var'}\napi:\n  port: {free_port()}\n"
        f"mcp:\n  host: 127.0.0.1   # a comment that stays\n  port: {port}\n"
    )
    before = runner.invoke(
        app, ["--config-dir", str(tmp_path), "mcp", "auth", "status"]
    )
    assert before.exit_code == 0, before.output
    assert "token:  not set" in before.output
    assert "server: not running" in before.output

    written = runner.invoke(
        app, ["--config-dir", str(tmp_path), "mcp", "auth", "generate"]
    )
    assert written.exit_code == 0, written.output
    env = (tmp_path / "env").read_text()
    assert env.startswith("BRICKLOGGER_MCP_TOKEN=")
    token = env.partition("=")[2].strip()
    assert token in written.output, "the token is printed; nothing else can show it"
    registration = (
        f"claude mcp add --transport http bricklogger http://127.0.0.1:{port}/mcp"
    )
    assert registration in written.output
    assert f'--header "Authorization: Bearer {token}"' in written.output
    daemon_yaml = (tmp_path / "daemon.yaml").read_text()
    assert "  token: ${BRICKLOGGER_MCP_TOKEN}\n" in daemon_yaml
    assert "a comment that stays" in daemon_yaml

    after = runner.invoke(app, ["--config-dir", str(tmp_path), "mcp", "auth", "status"])
    assert "token:  set — BRICKLOGGER_MCP_TOKEN in the env file" in after.output

    shown = runner.invoke(app, ["--config-dir", str(tmp_path), "mcp", "auth", "show"])
    assert shown.exit_code == 0 and token in shown.output

    again = runner.invoke(
        app, ["--config-dir", str(tmp_path), "mcp", "auth", "generate"]
    )
    assert again.exit_code == 0, again.output
    assert (tmp_path / "env").read_text().partition("=")[2].strip() != token
    assert (tmp_path / "daemon.yaml").read_text().count("token:") == 1


def test_mcp_auth_needs_the_machine_whose_env_file_it_is(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--config-dir",
            str(tmp_path),
            "--api",
            "http://10.0.0.1:8420",
            "mcp",
            "auth",
            "generate",
        ],
    )
    assert result.exit_code == 1 and "--api" in result.output
    assert not (tmp_path / "env").exists()


def test_mcp_auth_show_says_when_there_is_no_token(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config-dir", str(tmp_path), "mcp", "auth", "show"])
    assert result.exit_code == 1 and "generate" in result.output
