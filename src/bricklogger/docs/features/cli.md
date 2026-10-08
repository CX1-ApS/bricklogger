# CLI

The command is `bricklogger`. It is the control plane and the primary user
interface: every command maps to one operation on the daemon's API, which is
what gives the web interface its parity. The exceptions are running the daemon
itself, serving the web interface and the MCP server, and the offline
fallbacks: configuration, model upload and protocol tools also work without a
running daemon.

## Conventions

- **Output.** Human-readable tables by default. `--json` emits the API's JSON
  unchanged, for scripts. Errors go to stderr with a non-zero exit code.
- **Finding the daemon.** The CLI reads `daemon.yaml` in the config directory
  and talks to the API binding found there, with the token from the same file
  when one is set. `--api URL` overrides the binding for a deliberately
  exposed daemon, and the token then comes from `--token` or the environment
  variable `BRICKLOGGER_API_TOKEN`. The endpoint behind every command is
  listed in the [API reference](api.md).
- **Config directory.** `--config-dir PATH`, then the environment variable
  `BRICKLOGGER_CONFIG_DIR`, then `~/.config/bricklogger`, as defined in the
  [configuration document](configuration.md#location). `status` and every
  `config` view name the directory they read.
- **Secrets.** The [`env` file](configuration.md#the-env-file) in the config
  directory is read before the configuration is interpolated, so no command
  needs exported variables; a variable set in the shell wins over the file.
- **Without a daemon.** `status` says plainly that the daemon is not running.
  Commands that need it — points, query, `model tree`, start and stop of an
  instance — say so and fail. Configuration commands work directly on the files with the same
  validation; `model upload` and `model activate` validate the model and mark
  the version active in the data directory, and the daemon builds the working
  graph for it at start; protocol tools run in-process.
- **Writes through the API take effect immediately**, because they are
  validated. `daemon reload` exists for edits made outside the API.
- **A group without a subcommand shows its help.** `bricklogger sources`
  lists what can be done with sources; nothing is done until a subcommand is
  named.

## Command tree

```
bricklogger init [--non-interactive]
bricklogger validate
bricklogger status [warnings [clear [CODE [SUBJECT]]]]
bricklogger points [--instance NAME] [--outcome STATE] [--warning CODE] [--class CLASS]
bricklogger daemon run|start|stop|restart|reload|config
bricklogger services install|uninstall [--web] [--mcp]
bricklogger services status
bricklogger sources status [--state STATE]
bricklogger sources add NAME --type TYPE [settings]
bricklogger sources edit NAME [settings]
bricklogger sources remove NAME
bricklogger sources start|stop|restart NAME
bricklogger sources config show|edit
bricklogger sources NAME show|<tool> [params]
bricklogger destinations …                (the same commands)
bricklogger rules config show|edit
bricklogger notify test|status
bricklogger plugins [<type>]
bricklogger plugins add PACKAGE...
bricklogger plugins remove TYPE [--force]
bricklogger update status
bricklogger update all|core|<type> [--no-restart]
bricklogger model upload|list|activate|diff|export|tree
bricklogger query <sparql | @file>
bricklogger serve [--host HOST] [--port PORT] [--api URL]
bricklogger mcp serve [--http] [--host HOST] [--port PORT] [--api URL]
bricklogger mcp auth status|show|generate
```

## `init`

`init` is the first command on a new machine. It writes the four files into
the config directory and refuses to overwrite a file that exists, so it can
never destroy a configuration.

Run in a terminal it is **a guided setup**: it names the config and data
directories it will use, then asks which sources and which destinations to
configure, choosing among the installed types. For each instance it asks for
a name and then for every setting of the type's configuration schema —
required settings first, optional ones with their default offered, so Enter
keeps it. A setting the plugin marks as a secret is asked for without echo and
written to the [`env` file](configuration.md#the-env-file) under a variable
named after the instance and the key, `TSDB_PASSWORD` for the `password` of
`tsdb`; the YAML gets `${TSDB_PASSWORD}`. `daemon.yaml` is written with its
defaults and the resolved `data_dir`, `rules.yaml` with the one rule that
accepts every point every five minutes, and the result is validated and
printed with what to do next. A new plugin brings its own questions, since
they come from its schema, exactly as `add` gets its flags from it.

Last, the guided setup **offers the services**: whether to set them up now,
and if so whether the web interface and the MCP server over HTTP should run
too. A yes runs [`services install`](#services) with the answers as its
flags; a no says that the same command does it later. The question is left
out where `services install` would refuse — without systemd, as root or in a
container — and so is the daemon, which is then started as the
[`daemon`](#daemon) commands describe.

`init --non-interactive` asks nothing and writes the four files with
commented examples, for scripts and for a machine set up by hand afterwards;
it sets up no services. Through `--api` only this form is available.

## `validate`

`validate` validates the whole config directory with the daemon's validation,
including plugin schemas and rule parameters. Warnings, such as accepted
points without any destination, are reported beside errors. It works on the
files in the config directory and gives the same answer as the daemon would;
with `--api` it goes through the daemon's API instead, for a daemon on another
machine.

## The `config` views

Every owner of a file has a `config` group — `daemon config`, `sources
config`, `destinations config` and `rules config` — with two commands.
`… config show` prints the file as written, with environment variables left as
they stand — secrets are never printed — and names the directory it read.
`… config edit` opens the file in `$EDITOR` on a copy, validates the whole
directory with the change applied, and writes atomically — through the API
when the daemon runs, so the change takes effect at once, directly to the file
otherwise. A file that does not exist is shown as such and edited as empty.
A type that is [not installed](plugins.md#a-type-that-is-not-installed) is an
error for an instance the change adds or changes, and only a warning for one
it leaves as it was.
Like every group, `… config` alone shows its help.

## `daemon`

| Command | Effect |
|---------|--------|
| `daemon run` | Runs the daemon in the foreground with logs on stdout — for systemd and Docker |
| `daemon start` | Starts the daemon as a background process with a PID file in the data directory; refuses if one is already running |
| `daemon stop` | Graceful stop through the API, falling back to the PID file and a signal; waits for exit within `stop_timeout` |
| `daemon restart` | `stop`, then `start` |
| `daemon reload` | Reloads the configuration: validated as a whole, rejected with an error on failure, the running configuration kept |
| `daemon config show\|edit` | `daemon.yaml`, shown or edited as [the `config` views](#the-config-views) do |

Under the [services](#services), start, stop and restart are done with
`systemctl --user`; the CLI's versions are for a machine without systemd,
such as a commissioning laptop.

`daemon start` waits until the API answers before it returns, and reports
the PID and the log file. It refuses when the PID file names a live process
or when a daemon already answers at the API binding, however it was started.
A daemon that exits during start is reported with the last lines of its log.

## `services`

The daemon, the web interface and the MCP server over HTTP run as
`systemd --user` units of the login that installed Bricklogger.

| Command | Effect |
|---------|--------|
| `services install [--web] [--mcp]` | Writes the daemon's unit, and the web interface's and the MCP server's when their flag is given, enables lingering, reloads systemd, and enables and starts what it wrote |
| `services uninstall [--web] [--mcp]` | Stops, disables and removes the units — all of them without a flag, the ones named with one — and reloads systemd; the configuration and the data stay |
| `services status` | For each of the three units: whether it is written, enabled and active, and whether lingering is on |

The units live in `~/.config/systemd/user`:

| Unit | Command |
|------|---------|
| `bricklogger.service` | `bricklogger daemon run` |
| `bricklogger-web.service` | `bricklogger serve` |
| `bricklogger-mcp.service` | `bricklogger mcp serve --http` |

They are written from templates in the package, with the absolute path of the
command and the config directory in use, so a service reads the same
directory as the shell that installed it. The web interface and the MCP server
start after the daemon. **`install` can be run again:** a flag adds its unit
and leaves the others as they are, and a unit that is already written is
rewritten only when the templates differ — and then restarted if it runs.
[`update`](#update) brings the units up to date the same way.

**Lingering** lets the login's services start when the machine boots and keep
running after a logout. `install` enables it with `loginctl enable-linger`;
where the distribution reserves that for root, it says so, prints
`sudo loginctl enable-linger <login>`, and writes and starts the units
anyway, so they run until the next logout. `uninstall` leaves lingering as it
is, since other services of the login may rely on it.

`install` refuses, and changes nothing, in four cases: run as root, since
Bricklogger is installed per login; where `systemctl --user` does not answer,
such as WSL without systemd, naming `daemon start` and `serve` instead; in a
container, naming the compose file; and while a daemon started by hand answers
at the API binding, naming `daemon stop`. Like every group, `services` alone
shows its help.

## `status` and `points`

`status` begins with three lines that are always answered, daemon or no
daemon — one per door into the logger:

```
daemon: running — pid 4711 since 2026-09-09T10:02:11Z at http://127.0.0.1:8420
web:    not running — nothing answers at http://127.0.0.1:8421
mcp:    running — pid 4713 since 2026-09-09T10:02:12Z at http://127.0.0.1:8422/mcp
```

The daemon is running when its API answers at the binding in `daemon.yaml`,
the web interface when `serve` answers at its own binding, and the MCP server
when [`mcp serve --http`](mcp.md#how-it-is-started) answers at the `mcp`
binding. Each of the three answers `GET /health/live` with its own process ID
and the time it started, so a line reads the same whichever way the process
was started — as a [service](#services), by hand or in a container. An
assistant that speaks stdio has no server to answer there, which the line
says — `not running — nothing answers at http://127.0.0.1:8422/mcp (stdio
needs no server)`. No line makes the command fail: a daemon that is not
running is an answer, not an error.

When the daemon answers, the daemon summary of the
[status tree](daemon.md#status) follows — version, health, the directories,
the active model and the point counts — with one line per configured instance
of either role beneath it, so a single command answers whether the logger is
doing its job. `status warnings` is the flat warning list, and
`status warnings clear` clears it by hand — all of it, the warnings with a
`CODE`, or the one with a `CODE` and a `SUBJECT` — as an acknowledgement; a
cleared warning returns when its condition is asserted again, as the
[warning list](daemon.md#status) explains. With `--json` the two probes and
the summary come as one document: `daemon`, `web` and `mcp`, each with
`running`, `url`, `pid` and `started_at`, and `status`, `null` when the
daemon does not answer. The detail per instance belongs to the
two roles and is described under
[`sources` and `destinations`](#sources-and-destinations).

`points` is the paged, filterable [points view](daemon.md#points). The table
prints the URI, the source instance, the method with its fallback, the
outcome, the last valid value with its time and the warnings; the name and the
Brick class are too wide to stand beside them in a terminal and are carried by
`--json`, which is the whole view. Filters: `--instance`,
`--outcome`, `--warning` and `--class`; the last two search, so
`--class Temperature` is enough, as the
[points view](daemon.md#points) describes.

## `sources` and `destinations`

The two roles have the same commands, each on the instances of its own file.
`NAME` is the instance's name, the key in the file; `TYPE` is the plugin type
written in `type`, e.g. `bacnet-ip`.

| Command | Effect |
|---------|--------|
| `sources status [--state STATE]` | The configured instances with their state and the detail of the [status tree](daemon.md#status) — devices and outcomes for a source, spool and writes for a destination — narrowed to one state |
| `sources add NAME --type TYPE [settings]` | Writes a new instance into the file; refuses a name that already exists. Without settings it asks for them, as `init` does |
| `sources edit NAME [settings]` | Changes the settings given and leaves the rest as they are. Without settings it walks through them all, each with its current value to keep |
| `sources remove NAME` | Removes the instance from the file |
| `sources start\|stop\|restart NAME` | Starts, stops or restarts the instance |
| `sources config show\|edit` | `sources.yaml`, shown or edited as [the `config` views](#the-config-views) do |
| `sources NAME` | The instance's own help: `show`, and one command per tool of its type |
| `sources NAME show` | The instance's state and its configuration as written |
| `sources NAME <tool> [params]` | Runs one of the type's protocol tools on the instance; `--help` lists its parameters. A tool whose result is a document takes `-o FILE` |

**The states** are the instance states of the status tree — `starting`,
`running`, `failed` and `stopped` — and one more that only these views know:
`unconfigured`, an installed type without a single instance. `--state` takes
one of the five. Without a daemon the table shows the configured instances from
the file, their state unknown, and says so.

**The table lists instances, not possibilities.** An installed type with no
instance is not a source, so it is left out: on a machine where one of the
installed plugins is never used, a row for it would say nothing for ever under
a heading that asks what the sources are doing. `--state unconfigured` asks
which types are installed and unused, and [`plugins`](#plugins) is the
catalogue proper.

**The settings of `add` and `edit` are flags generated from the plugin's
configuration schema**, as tool flags are generated from the declaration, so
a new plugin brings its own flags and nothing in the CLI has to know them;
[`plugins <type>`](#plugins) shows the schema, and a flag the schema does not
have is refused with the ones it does. Values are typed by the schema:
`--device-instance 1201` is written as a number. Every string setting also
takes `--<key>-env NAME`, which writes `${NAME}` into the YAML instead of the
value, so the value stays in the [`env` file](configuration.md#the-env-file).
A setting the plugin marks as a **secret** — `password` and `token` in the
built-in plugins — takes only that form: a literal value is refused, so a
secret cannot end up in the file by accident.

```bash
bricklogger destinations add tsdb --type timescaledb \
  --dsn "postgres://bricklogger@127.0.0.1:5432/brick" --password-env TSDB_PASSWORD
```

The variable has to carry a value already: an unset one makes the
configuration invalid, and the write is refused as a whole, so the secret goes
into the `env` file first — [`init`](#init) does both in one go.

**Without settings, `add` and `edit` ask.** `sources add NAME --type TYPE`
alone walks through the type's settings exactly as `init` does — required
first, optional with the default offered — and `sources edit NAME` alone walks
through them with the instance's current values offered instead, so Enter
keeps each one. A secret that already stands as `${VAR}` is shown by its
variable's name only; Enter keeps it, and a new value is written to the `env`
file under the same variable, never into the YAML. Through `--api`, against a
daemon on another machine, the settings are given as flags, because the
secrets belong in that machine's `env` file.

`add`, `edit` and `remove` write the file exactly as the
[`config` views](#the-config-views) do: the whole configuration is validated
with the change applied and rejected as a whole if it does not hold, the file
is written atomically, and the write goes through the API whenever a daemon
answers, so the change takes effect at once. An instance is spliced into the
file as text, so its comments and the other instances stay as they were.
Removing a running instance stops it as the new configuration is applied.

**The instance is the address.** Tools and start/stop act on one instance,
because the instance owns the connection, and the instance's type says which
tools it has — so `sources bacnet_main discover` needs no type and no flag. A
tool always runs on a configured instance, with or without a daemon: without
one it binds the instance's configuration in-process. `sources NAME` alone
lists what can be done with it — `show`, and the tools as commands, each with
its parameters in its own `--help` — so a name is a place to start.

**Reserved words win.** `status`, `add`, `edit`, `remove`, `start`, `stop`,
`restart` and `config` are the subcommands; an instance may not be named after
one of them, and validation refuses such a name.

**Tools** and their flags are generated from the plugin's declaration, so a
new plugin brings its tools with it and nothing in the CLI has to know them.
Parameters are given as `--name value` after the tool, named as the
declaration shows them. The result is printed as a table or key-value lines,
or unchanged with `--json`. A tool whose result is a
[document](../architecture.md#protocol-tools) — an export, such as the BACnet/IP source's `pointlist` — is written as JSON, to standard output or to a file
with `-o FILE`. Long-running tools declare their duration as a parameter and
return a complete result.

**Stopping an instance** is a graceful stop within `stop_timeout`: for a
source, subscriptions are cancelled and sockets closed and freed; for a
destination, the connection is closed while its spool keeps filling. The
instance shows as `stopped` in status and appears in the warning list as
`instance_stopped`, so a forgotten stop stays visible. The operator's intent
**persists** across `daemon reload` and daemon restarts until someone starts
the instance again. A deliberate stop is not a failure: health stays `ok`
while at least one source runs, and reports `idle` when every source instance
is stopped.

## `rules`

`rules config show` and `rules config edit` show and edit `rules.yaml` as
[the `config` views](#the-config-views) do. The rule set itself is defined on
the [daemon page](daemon.md#point-selection-selectors).

## `notify`

Mail to the administrator: what is sent and when is defined on the
[notifications page](notifications.md), and the settings live under
`notifications` in [`daemon.yaml`](configuration.md#notifications).

| Command | Effect |
|---------|--------|
| `notify test` | Sends a test mail to the configured recipients at once and prints what the server answered, the failure included |
| `notify status` | Whether notifications are on, the last mail with its time and recipients, the last error, when the next summary is due, and what waits in the open window |

`notify test` is the command for a commissioning visit: it proves that mail
leaves the machine through the customer's firewall before anyone waits for an
alarm that never comes. It sends on the configuration as written, whether or
not `enabled` is true, so a mail server can be tried before notifications are
switched on — which is the order the model requirement imposes anyway. Both
commands need a running daemon, because both ask it what it knows, and both
take `--json` like every other command. Like every group, `notify` alone shows
its help.

## `plugins`

The catalogue: what this installation can do, whichever role a plugin has.

| Command | Effect |
|---------|--------|
| `plugins` | Installed plugins: type, role, version; a plugin that could not be loaded shows `failed:` and the error where its description would be |
| `plugins <type>` | The plugin's declaration: reference types, vocabulary, collection methods with their parameters, configuration schema and tools; for a plugin that could not be loaded, the error |
| `plugins add PACKAGE...` | Installs one or more packages into the uv tool environment the command runs from, holding the other installed plugins at their versions so a package that cannot live with them is refused, and prints the catalogue as it now is. `PACKAGE` is anything uv installs: a name, `name==version`, a wheel on disk or a git URL |
| `plugins remove TYPE [--force]` | Uninstalls the distribution that provides the type, with every other type it provides and the libraries only it needed; refuses while an instance of the type is configured, unless `--force` |

The first two read the installed declarations themselves when no daemon
answers, so they answer before anything is configured — which is where a new
installation starts, since the configuration schema shown here is what
[`init`](#init) asks about and [`sources add`](#sources-and-destinations)
turns into flags. `add` and `remove` change the environment, not the
configuration, and the daemon reads its plugins when it starts, so both end
by saying that it must be restarted; they restart nothing themselves, and
outside an installation made with `uv tool install` they print the uv command
to run instead. The [plugins page](plugins.md#installing-a-plugin) has the whole
procedure, from the install to what happens to a plugin that cannot be
loaded.

## `update`

`update` upgrades the installation from PyPI with uv, in the uv tool
environment the command runs from, as [`plugins add`](#plugins) does. Alone it
shows its help, like every group.

| Command | Effect |
|---------|--------|
| `update status` | Installs nothing: for Bricklogger and every installed plugin, the version installed, the newest release, and the newest release that fits the rest; when the last two differ, what holds it back |
| `update core` | Upgrades Bricklogger alone, the plugins held at their versions, to the newest release they allow; a newer one they exclude is named, with the plugin that excludes it and `update all` as the way past |
| `update <type>` | Upgrades the plugin that provides the type — and every other type the same distribution provides, which it names — to its newest release that fits the installed Bricklogger and the other plugins; a newer one that needs more is named the same way |
| `update all` | Upgrades Bricklogger and every plugin to the newest releases that fit together |

The three that install go through the same steps:

1. **The versions are noted**, of Bricklogger and of every plugin, before
   anything is installed.
2. **The new set is installed and validated** by the new version's own
   [`validate`](#validate), in a process of its own, since the one running is
   still the old code. Warnings are reported and stop nothing, so a configured
   type that is not installed is reported
   [as everywhere](plugins.md#a-type-that-is-not-installed). Errors, such as a
   setting the new version no longer accepts, put the noted versions back,
   are printed, and restart nothing: the machine stays as it was. A version
   installed from a wheel on disk is on no index and cannot be fetched
   again; then `update` says so, and the new version stays installed while
   what runs is still the previous one, until the configuration is made to
   fit or the previous wheel is installed again.
3. **The units are brought up to date.** The units are written from
   templates in the package, by [`services install`](#services) and by
   `update` alike; when the new version's differ from those installed,
   `update` rewrites the ones that are written and reloads systemd.
4. **What runs is restarted.** Of `bricklogger`, `bricklogger-web` and
   `bricklogger-mcp`, the units that are active are restarted in that order,
   and `update` waits until each answers again. A daemon or a server started
   by hand is not restarted; `update` names the command that restarts it.
   `--no-restart` leaves every process alone. A unit that does not answer is
   reported with where its log is, and nothing is rolled back then, because
   the destination may already have migrated its schema to the new version.

Like `plugins add`, `update` works on an installation made with
`uv tool install` and prints the uv command to run anywhere else. It needs no
rights beyond the login's own, and `update status` changes nothing.

**In a container** Bricklogger comes with the image. `update core` refuses and
names `docker compose pull` and `docker compose up -d`; `update <type>` and
`update all` upgrade the plugins in the [plugin volume](../docker.md#plugins)
and its manifest, held to the image's version of Bricklogger, and `all` says
that the rest comes with a new image. Nothing is restarted in a container;
`update` names `docker compose restart`. The newest release that
`update status` shows for Bricklogger is also the newest image's tag.

**Knowing there is one.** The daemon looks for newer releases once a day,
unless [`updates.check`](configuration.md#daemonyaml) is off.
What it finds stands as a line beneath the daemon summary in [`status`](#status-and-points)
and in the daily [summary mail](notifications.md#what-is-sent). A lookup that
cannot reach PyPI is silent: it is not a warning. `update status` looks
whenever it is run, whatever the setting.

## `model`

| Command | Effect |
|---------|--------|
| `model upload <file> [--no-activate]` | Validates the model, stores it as a new version and activates it. The CLI follows the daemon's [job](api.md#models-and-jobs) and shows its steps; the result shows the diff to the previously active version. `--no-activate` stores without activating |
| `model list` | Versions with upload time, activation history and which one is active |
| `model activate <version>` | Activates an earlier version with upload semantics: validate, atomic swap, re-evaluate — as a job, like upload |
| `model diff <a> <b>` | Points added, removed or changed between two versions, computed on the uploaded models |
| `model export [--version V] [--inferred] [--values] [--timeseries [--destination NAME]] [-o FILE]` | The model as uploaded by default; `--inferred` adds the inferred graph, `--values` the value overlay, `--timeseries` the time-series references of the destination that stores the model, with `--destination` naming one when several do |
| `model tree [--kind K] [--class C] [--finding F] [--warning W] [--outcome O] [--instance NAME] [--search TEXT] [--root URI] [--depth N] [--sort name\|class\|count]` | The active model as an indented hierarchy, built by the rules of the [model explorer](web.md#model-explorer): per line the URI, the class, the name where it differs, the [findings](daemon.md#model-findings) in brackets, a point's outcome and instance, and its warnings after `!`. `--root` on a [grouping](web.md#model-explorer) lists what it gathers instead, each member with its own subtree. `--json` gives the [entity document](api.md#entities) unchanged |

The model commands go through the API whenever a daemon answers, and
`upload` and `activate` then show the job's steps as they happen. Without a
daemon they work on the data directory, as described above — all but
`model tree`, which reads the working graph and therefore needs the daemon,
as `points` and `query` do.

## `query`

`query <sparql>` runs a read-only SPARQL query against the working graph —
model, ontology, inferred graph and value overlay — and prints the result as a
table, with URIs shortened by the model's prefixes, or raw with `--json` (the
SPARQL results JSON) or `--format csv|tsv|xml|turtle`. `@file` reads the
query from a file. The same guidance as for
[rules](daemon.md#sparql-as-an-escape-hatch) applies: subclasses and inverse
relations are materialised, transitive part hierarchies are written with
property paths.

## `serve`

`serve` runs the [web interface](web.md): a web server in the foreground that
serves browsers on a port of its own and talks to the daemon's API like every
other command. It reads `web.host`, `web.port` and `web.password` from
`daemon.yaml`; `--host` and `--port` override the binding, and `--api URL`
points it at a daemon on another machine. Like `daemon run`, it logs to stdout
and is meant to run under a service manager or in a container. It starts
without a daemon and shows plainly when the daemon cannot be reached, and it
answers `GET /health/live` with `200`, its process ID and the time it started
while it runs, which is what `status` asks it.

## `mcp`

`mcp` is the group around the [MCP server](mcp.md), through which an AI
assistant reads and changes the configuration and looks at status, points and
the model, over the daemon's API like every other command and on the files
when no daemon answers. Like every group, `mcp` alone shows its help.

| Command | Effect |
|---------|--------|
| `mcp serve [--http]` | Runs the server in the foreground: stdio without the flag, a streamable HTTP server with it |
| `mcp auth status` | Whether a token is set, where its value lives, and whether a running server accepts it |
| `mcp auth show` | Prints the token and the line that registers the server in a client |
| `mcp auth generate` | Writes a new token, prints it, and restarts a running server so it takes effect |

`mcp serve` without a flag speaks stdio, for a client that launches the
process itself; `--http` makes it listen as a streamable HTTP server on
`mcp.host` and `mcp.port` from `daemon.yaml`, which `--host` and `--port`
override, and the root option `--api URL` points it at a daemon on another
machine. Over HTTP it answers `GET /health/live` with `200`, its process ID
and the time it started while it runs, which is what `status` asks it. What it exposes to the assistant — resources,
tools and prompts — is defined on the same page.

`mcp auth` is the token an HTTP client needs, and only that: over stdio
nothing is bound and nothing is asked for. `generate` makes a token of 32
random bytes, writes the value into the [`env` file](configuration.md#the-env-file)
as `BRICKLOGGER_MCP_TOKEN` — or as the variable `mcp.token` already names —
and puts the reference `${BRICKLOGGER_MCP_TOKEN}` into `daemon.yaml` when that
setting is empty, so the secret stands as a reference like every other. It
then prints the token and the registration line, because a token is of no use
until it is in the client; that is the one place a secret is printed, and it
is printed because it has nowhere else to go. Whatever token was there stops
working. Since it writes the `env` file, `mcp auth generate` works on the
machine whose file it is and refuses with `--api`, as
[`sources edit`](#sources-and-destinations) refuses a typed secret.

A server that is already running keeps the old token until it is restarted.
`generate` does that itself when the server answers and
`bricklogger-mcp.service` is active, and waits until it answers again; when
the server was started by hand it says so instead, and `--no-restart` leaves
it alone in either case. `mcp auth status` answers the same question whenever
it is asked: it puts the token to a running server and reports whether it is
accepted or rejected, and a rejection means a restart is missing. `mcp auth
show` prints the token and the registration line again, for the next client.
