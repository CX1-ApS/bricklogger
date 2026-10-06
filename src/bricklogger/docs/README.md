# Bricklogger — Documentation

Bricklogger collects data from building automation systems based on a Brick
model of the building and writes it to time-series databases. This
documentation covers **all functionality** of the program.

## Index

| Document | Content |
|----------|---------|
| [getting-started.md](getting-started.md) | Installation, first configuration, first model |
| [docker.md](docker.md) | The container image, the compose file, plugins in a volume and upgrading |
| [vision.md](vision.md) | What Bricklogger is, principles and scope |
| [architecture.md](architecture.md) | Daemon, CLI, web, MCP, Brick model, configuration, plugins, observations |
| [features/configuration.md](features/configuration.md) | The four files of the config directory and their schema |
| [features/daemon.md](features/daemon.md) | Daemon functionality: selectors, model findings, status, logging, runtime state |
| [features/api.md](features/api.md) | The HTTP API: conventions and endpoints |
| [features/cli.md](features/cli.md) | CLI commands and behaviour |
| [features/web.md](features/web.md) | The web interface: serve, access, screens, the model explorer |
| [features/mcp.md](features/mcp.md) | The MCP server: how it is started, what an assistant is told, resources, tools and prompts |
| [features/sources.md](features/sources.md) | Data sources: the source contract and BACnet/IP |
| [features/destinations.md](features/destinations.md) | Data destinations, contract and TimescaleDB schema |
| [features/plugins.md](features/plugins.md) | Plugins: installing, removing and upgrading one; writing, testing and distributing one; the SDK |
| [features/notifications.md](features/notifications.md) | Mail to the administrator: alarms, all clears and the daily summary |
