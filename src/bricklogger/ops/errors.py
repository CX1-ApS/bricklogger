"""What goes wrong in an operation, said without any one interface's vocabulary.

The CLI turns these into its messages and exit codes and the MCP server into
tool errors an assistant can act on; the web interface keeps its own client.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from bricklogger.config.issues import ConfigIssue


class OperationError(Exception):
    """An operation could not be carried out; ``issues`` carries validation errors."""

    def __init__(self, message: str, issues: Sequence[ConfigIssue] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.issues = list(issues)

    def describe(self) -> str:
        """The message, then one line per issue."""
        return "\n".join([self.message, *(f"  {issue}" for issue in self.issues)])


class DaemonRequired(OperationError):
    """The operation needs the running daemon, and none answers."""


class DaemonUnreachable(OperationError):
    """Nothing answers at the daemon's API binding, or the answer broke off."""


class ApiError(OperationError):
    """The daemon answered with a problem document; ``message`` describes it whole."""

    def __init__(
        self, status: int, body: Mapping[str, Any] | None, message: str
    ) -> None:
        self.status = status
        self.body = dict(body) if body else {}
        issues = [
            ConfigIssue.from_dict(entry)
            for entry in self.body.get("errors") or []
            if isinstance(entry, Mapping) and "file" in entry
        ]
        super().__init__(message, issues)

    def describe(self) -> str:
        """The message alone: it lists the errors already."""
        return self.message
