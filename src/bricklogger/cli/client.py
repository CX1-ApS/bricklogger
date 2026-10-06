"""The CLI's client for the daemon's API: the shared client, with every failure
turned into the CLI's error message and exit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

import typer

from bricklogger.cli.context import CliContext
from bricklogger.cli.output import fail
from bricklogger.ops.client import DaemonApi, describe_problem, format_error
from bricklogger.ops.errors import OperationError

if TYPE_CHECKING:
    import httpx

__all__ = ["ApiClient", "describe_problem", "format_error", "reachable_client"]


class ApiClient(DaemonApi):
    """Talks to one daemon; raises a CLI exit when it cannot be reached."""

    @classmethod
    def from_context(cls, context: CliContext) -> Self:
        try:
            return cls.from_directory(
                context.config_dir, context.api_url, context.token
            )
        except OperationError as exc:
            raise fail(exc.message) from exc

    def prefixes(self) -> dict[str, str]:
        """The active model's prefixes, for shortening URIs; empty when unknown."""
        try:
            found = self.get("/v1/models").get("prefixes")
        except typer.Exit:
            return {}
        return dict(found) if isinstance(found, dict) else {}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return super()._request(method, path, **kwargs)
        except OperationError as exc:
            raise fail(exc.message) from exc


def reachable_client(context: CliContext) -> ApiClient | None:
    """The client when ``--api`` names a daemon or one answers at the binding.

    ``None`` means: work without a daemon. With ``--api`` the daemon is
    required, so a client comes back whether it answers or not.
    """
    try:
        client = ApiClient.from_context(context)
    except typer.Exit:
        if context.api_url is not None:
            raise
        return None
    if context.api_url is not None or client.reachable():
        return client
    return None
