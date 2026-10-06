"""Bricklogger: a data bridge for building automation, driven by a Brick model.

The package is laid out after the architecture in ``docs/architecture.md``:

- ``bricklogger.daemon`` — the long-running core that collects and writes,
- ``bricklogger.cli`` — the ``bricklogger`` command, the control plane,
- ``bricklogger.web`` — the web interface served by ``bricklogger serve``,
- ``bricklogger.mcp`` — the MCP server served by ``bricklogger mcp serve``, for
  an AI assistant,
- ``bricklogger.ops`` — the operations the CLI and the MCP server share,
- ``bricklogger.sdk`` — the contract and the helpers plugins are built on,
- ``bricklogger.plugins`` — the built-in source (BACnet/IP) and
  destination (TimescaleDB), registered through the same entry points as
  external plugins.
"""

__version__ = "0.2.0"
