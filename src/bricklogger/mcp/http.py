"""The MCP server over streamable HTTP: the SDK's application with a liveness
answer in front of it and, whenever one is set, the bearer token ``daemon.yaml``
names. See ``docs/features/mcp.md``, "Access"."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from bricklogger.config.schema import is_loopback
from bricklogger.ops.status import live_answer

PROBLEM = "application/problem+json"


class BearerToken:
    """Refuses every request outside ``/health`` that does not carry the token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] == "http" and not path.startswith("/health"):
            header = Headers(scope=scope).get("authorization", "")
            if not hmac.compare_digest(header, self.expected):
                response = JSONResponse(
                    {
                        "type": "urn:bricklogger:problem:unauthorized",
                        "title": "Unauthorized",
                        "status": 401,
                        "detail": "a valid bearer token is required",
                    },
                    status_code=401,
                    media_type=PROBLEM,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(server: MCPServer, *, host: str, token: str | None) -> ASGIApp:
    """The application: ``/health/live``, the MCP endpoint at ``/mcp``, and the
    token check in front of both when a token is given.

    Bound to loopback, the SDK's own check that requests are addressed to
    localhost stays on; bound elsewhere it is switched off, because the token
    is the protection there and the host names are the operator's.
    """
    if is_loopback(host):
        inner = server.streamable_http_app()
    else:
        inner = server.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        )

    started_at = datetime.now(UTC)

    async def live(_: Request) -> Response:
        return JSONResponse(live_answer(started_at))

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app: ASGIApp = Starlette(
        routes=[Route("/health/live", live), Mount("/", app=inner)],
        lifespan=lifespan,
    )
    if token:
        app = BearerToken(app, token)
    return app


def serve_http(server: MCPServer, *, host: str, port: int, token: str | None) -> None:
    """Serve until stopped."""
    uvicorn.run(
        build_http_app(server, host=host, token=token),
        host=host,
        port=port,
        log_level="warning",
    )
