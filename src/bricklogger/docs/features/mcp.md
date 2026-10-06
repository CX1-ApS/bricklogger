# MCP

The MCP server is the third door into the same functionality as the CLI and
the web interface, made for an AI assistant rather than for a person. It is
served by **`bricklogger mcp serve`**, a process of its own that talks to the
daemon's [HTTP API](api.md) exactly as the CLI does, and it speaks the
[Model Context Protocol](https://modelcontextprotocol.io/), so any assistant
that speaks MCP can read the logger's configuration, explain its options and
change it on the operator's behalf. Its purpose is **configuration**: it knows
every setting of the four files and of every installed plugin, it validates
before it writes, and it can look at status, points and the model to see what
a change did. It adds no functionality of its own. Every tool maps to one
operation the CLI has too, so nothing can be done through an assistant that
could not be done by hand.

## How it is started

- **A process of its own.** `bricklogger mcp serve` runs the server in the
  foreground, as `serve` runs the web interface. It finds the config directory
  the way every command does — `--config-dir`, then `BRICKLOGGER_CONFIG_DIR`,
  then `/etc/bricklogger` when it exists, then `~/.config/bricklogger` — and
  the daemon through `daemon.yaml`; `--api URL` and `--token` point it at a
  daemon on another machine, as for the CLI.
- **Two transports.** Without a flag it speaks **stdio**: the MCP client
  launches the process and talks over its standard input and output, which is
  how a local assistant connects, and nothing needs a port or a token. With
  `--http` it listens as a **streamable HTTP** server at the path `/mcp`, for
  an assistant on another machine; `mcp.host` and `mcp.port` in
  [`daemon.yaml`](configuration.md#daemonyaml) say where, and `--host` and
  `--port` override them.
- **Without a daemon.** The server starts whether or not the daemon runs,
  exactly as the CLI works without one: configuration is read and written on
  the files with the same validation, the catalogue comes from the installed
  plugins, protocol tools run in-process, and the tools that need the daemon —
  status, warnings, points, the model tree and queries — say so plainly. That
  is deliberate: the first configuration of a new machine is made before the
  daemon can start.
- **Registering it.** With Claude Code on the machine itself:
  `claude mcp add --transport stdio bricklogger -- bricklogger mcp serve`.
  From another machine over ssh, the command in the client's configuration is
  `ssh <host> bricklogger mcp serve`; from Windows to a logger under WSL it is
  `wsl.exe -- bricklogger mcp serve`. Over HTTP the whole line is what
  [`bricklogger mcp auth`](#the-token) prints, token and all.
  [Getting started](../getting-started.md#connect-an-assistant) shows the same
  steps.
- **A liveness answer.** Over HTTP the server answers `GET /health/live` with
  `200` while it runs, as `serve` does, and that answer is the third line of
  [`status`](cli.md#status-and-points) — `mcp: running` — so an operator sees
  whether the server is up without going to the client. An installation that
  only speaks stdio has nothing answering there, and the line says as much.
- **A unit of its own.** The install script writes `bricklogger-mcp.service`
  beside the daemon's and the web interface's units, left stopped, with
  `bricklogger mcp serve --http` as its command, so a machine that should
  offer MCP over HTTP enables it with `systemctl` — and `mcp auth generate`
  restarts that unit when it replaces the token.

## Access

Over stdio there is nothing to protect: the process runs as the user who
launched it, with that user's rights on the config directory, exactly like the
CLI. Over HTTP the server binds to **localhost by default**, on port 8422, and
is guarded by one token and no user accounts. **A token that is set is
required**, wherever the server is bound: every request to `/mcp` carries it
as `Authorization: Bearer <token>`, and a missing or wrong one gives `401`.
Only `GET /health/live` is free, so `status` can see whether the server runs.
Bound beyond localhost the token is not a choice — validation refuses the
configuration without `mcp.token` — and on localhost it is one, made by
generating a token. TLS is a reverse proxy's job, as for the API and the web
interface. The settings live in
[`daemon.yaml`](configuration.md#daemonyaml) under `mcp`, and the flags of
`bricklogger mcp serve` override the binding.

### The token

`bricklogger mcp auth generate` is that choice carried out in one command. It
makes a token of 32 random bytes, writes the value into the
[`env` file](configuration.md#the-env-file) as `BRICKLOGGER_MCP_TOKEN` — or as
the variable `mcp.token` already names — and the reference
`${BRICKLOGGER_MCP_TOKEN}` into `daemon.yaml` when that setting is empty, so
the token is a secret like every other: a reference in the file, the value
beside it in the `env` file, never in the API, the web interface or a
resource. Then it prints the token with the line that registers the server in
a client:

```
claude mcp add --transport http bricklogger http://127.0.0.1:8422/mcp \
  --header "Authorization: Bearer <token>"
```

That is the one place a secret is printed, and it is printed because a token
nobody can read is worth nothing. The token that was there stops working the
moment the server restarts, and `generate` restarts it: when the server
answers and `bricklogger-mcp.service` is active it restarts the unit and waits
until it answers again, and when the server was started by hand it says
plainly that the running one keeps the old token. `mcp auth show` prints the
token and the line again, for the next client, and `mcp auth status` reports
whether a token is set, which variable holds it, and whether the running
server accepts it — a rejection is a restart that never happened. The
commands are described with the rest of the CLI on
[its page](cli.md#mcp); because they write the `env` file they work on the
machine whose file it is, not through `--api`.

## What the assistant is told

An assistant is only as good as what it knows about the program, so the
server carries its knowledge with it, in three forms:

- **Instructions.** When a client connects it receives a short text about the
  configuration model: the four files and what each holds; that rules are
  evaluated top down, the first match wins and points no rule matches are not
  logged; that secrets stand as `${VAR}` and live in the `env` file; how
  durations, sizes and times of day are written; that a write is validated as
  a whole and refused as a whole; and that it must validate before it writes
  and never ask for a secret's value.
- **Schemas with descriptions.** Every setting of `daemon.yaml`, every key of
  a rule and every setting of every installed plugin carries a one-line
  description in its schema — the same text the CLI's `plugins <type>`, the
  `init` wizard and the web interface's forms show. The schemas are resources,
  so the assistant reads exactly which keys exist, which are required, what
  the defaults are and which are secrets.
- **The documentation.** This documentation ships inside the package and is
  served as resources, page by page, so the assistant reads the same text an
  operator reads — the configuration page, the sources and destinations, the
  rule selectors — instead of guessing.

## Resources

Resources are what the assistant reads; none of them changes anything. The
URIs begin with `bricklogger://`.

| Resource | Content |
|----------|---------|
| `bricklogger://config/{file}` | One of the four files as written, environment variables left as they stand |
| `bricklogger://schema/{file}` | The JSON Schema of `daemon` or `rules`; for `sources` and `destinations` the shape of an instance, with one alternative per installed type |
| `bricklogger://plugins` | The catalogue: the installed plugins with type, role, version, description and instances, and the error of a plugin that could not be loaded |
| `bricklogger://plugins/{type}` | One plugin's declaration — configuration schema, reference types, collection methods and tools — as the [API](api.md#plugins) gives it |
| `bricklogger://validation` | The validation result for the configuration as it stands |
| `bricklogger://docs` | The index of the documentation pages |
| `bricklogger://docs/{page}` | One page, e.g. `features/configuration`, as Markdown |

Secrets never appear: the files are given as written, with `${VAR}` in place,
and the `env` file is no resource and no tool reads it.

## Tools

Tools are what the assistant does. Every tool maps to one operation of the
[API](api.md) and one command of the [CLI](cli.md), so an assistant can do
nothing an operator could not. Each tool declares whether it only reads,
whether it can remove something, and whether it reaches out onto a network,
so a client can ask the operator before it runs; the reading tools need no
such confirmation.

**Reading**

| Tool | Does | CLI |
|------|------|-----|
| `get_config` | One of the four files, or all four, as written, and the directory they came from | `… config show` |
| `validate_config` | Validates the configuration as it stands, or a proposed set of files given as text — the dry run before a write | `validate` |
| `list_plugins` | The catalogue | `plugins` |
| `describe_plugin` | One plugin's declaration with its configuration schema | `plugins <type>` |
| `get_schema` | The JSON Schema of one file, as the resource gives it | — |
| `read_docs` | A documentation page, or the list of pages | — |
| `get_status` | Whether the daemon, the web interface and the MCP server run, and the daemon summary when it answers | `status` |
| `list_warnings` | The warning list | `status warnings` |
| `list_points` | The paged points view with its filters | `points` |
| `model_tree` | The active model as a hierarchy with the explorer's filters — the way to find the URIs a rule's selector needs | `model tree` |
| `query` | A read-only SPARQL query against the working graph | `query` |
| `run_tool` | One of an instance's protocol tools — discover a device, list its objects, resolve a point — through the daemon, or in-process without one | `sources NAME <tool>` |

**Writing**

| Tool | Does | CLI |
|------|------|-----|
| `init_config` | Writes the four files with commented examples into an empty config directory; refuses to overwrite | `init --non-interactive` |
| `set_config` | Replaces one file with the YAML text given, so comments and layout are the assistant's to keep | `… config edit` |
| `add_instance` | Writes a new source or destination from settings the plugin's schema validates, spliced into the file so comments and the other instances stay | `sources add` |
| `edit_instance` | Changes the settings given and leaves the rest as they are | `sources edit` |
| `remove_instance` | Removes an instance from its file | `sources remove` |
| `set_rules` | Replaces the rule set with a list of rules validated by the rule schema, written as a fresh file; `set_config` is the way to keep comments | `rules config edit` |
| `reload_daemon` | Reloads the configuration, for edits made outside the API | `daemon reload` |

**Writes behave as they do everywhere else.** The whole configuration is
validated with the change applied and refused as a whole when it does not
hold, the file is written atomically, and the write goes through the API
whenever a daemon answers, so the change takes effect at once. A refused
write answers with the errors — file, instance or rule, key and message — so
the assistant can correct and try again, and `validate_config` with the
proposed text is the way to check first. Nothing here uploads a model, starts
or stops an instance, stops the daemon, sends mail, or installs, upgrades or
removes a [plugin](plugins.md#installing-a-plugin): those remain the CLI's and the web
interface's, on purpose, so that an assistant configures and observes but does
not operate the plant or change what is installed on the machine.

**Secrets are references, never values.** A setting the plugin marks as a
secret is taken only as `${VAR}`; a literal value is refused, as
[`sources add`](cli.md#sources-and-destinations) refuses it. The assistant is
told never to ask for the value and to tell the operator to put it in the
[`env` file](configuration.md#the-env-file) — `bricklogger sources edit NAME`
asks for it without echo — after which validation reports whether the variable
is set, and nothing more. No tool reads or writes the `env` file, so a secret
never passes through the assistant, its client or their logs.

## Prompts

Prompts are starting points the operator picks in the client, not something
the assistant calls. Three ship with the server:

| Prompt | Starts |
|--------|--------|
| `setup` | The first configuration of a new machine: which plugins are installed, which instances to write, the rule set, validation and what to do next |
| `new_instance` | Adding a source or destination of a given type: the schema's settings one by one, the secret's variable named, the write and its validation |
| `troubleshoot` | Nothing arrives: status, warnings, the points view and the protocol tools, in the order [Getting started](../getting-started.md#if-nothing-arrives) gives them |

## Security

The server has the operator's powers and no more: over stdio it runs as the
user who started it, over HTTP as the user of its unit, with the same rights
on the config directory the CLI has. Three things bound what an assistant can
do with them. **It cannot reach what is not there:** no tool writes to a
device — the BACnet/IP source's [read-only guarantee](sources.md#read-only)
stands, and the protocol tools are the declared ones — and no tool uploads
models, controls instances or stops the daemon. **The client asks before it
acts:** every tool that writes or reaches a network says so in its
declaration, and MCP clients ask the operator before running such a tool
unless told not to. **Secrets stay out:** the `env` file is unreachable, the
files are shown as written, and a secret's value is refused as input — the
server's own token included, which no tool reads, writes or generates, so it
stays between the operator and `bricklogger mcp auth`. What
the server cannot vouch for is what the assistant reads: point names, labels
in a model and comments in a configuration file come from the building and
from other people, and an assistant that follows instructions found there is
a problem for the client to solve, not for the server. This page says so
where an operator will see it.
