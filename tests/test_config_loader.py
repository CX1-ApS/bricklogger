"""Loading the config directory: missing files, environment variables, and the
shape of each file."""

from datetime import timedelta
from pathlib import Path

import pytest

from bricklogger.config import (
    CONFIG_DIR_ENV,
    ConfigIssue,
    default_data_dir,
    load_configuration,
    loader,
    resolve_config_dir,
    user_config_dir,
    write_examples,
)
from bricklogger.config.daemon import load_daemon_settings


def write(config_dir: Path, **files: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (config_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
    return config_dir


def test_config_dir_resolution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system = tmp_path / "etc" / "bricklogger"
    monkeypatch.setattr(loader, "SYSTEM_CONFIG_DIR", system)
    assert resolve_config_dir(tmp_path, {CONFIG_DIR_ENV: "/tmp/x"}) == tmp_path
    assert resolve_config_dir(None, {CONFIG_DIR_ENV: "/tmp/x"}) == Path("/tmp/x")
    assert resolve_config_dir(None, {}, home=tmp_path) == user_config_dir(tmp_path)
    system.mkdir(parents=True)
    assert resolve_config_dir(None, {}, home=tmp_path) == user_config_dir(tmp_path), (
        "a directory in /etc, left by an earlier installation, is not read"
    )


def test_the_data_directory_follows_the_config_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "SYSTEM_CONFIG_DIR", tmp_path / "etc")
    monkeypatch.setattr(loader, "SYSTEM_DATA_DIR", tmp_path / "var")
    assert default_data_dir(tmp_path / "etc") == tmp_path / "var"
    assert default_data_dir(tmp_path / "elsewhere", home=tmp_path) == (
        tmp_path / ".local" / "share" / "bricklogger"
    )
    result = load_configuration(tmp_path / "elsewhere", env={})
    assert result.configuration is not None
    assert result.configuration.daemon.data_dir == default_data_dir(
        tmp_path / "elsewhere"
    )


def test_config_init_writes_the_resolved_data_dir(tmp_path: Path) -> None:
    write_examples(tmp_path)
    text = (tmp_path / "daemon.yaml").read_text(encoding="utf-8")
    assert f"data_dir: {default_data_dir(tmp_path)}" in text


def test_missing_files_are_read_as_empty(tmp_path: Path) -> None:
    result = load_configuration(tmp_path, env={})
    assert result.issues == []
    config = result.configuration
    assert config is not None
    assert config.daemon.api.host == "127.0.0.1"
    assert config.daemon.api.port == 8420
    assert config.daemon.data_dir == default_data_dir(tmp_path)
    assert config.daemon.stop_timeout == timedelta(seconds=10)
    assert config.daemon.log_file == default_data_dir(tmp_path) / "bricklogger.log"
    assert config.sources == {}
    assert config.destinations == {}
    assert config.rules == []


def test_a_file_of_only_comments_is_empty(tmp_path: Path) -> None:
    write(tmp_path, rules="# nothing yet\n", daemon="# defaults\n")
    result = load_configuration(tmp_path, env={})
    assert result.configuration is not None
    assert result.configuration.rules == []


def test_environment_variables_are_filled_in(tmp_path: Path) -> None:
    write(
        tmp_path,
        destinations="tsdb:\n  type: timescaledb\n  dsn: x\n  password: ${PW}\n",
    )
    result = load_configuration(tmp_path, env={"PW": "secret"})
    assert result.configuration is not None
    assert result.configuration.destinations["tsdb"].settings["password"] == "secret"


def test_secrets_come_from_the_env_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        destinations="tsdb:\n  type: timescaledb\n  dsn: x\n  password: ${PW}\n",
    )
    (tmp_path / "env").write_text(
        '# the secrets\n\nexport PW="from the file"\n', encoding="utf-8"
    )
    result = load_configuration(tmp_path, env={})
    assert result.configuration is not None
    settings = result.configuration.destinations["tsdb"].settings
    assert settings["password"] == "from the file"


def test_the_environment_wins_over_the_env_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        destinations="tsdb:\n  type: timescaledb\n  dsn: x\n  password: ${PW}\n",
    )
    (tmp_path / "env").write_text("PW=from the file\n", encoding="utf-8")
    result = load_configuration(tmp_path, env={"PW": "from the shell"})
    assert result.configuration is not None
    settings = result.configuration.destinations["tsdb"].settings
    assert settings["password"] == "from the shell"


def test_a_missing_environment_variable_names_the_key(tmp_path: Path) -> None:
    write(tmp_path, destinations="tsdb:\n  type: timescaledb\n  password: ${PW}\n")
    result = load_configuration(tmp_path, env={})
    assert result.configuration is None
    assert result.issues == [
        ConfigIssue(
            "destinations", "environment variable PW is not set", "tsdb", "password"
        )
    ]


def test_invalid_yaml_is_reported_with_its_position(tmp_path: Path) -> None:
    write(tmp_path, sources="bacnet_main:\n  type: [\n")
    result = load_configuration(tmp_path, env={})
    assert result.configuration is None
    assert len(result.issues) == 1
    assert result.issues[0].file == "sources"
    assert "not valid YAML" in result.issues[0].message
    assert "line" in result.issues[0].message


def test_the_wrong_shape_is_reported(tmp_path: Path) -> None:
    write(tmp_path, rules="name: not a list\n", daemon="- not\n- a mapping\n")
    result = load_configuration(tmp_path, env={})
    assert {(issue.file, issue.message) for issue in result.issues} == {
        ("rules", "the file must contain a list of rules"),
        ("daemon", "the file must contain a mapping"),
    }


