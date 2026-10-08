# Getting started

Bricklogger runs on Linux and is installed with uv, in the home directory of
the login that is to run it. This page takes a new installation from nothing
to the first observations in the database.

## Install

Bricklogger is installed with [uv](https://docs.astral.sh/uv/), which also
fetches a Python that fits when the machine has none, so nothing else needs to
be there first. A machine without uv gets it with its installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, as the login that is to run the logger — not as root:

```bash
uv tool install bricklogger
```

The environment lands in uv's tool directory and the `bricklogger` command in
`~/.local/bin`; nothing outside the home directory is touched. When
`~/.local/bin` is not on the path, `uv tool update-shell` puts it there. A
given version is `uv tool install bricklogger==0.2.4`, and a wheel on disk is
installed by its path instead of the name.

Bricklogger runs **as the login that installed it**, with its configuration
and data in that login's home directory and its services as `systemd --user`
units; there is no installation for the whole machine and no user of its own.
A dedicated login for the logger keeps it apart from the people who work on
the machine. A source that reads a serial port needs that login in the group
that owns the port, `dialout` on most distributions:
`sudo usermod -aG dialout <login>`.

`bricklogger --version` confirms the installation, and `bricklogger plugins`
lists the built-in plugins. A plugin is an ordinary Python package that the
daemon finds through its entry points; it is added afterwards with
`bricklogger plugins add`, as the [plugins page](features/plugins.md)
describes. The iBOS source, for one, is `bricklogger plugins add
bricklogger-ibos`.

On a host where everything else is a container, the same program is an
image: `ghcr.io/cx1-aps/bricklogger`, with a compose file and the rest on the
[Docker page](docker.md). The two installations have the same commands, the
same four configuration files and the same directories; the rest of this page
applies to both, with every command run in the container.

## Prepare the database

TimescaleDB needs a database with the `timescaledb` extension and a role that
may create tables in it. The destination creates and migrates its own schema
on its first start, so nothing else is prepared by hand.

```sql
CREATE DATABASE brick;
\c brick
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE ROLE bricklogger LOGIN PASSWORD '...';
GRANT ALL ON SCHEMA public TO bricklogger;
```

## Create the configuration

The configuration lives in `~/.config/bricklogger` and the data in
`~/.local/share/bricklogger`, as the
[configuration document](features/configuration.md#location) describes.
`init` creates both and fills the first in a guided setup, and refuses to
overwrite a file that exists:

```bash
bricklogger init
```

It names the directories it uses, then asks which sources and destinations to
configure among the installed types, and for each instance a name and its
settings — the required ones first, the optional ones with their default
offered, so Enter keeps it. For a BACnet/IP source that is this machine's
address on the BACnet network and a device instance for the logger itself,
unique on that network; for a TimescaleDB destination the DSN and the password,
which is asked for without echo and written to the
[`env` file](features/configuration.md#the-env-file) as `TSDB_PASSWORD`, with
`${TSDB_PASSWORD}` in the YAML. `rules.yaml` gets the one rule that accepts
every point in the model every five minutes — a good first run, since
`brick:Point` matches all of them — and the result is validated. Last, `init`
offers to set up the services, as the next section describes.

What it wrote can be read and changed at any time: `bricklogger sources
config show`, `destinations config show`, `rules config show` and `daemon
config show` print the files, `… config edit` opens them in `$EDITOR`, and
[`sources add`](features/cli.md#sources-and-destinations) writes an instance
from flags. The four files look like this:

```yaml
# daemon.yaml — as init wrote it; every value shown is a default
api:
  host: 127.0.0.1
  port: 8420
data_dir: /home/logger/.local/share/bricklogger
stop_timeout: 10s
log:
  level: info
  format: text
```

```yaml
# sources.yaml
bacnet_main:
  type: bacnet-ip
  address: 192.168.10.5/24
  device_instance: 1200
```

```yaml
# destinations.yaml
tsdb:
  type: timescaledb
  dsn: "postgres://bricklogger@db.example.com:5432/brick"
  password: ${TSDB_PASSWORD}
```

```yaml
# rules.yaml
- name: Everything, every five minutes
  match: { class: brick:Point }
  action: accept
  method: poll
  interval: 5m
```

A file that does not exist is read as empty, so only the files you use need
content. `init --non-interactive` writes the four files with commented
examples instead of asking, for a machine set up by hand; the secret then goes
into the `env` file yourself, and the whole is checked with:

```bash
bricklogger validate
```

## Start the services

The daemon, the web interface and the MCP server over HTTP run as
`systemd --user` services of the login, with lingering enabled, so they start
when the machine boots and keep running after a logout. `init` offers to set
them up when it is done — the daemon always, the web interface and the MCP
server if you want them — and the same is one command at any time:

```bash
bricklogger services install --web
bricklogger status
```

`services install` writes the units, enables and starts them, and
`--mcp` adds the MCP server; the [CLI page](features/cli.md#services) has the
rest. Where the distribution lets only root enable lingering, it says so and
prints the one `sudo` command that does it; until then the services stop at
logout.

Status shows the daemon and the web interface running, and the daemon's
summary reports `idle`: it runs, but it has no model yet.

On a machine without systemd, such as a commissioning laptop under WSL,
`bricklogger daemon start` runs the daemon in the background instead and
`bricklogger serve` the web interface in the foreground. In a container the
command is `bricklogger daemon run`, which stays in the foreground and logs to
stdout.

## Open the web interface

Browse to `http://127.0.0.1:8421`. Everything below can be done there as
well; the commands are shown because they can be scripted.

## Connect an assistant

An AI assistant that speaks MCP can do the configuration for you and check
the result. On the machine itself, with Claude Code installed:

```bash
claude mcp add --transport stdio bricklogger -- bricklogger mcp serve
```

Then ask it, in your own words, for a BACnet/IP source on this machine's
address, a TimescaleDB destination with its password in the `env` file, and a
rule set that logs every temperature sensor every five minutes. It reads the
plugins' schemas and this documentation, validates before it writes, and
refuses to take a secret's value — that you put in the `env` file yourself.
From another machine the command in the client's configuration is
`ssh <host> bricklogger mcp serve`, or the server runs over HTTP:
`bricklogger mcp auth generate` writes a token and prints the whole
registration line with it, and `bricklogger mcp serve --http` serves it, as
the [MCP page](features/mcp.md#the-token) describes. `bricklogger status`
shows on its third line whether that server is up.

## Upload the model

```bash
bricklogger model upload building.ttl
```

The model is validated, inferred and activated — the CLI follows the job and
shows its steps — and the result is the diff to the previously active version,
which for a first upload is every point. Then:

```bash
bricklogger status
bricklogger points
```

Status now reports `ok`, and the points view shows every accepted point with
its source instance and outcome. Within the first interval the observations
arrive: `bricklogger points --outcome active` shows the last valid value per
point, and the database's `observations` table fills.

## Switch notifications on

With a model in place, Bricklogger can mail an administrator when something
goes wrong, when it is put right, and once a day as proof that the logger is
still alive. It is off until it is switched on, and it needs a model, which is
why it comes here and not earlier. Put the mail server and the recipients under
`notifications` in `daemon.yaml`, keep the password in the `env` file, and try
it before switching it on:

```bash
bricklogger notify test
```

The test mail goes out on the configuration as written and prints what the mail
server answered, so a firewall that swallows submission is found now rather
than on the night nothing arrives. Then set `enabled: true`, reload, and check
with `bricklogger notify status`. The settings and what each mail contains are
described under [notifications](features/notifications.md).

## If nothing arrives

- `bricklogger status warnings` lists what stands in the way, one line per
  cause: `unclaimed`, `no_reference`, `rejected` points with their reason,
  `read_error` and the rest of the [warning codes](features/daemon.md#status).
- `bricklogger sources bacnet_main discover` shows the devices that answer on
  the network. If none does, the address in `sources.yaml` or the network is
  the problem, not the model.
- `bricklogger sources bacnet_main resolve <point-URI>` shows how one point's
  reference resolves — device, address, object, property — and reads it.
- `bricklogger destinations status` shows whether the database takes writes.
  A database that is down fills the spool, and the observations follow when it
  is back.

## Upgrade

`status` and the daily summary say when there is a newer release, and
`bricklogger update status` shows what there is. Then:

```bash
bricklogger update all
```

It upgrades Bricklogger and the plugins together, checks the configuration
with the new version before anything is restarted, puts the previous versions
back if it does not hold, and restarts the services that run. The
configuration and the data stay where they are. `update core` upgrades
Bricklogger alone, as far as the installed plugins allow, and `update <type>`
one plugin; the [CLI page](features/cli.md#update) has the rest.

`uv tool upgrade bricklogger` works too and keeps the plugins, but moves all
of them and checks nothing first; restart the services afterwards. Do not run
`uv tool install bricklogger` again on an installation with plugins: it
replaces the packages in the environment with the ones on its command line,
and the plugins are uninstalled. A container installation pulls a new image
instead, as the [Docker page](docker.md#upgrading) describes.

## Uninstall

```bash
bricklogger services uninstall
uv tool uninstall bricklogger
```

The services go first, while the command that knows them is still there. The
configuration in `~/.config/bricklogger` and the data in
`~/.local/share/bricklogger` are kept; remove them by hand when they are no
longer wanted.

The destination migrates its schema at start. An incompatible configuration
is rejected at start with a message naming the file and the key.
