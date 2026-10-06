"""Validation of the whole directory against the installed plugins."""

from datetime import time, timedelta
from pathlib import Path

from bricklogger.config import EXAMPLES, ConfigIssue, validate_configuration
from bricklogger.model.versions import ModelStore
from bricklogger.plugins.bacnet_ip.declaration import SOURCE, BACnetIPConfig
from bricklogger.plugins.timescaledb.declaration import DESTINATION
from bricklogger.sdk.registry import PluginRegistry

REGISTRY = PluginRegistry.of(SOURCE, DESTINATION)
ENV = {"TSDB_PASSWORD": "secret"}


def write(config_dir: Path, **files: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (config_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
    return config_dir


def test_the_example_files_are_valid_without_warnings(tmp_path: Path) -> None:
    write(tmp_path, **EXAMPLES)
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.errors == []
    assert result.warnings == []
    assert result.valid
    assert result.configuration is not None
    assert result.as_dict() == {"valid": True, "errors": [], "warnings": []}


def test_an_empty_directory_is_valid(tmp_path: Path) -> None:
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.valid
    assert result.warnings == []


def test_a_type_that_is_not_installed_is_a_warning_in_the_files_as_they_are(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        sources="m:\n  type: modbus\n",
        destinations="d:\n  type: influx\n",
    )
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.valid and result.configuration is not None
    assert result.warnings == [
        ConfigIssue(
            "sources",
            "the instance will be failed: the type 'modbus' is not installed; "
            "add its plugin with `bricklogger plugins add`",
            "m",
            "type",
        ),
        ConfigIssue(
            "destinations",
            "the instance will be failed: the type 'influx' is not installed; "
            "add its plugin with `bricklogger plugins add`",
            "d",
            "type",
        ),
    ]


def test_a_change_that_adds_an_unknown_type_is_refused_with_the_installed_ones(
    tmp_path: Path,
) -> None:
    write(tmp_path, sources="", destinations="")
    result = validate_configuration(
        tmp_path,
        REGISTRY,
        ENV,
        {"sources": "m:\n  type: modbus\n", "destinations": "d:\n  type: influx\n"},
    )
    assert result.errors == [
        ConfigIssue(
            "sources", "unknown source type 'modbus'; installed: bacnet-ip", "m", "type"
        ),
        ConfigIssue(
            "destinations",
            "unknown destination type 'influx'; installed: timescaledb",
            "d",
            "type",
        ),
    ]


def test_a_change_warns_about_the_instances_it_leaves_alone(tmp_path: Path) -> None:
    """A plugin missing after an upgrade must not stop the rest of the file
    from being edited; only an instance the change touches is held to it."""
    write(tmp_path, sources="old:\n  type: modbus\n")
    kept = "old:\n  type: modbus\n"
    result = validate_configuration(
        tmp_path, REGISTRY, ENV, {"sources": kept + "new:\n  type: modbsu\n"}
    )
    assert [(i.subject, i.message.split(";")[0]) for i in result.errors] == [
        ("new", "unknown source type 'modbsu'")
    ]
    assert [i.subject for i in result.warnings] == ["old"]

    edited = validate_configuration(
        tmp_path, REGISTRY, ENV, {"sources": "old:\n  type: modbus\n  x: 1\n"}
    )
    assert [i.subject for i in edited.errors] == ["old"]


def test_plugin_settings_are_validated_against_the_plugin_schema(
    tmp_path: Path,
) -> None:
    write(
        tmp_path,
        sources=(
            "bacnet_main:\n"
            "  type: bacnet-ip\n"
            "  address: 192.168.10.5\n"
            '  devices: ["1999-1000"]\n'
            "  timeuot: 3s\n"
        ),
    )
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    by_key = {issue.key: issue.message for issue in result.errors}
    assert all(issue.subject == "bacnet_main" for issue in result.errors)
    assert "prefix length" in by_key["address"]
    assert by_key["device_instance"] == "Field required"
    assert "low to high" in by_key["devices.0"]
    assert by_key["timeuot"] == "Extra inputs are not permitted"


def test_exposed_bindings_need_their_secrets(tmp_path: Path) -> None:
    write(tmp_path, daemon="api:\n  host: 0.0.0.0\nweb:\n  host: 0.0.0.0\n")
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert {(issue.key, issue.message) for issue in result.errors} == {
        ("api.token", "required when api.host is not a loopback address"),
        ("web.password", "required when web.host is not a loopback address"),
    }
    write(
        tmp_path,
        daemon=(
            "api:\n  host: 0.0.0.0\n  token: ${TOKEN}\n"
            "web:\n  host: 0.0.0.0\n  password: ${PW}\n"
        ),
    )
    result = validate_configuration(
        tmp_path, REGISTRY, {**ENV, "TOKEN": "t", "PW": "p"}
    )
    assert result.valid


def test_unknown_collection_methods_are_reported(tmp_path: Path) -> None:
    write(
        tmp_path,
        rules=(
            "- name: cov\n  match: { class: brick:Point }\n  action: accept\n"
            "  method: cov\n  fallback: { method: poll, interval: 5m }\n"
        ),
        destinations=EXAMPLES["destinations"],
    )
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.errors == [
        ConfigIssue(
            "rules",
            "unknown collection method 'cov'; the installed sources offer: poll",
            "cov",
            "method",
        )
    ]


def test_accepting_points_without_a_destination_is_a_warning(tmp_path: Path) -> None:
    write(tmp_path, rules=EXAMPLES["rules"])
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.valid
    assert result.warnings == [
        ConfigIssue(
            "destinations",
            "the rule set accepts points, but no destination is configured",
        )
    ]


def test_the_built_in_plugins_are_found_through_entry_points() -> None:
    registry = PluginRegistry.from_entry_points()
    assert registry.sources["bacnet-ip"] is SOURCE
    assert registry.destinations["timescaledb"] is DESTINATION


def test_bacnet_scope_rules() -> None:
    config = BACnetIPConfig.model_validate(
        {
            "address": "192.168.10.5/24:47809",
            "device_instance": 1200,
            "devices": ["1000-1999", 2500],
            "subnet": "192.168.10.0/24",
        }
    )
    assert (config.local_ip, config.prefix_length, config.port) == (
        "192.168.10.5",
        24,
        47809,
    )
    assert config.claims_device(1500, "192.168.10.20")
    assert config.claims_device(2500, "192.168.10.21")
    assert not config.claims_device(3000, "192.168.10.20")
    assert not config.claims_device(1500, "192.168.11.20")
    assert not config.claims_device(1500, None), "a subnet restriction needs an IP"
    unrestricted = BACnetIPConfig.model_validate(
        {"address": "10.0.0.1/8", "device_instance": 1}
    )
    assert unrestricted.claims_device(4_000_000, None)
    assert unrestricted.model_dump(mode="json")["timeout"] == "3s"


NOTIFICATIONS = """\
notifications:
  enabled: true
  smtp:
    host: smtp.example.com
  from: bricklogger@example.com
  to:
    - drift@example.com
"""


def data_dir_with_a_model(tmp_path: Path) -> Path:
    """A data directory whose model store has an active version."""
    data_dir = tmp_path / "var"
    store = ModelStore(data_dir)
    store.store(b"", "turtle")
    store.set_active(1)
    return data_dir


def test_notifications_are_off_when_the_section_is_absent(tmp_path: Path) -> None:
    write(tmp_path, daemon="")
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert result.valid
    assert result.configuration is not None
    settings = result.configuration.daemon.notifications
    assert settings.enabled is False
    assert settings.window == timedelta(minutes=2)
    assert settings.min_interval == timedelta(minutes=15)
    assert settings.digest == time(7, 0)


def test_notifications_need_an_active_model_to_be_enabled(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    data_dir.mkdir()
    config_dir = write(
        tmp_path / "etc", daemon=f"data_dir: {data_dir}\n{NOTIFICATIONS}"
    )
    result = validate_configuration(config_dir, REGISTRY, ENV)
    assert (
        ConfigIssue(
            "daemon",
            "notifications can only be enabled where a model is active; "
            "upload a model first",
            key="notifications.enabled",
        )
        in result.errors
    )


def test_enabled_notifications_need_a_server_a_sender_and_a_recipient(
    tmp_path: Path,
) -> None:
    data_dir = data_dir_with_a_model(tmp_path)
    config_dir = write(
        tmp_path / "etc",
        daemon=f"data_dir: {data_dir}\nnotifications:\n  enabled: true\n",
    )
    result = validate_configuration(config_dir, REGISTRY, ENV)
    assert [issue.key for issue in result.errors] == [
        "notifications.smtp.host",
        "notifications.from",
        "notifications.to",
    ]
    assert all(
        issue.message == "required when notifications are enabled"
        for issue in result.errors
    )


def test_notifications_with_a_model_and_a_server_are_valid(tmp_path: Path) -> None:
    data_dir = data_dir_with_a_model(tmp_path)
    config_dir = write(
        tmp_path / "etc", daemon=f"data_dir: {data_dir}\n{NOTIFICATIONS}"
    )
    result = validate_configuration(config_dir, REGISTRY, ENV)
    assert result.errors == []
    assert result.configuration is not None
    settings = result.configuration.daemon.notifications
    assert settings.enabled is True
    assert settings.smtp.host == "smtp.example.com"
    assert settings.smtp.port == 587
    assert settings.smtp.security == "starttls"
    assert settings.sender == "bricklogger@example.com"
    assert settings.to == ["drift@example.com"]


def test_an_unknown_notification_key_is_refused(tmp_path: Path) -> None:
    config_dir = write(
        tmp_path / "etc", daemon="notifications:\n  enabled: false\n  cc: a@b.c\n"
    )
    result = validate_configuration(config_dir, REGISTRY, ENV)
    assert not result.valid
    assert any("cc" in (issue.key or "") for issue in result.errors)


def test_the_mcp_token_is_required_beyond_loopback(tmp_path: Path) -> None:
    write(tmp_path, daemon="mcp:\n  host: 0.0.0.0\n")
    result = validate_configuration(tmp_path, REGISTRY, ENV)
    assert {(issue.key, issue.message) for issue in result.errors} == {
        ("mcp.token", "required when mcp.host is not a loopback address"),
    }
    write(tmp_path, daemon="mcp:\n  host: 0.0.0.0\n  token: ${MCP}\n")
    assert validate_configuration(tmp_path, REGISTRY, {**ENV, "MCP": "t"}).valid


def test_every_setting_carries_a_description() -> None:
    """The MCP server, the CLI and the web forms show what a setting means."""
    from bricklogger.config.schema import DaemonSettings, Rule

    for model in (DaemonSettings, Rule, BACnetIPConfig, DESTINATION.config_schema):
        schema = model.model_json_schema()
        definitions = {**schema.get("$defs", {}), "root": schema}
        for name, definition in definitions.items():
            for key, spec in (definition.get("properties") or {}).items():
                assert spec.get("description"), f"{model.__name__}.{name}.{key}"
