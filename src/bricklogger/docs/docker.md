# Docker

Bricklogger also runs as a container. The image holds the same program the
[install script](getting-started.md#install) installs — the same commands, the
same four configuration files, the same directories — so a host where
everything else is a container needs nothing else installed. The install
script remains the primary installation; the image is the second way, and
neither changes how Bricklogger works.

## The image

`ghcr.io/cx1-aps/bricklogger`, built from the project's own source and
published by GitHub Actions.

| Tag | What it is |
|-----|------------|
| `0.2.2` | One released version — what a building's logger should name |
| `0.2` | The newest patch of that minor version |
| `latest` | The newest release |

Every version is built for `linux/amd64` and `linux/arm64`, and only after the
test suite has passed.

Inside, the layout is the one a service install gives: the Python environment
under `/opt/bricklogger`, the `bricklogger` command on the path, the
configuration in `/etc/bricklogger`, the data in `/var/lib/bricklogger`. All
of it belongs to an unprivileged user, and the container runs as that user and
never as root.

The image runs **one process**, and which one is the command:

| Command | Port | The process |
|---------|------|-------------|
| `bricklogger daemon run` (the default) | 8420 | The daemon and its API |
| `bricklogger serve` | 8421 | The web interface |
| `bricklogger mcp serve --http` | 8422 | The MCP server over HTTP |

That is the same division as the three systemd units, and the same
`daemon.yaml` decides the bindings and the ports.

## The compose file

Three volumes, and the daemon alone unless more is asked for. The compose file
brings **no database**: a logger writes to a TimescaleDB that already exists,
[prepared as it is on any other install](getting-started.md#prepare-the-database),
and reached over the network.

The three services carry the names of the three processes — `daemon`, `web`,
`mcp` — and the stack names itself, so the containers are
`bricklogger-daemon-1`, `bricklogger-web-1` and `bricklogger-mcp-1` whatever
the directory holding the file is called.

```yaml
name: bricklogger

services:
  daemon:
    image: ghcr.io/cx1-aps/bricklogger:0.2.2
    network_mode: host
    restart: unless-stopped
    stop_grace_period: 30s
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

  web:
    image: ghcr.io/cx1-aps/bricklogger:0.2.2
    command: ["bricklogger", "serve"]
    profiles: ["web"]
    depends_on: ["daemon"]
    network_mode: host
    restart: unless-stopped
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

  mcp:
    image: ghcr.io/cx1-aps/bricklogger:0.2.2
    command: ["bricklogger", "mcp", "serve", "--http"]
    profiles: ["mcp"]
    depends_on: ["daemon"]
    network_mode: host
    restart: unless-stopped
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

volumes:
  config:
  data:
  plugins:
```

```bash
docker compose up -d                        # the daemon alone
docker compose --profile web up -d          # and the web interface
docker compose --profile web --profile mcp up -d
```

The web interface and the MCP server are behind profiles because an
installation that only logs data needs neither, and the MCP server is of no
use before its [token](features/mcp.md#the-token) exists. They read the same
configuration volume, so they are switched on and off without touching it.

!!! note "One volume for the plugins"

    The plugin volume is mounted **inside** the data directory, at
    `/var/lib/bricklogger/plugins`, and is a volume of its own so it can be
    emptied and rebuilt without touching the runtime state, the model versions
    and the working graph beside it. See [plugins](#plugins) below.

## Bind mounts, and the user the container runs as

The compose file uses **named volumes**, and that is the path with the fewest
surprises: Docker fills a fresh named volume from the image, ownership
included, so the three directories belong to the container's user from the
first start.

Mounting **host directories** instead — `./config:/etc/bricklogger` and its
two siblings — is a reasonable wish: the configuration is then editable from
the host and backed up with the rest of the machine. It comes with one thing
to know. The container runs as an unprivileged user, `uid 999`, and a bind
mount keeps the host's ownership. A directory that does not exist when the
container starts is created **by Docker, as root**, and the daemon can then
write nothing: it cannot write the configuration, and it refuses to start
because it cannot open its [runtime state](features/daemon.md#a-data-directory-it-cannot-write).

Two ways out, and either is enough:

```bash
# the directories belong to the container's user
sudo mkdir -p config data plugins
sudo chown -R 999:999 config data plugins
```

```yaml
# or the container runs as the user that owns them
services:
  daemon:
    user: "1000:1000"
```

The second is the one to reach for when the files should stay the host user's
own — `id -u` and `id -g` on the host give the two numbers, the daemon then
runs as that user, and what it writes is readable and editable without `sudo`.
Any user works; nothing in the image requires `uid 999` when the directories
it writes to are mounted from the host. Set it on all three services, so they
write as the same user.

!!! note "A stray directory in the data directory"

    With bind mounts, the plugin mount lies inside the data mount, and Docker
    creates the mount point in the host's data directory as root before
    mounting over it. An empty `data/plugins` owned by root on the host is
    that mount point, not the plugins; they are in the directory mounted over
    it.

Every command in the documentation is run in the container:

```bash
docker compose exec daemon bricklogger status
docker compose exec daemon bricklogger points --outcome active
docker compose exec daemon bricklogger model upload /models/building.ttl
```

A model file comes from the host by mounting the directory that holds it, or
through the web interface, which uploads it from the browser.

## The network

The services run on the **host's own network**, and that is not a detail:
BACnet/IP finds devices by broadcasting a Who-Is on the local network, and a
bridge network's address translation neither carries that broadcast out nor
the devices' answers back. On the host network the source behaves exactly as
on an ordinary install — `address` is the host's own IPv4 address with its
prefix length, and `bricklogger sources <name> discover` answers.

Two consequences are worth knowing:

- **The ports are the host's.** 8420, 8421 and 8422 are bound directly, and
  `ports:` in the compose file does nothing. What the outside world can reach
  is decided by `api.host`, `web.host` and `mcp.host` in `daemon.yaml`, which
  default to `127.0.0.1` — the host itself and nothing else.
- **The three processes share loopback**, so the web interface and the MCP
  server reach the daemon's API on `127.0.0.1:8420` with no token, exactly as
  three units on one machine do.

An installation with no BACnet — one that reads a cloud service through a
plugin, say — can
run on an ordinary bridge network instead: drop `network_mode: host`, publish
the ports that should be reachable, set `api.host` to `0.0.0.0` with an
[`api.token`](features/configuration.md#daemonyaml), which the configuration
then requires, and point the other two at the daemon with
`--api http://daemon:8420`.

## The first start

The first `docker compose up -d` finds an empty configuration volume, writes
the four files into it and starts the daemon. The files are the ones
[`init --non-interactive`](features/cli.md#init) writes, with the example
source and destination **commented out**: they name another building's BACnet
address and a database whose password nobody has set, and the daemon refuses a
configuration it cannot resolve — a container that would not start, on a
restart policy that would try again and again. Commented out, they are an
example to uncomment and edit, and the daemon starts with an empty
configuration: no sources, no destinations, nothing accepted, and `status`
reports `idle` until there is something to do. Nothing is overwritten later.

To answer the guided questions instead, run `init` **before** the first
`up`, while the volume is still empty:

```bash
docker compose run --rm -it daemon bricklogger init
```

`init` refuses to overwrite a file that exists, so once the examples are
there, the way on is one instance at a time — the same questions, asked per
instance:

```bash
docker compose exec -it daemon bricklogger sources add bacnet_main --type bacnet-ip
docker compose exec -it daemon bricklogger destinations add tsdb --type timescaledb
docker compose exec daemon bricklogger validate
```

Or through the web interface's configuration screen, which edits the same
files.

Secrets go where they always go: the
[`env` file](features/configuration.md#the-env-file) in the configuration
volume, which `sources add` and `destinations add` write themselves when they
ask for a password without echo. A value already in the process environment
wins over the file, so `environment:` in the compose file also works — but
then the secret lives in a file beside the compose file rather than in the
volume with mode `600`, which is why the `env` file is the documented place.

## Plugins

A plugin is a Python package, and an image cannot grow one. The container
therefore keeps its plugins in a **volume of its own**, and installs them
there:

```bash
docker compose exec daemon bricklogger plugins add bricklogger-homeassistant==0.2.0
docker compose restart daemon
```

In a container, `plugins add` installs into the plugin volume instead of into
the image's environment, and otherwise behaves as
[the plugins page](features/plugins.md#installing-a-plugin) describes: it
takes a name, a name with a version, a wheel or a git URL, it prints the
catalogue when it is done, and it restarts nothing. Two things are its own:

- **What was asked for is written down.** The volume keeps a manifest of the
  packages that were installed into it, so the same set can be laid down again
  against another image — see [upgrading](#upgrading) below.
- **The image's own packages win.** The plugin directory is added to the path
  after them, and an installation is constrained to the versions the image
  already has, so a plugin cannot quietly replace the library Bricklogger
  itself runs on. A plugin that cannot live with those versions fails to
  install and says which package it wanted. What the image installed from a
  file rather than an index — Bricklogger itself — no index can supply, so
  the resolver is handed a wheel with the metadata and nothing else, at the
  image's version; the copy it installs is removed again with the other
  duplicates, and the image's is the one on the path.

The `web` service mounts the same volume, so the web interface's
[Plugins screen](features/web.md#screens) installs into it as well, and
`docker compose restart daemon` is the restart in both cases.

A plugin installed from a wheel or another path is recorded as that path,
since that is what was asked for. Keep it reachable from the container, or add
the plugin again from an index after an upgrade.

`plugins remove TYPE` uninstalls from the volume, with
[what only that plugin needed](features/plugins.md#removing-a-plugin), and
drops the line from the manifest. The built-in types live in the image and cannot be removed, as
everywhere else.

## Upgrading

Name the new tag in the compose file, then:

```bash
docker compose pull
docker compose up -d
```

The three volumes are untouched, so the configuration, the data **and the
plugins** survive. A plugin in the volume was installed against the Python and
the Bricklogger version of the image that installed it, and the volume
records which. When a new image differs from that record, the first container
to start lays the manifest down again before the daemon starts:

- The one reinstall needs a package index; every other start needs nothing.
- The manifest is laid down in its order, each package resolved
  [with those before it](features/plugins.md#installing-a-plugin). A package
  that cannot be installed, or cannot live with the plugins laid down before
  it, is logged and left out. The daemon starts anyway, and the instances of
  the types it would have provided are
  [failed](features/plugins.md#a-type-that-is-not-installed) until it is
  installed, exactly as they would be on any other install.
- When several containers start at once, one does the work and the others wait
  for it to finish.

Going back to the previous tag works the same way, and lays the plugins down
against that image again.

Between images, the plugins are upgraded in the volume with
[`update`](features/cli.md#update): `update <type>` for one, `update all` for
all of them, each held to the image's version of Bricklogger, validated
before it is kept, and written to the manifest so a new image lays the same
set down. `update core` refuses in a container and names the two commands
above. Restart the containers afterwards with `docker compose restart`.

The destination migrates its own schema when it starts, and an incompatible
configuration is rejected at start with the file and the key named — as on
every other install.

## Logs and lifecycle

`daemon run` logs to stdout, which Docker collects:

```bash
docker compose logs -f daemon
```

`log.format: json` in `daemon.yaml` turns the lines into JSON for a log
shipper. `log.file` and its rotation belong to `daemon start` and do nothing
here.

Stopping the container sends `SIGTERM`, and the daemon shuts down the way
`daemon stop` does: it finishes what is in flight, flushes and stops the
sources. Keep `stop_grace_period` above the `stop_timeout` in `daemon.yaml`
— 30 seconds against the default 10 — so Docker does not cut the shutdown
short. `restart: unless-stopped` gives what `Restart=on-failure` gives the
service: the daemon comes back after a crash and after a reboot, and stays
down when it was stopped on purpose.

Three commands belong to a machine without a service manager and have no
place here: `daemon start`, `daemon stop` and `daemon restart` background a
process and write a PID file, while in a container the container **is** the
process. Restart it instead. `daemon reload` is unaffected and rereads the
configuration in place.

## Without compose

The same thing, by hand:

```bash
docker run -d --name bricklogger \
  --network host \
  --restart unless-stopped \
  -v bricklogger-config:/etc/bricklogger \
  -v bricklogger-data:/var/lib/bricklogger \
  -v bricklogger-plugins:/var/lib/bricklogger/plugins \
  ghcr.io/cx1-aps/bricklogger:0.2.2
```

The web interface and the MCP server are two more `docker run` lines with the
same volumes and their own command.

## When something is wrong

- **`docker compose logs daemon`** is the first place: a configuration
  the daemon rejects names the file and the key there.
- **The containers restart in a ring, and nothing answers.** The log says
  `the data directory ... cannot be used`, and a bind mount is behind it: the
  directory belongs to another user than the one the container runs as. See
  [bind mounts](#bind-mounts-and-the-user-the-container-runs-as).
- **No devices answer.** The container must be on the host network, and
  `address` in `sources.yaml` must be the host's own address with the right
  prefix length; `bricklogger sources <name> discover` in the container tells
  the two cases apart.
- **A plugin is gone after an upgrade.** Its instances are failed in
  `bricklogger status` with the type not installed, and the log line from the
  start says why the reinstall left it out — often that it could not reach a
  package index. A plugin that is installed but no longer loads shows as
  failed in `bricklogger plugins` with the reason.
- **The web interface cannot reach the daemon.** On the host network both must
  agree on `api.host`; on a bridge network the daemon must bind `0.0.0.0` and
  have a token.
