# Docker

Bricklogger also runs as a container. The image holds the same program
[`uv tool install`](getting-started.md#install) installs — the same commands
and the same four configuration files — so a host where everything else is a
container needs nothing else installed. The installation with uv remains the
primary one; the image is the second way, and neither changes how
Bricklogger works.

## The image

`ghcr.io/cx1-aps/bricklogger`, built from the project's own source and
published by GitHub Actions.

| Tag | What it is |
|-----|------------|
| `0.3.1` | One released version — what a building's logger should name |
| `0.3` | The newest patch of that minor version |
| `latest` | The newest release |

Every version is built for `linux/amd64` and `linux/arm64`, and only after the
test suite has passed.

Inside, the Python environment is under `/opt/bricklogger`, the
`bricklogger` command on the path, the configuration in `/etc/bricklogger` —
named by `BRICKLOGGER_CONFIG_DIR` — and the data in `/var/lib/bricklogger`. All
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

Four services and four volumes, and all of them start with one command. The
database comes with it: `db` is TimescaleDB, prepared at its first start for
Bricklogger, and the daemon writes to it with no further setup. A site whose
database lives elsewhere, or that writes to no TimescaleDB at all, removes
`db` — see [without the database](#without-the-database) below.

The services carry the names of the processes — `db`, `daemon`, `web`, `mcp`
— and the stack names itself, so the containers are `bricklogger-db-1`,
`bricklogger-daemon-1`, `bricklogger-web-1` and `bricklogger-mcp-1` whatever
the directory holding the file is called.

```yaml
name: bricklogger

services:
  db:
    image: timescale/timescaledb-ha:pg18
    command: ["postgres", "-c", "listen_addresses=127.0.0.1",
              "-c", "port=${BRICKLOGGER_DB_PORT:-5432}"]
    network_mode: host
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      POSTGRES_INITDB_ARGS: --auth-host=scram-sha-256
      BRICKLOGGER_DB_NAME: ${BRICKLOGGER_DB_NAME:-bricklogger}
      BRICKLOGGER_DB_USER: ${BRICKLOGGER_DB_USER:-bricklogger}
      BRICKLOGGER_DB_PASSWORD: ${BRICKLOGGER_DB_PASSWORD:?set BRICKLOGGER_DB_PASSWORD in .env}
    configs:
      - source: bricklogger-role
        target: /docker-entrypoint-initdb.d/100-bricklogger-role.sh
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -p ${BRICKLOGGER_DB_PORT:-5432} -U postgres"]
      interval: 5s
      retries: 24
    volumes:
      - db:/home/postgres/pgdata

  daemon:
    image: ghcr.io/cx1-aps/bricklogger:0.3.1
    command: ["bricklogger", "daemon", "run"]
    network_mode: host
    restart: unless-stopped
    stop_grace_period: 30s
    depends_on:
      db: { condition: service_healthy }
    environment:
      BRICKLOGGER_DB_PORT: ${BRICKLOGGER_DB_PORT:-5432}
      BRICKLOGGER_DB_NAME: ${BRICKLOGGER_DB_NAME:-bricklogger}
      BRICKLOGGER_DB_USER: ${BRICKLOGGER_DB_USER:-bricklogger}
      BRICKLOGGER_DB_PASSWORD: ${BRICKLOGGER_DB_PASSWORD:-}
      BRICKLOGGER_PLUGINS: ${BRICKLOGGER_PLUGINS:-}
    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/health/live')"]
      interval: 5s
      start_period: 5m
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

  web:
    image: ghcr.io/cx1-aps/bricklogger:0.3.1
    command: ["bricklogger", "serve"]
    network_mode: host
    restart: unless-stopped
    depends_on:
      daemon: { condition: service_healthy }
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

  mcp:
    image: ghcr.io/cx1-aps/bricklogger:0.3.1
    command: ["bricklogger", "mcp", "serve", "--http"]
    network_mode: host
    restart: unless-stopped
    depends_on:
      daemon: { condition: service_healthy }
    volumes:
      - config:/etc/bricklogger
      - data:/var/lib/bricklogger
      - plugins:/var/lib/bricklogger/plugins

configs:
  bricklogger-role:
    content: |
      # Run once, when the database's volume is empty: the role Bricklogger
      # logs in as, owner of its database and of the extension in it, so the
      # destination can update the extension itself. "$$" is a "$" for the shell.
      psql -v ON_ERROR_STOP=1 -U postgres -v db="$$BRICKLOGGER_DB_NAME" \
        -v user="$$BRICKLOGGER_DB_USER" -v password="$$BRICKLOGGER_DB_PASSWORD" <<'SQL'
      CREATE ROLE :"user" LOGIN PASSWORD :'password';
      CREATE DATABASE :"db" OWNER :"user" TEMPLATE template0;
      \connect :"db"
      SET ROLE :"user";
      CREATE EXTENSION IF NOT EXISTS timescaledb;
      SQL

volumes:
  config:
  data:
  plugins:
  db:
```

Beside it lies an **`.env`** file, which Compose reads by itself. Two values
are required, and Compose refuses to start without them rather than run with a
password from an example:

```bash
# .env
POSTGRES_PASSWORD=...              # the database's admin; only db sees it
BRICKLOGGER_DB_PASSWORD=...        # the role Bricklogger logs in as
# optional, with their defaults
#BRICKLOGGER_DB_NAME=bricklogger
#BRICKLOGGER_DB_USER=bricklogger
#BRICKLOGGER_DB_PORT=5432
#BRICKLOGGER_PLUGINS="bricklogger-ibos"
```

Then:

```bash
docker compose up -d
```

starts the database, then the daemon once the database answers, then the web
interface and the MCP server once the daemon answers. The web interface is on
`http://127.0.0.1:8421`, where the sources are added and the model uploaded,
and the MCP server answers on 8422 with no [token](features/mcp.md#the-token)
as long as it is bound to localhost.

The admin password stays with `db`: the daemon gets only its own role's
password, and only through its environment. `POSTGRES_INITDB_ARGS` makes the
database ask for a password on loopback too, which the image would otherwise
trust: on the host's network, loopback is every process on the host. The daemon's command is written
out, although it is the image's default, because the
[first start](#the-first-start) is the work of the container that runs it.

The compose file needs Compose 2.23 or newer, for the inline `configs`.

!!! note "One volume for the plugins"

    The plugin volume is mounted **inside** the data directory, at
    `/var/lib/bricklogger/plugins`, and is a volume of its own so it can be
    emptied and rebuilt without touching the runtime state, the model versions
    and the working graph beside it. See [plugins](#plugins) below.

### Without the database

The database is a service like the others, and Compose starts every service in
the file, so a site that does not want it removes it: the `db` service, its
volume and the daemon's `depends_on`. What the daemon then writes to follows
from `BRICKLOGGER_DB_PASSWORD`:

- **Unset**, the first start writes the commented example in
  `destinations.yaml`, as for any other destination, and the daemon starts
  with none.
- **Set**, with `BRICKLOGGER_DB_HOST` naming the database's host in the
  daemon's `environment`, the first start writes a working TimescaleDB
  destination against it. That database is prepared by whoever looks after
  it, as [getting started](getting-started.md#prepare-the-database) shows;
  the container installs nothing and asks for no admin login.

## Bind mounts, and the user the container runs as

The compose file uses **named volumes**, and that is the path with the fewest
surprises: Docker fills a fresh named volume from the image, ownership
included, so Bricklogger's three directories belong to the container's user
from the first start, and the database's to its own.

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
it writes to are mounted from the host. Set it on the three Bricklogger
services, so they write as the same user; the database's container keeps its
own user and its named volume.

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

Three consequences are worth knowing:

- **The ports are the host's.** 8420, 8421, 8422 and the database's 5432 are
  bound directly, and `ports:` in the compose file does nothing. What the
  outside world can reach is decided by `api.host`, `web.host` and `mcp.host`
  in `daemon.yaml`, which default to `127.0.0.1` — the host itself and
  nothing else — and the database listens on `127.0.0.1` alone.
- **The four processes share loopback**, so the daemon reaches the database
  on `localhost`, and the web interface and the MCP server reach the daemon's
  API on `127.0.0.1:8420` with no token, exactly as on a machine where
  everything is installed side by side.
- **A host that runs PostgreSQL already** has 5432 taken. Set
  `BRICKLOGGER_DB_PORT` in `.env` to a free port; the database listens on it
  and the destination is written with it.

An installation with no BACnet — one that reads a cloud service through a
plugin, say — can
run on an ordinary bridge network instead: drop `network_mode: host`, publish
the ports that should be reachable, set `api.host` to `0.0.0.0` with an
[`api.token`](features/configuration.md#daemonyaml), which the configuration
then requires, point the other two at the daemon with
`--api http://daemon:8420`, drop `listen_addresses` from the database's
command and give the daemon `BRICKLOGGER_DB_HOST: db`.

## The first start

The first `docker compose up -d` finds three empty volumes and fills them,
each once:

1. **The database.** `db` finds its volume empty, creates the cluster with
   `POSTGRES_PASSWORD` for the admin `postgres`, and — through the script in
   the compose file — the role `bricklogger` with `BRICKLOGGER_DB_PASSWORD`
   and the database `bricklogger` with the `timescaledb` extension, both owned
   by the role. It listens when that is done, and the daemon waits for it.
2. **The configuration.** The daemon's container finds the configuration
   volume empty and writes the four files that
   [`init --non-interactive`](features/cli.md#init) writes. Its environment
   carries the database's variables, so `destinations.yaml` gets a working
   destination and not an example:

    ```yaml
    tsdb:
      type: timescaledb
      dsn: "postgres://bricklogger@localhost:5432/bricklogger"
      password: ${BRICKLOGGER_DB_PASSWORD}
    ```

    The password stays in the environment, and the YAML names it. The example
    source in `sources.yaml` is **commented out**: it names another building's
    BACnet address, and the daemon refuses a configuration it cannot resolve —
    a container that would not start, on a restart policy that would try again
    and again.
3. **The plugins.** The plugin volume is empty, so the packages in
   `BRICKLOGGER_PLUGINS` are installed into it, as
   [`plugins add`](#plugins) would, and recorded in its manifest. A package
   that cannot be installed is logged and left out, and the daemon starts
   anyway.

The daemon then starts, the destination creates its tables, and `status`
reports `idle` until there is a source and a model. The web interface and the
MCP server start once the daemon answers. The first start is **the work of the
container that runs `daemon run`**: the other two wait for it, so two
containers never write the same files.

Each of the three happens **once**. A volume that is not empty is left as it
is, so the next `up`, an upgrade or a changed `.env` touches nothing: a new
`BRICKLOGGER_DB_PASSWORD` does not change the role's password, which is
changed in the database, and a new `BRICKLOGGER_PLUGINS` installs nothing,
which is what [`plugins add`](#plugins) is for.

From there, the way on is the web interface, which adds sources on the
configuration screen and installs plugins from the
[catalogue](features/plugins.md#the-catalogue), or the guided
[`init`](features/cli.md#init) in the running container:

```bash
docker compose exec -it daemon bricklogger init
docker compose restart daemon
```

It asks about each file that exists: replace is the default for
`sources.yaml`, which still holds the commented example, and skip for
`destinations.yaml`, which no longer does. The database step installs nothing
and asks for no admin in a container; it writes the destination from the
database variables when it is replaced. `sources add` and `destinations add`
do the same one instance at a time:

```bash
docker compose exec -it daemon bricklogger sources add bacnet_main --type bacnet-ip
docker compose exec daemon bricklogger validate
```

Secrets of the instances go where they always go: the
[`env` file](features/configuration.md#the-env-file) in the configuration
volume, with mode `600`, which `init`, `sources add` and `destinations add`
write themselves when they ask for a secret without echo. The two passwords
in `.env` are the exception, because the database's container needs them
before any configuration exists; keep `.env` readable by its owner alone.

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
installed plugins when it is done, and it restarts nothing. Two things are its
own:

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
[Plugins screen](features/web.md#screens), with its
[catalogue](features/plugins.md#the-catalogue), installs into it as well, and
`docker compose restart daemon` is the restart in both cases.

**The plugins of a new installation** can be named before it exists, in
`BRICKLOGGER_PLUGINS` in `.env`: packages written as for `plugins add`,
separated by spaces, or one per line in double quotes when a package is
written as `name @ url`, which holds a space itself:

```bash
BRICKLOGGER_PLUGINS="bricklogger-ibos bricklogger-homeassistant>=0.2,<0.3"
```

They are installed at the [first start](#the-first-start), when the plugin
volume is empty, and from then on the manifest is what counts. The variable
is read **only then**: changing it later installs and removes nothing, so two
containers of one image never differ because of what their environment said
at a start.

A plugin installed from a wheel or another path is recorded as that path,
since that is what was asked for. Keep it reachable from the container, or add
the plugin again from an index after an upgrade.

`plugins remove TYPE` uninstalls from the volume, with
[what only that plugin needed](features/plugins.md#removing-a-plugin), and
drops the line from the manifest. The built-in types live in the image and
cannot be removed, as everywhere else.

## Upgrading

Name the new tag in the compose file, then:

```bash
docker compose pull
docker compose up -d
```

The volumes are untouched, so the configuration, the data, the database **and
the plugins** survive. A plugin in the volume was installed against the Python and
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

The database's image is named by its **major version alone**, `pg18`, so a
pull brings PostgreSQL's patch releases and the newest TimescaleDB without
waiting for a release of Bricklogger:

```bash
docker compose pull db
docker compose up -d
```

The extension in the database stays at its version until it is updated, and
the destination does that at its next start, as the
[destinations page](features/destinations.md#timescaledb) describes.

A new **major version of PostgreSQL** is never a pull: the data directory of
one major version cannot be opened by the next. The compose file keeps
`pg18`, and moving to another is a dump and a restore, or `pg_upgrade`, done
deliberately; a release of Bricklogger that names a new major version says so
in its notes and leaves the move to the operator.

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

The daemon, by hand, writing to a database that already exists:

```bash
docker run -d --name bricklogger \
  --network host \
  -e BRICKLOGGER_DB_HOST=localhost -e BRICKLOGGER_DB_PASSWORD \
  --restart unless-stopped \
  -v bricklogger-config:/etc/bricklogger \
  -v bricklogger-data:/var/lib/bricklogger \
  -v bricklogger-plugins:/var/lib/bricklogger/plugins \
  ghcr.io/cx1-aps/bricklogger:0.3.1
```

`-e BRICKLOGGER_DB_PASSWORD` passes the value from the shell, so it is not on
the command line. The web interface and the MCP server are two more
`docker run` lines with the same volumes and their own command, and the
database one more with the image, the command, the environment, the script
and the volume of the compose file's `db`.

## When something is wrong

- **`docker compose logs daemon`** is the first place: a configuration
  the daemon rejects names the file and the key there.
- **`docker compose up` stops before it starts anything** with
  `set POSTGRES_PASSWORD in .env`: the two passwords are missing from `.env`,
  or `.env` is not beside the compose file.
- **The daemon never starts, waiting for `db`.** `docker compose logs db` says
  why; a port already in use means a PostgreSQL on the host, and
  `BRICKLOGGER_DB_PORT` in `.env` moves the database to another.
- **The destination is refused its login after `.env` was changed.** The role
  got its password at the database's first start, and a new
  `BRICKLOGGER_DB_PASSWORD` does not reach it. Set it in the database too, then
  restart the daemon:

    ```bash
    docker compose exec db psql -U postgres \
      -c "ALTER ROLE bricklogger PASSWORD 'the new password'"
    ```

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
