# Daemon

## Point selection: selectors

The rule set's overall semantics — ordered list, first match wins, implicit
deny — are described in the [architecture](../architecture.md). This page
defines how a rule selects points.

### `match`: simplified selectors

A rule's `match` supports four keys:

| Selector | Matches | Example |
|----------|---------|---------|
| `class` | The point's Brick class, **including subclasses** | `brick:Temperature_Sensor` |
| `equipment` | The equipment the point belongs to | `ex:AHU_01` |
| `location` | The point's location (zone, room, floor, building): the location it is a point of, or the location of its equipment, and everything those are part of or located in | `ex:Room_1_17` |
| `point` | One specific point by its URI | `ex:TT_1_17` |

- **Subclasses:** `class` follows Brick's class hierarchy, so
  `brick:Temperature_Sensor` also hits e.g.
  `brick:Zone_Air_Temperature_Sensor`. An exact match is obtained by giving a
  leaf class.
- **Transitivity:** `equipment` and `location` match through the graph's part
  hierarchies: `location: ex:Floor_1` also hits points in rooms on the floor
  (`isPartOf` chains), and `equipment: ex:AHU_01` also hits points on the
  equipment's sub-components (`hasPart`). For locations the chain also
  follows where a location is located (`hasLocation`, `rec:locatedIn`), and
  Brick's and RealEstateCore's relations count alike.
- **A point's location:** a location is a Brick `Location` or a
  RealEstateCore space — `rec:Building`, `rec:Level`, `rec:Room` and its
  kinds, `rec:HVACZone`. Brick models a zone sensor as a point *of* the zone
  or room (`isPointOf`), and a point on a piece of equipment has no location
  of its own. A point's location is therefore the location it is a point of,
  or the location of its equipment and of anything that equipment is part of,
  and from there everything the location is part of or located in — an AHU's
  supply air temperature is in the plant room the AHU is in, on that room's
  floor and in the building.
- **Zones and rooms:** a zone within a room is `rec:isPartOf` the room, and a
  room may hold several zones; a zone that spans rooms has them as parts,
  `rec:hasPart`. A zone's points count in the zone, in every room the zone
  has as a part, and in everything those rooms and the zone are part of, so
  `location: ex:Room_1_02` reaches the zone's temperature and CO2 either way.
  Only zones are expanded into their parts: a building's own points are not
  in every room of the building.
- **AND:** If several keys appear in the same `match`, all must be satisfied.
- **OR:** A list as value matches if just one element matches:
  `location: [ex:Room_1_17, ex:Room_1_18]`.

### `match_regex`: regular expressions

As an alternative to `match`, a rule can use `match_regex` with the same four
keys, where the values are regular expressions:

```yaml
- name: All rooms on floor 1
  match_regex: { location: "ex:Room_1_.*" }
  action: accept
  method: poll
  interval: 10m
```

- The expression must match the **whole** URI in **prefixed form** (e.g.
  `ex:Room_1_17`) — the same form written in the configuration, using the
  model's own prefixes and Brick's.
- `class` in `match_regex` is a pure string match against the classes the
  model asserts, **without** the subclass hierarchy; `equipment` and
  `location` match through the part hierarchies as in `match`.
- `match` and `match_regex` are **mutually exclusive**: a rule uses one or
  the other. To combine the two forms, use SPARQL.

### SPARQL as an escape hatch

When the selectors are not enough, a rule can instead give a raw SPARQL query
that selects the points. That covers arbitrarily complex selections — boolean
logic, relations across the graph, and combinations of exact match and regex.
The query's **first selected variable** is the point. The model's prefixes and
Brick's are declared for the query already, so `PREFIX` lines are needed only
for others.

A rule that cannot be evaluated as written — a value with an unknown prefix, a
query that fails — is skipped with a warning in status, and the rules after it
are evaluated as if it were absent. A regular expression that does not compile
is caught earlier, by validation.

