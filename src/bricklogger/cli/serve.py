"""``bricklogger serve``: the web interface in the foreground, a thin client
over the daemon's API on a port of its own."""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from bricklogger.cli.context import cli_context
from bricklogger.cli.output import fail
from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.config.schema import is_loopback

log = logging.getLogger(__name__)


def serve_command(
    ctx: typer.Context,
    host: Annotated[
        str | None, typer.Option("--host", help="Bind here; default web.host.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Listen here; default web.port.")
    ] = None,
) -> None:
    """Serve the web interface; it talks to the daemon's API like every command.

    Reads web.host, web.port and web.password from daemon.yaml; --host and
    --port override the binding, and the root option --api points it at a
    daemon on another machine. Bound beyond localhost, a login is required.
    """
    import uvicorn

    from bricklogger.web.app import create_web_app

    context = cli_context(ctx)
    try:
        settings = load_daemon_settings(context.config_dir)
    except ConfigError as exc:
        raise fail(f"daemon.yaml: {exc}") from exc
    api_url = context.api_url or f"http://{settings.api.host}:{settings.api.port}"
    token = context.token or settings.api.token
    bind_host = host or settings.web.host
    bind_port = port or settings.web.port
    exposed = not is_loopback(bind_host)
    if exposed and not settings.web.password:
        raise fail(
            "web.password is required when the web interface is bound beyond "
            "localhost; set it in daemon.yaml through an environment variable"
        )
    logging.basicConfig(
        level=settings.log.level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    password = settings.web.password if exposed else None
    app = create_web_app(api_url, token, password, config_dir=context.config_dir)
    log.info(
        "serving the web interface on http://%s:%d for the daemon at %s",
        bind_host,
        bind_port,
        api_url,
    )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="warning")
