"""``bricklogger daemon``: run, start, stop, restart and reload; ``status``;
``points``."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from bricklogger.cli.client import ApiClient
from bricklogger.cli.config import config_view
from bricklogger.cli.context import CliContext, cli_context, operations_of
from bricklogger.cli.output import fail, print_json, stdout
from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.config.schema import DaemonSettings
from bricklogger.model.prefixes import compact
from bricklogger.ops.errors import OperationError
from bricklogger.ops.status import PID_FILE as PID_FILE
from bricklogger.ops.status import probe
from bricklogger.ops.status import process_alive as process_alive
from bricklogger.ops.status import read_pid as read_pid
from bricklogger.ops.updates import available_line

daemon_app = typer.Typer(
    no_args_is_help=True,
    help="Run, start, stop, restart and reload the daemon; `config` for daemon.yaml.",
)
daemon_app.add_typer(config_view("daemon"), name="config")

START_TIMEOUT = 30.0

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit the API's JSON instead of a table.")
]


@daemon_app.command("run")
def run(
    ctx: typer.Context,
    log_to_file: Annotated[
        bool,
        typer.Option(
            "--log-to-file",
            hidden=True,
            help="Log to log.file with rotation instead of stdout (daemon start).",
        ),
    ] = False,
) -> None:
    """Run the daemon in the foreground with logs on stdout."""
    from bricklogger.daemon.run import run_daemon

    raise typer.Exit(run_daemon(cli_context(ctx).config_dir, log_to_file=log_to_file))


def _settings(context: CliContext) -> DaemonSettings:
    try:
        return load_daemon_settings(context.config_dir)
    except ConfigError as exc:
        raise fail(f"daemon.yaml: {exc}") from exc


def wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return condition()


def _log_tail(log_file: Path, lines: int = 5) -> str:
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip("\n").split("\n")[-lines:])


def _start(context: CliContext, settings: DaemonSettings) -> None:
    pid_file = settings.data_dir / PID_FILE
    existing = read_pid(pid_file)
    if existing is not None and process_alive(existing):
        raise fail(f"the daemon is already running (pid {existing})")
    client = ApiClient.from_context(context)
    if client.reachable():
        raise fail(
            f"a daemon already answers at {client.base_url}; it was started another way"
        )
    pid_file.unlink(missing_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "bricklogger",
        "--config-dir",
        str(context.config_dir),
        "daemon",
        "run",
        "--log-to-file",
    ]
    with log_file.open("ab") as stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )
    pid_file.write_text(f"{process.pid}\n")
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            pid_file.unlink(missing_ok=True)
            raise fail(
                f"the daemon exited with code {code}; see {log_file}\n"
                f"{_log_tail(log_file)}"
            )
        if client.reachable():
            typer.echo(f"started (pid {process.pid}); log: {log_file}")
            return
        time.sleep(0.2)
    raise fail(
        f"the daemon (pid {process.pid}) did not answer within {START_TIMEOUT:g}s; "
        f"see {log_file}"
    )


def _stop(
    context: CliContext, settings: DaemonSettings, *, quiet: bool = False
) -> bool:
    """Stop the daemon; True when one was running and has stopped."""
    pid_file = settings.data_dir / PID_FILE
    pid = read_pid(pid_file)
    alive = pid is not None and process_alive(pid)
    client = ApiClient.from_context(context)
    timeout = settings.stop_timeout.total_seconds() + 5.0
    if client.reachable():
        client.post("/v1/daemon/stop")
        client.close()
        typer.echo("stopping")
        gone: Callable[[], bool] = (
            (lambda: not process_alive(pid))
            if alive and pid is not None
            else (lambda: not client.reachable())
        )
        if wait_until(gone, timeout):
            if alive:
                pid_file.unlink(missing_ok=True)
            typer.echo("stopped")
            return True
        if not alive:
            raise fail(f"the daemon did not stop within {timeout:g}s")
    if alive and pid is not None:
        os.kill(pid, signal.SIGTERM)
        typer.echo(f"sent SIGTERM to pid {pid}")
        if wait_until(lambda: not process_alive(pid), timeout):
            pid_file.unlink(missing_ok=True)
            typer.echo("stopped")
            return True
        raise fail(f"pid {pid} did not exit within {timeout:g}s")
    if pid is not None:
        pid_file.unlink(missing_ok=True)
    if quiet:
        return False
    raise fail("the daemon is not running")


@daemon_app.command("start")
def start(ctx: typer.Context) -> None:
    """Start the daemon in the background, with a PID file in the data directory."""
    context = cli_context(ctx)
    _start(context, _settings(context))


@daemon_app.command("stop")
def stop(ctx: typer.Context) -> None:
    """Stop the daemon gracefully: through the API, else by PID file and signal."""
    context = cli_context(ctx)
    _stop(context, _settings(context))


@daemon_app.command("restart")
def restart(ctx: typer.Context) -> None:
    """Stop the daemon if it runs, then start it."""
    context = cli_context(ctx)
    settings = _settings(context)
    _stop(context, settings, quiet=True)
    _start(context, settings)


@daemon_app.command("reload")
def reload(ctx: typer.Context, json_output: JsonFlag = False) -> None:
    """Reload the configuration; an invalid one is rejected and the running one kept."""
    result = ApiClient.from_context(cli_context(ctx)).post("/v1/daemon/reload")
    if json_output:
        print_json(result)
        return
    typer.echo("reloaded")
    for warning in result.get("warnings", []):
        typer.echo(f"warning: {warning['message']}")


def status_command(
    ctx: typer.Context,
    branch: Annotated[
        str | None,
        typer.Argument(help="warnings; default the summary."),
    ] = None,
    action: Annotated[
        str | None,
        typer.Argument(help="With warnings: clear."),
    ] = None,
    code: Annotated[
        str | None,
        typer.Argument(help="With clear: only the warnings with this code."),
    ] = None,
    subject: Annotated[
        str | None,
        typer.Argument(help="With clear and a code: only this subject."),
    ] = None,
    json_output: JsonFlag = False,
) -> None:
    """Whether the daemon, the web interface and the MCP server run, and the
    daemon's summary."""
    context = cli_context(ctx)
    if branch not in (None, "warnings"):
        raise fail(
            "the only branch is warnings; an instance's own status is under "
            "`bricklogger sources` and `bricklogger destinations`"
        )
    if branch == "warnings":
        client = ApiClient.from_context(context)
        if action is not None:
            if action != "clear":
                raise fail("the only action under warnings is clear")
            result = client.delete("/v1/status/warnings", code=code, subject=subject)
            if json_output:
                print_json(result)
            else:
                cleared = int(result.get("cleared", 0))
                typer.echo(f"cleared {cleared} warning{'' if cleared == 1 else 's'}")
            return
        data = client.get("/v1/status/warnings")
        if json_output:
            print_json(data)
        else:
            _echo_warnings(data)
        return
    try:
        data = probe(operations_of(context))
    except OperationError as exc:
        raise fail(exc.message) from exc
    if json_output:
        print_json(data)
        return
    typer.echo(_probe_line("daemon", data["daemon"]))
    typer.echo(_probe_line("web", data["web"]))
    typer.echo(_probe_line("mcp", data["mcp"], note="stdio needs no server"))
    if data["status"] is not None:
        typer.echo("")
        _echo_summary(data["status"])