The query runs against the
[working graph](../architecture.md#the-working-graph) with the ontology and
the OWL-RL-inferred triples: `?p a brick:Temperature_Sensor` hits all
subclasses, and relations can be followed in both directions. Transitive part
hierarchies are written explicitly with property paths, e.g.
`brick:isPartOf*`.

The full rule schema — `action`, `interval` and the other keys — is defined
in the [configuration document](configuration.md).

## Model findings

A finding is what the **model** lacks, as against a warning, which is what the
**daemon** has run into. It is read from the graph whenever it is asked for,
holds for as long as that version is active, and is therefore never withdrawn,
counted or cleared: the way to end a finding is to correct the model and
activate it anew. Findings travel with the
[entity document](api.md#entities), which is what the
[model explorer](web.md#model-explorer) draws and `model tree` prints, and
each of them is a filter there.

| Code | On | Meaning |
|------|----|---------|
| `no_owner` | A point | It is a point of no equipment and of no location, and is located nowhere: nothing in the building answers for it |
| `no_reference` | A point | It has no external reference, so no source can address it. On a point a rule accepts, the daemon raises the warning of the same name, and the explorer and `model tree` then show it once |
| `no_location` | Equipment | Neither it nor anything it is part of has a location, so its points inherit none either |
| `no_relations` | A point, equipment, a location or a system | It stands alone in the model, tied to nothing else |
| `deprecated_class` | Anything | One of its asserted classes is marked deprecated in the bundled Brick — the spatial classes that moved to RealEstateCore, among others |

A deprecated class is read from Brick's own `owl:deprecated`, not from the
warnings of the validation that ran at upload: the ontology marks every
deprecated class, while Brick's shape reports only those that name a
replacement. A model with deprecated classes is valid and logs perfectly
well; the finding is there so a model can be modernised deliberately.

## Status

Everything in status comes from runtime state and the status channels of the
plugins. Status never asks a plugin synchronously — the same principle as for
metadata. It is machine-readable JSON with stable field names; the CLI renders
tables and can emit the JSON unchanged, which is what gives the web interface
its parity. The endpoints are listed in the
[API reference](api.md#status-and-points).

### Health

Two endpoints, for two kinds of monitor:

| Endpoint | Purpose | Result |
|----------|---------|--------|
| `/health/live` | Is the process alive | Always 200 while the daemon runs |
| `/health` | Is the logger doing its job | 200 for `ok` and `idle`, 503 for `degraded` |

- `ok`: collecting, nothing failed.
- `idle`: no model, or every source instance stopped by the operator — a
  healthy daemon that is deliberately not collecting.
- `degraded`: collecting, but a source or destination instance is failed, or a
  destination's spool has dropped data and has not been drained since.

The split exists so that a restart-on-failure probe uses `/health/live` and an
alerting probe uses `/health`; a dead database never causes a daemon restart.

### The status tree

- **Daemon:** version, uptime, state, config and data directories, the active
  model version with upload and activation times, counts of points accepted,
  assigned and active, and every configured instance with its role, type and
  state, so the summary alone says whether they are all running. When the
  daily [update check](cli.md#update) has found newer releases of Bricklogger
  or its plugins, they stand here too, with the time of the check.
- **Sources**, per instance: state (`starting`, `running`, `failed`,
  `stopped`), type, resources, last error, restart count and next retry. Per
  device: reachable or not, last success, error counters and skipped poll
  rounds. Per outcome: how many points are `active`, `unsupported` and
  `rejected`. An instance whose plugin could not be loaded, or whose type no
  installed plugin provides, source or destination, is `failed` with the error
  and has no next retry: it runs when the plugin loads at a later start.
- **Destinations**, per instance: state, whether it stores metadata, spool
  depth as observations, bytes and age of the oldest entry, dropped count,
  last successful write, last error.
- **Warnings:** one flat list. Each entry has a code and its kind, a subject
  — a point URI, an instance or a device — a message, first seen, last seen
  and a count. The per-point and per-instance views filter the same list.

**A warning is a condition that holds now.** Every code has a defined end,
and the daemon withdraws the warning when it sees that end, so a warning that
stands is true; one that stands after its condition is over is a bug in
Bricklogger, not something to live with. The operator may also clear a
warning by hand — `status warnings clear`, or the buttons on the overview —
as an acknowledgement; a cleared warning returns the next time its condition
is asserted. Counts and the two times survive a restart with the rest of the
runtime state, except where the end is the restart itself.

| Code | Kind | Meaning | Withdrawn when |
|------|------|---------|----------------|
| `unclaimed` | `model` | An accepted point that no source instance claims | The next plan no longer finds it |
| `unknown_reference` | `model` | None of the point's references is understood by an installed source | The next plan no longer finds it |
| `no_reference` | `model` | An accepted point without an external reference | The next plan no longer finds it |
| `no_fallback` | `model` | A device cannot do the requested method, and the rule has no `fallback` | The next plan, or the point's outcome is no longer `unsupported` |
| `rejected` | `model` | A source rejected an assigned point; the message carries its reason | The point gets an outcome other than `rejected` |
| `rule_skipped` | `model` | A rule could not be evaluated as written and was skipped | The next plan no longer finds it |
| `unit_conflict` | `model` | The graph's unit and the protocol's unit disagree | The next plan, or metadata in which the two agree |
| `read_error` | `operation` | The device answers a read with an error | The point delivers a value |
| `poll_overrun` | `operation` | A device keeps missing its poll interval, and rounds are skipped | The device reports a round without a skip |
| `rate_limited` | `operation` | A source's request budget makes its rounds outlast their interval; the message carries the cadence it can keep | The source's rounds keep their interval again |
| `future_timestamp` | `operation` | Observations rejected for a timestamp in the future | The point delivers an observation with an acceptable timestamp |
| `spool_drop` | `operation` | A destination's spool has dropped observations | The spool has been drained again; the dropped total stays in the destination's status |
| `instance_failed` | `operation` | A source or destination instance is failed, including one whose plugin could not be loaded or is not installed | The instance runs again |
| `instance_stopped` | `operation` | An instance stopped by the operator | The instance is started |
| `stop_timeout` | `operation` | An instance did not stop within `stop_timeout` and was abandoned | The daemon starts anew — the abandoned instance died with the old process — or the instance stops cleanly |
| `notify_failed` | `operation` | A notification mail could not be sent; the message carries the server's answer | A mail goes through again |
| `notifications_dormant` | `operation` | Notifications are switched on, but no model is active, so nothing can be sent | A model is active again |

**The kind** says what a warning is about. `operation` means the logger is not
doing its job right now; `model` means the model or the rule set leaves
something unresolved, which stands until one of them changes. Status, the
points view and the manual clear treat the two alike. The difference decides
which of them [notifications](notifications.md#which-warnings-raise-an-alarm)
mail at once and which wait for the daily summary — and the last two codes,
being about the mail itself, never mail at all.

### Points

A separate, paged and filterable view, because a site has thousands of
points: per point its URI, name and Brick class, source instance, method and
whether the fallback is in effect, outcome, time of the last observation, last
valid value with its time, and its warnings. Filters on instance, outcome,
warning code and Brick class. The web interface shows the class as well as
filtering on it, because a name says what an engineer called the point and the
class says what Bricklogger reads it as, and the two part company more often
than one would like: `Luftmængde Indblæsning` is a
`brick:Supply_Air_Flow_Sensor`.

The instance and the outcome are chosen from what exists and must match in
full. The warning code and the Brick class are **searched**: the text given
need only appear somewhere in the value, without regard to case, so `unit`
finds `unit_conflict` and `Temperature` finds every temperature sensor
whatever its Brick class is called. The class is still compared against the
class the model asserts for the point, in prefixed form, and a full IRI is
shortened before the comparison; subclasses are a
[rule selector's](#point-selection-selectors) business, not this view's.

## Logging

The daemon logs its own life — start and stop, reloads and model activations,
instance state changes, and every warning the first time it appears — never
the observations, whose volume belongs in the time series. The settings live
in [`daemon.yaml`](configuration.md#daemonyaml):

- **Level.** `log.level` is `debug`, `info`, `warning` or `error`; the
  default is `info`. `debug` adds the plugins' protocol traffic and is meant
  for troubleshooting, not for running.
- **Format.** `log.format` is `text` for people or `json` for log shippers:
  one object per line with time, level, logger and message, plus the fields
  the message carries, such as the instance or point concerned.
- **Destination.** `daemon run` logs to stdout only, because systemd and
  Docker collect it themselves. `daemon start` writes to `log.file`, by
  default `bricklogger.log` in the data directory, rotated by size: when the
  file reaches `log.max_size` (default `10MB`) it is renamed and a new one
  started, and the `log.keep` (default `5`) newest old files are kept.

## Runtime state

The daemon's operational state lives in an SQLite database in the data
directory, as the [architecture](../architecture.md#runtime-state) describes.
It holds:

- per point: the outcome and its reason, the method in effect and whether the
  fallback is active, the time of the last observation, and the last valid
  value with its time — which is also what the value overlay of the working
  graph is rebuilt from,
- per device: reachability, last success, error counters and skipped poll
  rounds,
- per instance: state, last error, restart count and the operator's stop
  intent,
- the warning list with first and last seen times and counts,
- the activation history of the model versions.

### A data directory it cannot write

The daemon cannot run without its state, so a data directory it cannot write
is a start that fails — but it fails with **one line that names the cause**:

```
error: the data directory /var/lib/bricklogger cannot be used: [Errno 13]
Permission denied. It belongs to uid 0, and the daemon runs as uid 999
```

The directory, the error, its owner and the user the daemon runs as, and exit
code 1 — never a Python traceback. The owner and the running user are named
because the two together are the answer nine times out of ten: a directory
created by another user, a service that changed user, or a
[container with a bind mount](../docker.md#bind-mounts-and-the-user-the-container-runs-as).
The same line is what a restart policy repeats, so a container that comes back
again and again says why each time.

The tables are **internal** and may change between versions of Bricklogger;
status, the API and the SPARQL endpoint are the interfaces to this state. The
active model version is recorded beside the version files, so the runtime
state can be **deleted** while the daemon is stopped: counters, last values,
warnings and the operator's stops are lost, the daemon starts on the active
model with every instance running, and the working graph is rebuilt; a
history source, which resumes from the last values, fetches its `backfill`
again.
