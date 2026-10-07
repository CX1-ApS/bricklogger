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

As the login that is to run the logger, not as root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # uv, if the machine has none
uv tool install bricklogger
```

uv brings a Python that fits, so the machine needs nothing else. Everything
stays in that login's home directory: the program in uv's tool directory, the
command in `~/.local/bin`, the configuration in `~/.config/bricklogger` and the
data in `~/.local/share/bricklogger`. `uv tool install bricklogger==0.2.3`
installs a given version.

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
bricklogger init                        # guided: sources, destinations, secrets, services
bricklogger validate                    # after any edit by hand
bricklogger services install --web      # if init did not: the daemon and the web interface
bricklogger model upload building.ttl   # validate, infer, activate
bricklogger status
bricklogger points --outcome active     # the last value of every point
```

`init` asks for the settings of each source and destination and writes four
files: `sources.yaml`, `destinations.yaml`, `rules.yaml` and `daemon.yaml`.
Secrets, such as the database password, go into an `env` file beside them and
are referred to as `${NAME}`. Last, `init` offers to run the daemon — and the
web interface and the MCP server, if you want them — as `systemd --user`
services that start at boot. The first rule set accepts every point in the
model every five minutes; [rules](https://cx1-aps.github.io/bricklogger/features/configuration/)
narrow that down by class, location, equipment or any SPARQL pattern.

## The web interface and an assistant

The web interface is at `http://127.0.0.1:8421` once its service runs, or
after `bricklogger serve` in a terminal. Everything the CLI does can be done
there, and the model explorer shows the
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
systemctl --user restart bricklogger         # the daemon reads plugins at start
```

A plugin is a Python package that provides a source or a destination type. A
plugin that cannot load, or is configured but not installed, stops nothing:
its instances show as failed and the rest keep running. Writing one is described under
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

```bash
bricklogger update status        # what there is to upgrade
bricklogger update all           # Bricklogger and the plugins together
```

`update` checks the configuration with the new version before it restarts
anything, and puts the previous versions back if it does not hold; the
configuration and the data stay where they are. `update core` upgrades
Bricklogger alone, `update <type>` one plugin. Do not run
`uv tool install bricklogger` again over an installation with plugins: it
uninstalls every plugin not named with `--with`. A container pulls the new
image instead.

**Upgrading from 0.2 installed with the install script:** the install script
is gone, and so is installing as root. Remove the old installation with the
script it came from — `curl -fsSL
https://github.com/CX1-ApS/bricklogger/releases/download/v0.2.2/install.sh |
sudo sh -s -- --uninstall`, without `sudo` for one in a home directory — then
install as above, add the plugins again with `bricklogger plugins add`, copy
the configuration, its `env` file and the data over if they lay in
`/etc/bricklogger` and `/var/lib/bricklogger` — with `data_dir` in
`daemon.yaml` changed to the new place — and run `bricklogger services install`.

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
