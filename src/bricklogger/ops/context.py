"""What every operation works from: the config directory, how the daemon is
reached, and the installed plugins."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bricklogger.ops.client import DaemonApi, reachable_api
from bricklogger.ops.errors import DaemonRequired, OperationError
from bricklogger.sdk.registry import PluginError, PluginRegistry


@dataclass
class Operations:
    """The place an interface works from.

    ``api_url`` names a daemon elsewhere and makes it required, as the CLI's
    ``--api`` does. ``registry`` and ``env`` are given by tests, which bring
    fake plugins and an environment of their own.
    """

    config_dir: Path
    api_url: str | None = None
    token: str | None = None
    registry: PluginRegistry | None = None
    env: Mapping[str, str] | None = None

    def plugins(self) -> PluginRegistry:
        """The installed plugins, loaded once."""
        if self.registry is None:
            try:
                self.registry = PluginRegistry.from_entry_points()
            except PluginError as exc:
                raise OperationError(str(exc)) from exc
        return self.registry

    def client(self) -> DaemonApi | None:
        """A client when a daemon answers, or ``--api`` names one; else ``None``."""
        return reachable_api(self.config_dir, self.api_url, self.token, self.env)

    @contextmanager
    def session(self) -> Iterator[DaemonApi | None]:
        """A client for one operation, closed afterwards; ``None`` without a daemon."""
        client = self.client()
        try:
            yield client
        finally:
            if client is not None:
                client.close()

    def require(self, client: DaemonApi | None) -> DaemonApi:
        """The client, or the plain refusal of an operation that needs the daemon."""
        if client is None:
            raise DaemonRequired(
                "this needs the running daemon, and none answers at the binding "
                "in daemon.yaml"
            )
        return client
