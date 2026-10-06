"""``daemon start``, ``stop`` and ``restart`` against a real background process,
and the daemon's JSON log format."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import httpx
from typer.testing import CliRunner

from bricklogger.cli import app
from bricklogger.cli.daemon_commands import PID_FILE, process_alive, read_pid
from bricklogger.config.schema import LogSettings
from bricklogger.daemon.logging import JsonFormatter, configure_logging
from tests.support import free_port

runner = CliRunner()


def _config(tmp_path: Path, template: Path, port: int) -> Path:
    config_dir = tmp_path / "etc"
    data_dir = tmp_path / "var"
    config_dir.mkdir()
    shutil.copytree(template, data_dir)
    (config_dir / "daemon.yaml").write_text(
        f"data_dir: {data_dir}\napi:\n  port: {port}\nlog:\n  format: json\n"
    )
    (config_dir / "rules.yaml").write_text(
        "- match: { class: brick:Point }\n  action: deny\n"
    )
    return config_dir


def test_daemon_start_stop_and_restart_as_a_background_process(
    tmp_path: Path, template: Path
) -> None:
    port = free_port()
    config_dir = _config(tmp_path, template, port)
    base = ["--config-dir", str(config_dir), "daemon"]
    pid_file = tmp_path / "var" / PID_FILE
    log_file = tmp_path / "var" / "bricklogger.log"

    stopped = runner.invoke(app, [*base, "stop"])
    assert stopped.exit_code == 1 and "not running" in stopped.output

    started = runner.invoke(app, [*base, "start"])
    assert started.exit_code == 0, started.output
    assert "started (pid" in started.output
    pid = read_pid(pid_file)
    assert pid is not None and process_alive(pid)
    try:
        assert httpx.get(f"http://127.0.0.1:{port}/health/live").json() == {
            "live": "ok"
        }
        again = runner.invoke(app, [*base, "start"])
        assert again.exit_code == 1 and "already running" in again.output

        raw = [line for line in log_file.read_text().splitlines() if line]
        assert all(line.startswith("{") for line in raw), raw
        lines = [json.loads(line) for line in raw]
        assert any("daemon started" in line["message"] for line in lines), lines
        assert all(
            {"time", "level", "logger", "message"} <= set(line) for line in lines
        )

        restarted = runner.invoke(app, [*base, "restart"])
        assert restarted.exit_code == 0, restarted.output
        assert "stopped" in restarted.output and "started (pid" in restarted.output
        new_pid = read_pid(pid_file)
        assert new_pid is not None and new_pid != pid and process_alive(new_pid)
        assert not process_alive(pid)
        pid = new_pid

        stopped = runner.invoke(app, [*base, "stop"])
        assert stopped.exit_code == 0, stopped.output
        assert "stopped" in stopped.output
        assert not process_alive(pid) and not pid_file.exists()
    finally:
        if process_alive(pid):
            runner.invoke(app, [*base, "stop"])


def test_json_formatter_carries_the_extras() -> None:
    record = logging.LogRecord(
        "bricklogger.test", logging.WARNING, __file__, 1, "point %s", ("x",), None
    )
    record.instance = "bacnet_main"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "warning" and payload["logger"] == "bricklogger.test"
    assert payload["message"] == "point x" and payload["instance"] == "bacnet_main"
    assert payload["time"].endswith("+00:00")


def test_configure_logging_rotates_a_file(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "bricklogger.log"
    settings = LogSettings(level="info", format="text", max_size="1KB", keep=2)
    configure_logging(settings, file=log_file)
    try:
        log = logging.getLogger("bricklogger.test.rotation")
        for index in range(60):
            log.info("line %d of a message long enough to rotate the file", index)
        names = sorted(path.name for path in log_file.parent.iterdir())
        assert names == ["bricklogger.log", "bricklogger.log.1", "bricklogger.log.2"]
        assert log_file.stat().st_size <= 1024 + 200
    finally:
        configure_logging(LogSettings(level="warning"), file=None)
