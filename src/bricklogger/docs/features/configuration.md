# Configuration

All configuration lives in a config directory with four YAML files, which
together are the source of truth. Write paths, reload semantics and secrets
are described in the
[architecture's configuration section](../architecture.md#configuration);
this page defines the schema of the files.

Each file's content is **directly the section's content** — the file name
carries the meaning, and there is no wrapping top-level key.

## Location

Configuration and data live where the user that runs Bricklogger can write:

| Runs as | Configuration | Data |
|---------|---------------|------|
| `root` or the service user | `/etc/bricklogger` | `/var/lib/bricklogger` |
| an ordinary login | `~/.config/bricklogger` | `~/.local/share/bricklogger` |

The config directory is resolved in this order: the CLI flag
**`--config-dir`**, the environment variable **`BRICKLOGGER_CONFIG_DIR`**,
**`/etc/bricklogger`** when that directory exists, and
**`~/.config/bricklogger`** otherwise. A machine installed as a service thus
needs neither flag nor variable, and neither does a run in a home directory on
a machine without a system installation. `status` and every `config` view
name the directory in use, so a shell that reads a different one than the
daemon does is visible at once.

Bricklogger writes its own configuration: `init`, the `config edit` views,
`add`, `edit` and `remove` of an instance and the web interface's editor write
a temporary file in the config directory and rename it into place. **The user the daemon runs as must therefore be able to
write the directory itself**, not only the files in it — a config directory
owned by `root` and merely readable by the service leaves the daemon running,
but refuses every edit.

## The files

| File | Content |
|------|---------|
| `daemon.yaml` | The daemon's own settings (API binding, data directory, logging, notifications) and the bindings of the web interface and the MCP server |
| `sources.yaml` | Source instances, named as a map |
| `destinations.yaml` | Destination instances, named as a map |
| `rules.yaml` | The rule set for point selection, ordered list |

A file that does not exist is read as **empty**: `daemon.yaml` then gives
every default, empty `sources.yaml` and `destinations.yaml` give no instances,
and an empty `rules.yaml` accepts nothing. Validation **warns** — it does not
fail — when the rule set accepts points but no destination is configured.
`bricklogger init` writes all four files, guided or with commented examples.

### The `env` file

Secrets live in an `env` file in the config directory, one `NAME=value` per
line, and are interpolated into the YAML as `${NAME}`:

```
TSDB_PASSWORD=...
SMTP_PASSWORD=...
```

The daemon and the CLI both read it, so every command works in any shell
without the variables being exported first, and a value already in the
process environment wins over the file — a single command can thus be run
with another token. Blank lines and lines beginning with `#` are ignored, and
a value may be quoted.

The file is not one of the four: it is not reachable through the API or the
web interface, no `config` view prints it, and the install script creates
it empty with mode `600`. Under a service manager it is read by the daemon
itself, so no `EnvironmentFile` is needed.

## `daemon.yaml`

```yaml
api:
  host: 127.0.0.1                  # default: localhost only
  port: 8420
  token: ${BRICKLOGGER_API_TOKEN}  # required when host is not loopback
data_dir: /var/lib/bricklogger     # default; see Location
stop_timeout: 10s                  # default
log:
  level: info                      # default
  format: text                     # default; or json
  file: /var/lib/bricklogger/bricklogger.log  # default: <data_dir>/bricklogger.log
  max_size: 10MB                   # default
  keep: 5                          # default
web:
  host: 127.0.0.1                  # default: localhost only
  port: 8421
  password: ${BRICKLOGGER_WEB_PASSWORD}  # required when host is not loopback
mcp:
  host: 127.0.0.1                  # default: localhost only
  port: 8422
  token: ${BRICKLOGGER_MCP_TOKEN}  # written by `bricklogger mcp auth generate`
notifications:
  enabled: false                   # default; can only be set with an active model
  smtp:
    host: smtp.example.com
    port: 587                      # default
    security: starttls             # default; or tls, or none
    username: bricklogger@example.com    # optional
    password: ${SMTP_PASSWORD}     # optional
  from: bricklogger@example.com
  to:
    - drift@example.com
  window: 2m                       # default
  min_interval: 15m                # default
  digest: "07:00"                  # default; the machine's local time
```

- `api.host`/`api.port`: where the HTTP API binds. The default is localhost;
  exposing it externally is a deliberate choice.
- `api.token`: required when `api.host` is not a loopback address — validation
  refuses the configuration otherwise — and set through an environment
  variable like every secret. Clients send it as a bearer token, as the
  [API reference](api.md#conventions) describes.
- `data_dir`: the data directory with runtime state (SQLite), the model
  versions and the working graph. The default is **`/var/lib/bricklogger`**
  beside a config directory in `/etc`, and **`~/.local/share/bricklogger`**
  beside one under the home directory; `init` writes the resolved path
  into the file, so a written configuration never leaves it implicit.
- `stop_timeout`: how long a plugin instance gets to stop gracefully before it
  is abandoned and reported in status. The default is **`10s`**.
- `log.level`, `log.format`: the daemon's own [logging](daemon.md#logging):
  `debug`, `info`, `warning` or `error`, as `text` or `json`.
- `log.file`, `log.max_size`, `log.keep`: the log file and its rotation by
  size, used by `daemon start` only; `daemon run` logs to stdout.
- `web.host`, `web.port`, `web.password`: where [`bricklogger serve`](cli.md#serve)
  binds, and the password it requires when bound beyond loopback, set through
  an environment variable. The daemon itself ignores this section.
- `mcp.host`, `mcp.port`, `mcp.token`: where
  [`bricklogger mcp serve --http`](mcp.md#access) binds, and the token clients
  send as a bearer token — required when bound beyond loopback and enforced
  whenever it is set, so a localhost server is guarded as soon as there is a
  token. [`mcp auth generate`](cli.md#mcp) writes the value into the `env`
  file and the reference here; by hand it is a secret like any other. The
  daemon ignores this section too.
- `notifications`: mail to the administrator, off by default, defined below.

### Notifications

What is sent and when is described on the
[notifications page](notifications.md); this is the schema. An absent section
is read as `enabled: false`, and nothing but `enabled` is required while
notifications are off, so the section can be left out entirely.

| Key | Requirement | Content |
|-----|-------------|---------|
| `enabled` | default `false` | Whether mail is sent at all. Setting it to `true` requires an active model |
| `smtp.host` | required when enabled | The mail server to submit to |
| `smtp.port` | default `587` | |
| `smtp.security` | default `starttls` | `starttls` for the submission port, `tls` for an implicit-TLS port, `none` for a relay on the local network |
| `smtp.username` | optional | Left out for a relay that takes unauthenticated mail from its own hosts |
| `smtp.password` | optional | A secret: given as `${VAR}` and kept in the [`env` file](#the-env-file) |
| `from` | required when enabled | The sender address |
| `to` | required when enabled | The recipients, at least one |
| `window` | default `2m` | How long the daemon gathers events before sending one mail |
| `min_interval` | default `15m` | The floor between two mails |
| `digest` | default `07:00` | When the daily summary is sent, in the machine's local time |

**`enabled: true` requires an active model.** Validation reads the data
directory named by `data_dir`, and a configuration that switches
notifications on where no model version is active is **refused** with an error
on `notifications.enabled` — as `api.token` is required on a binding that is
not loopback. Upload a model first, then switch notifications on. A model that
goes missing afterwards does not stop the daemon; notifications go dormant and
say so in status, as the [notifications page](notifications.md#switching-it-on)
describes.

**Durations** are written as everywhere else: `2m`, `15m`, `1h`. `digest` is
a time of day as `HH:MM`, quoted, because YAML would otherwise read it as a
sexagesimal number.

## `sources.yaml` and `destinations.yaml`

Instances are named with **the name as key**; the name is thereby unique by
construction and can be referred to in status and error messages. `type`
selects the plugin type — built-in types and custom plugins are given the
same way. The remaining keys are the type's own settings and are validated
against the type's [configuration schema](../architecture.md#declaration);
errors name the instance and the key.

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

Accepted points are written to **all** configured destinations; there is no
routing per rule.

Two keys in a destination instance are **reserved for the daemon** and never
reach the plugin: `spool` and `batch`.

```yaml
# destinations.yaml
tsdb:
  type: timescaledb
  dsn: "postgres://bricklogger@db.example.com:5432/brick"
  password: ${TSDB_PASSWORD}
  spool:
    max_size: 1GB     # default
    max_age: 7d       # default
  batch:
    size: 1000        # default: observations per write
    interval: 5s      # default: flush at least this often
```

- `spool.max_size` / `spool.max_age`: caps on the
  [persistent spool](../architecture.md#backpressure-the-spool) that sits
  between the daemon and this destination; when a cap is reached, the oldest
  observations are dropped and counted in status.
- `batch.size` / `batch.interval`: how many observations go into one write,
  and how long the daemon waits at most before writing a partial batch.

**Sizes** are written as a number with a unit: `500MB`, `1GB`, in binary
multiples, so `1GB` is 1024³ bytes.

## `rules.yaml`

The rule set's semantics (ordered, first match wins, implicit deny) are
described in the [architecture](../architecture.md), and the selectors'
vocabulary in the [daemon document](daemon.md). The file is an ordered list of
rules with these keys:

| Key | Requirement | Content |
|-----|-------------|---------|
| `name` | optional | Readable name for status and troubleshooting |
| `match` / `match_regex` / `sparql` | exactly one | The selection of points |
| `action` | required | `accept` or `deny` |
| `method` | required with `accept` | Collection method: `poll` or a source-specific method declared by the source's plugin |
| `interval` | required with `method: poll`, forbidden otherwise | Polling interval as a duration |
| `fallback` | optional with a source-specific `method` | Alternative collection method, same schema (`method` + `interval` if applicable) |

A position is always taken explicitly on both `action` and `method` — no rule
is implicitly one or the other.

**Durations** are written as a number with a unit: `30s`, `5m`, `1h`, `7d`.

### Collection method: `method`

`poll` exists for all sources and requires an `interval`. In addition, a
source can offer its own methods — e.g. a subscription to changes on the
device instead of polling, which takes no `interval`. Which methods a source
supports is declared by the source's plugin with a parameter schema per
method, and validation catches unknown methods and parameters that do not fit
the schema. The built-in source, BACnet/IP, offers `poll` only; the
methods a source offers are listed on the [sources page](sources.md).

If a rule with a source-specific method hits points whose source or device
does **not** support the method, the rule's `fallback` is used. If there is no
fallback, the points are not collected, and the daemon shows a clear warning
in status.

```yaml
# rules.yaml
- name: Exclude test room
  match: { location: "ex:Room_1_17" }
  action: deny

- name: Ventilation temperatures
  match: { class: brick:Temperature_Sensor, equipment: "ex:AHU_01" }
  action: accept
  method: poll
  interval: 5m

- name: All energy meters
  match: { class: brick:Energy_Sensor }
  action: accept
  method: poll
  interval: 15m

# everything else: implicit deny
```