def test_unknown_daemon_keys_are_rejected(tmp_path: Path) -> None:
    write(tmp_path, daemon="api:\n  hots: 127.0.0.1\n")
    result = load_configuration(tmp_path, env={})
    assert result.issues == [
        ConfigIssue("daemon", "Extra inputs are not permitted", None, "api.hots")
    ]


def test_instances_keep_their_own_keys_and_the_reserved_ones_are_parsed(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        sources="bacnet_main:\n  type: bacnet-ip\n  address: 10.0.0.5/24\n",
        destinations=(
            "tsdb:\n  type: timescaledb\n  dsn: x\n"
            "  spool: { max_size: 500MB }\n  batch: { size: 10 }\n"
        ),
    )
    result = load_configuration(tmp_path, env={})
    config = result.configuration
    assert config is not None
    assert config.sources["bacnet_main"].settings == {"address": "10.0.0.5/24"}
    tsdb = config.destinations["tsdb"]
    assert tsdb.settings == {"dsn": "x"}
    assert tsdb.spool.max_size == 500 * 1024**2
    assert tsdb.spool.max_age == timedelta(days=7)
    assert tsdb.batch.size == 10
    assert tsdb.batch.interval == timedelta(seconds=5)


def test_an_instance_without_a_type_is_reported(tmp_path: Path) -> None:
    write(tmp_path, sources="bacnet_main:\n  address: 10.0.0.5/24\n")
    result = load_configuration(tmp_path, env={})
    assert result.issues == [
        ConfigIssue("sources", "Field required", "bacnet_main", "type")
    ]


def test_rules_parse_selectors_and_intervals(tmp_path: Path) -> None:
    write(
        tmp_path,
        rules=(
            "- name: Exclude test room\n"
            "  match: { location: ex:Room_1_17 }\n"
            "  action: deny\n"
            "- match: { class: brick:Temperature_Sensor,\n"
            "           equipment: [ex:AHU_01, ex:AHU_02] }\n"
            "  action: accept\n"
            "  method: poll\n"
            "  interval: 5m\n"
            "- match_regex: { location: 'ex:Room_1_.*' }\n"
            "  action: accept\n"
            "  method: subscribe\n"
            "  fallback: { method: poll, interval: 10m }\n"
        ),
    )
    result = load_configuration(tmp_path, env={})
    assert result.issues == []
    config = result.configuration
    assert config is not None
    deny, accept, regex = config.rules
    assert deny.match is not None and deny.match.location == "ex:Room_1_17"
    assert accept.match is not None
    assert accept.match.class_ == "brick:Temperature_Sensor"
    assert accept.match.equipment == ["ex:AHU_01", "ex:AHU_02"]
    assert accept.interval == timedelta(minutes=5)
    assert regex.fallback is not None
    assert regex.fallback.interval == timedelta(minutes=10)
    assert config.rule_label(0) == "Exclude test room"
    assert config.rule_label(1) == "rule 2"


def test_rule_shape_errors_name_the_rule(tmp_path: Path) -> None:
    write(
        tmp_path,
        rules=(
            "- name: two selectors\n"
            "  match: { point: ex:P }\n"
            "  sparql: SELECT ?p WHERE {}\n"
            "  action: deny\n"
            "- name: deny with method\n"
            "  match: { point: ex:P }\n"
            "  action: deny\n"
            "  method: poll\n"
            "- name: poll without interval\n"
            "  match: { point: ex:P }\n"
            "  action: accept\n"
            "  method: poll\n"
            "- name: interval on another method\n"
            "  match: { point: ex:P }\n"
            "  action: accept\n"
            "  method: subscribe\n"
            "  interval: 5m\n"
            "- name: fallback on poll\n"
            "  match: { point: ex:P }\n"
            "  action: accept\n"
            "  method: poll\n"
            "  interval: 5m\n"
            "  fallback: { method: subscribe }\n"
            "- match: { point: ex:P }\n"
            "  action: accept\n"
            "  method: poll\n"
            "  interval: 0s\n"
            "- match: {}\n"
            "  action: deny\n"
        ),
    )
    result = load_configuration(tmp_path, env={})
    assert result.configuration is None
    by_subject = {issue.subject: issue.message for issue in result.issues}
    assert by_subject["two selectors"] == (
        "exactly one of match, match_regex or sparql is required"
    )
    assert by_subject["deny with method"] == "method is not allowed on a deny rule"
    assert (
        by_subject["poll without interval"] == "interval is required with method poll"
    )
    assert by_subject["interval on another method"] == (
        "interval is only allowed with method poll"
    )
    assert by_subject["fallback on poll"] == "fallback is not allowed with method poll"
    assert by_subject["rule 6"] == "interval must be longer than zero"
    assert by_subject["rule 7"] == (
        "a selector needs at least one of class, equipment, location or point"
    )


def test_daemon_yaml_alone_also_reads_the_env_file(tmp_path: Path) -> None:
    """daemon.yaml held no secret until notifications arrived.

    The loader that reads it on its own is what every CLI command and
    `daemon run` use to find the daemon, so a `${VAR}` there must resolve the
    same way it does when the whole directory is loaded.
    """
    write(tmp_path, daemon="notifications:\n  smtp:\n    password: ${SMTP_PW}\n")
    (tmp_path / "env").write_text("SMTP_PW=from the file\n", encoding="utf-8")

    settings = load_daemon_settings(tmp_path, env={})
    assert settings.notifications.smtp.password == "from the file"


def test_the_environment_still_wins_for_daemon_yaml_alone(tmp_path: Path) -> None:
    write(tmp_path, daemon="notifications:\n  smtp:\n    password: ${SMTP_PW}\n")
    (tmp_path / "env").write_text("SMTP_PW=from the file\n", encoding="utf-8")

    settings = load_daemon_settings(tmp_path, env={"SMTP_PW": "from the shell"})
    assert settings.notifications.smtp.password == "from the shell"
