"""Running a protocol tool on a configured source instance: through the daemon
that owns the connection, or in-process when none runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from bricklogger.daemon.plugins import run_tool_offline
from bricklogger.ops.client import DaemonApi
from bricklogger.ops.config import instances
from bricklogger.ops.context import Operations
from bricklogger.ops.errors import OperationError


def run_instance_tool(
    ops: Operations,
    client: DaemonApi | None,
    name: str,
    tool: str,
    parameters: Mapping[str, Any],
) -> Any:
    """The tool's own structured result; only sources have tools."""
    settings = instances(ops, client, "source").get(name)
    if not isinstance(settings, Mapping):
        raise OperationError(f"no source named {name!r} is configured")
    type_name = str(settings.get("type") or "")
    if client is not None:
        return client.post(
            f"/v1/plugins/{type_name}/instances/{name}/tools/{tool}",
            json=dict(parameters),
        )
    try:
        return run_tool_offline(
            ops.config_dir, ops.plugins(), type_name, name, tool, dict(parameters)
        )
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise OperationError("invalid tool parameters:\n" + "\n".join(lines)) from exc
    except (KeyError, LookupError, RuntimeError, OSError) as exc:
        raise OperationError(str(exc)) from exc
