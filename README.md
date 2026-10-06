# Bricklogger

Bricklogger is a data bridge for building automation. It reads a
[Brick](https://brickschema.org/) model of a building, collects the values of
the points the model describes from the building's automation systems, and
writes them to a time-series database. The model decides what is collected:
upload a new model, and the logger follows it.

- **Sources:** BACnet/IP on the local network is built in; other systems are
  added as plugins, such as the [iBOS source](https://github.com/CX1-ApS/bricklogger-ibos)
  for the iBOS Data API in the cloud.
- **Destination:** TimescaleDB, with every observation timestamped by its
  source and a spool that holds them while the database is away.
- **Interfaces:** a command-line interface first, a web interface with the same
  abilities beside it — including a model explorer — an HTTP API, and an MCP
  server so an AI assistant can configure the logger for you.
- **Read only:** Bricklogger never writes to a building automation system.

The full documentation is at **<https://cx1-aps.github.io/bricklogger/>**. This
page is the short version.

## What you need

- A Linux machine, amd64 or arm64, that can reach the building's network —
  for BACnet/IP, on the same subnet as the devices or with a route to them.
- A Brick model of the building as Turtle (`.ttl`), with the points' external
  references (BACnet device and object, for instance).
- A TimescaleDB database Bricklogger may create tables in.

## Install

```bash
curl -fsSL https://github.com/CX1-ApS/bricklogger/releases/latest/download/install.sh | sudo sh
```

The script brings its own Python, so the machine needs only `curl`. With
`sudo` it installs a service: the program under `/opt/bricklogger`, the
configuration in `/etc/bricklogger`, the data in `/var/lib/bricklogger` and
three systemd units, left stopped. Without `sudo` everything stays in your home
directory. `--version 0.2.0` installs a given version; options come after
`--` when the script is piped: `… | sudo sh -s -- --version 0.2.0`.

**As a container** the same program is `ghcr.io/cx1-aps/bricklogger`, with a
compose file for the daemon, the web interface and the MCP server; see
[Docker](https://cx1-aps.github.io/bricklogger/docker/).

## Prepare the database

```sql
CREATE DATABASE brick;
\c brick
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE ROLE bricklogger LOGIN PASSWORD '...';
GRANT ALL ON SCHEMA public TO bricklogger;
```

Bricklogger creates and migrates its own tables at the first start.

## First setup

```bash
bricklogger init                        # guided: sources, destinations, secrets
bricklogger validate                    # after any edit by hand
bricklogger daemon start                # or: sudo systemctl enable --now bricklogger
bricklogger model upload building.ttl   # validate, infer, activate
bricklogger status
bricklogger points --outcome active     # the last value of every point
```

`init` asks for the settings of each source and destination and writes four
files: `sources.yaml`, `destinations.yaml`, `rules.yaml` and `daemon.yaml`.
Secrets, such as the database password, go into an `env` file beside them and
are referred to as `${NAME}`. The first rule set accepts every point in the
model every five minutes; [rules](https://cx1-aps.github.io/bricklogger/features/configuration/)
narrow that down by class, location, equipment or any SPARQL pattern.

## The web interface and an assistant

```bash
bricklogger serve        # http://127.0.0.1:8421
```

Everything the CLI does can be done there, and the model explorer shows the
building as a tree and a graph, with every point's outcome and value.

To let an AI assistant that speaks MCP set up sources, destinations and rules,
register the MCP server with it — for Claude Code on the same machine:

```bash
claude mcp add --transport stdio bricklogger -- bricklogger mcp serve
```

The assistant reads the plugins' schemas and this documentation, validates
before it writes, and never takes a secret's value. See
[MCP](https://cx1-aps.github.io/bricklogger/features/mcp/) for remote use.

## Plugins

```bash
bricklogger plugins                          # what is installed
bricklogger plugins add bricklogger-ibos     # add a plugin from PyPI
bricklogger plugins remove ibos              # remove one by its type
sudo systemctl restart bricklogger           # the daemon reads plugins at start
```

A plugin is a Python package that provides a source or a destination type. A
plugin that cannot load stops nothing: its instances show as failed and the
rest keep running. Writing one is described under
[plugins](https://cx1-aps.github.io/bricklogger/features/plugins/).

## Mail notifications

Bricklogger can mail an administrator when something goes wrong, when it is
put right, and once a day as proof that it is alive. Put the mail server and
recipients under `notifications` in `daemon.yaml`, then:

```bash
bricklogger notify test
```

See [notifications](https://cx1-aps.github.io/bricklogger/features/notifications/).

## Upgrade

Run the install script again; it leaves the plugins, the configuration and the
data where they are. Then restart the daemon. A container pulls the new image
instead:

```bash
curl -fsSL https://github.com/CX1-ApS/bricklogger/releases/latest/download/install.sh | sudo sh
sudo systemctl restart bricklogger
```

After an upgrade to a new minor version, `bricklogger plugins` shows whether
every plugin still loads.

**Upgrading from 0.1 with an iBOS source:** iBOS is no longer built in. The
`ibos` instance shows as failed until the plugin is added with
`bricklogger plugins add bricklogger-ibos` and the daemon is restarted; the
configuration and the collected history stay as they were.

## If nothing arrives

- `bricklogger status warnings` lists what stands in the way, one line per
  cause — points no source claims, points without a reference, read errors.
- `bricklogger sources bacnet_main discover` shows the BACnet devices that
  answer. If none does, the address in `sources.yaml` or the network is the
  problem, not the model.
- `bricklogger sources bacnet_main resolve <point>` shows how one point's
  reference resolves, and reads it.
- `bricklogger destinations status` shows whether the database takes writes.
  While it does not, observations wait in the spool.

## Reporting a problem

Bugs and questions go in the
[issues](https://github.com/CX1-ApS/bricklogger/issues). A security
vulnerability is reported privately, as [SECURITY.md](https://github.com/CX1-ApS/bricklogger/blob/main/SECURITY.md) describes.

## License

[MIT](https://github.com/CX1-ApS/bricklogger/blob/main/LICENSE). Bricklogger bundles Brick's ontology and a few web libraries
and fonts under their own licenses, listed with them in the package.
