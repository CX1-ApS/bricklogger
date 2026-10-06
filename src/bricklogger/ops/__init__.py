"""The operations the interfaces share.

The CLI and the MCP server both configure the logger, look at what it does and
run its protocol tools, through the daemon's API when a daemon answers and on
the files in the config directory when none does. This package holds those
operations once, said without any one interface's vocabulary: it prints
nothing, exits nowhere, and reports failure with :class:`OperationError`.
See ``docs/features/mcp.md`` and ``docs/features/cli.md``.
"""

from bricklogger.ops.client import DaemonApi, reachable_api
from bricklogger.ops.context import Operations
from bricklogger.ops.errors import (
    ApiError,
    DaemonRequired,
    DaemonUnreachable,
    OperationError,
)

__all__ = [
    "ApiError",
    "DaemonApi",
    "DaemonRequired",
    "DaemonUnreachable",
    "OperationError",
    "Operations",
    "reachable_api",
]