def _probe_line(what: str, probe: Mapping[str, Any], note: str | None = None) -> str:
    label = f"{what}:".ljust(8)
    if not probe["running"]:
        aside = f" ({note})" if note else ""
        return f"{label}not running — nothing answers at {probe['url']}{aside}"
    parts = []
    if probe.get("pid") is not None:
        parts.append(f"pid {probe['pid']}")
    if probe.get("started_at"):
        parts.append(f"since {probe['started_at']}")
    detail = f" — {' '.join(parts)}" if parts else ""
    return f"{label}running{detail} at {probe['url']}"


def points_command(
    ctx: typer.Context,
    instance: Annotated[str | None, typer.Option("--instance")] = None,
    outcome: Annotated[str | None, typer.Option("--outcome")] = None,
    warning: Annotated[
        str | None, typer.Option("--warning", help="Points carrying this warning code.")
    ] = None,
    brick_class: Annotated[
        str | None,
        typer.Option("--class", help="Points of this Brick class, e.g. brick:AHU."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit")] = 200,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_output: JsonFlag = False,
) -> None:
    """The points view, paged: URI, instance, method, outcome, value, warnings."""
    client = ApiClient.from_context(cli_context(ctx))
    data = client.get(
        "/v1/points",
        instance=instance,
        outcome=outcome,
        warning=warning,
        **{"class": brick_class},
        limit=limit,
        offset=offset,
    )
    if json_output:
        print_json(data)
        return
    prefixes = client.prefixes()
    table = Table(box=None)
    table.add_column("Point", overflow="fold")
    for column in ("Instance", "Method", "Outcome", "Last value", "At", "Warnings"):
        table.add_column(column)
    for item in data["items"]:
        last = item.get("last_valid") or {}
        method = item["method"] + (" (fallback)" if item.get("fallback_active") else "")
        table.add_row(
            compact(item["uri"], prefixes),
            item.get("instance") or "",
            method,
            item.get("outcome") or "pending",
            "" if last.get("value") is None else str(last["value"]),
            last.get("time") or "",
            ", ".join(item.get("warnings", [])),
        )
    stdout.print(table)
    typer.echo(
        f"{data['total']} points, showing {len(data['items'])} from {data['offset']}"
    )


def _echo_summary(data: dict[str, Any]) -> None:
    model = data["model"]
    active = model["active"]
    points = data["points"]
    if active:
        model_line = (
            f"model: version {active['version']} activated {model['activated_at']}"
        )
    else:
        model_line = "model: none active"
        if model.get("error"):
            model_line += f" ({model['error']})"
    lines = [
        f"bricklogger {data['version']}  health: {data['health']}",
        f"config: {data['config_dir']}  data: {data['data_dir']}",
        model_line,
        f"points: {points['accepted']} accepted, {points['assigned']} assigned, "
        f"{points['active']} active, {points['unsupported']} unsupported, "
        f"{points['rejected']} rejected",
        f"warnings: {data['warnings']}  "
        f"observations received: {data['observations_received']}",
    ]
    for row in data.get("instances", []):
        lines.append(f"  {row['role']} {row['name']} ({row['type']}): {row['state']}")
    newer = available_line(data.get("updates"))
    if newer is not None:
        lines.append(newer)
    for line in lines:
        typer.echo(line)


def _echo_warnings(rows: list[dict[str, Any]]) -> None:
    if not rows:
        typer.echo("no warnings")
        return
    table = Table(box=None)
    for column in ("Code", "Subject", "Message", "First seen", "Last seen", "Count"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row["code"],
            row["subject"],
            row["message"],
            row["first_seen"],
            str(row.get("last_seen") or ""),
            str(row["count"]),
        )
    stdout.print(table)
