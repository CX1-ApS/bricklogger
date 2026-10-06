"""``bricklogger mcp``: the MCP server, and the token an HTTP client sends.

``mcp serve`` runs the server in the foreground, stdio by default and
streamable HTTP with ``--http``; ``mcp auth`` writes that token, prints it
with the line that registers the server in a client, and says whether a
running server has it yet. See ``docs/features/mcp.md``.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Annotated

import typer
import yaml

from bricklogger.cli.context import CliContext, cli_context, operations_of
from bricklogger.cli.guided import write_env
from bricklogger.cli.output import fail
from bricklogger.config import read_texts
from bricklogger.config.daemon import load_daemon_settings, with_mcp_token
from bricklogger.config.issues import ConfigError
from bricklogger.config.schema import DaemonSettings, is_loopback
from bricklogger.ops.config import REFERENCE, write_file
from bricklogger.ops.errors import OperationError
from bricklogger.ops.status import probe_mcp

log = logging.getLogger(__name__)

DEFAULT_VARIABLE = "BRICKLOGGER_MCP_TOKEN"
UNIT = "bricklogger-mcp.service"

mcp_app = typer.Typer(
    name="mcp",
    help="The MCP server for an AI assistant: serve it, and the token it asks for.",
    no_args_is_help=True,
)
auth_app = typer.Typer(
    name="auth",
    help="The bearer token an MCP client sends over HTTP.",
    no_args_is_help=True,
)
mcp_app.add_typer(auth_app, name="auth")


@mcp_app.command("serve")
def serve_command(
    ctx: typer.Context,
    http: Annotated[
        bool,
        typer.Option(
            "--http",
            help="Listen as a streamable HTTP server instead of speaking stdio.",
        ),
    ] = False,
    host: Annotated[
        str | None,
        typer.Option("--host", help="With --http: bind here; default mcp.host."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="With --http: listen here; default mcp.port."),
    ] = None,
) -> None:
    """Serve the MCP server for an AI assistant: stdio by default, HTTP with --http.

    It reads and changes the configuration over the daemon's API like every
    command, and on the files when no daemon answers. Over HTTP a token that is
    set is required on every request, and bound beyond localhost mcp.token must
    be set at all; `mcp auth generate` writes one.
    """
    from bricklogger.mcp import create_mcp_server
    from bricklogger.mcp.http import serve_http

    context = cli_context(ctx)
    if not http and (host is not None or port is not None):
        raise fail("--host and --port apply with --http; over stdio nothing is bound")
    server = create_mcp_server(operations_of(context))
    if not http:
        server.run()
        return
    settings = _settings(context)
    bind_host = host or settings.mcp.host
    bind_port = port or settings.mcp.port
    if not is_loopback(bind_host) and not settings.mcp.token:
        raise fail(
            "mcp.token is required when the MCP server is bound beyond localhost; "
            "bricklogger mcp auth generate writes one"
        )
    logging.basicConfig(
        level=settings.log.level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("serving MCP on http://%s:%d/mcp", bind_host, bind_port)
    serve_http(server, host=bind_host, port=bind_port, token=settings.mcp.token)


@auth_app.command("status")
def auth_status_command(ctx: typer.Context) -> None:
    """Whether a token is set, where its value lives, and whether the server has it."""
    context = cli_context(ctx)
    settings = _settings(context)
    written = _written_token(context.config_dir)
    variable = _variable(written)
    token = settings.mcp.token
    typer.echo(_token_line(written, variable, token))
    typer.echo(_server_line(settings, token))


@auth_app.command("show")
def auth_show_command(ctx: typer.Context) -> None:
    """Print the token and the line that registers the server in a client."""
    context = cli_context(ctx)
    settings = _settings(context)
    if not settings.mcp.token:
        raise fail(
            "no token is set; bricklogger mcp auth generate writes one and prints it"
        )
    _print_token(settings, settings.mcp.token)


@auth_app.command("generate")
def auth_generate_command(
    ctx: typer.Context,
    restart: Annotated[
        bool,
        typer.Option(
            "--restart/--no-restart",
            help="Restart a running server under systemd so the token takes effect.",
        ),
    ] = True,
) -> None:
    """Write a new token, print it, and restart a running server so it takes effect.

    The value goes into the env file and a reference to it into daemon.yaml,
    as every secret does; whatever token was there stops working.
    """
    context = cli_context(ctx)
    if context.api_url is not None:
        raise fail(
            "mcp auth writes the env file, so it works on the machine whose file "
            "it is, not through --api"
        )
    before = probe_mcp(_settings(context))
    written = _written_token(context.config_dir)
    variable = _variable(written)
    reference = f"${{{variable}}}"
    token = secrets.token_urlsafe(32)
    try:
        env_path = write_env(context.config_dir, {variable: token})
    except OSError as exc:
        raise fail(f"could not write the env file: {exc}") from exc
    typer.echo(f"written to {env_path} as {variable}")
    if written != reference:
        _reference_in_daemon_yaml(context, reference)
        typer.echo(f"daemon.yaml: mcp.token is {reference}")
    settings = _settings(context)
    _print_token(settings, token)
    typer.echo("")
    typer.echo(_restart_line(before, restart))


def _print_token(settings: DaemonSettings, token: str) -> None:
    """The token and the registration line — the one place a secret is printed."""
    url = probe_mcp(settings)["url"]
    typer.echo("")
    typer.echo(token)
    typer.echo("")
    typer.echo("register it in a client with:")
    typer.echo("")
    typer.echo(f"claude mcp add --transport http bricklogger {url} \\")
    typer.echo(f'  --header "Authorization: Bearer {token}"')


def _reference_in_daemon_yaml(context: CliContext, reference: str) -> None:
    """Put ``mcp.token: ${VARIABLE}`` in the file, validated as a whole."""
    ops = operations_of(context)
    text = read_texts(context.config_dir).get("daemon")
    try:
        with ops.session() as client:
            write_file(ops, client, "daemon", with_mcp_token(text, reference))
    except OperationError as exc:
        raise fail(
            f"{exc.message}; the value is in the env file, but daemon.yaml still "
            f"does not point at it — set mcp.token to {reference} by hand"
        ) from exc


def _restart_line(before: dict[str, object], restart: bool) -> str:
    """What became of the server that was running with the previous token."""
    if not before["running"]:
        return "no server answers there; it takes the token up when it starts"
    if not restart:
        return (
            "the running server keeps the old token until it is restarted "
            "(--no-restart)"
        )
    command = _systemctl()
    if command is None:
        return (
            "the running server keeps the old token until it is restarted; it is "
            f"not running as {UNIT}, so restart it yourself"
        )
    if _restart_unit(command, str(before["url"]).removesuffix("/mcp")):
        return f"restarted {UNIT}; the new token is in effect"
    return f"{UNIT} was restarted but does not answer yet; check it with systemctl"


def _systemctl() -> list[str] | None:
    """The systemctl that has the MCP unit active, system or user; else ``None``."""
    if shutil.which("systemctl") is None:
        return None
    for command in (["systemctl"], ["systemctl", "--user"]):
        try:
            answer = subprocess.run(
                [*command, "is-active", UNIT],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if answer.stdout.strip() == "active":
            return command
    return None


def _restart_unit(command: list[str], url: str, timeout: float = 15.0) -> bool:
    """Restart the unit and wait until the server answers again."""
    import httpx

    try:
        subprocess.run(
            [*command, "restart", UNIT], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health/live", timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def _token_line(written: str | None, variable: str, token: str | None) -> str:
    if not written:
        return "token:  not set — bricklogger mcp auth generate writes one"
    if not REFERENCE.match(written):
        return (
            "token:  set — written in daemon.yaml as a value; "
            "bricklogger mcp auth generate moves it to the env file"
        )
    if not token:
        return (
            f"token:  named in daemon.yaml as {written}, but {variable} has no "
            "value in the env file"
        )
    return f"token:  set — {variable} in the env file, referenced from daemon.yaml"


def _server_line(settings: DaemonSettings, token: str | None) -> str:
    state = probe_mcp(settings)
    url = str(state["url"])
    if not state["running"]:
        return f"server: not running — nothing answers at {url}"
    if not token:
        return f"server: running at {url} — open, no token is required"
    accepts = _accepts(url, token)
    if accepts is None:
        return f"server: running at {url} — it could not be asked about the token"
    if accepts:
        return f"server: running at {url} — accepts this token"
    return (
        f"server: running at {url} — rejects this token; restart it, "
        f"e.g. systemctl restart {UNIT}"
    )


def _accepts(url: str, token: str) -> bool | None:
    """Whether the running server takes this token; ``None`` when it will not say.

    Any answer but ``401`` means the token got through: the endpoint refuses a
    request without a session of its own, and that refusal comes after the
    token is checked.
    """
    import httpx

    try:
        answer = httpx.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=2.0
        )
    except httpx.HTTPError:
        return None
    return answer.status_code != 401


def _settings(context: CliContext) -> DaemonSettings:
    try:
        return load_daemon_settings(context.config_dir)
    except ConfigError as exc:
        raise fail(f"daemon.yaml: {exc}") from exc


def _written_token(config_dir: Path) -> str | None:
    """``mcp.token`` as the file has it — a reference, not the secret's value."""
    text = read_texts(config_dir).get("daemon") or ""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise fail(f"daemon.yaml: {exc}") from exc
    mcp = data.get("mcp") if isinstance(data, dict) else None
    token = mcp.get("token") if isinstance(mcp, dict) else None
    return str(token) if token else None


def _variable(written: str | None) -> str:
    """The env variable the token lives in: the one the file names, or the default."""
    if written and REFERENCE.match(written):
        return written[2:-1]
    return DEFAULT_VARIABLE
