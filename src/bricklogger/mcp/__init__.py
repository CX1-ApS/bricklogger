"""The MCP server: the third door into the same functionality as the CLI and the
web interface, for an AI assistant. Served by ``bricklogger mcp serve``; see
``docs/features/mcp.md``."""

from bricklogger.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]
