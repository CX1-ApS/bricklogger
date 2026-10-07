# Plugins

Sources and destinations are **plugins**: Python packages that follow the
contract defined in the [architecture](../architecture.md#plugins-sources-and-destinations)
and are found through entry points. The built-in BACnet/IP and TimescaleDB follow the same contract as a plugin written anywhere else; there
is no privileged code path. This page is for two readers: the **operator**,
who installs, upgrades and removes a plugin, and the **author**, who writes
one. The contract itself is defined in the architecture and not repeated
here; this page says how it is used.

## What a plugin is

A plugin is an ordinary Python distribution, installed in the same environment
as Bricklogger, that registers one entry point per type it provides in one of
two groups:

| Group | What the entry point loads to | Written in |
|-------|-------------------------------|------------|
| `bricklogger.sources` | A `SourceDeclaration` | `sources.yaml` |
| `bricklogger.destinations` | A `DestinationDeclaration` | `destinations.yaml` |

The entry point's **name is the type name**: what `type:` says in the
configuration and what `bricklogger plugins <type>` describes. The
declaration is static and is read without starting anything, which is what
lets the CLI list a plugin, `init` ask about its settings, the web interface
build its forms and the assistant read its schema before an instance exists.
The daemon reads the entry points **when it starts**.

## Installing a plugin

A plugin is installed **into the uv tool environment Bricklogger was
installed into**, beside Bricklogger, and the daemon finds it through its
entry points without any configuration. Three ways lead there, and they are
the same operation:

- **From the CLI**, `bricklogger plugins add PACKAGE`, which takes one or more
  packages. The command runs uv against the environment it runs from itself,
  and when it is done prints the catalogue as it now is.
- **From the web interface**, on the [Plugins screen](web.md#screens), which
  runs the same operation in the process `serve` runs in, against the
  environment it runs from.
- **By hand**, with uv itself, naming every plugin:

```bash
uv tool install bricklogger --with bricklogger-ibos --with PACKAGE
```

uv keeps a record of what the tool environment was installed with —
Bricklogger and one `--with` per plugin — and `uv tool install` **replaces**
that record with its command line: a plugin left off it is uninstalled.
`uv tool list --show-with` shows the record as it stands. `add` and `remove`
keep it themselves, so `uv tool upgrade bricklogger` and a later
[`update`](cli.md#update) keep the plugins too, and they leave Bricklogger and
the plugins in it without versions, so nothing is pinned that
`uv tool upgrade` could not move.

`PACKAGE` is anything uv installs: a name from PyPI or another index
(`bricklogger-httpjson`), a name with a version
(`bricklogger-httpjson==0.2.1`), a wheel on disk
(`./bricklogger_httpjson-0.2.1-py3-none-any.whl`) or a git URL. `add`
upgrades a package that is already installed, and installs a wheel even when
its version is already present; two builds of a development wheel carry the
same version, and the second must win.

`add` also **holds the other plugins where they are.** Every plugin shares
one environment, and a process imports one version of a library, so two
plugins that need incompatible versions of the same library cannot both work.
`add` therefore resolves what it was asked for **together with the plugins
already installed**, each held at its installed version, and refuses a
package that cannot live with them before anything has changed; uv's message
names the two that disagree. Only what was asked for moves: the other plugins,
and the libraries they share, keep their versions unless the new package needs
otherwise and the rest can live with it. The way out of a refusal is a
release of one plugin that widens its requirement, or `remove` of the other.
A package given as a bare URL or a path to a directory names no distribution,
so `add` cannot tell which installed plugin it replaces and holds that one
too; write `name @ url` for a plugin that is already installed. The command
run by hand checks nothing of the kind: it is the same uv, given only what is
on its command line.

`add` and `remove` work on an installation made with `uv tool install`. They
find uv on the path, and in `~/.local/bin` where its installer puts it, since
a service's path is short. Anywhere else, such as a checkout run with
`uv run`, they print the command to run instead of guessing at the
environment. Neither needs rights beyond the login's own.

### In a container

An image cannot grow a package, so a container keeps its plugins in a volume
of its own, and `add` installs into that volume instead of into the image's
environment. What was asked for is written down beside them, so a new image
lays the same set down again; the image's own packages take precedence over
the volume's, and an installation is constrained to their versions. The
[Docker page](../docker.md#plugins) describes the volume, the manifest and what
happens on an upgrade.

### Restart afterwards

The daemon reads its plugins when it starts, so a plugin added or removed
while it runs is not seen until the next start:

```bash
systemctl --user restart bricklogger      # a service
bricklogger daemon restart                # a daemon started by hand
```

The MCP server reads them the same way when it runs as a service over HTTP,
so it is restarted too; the web interface asks the daemon and needs nothing.
`add`, `remove` and the web interface's Plugins screen say this when they are
done and restart nothing themselves: a restart interrupts collection, and when
that happens is the operator's call.

### Removing a plugin

```bash
bricklogger plugins remove TYPE
```

`remove` uninstalls the distribution that provides the type, and with it
every other type the same distribution provides, which it names. It
**refuses while an instance of the type is configured** in `sources.yaml` or
`destinations.yaml`, naming the instances: after the next start they would
be [failed](#a-type-that-is-not-installed) and raise an alarm. Remove the
instances first, with [`sources remove`](cli.md#sources-and-destinations) or
`destinations remove`. `--force` uninstalls anyway; from the next start the
instances are failed, and the rest runs, until they are removed or the plugin
is added again. The built-in types cannot be removed: their
distribution is Bricklogger itself, and `remove` says so.

`remove` also **takes with it what only that plugin needed.** The plugin
leaves uv's record, and uv resolves the environment again without it: what no
remaining plugin and not Bricklogger itself requires is uninstalled in the
same command, and `remove` names it. A library another plugin still needs
stays. The environment holds what the record requires and nothing else, so a
library installed into it by other means — `uv pip install`, say — goes at
the next `add`, `remove`, `update` or `uv tool upgrade`; a library wanted
beside the plugins is added with `plugins add` like a plugin.

### Upgrading

[`bricklogger update`](cli.md#update) upgrades Bricklogger and its plugins
from PyPI, validates the result before anything is restarted, and puts the
previous versions back when it does not hold. `update all` moves the plugins
with Bricklogger; `update core` holds them, and since a plugin is built for a
[minor version](#compatibility) of Bricklogger, it goes no further than the
plugins allow and names the one that holds it back; `update <type>` upgrades
one plugin. `update status` shows what there is to upgrade.

`uv tool upgrade bricklogger` also upgrades Bricklogger, and the plugins in
uv's record with it, unchecked; a container that pulls a new image lays its
plugin volume down again against it. After either, look at
`bricklogger plugins`: a plugin that no longer loads shows as
[failed](#when-a-plugin-cannot-load) with the reason, and the author's
release for the new version is installed with `update <type>`.

## When a plugin cannot load

A plugin whose entry point raises when it is loaded, typically for a missing
dependency or because it was built against another version of the contract,
or whose declaration does not match its entry point, **stops nothing**:

- **The catalogue lists it** with its type, its role from the entry point
  group and its version from the distribution, and `failed: <error>` where its
  description would be. `plugins <type>` prints the whole error, and the rows
  of [`GET /v1/plugins`](api.md#plugins) carry it.
- **Its configured instances are `failed`** in status with the same error and
  raise the warning `instance_failed`, which the
  [notifications](notifications.md) mail like any other alarm.
- **Validation warns** about each such instance, with the error, instead of
  checking its settings; the configuration is still valid, so the daemon
  starts.
- **Everything else runs.** Fix the cause and restart the daemon.

### A type that is not installed

A configured instance whose type no installed plugin provides is treated as
one whose plugin cannot load. This is the case after an upgrade that moved a
type out into a plugin of its own, when a container could not lay a plugin
down, and when `type:` is misspelt in a file edited by hand. The daemon
starts, and the instance is `failed` with the error that the type is not
installed and that `bricklogger plugins add` installs it, raising
`instance_failed`; validation warns with the same message. The catalogue lists
only what is installed, so the type does not appear in `bricklogger plugins`.

A change written through the [CLI](cli.md#sources-and-destinations), the web
interface, the API or the MCP server is held to more: it is refused when an
instance it adds or changes names a type that is not installed, so a
misspelling is caught where it is typed. Instances the change leaves as they
were are only warned about, so the rest of a file can still be edited while a
plugin is missing.

The checks at load are four: the entry point must load to a
`SourceDeclaration` in `bricklogger.sources` and a `DestinationDeclaration` in
`bricklogger.destinations`, the declaration's `type_name` must equal the entry
point's name, the name must not be one of `all`, `core` and `status`, which
[`update`](cli.md#update) takes as words of its own, and importing the module
must succeed.

## Writing a plugin

### Naming and layout

| | Convention | Example |
|---|-----------|---------|
| Distribution | `bricklogger-<type>` | `bricklogger-httpjson` |
| Importable module | `bricklogger_<type>` | `bricklogger_httpjson` |
| Entry point name | the type name | `httpjson` |

The type name is written in YAML and typed on the command line, so it is
lowercase letters, digits and hyphens; the module replaces the hyphen with an
underscore. `all`, `core` and `status` are taken by
[`bricklogger update`](cli.md#update) and cannot name a type. A plugin that
provides several types registers several entry
points in one distribution. A small plugin fits in one module:

```
bricklogger-httpjson/
├── pyproject.toml
├── README.md
├── src/bricklogger_httpjson/
│   └── __init__.py        # configuration, source, declaration
└── tests/
    └── test_source.py
```

### The project file

```toml
[project]
name = "bricklogger-httpjson"
version = "0.1.0"
description = "Bricklogger source: points read from a JSON endpoint"
requires-python = ">=3.11"
dependencies = ["bricklogger>=0.2,<0.3", "httpx>=0.27"]

[project.entry-points."bricklogger.sources"]
httpjson = "bricklogger_httpjson:SOURCE"

[dependency-groups]
dev = ["pytest>=8.0", "pytest-httpserver>=1.0"]

[tool.uv.sources]
bricklogger = { path = "../bricklogger", editable = true }
```

The plugin **depends on `bricklogger`**, the whole distribution, and that is
where the SDK comes from; there is no separate SDK package. Its own
dependencies are listed as usual and land in the same environment as
Bricklogger's and every other plugin's, so a version conflict with either is
found at install, not at run time. Keep the bounds as wide as the plugin can
honestly take, a lower bound and no upper one without a reason: every plugin
on a machine shares one copy of each library, and the tightest bound among
them decides whether two plugins can be
[installed together](#installing-a-plugin). The `tool.uv.sources` entry is
for development, where the dependency is satisfied from a checkout of
Bricklogger; it stays out of the built wheel, so the released plugin resolves
`bricklogger` from PyPI like any other dependency. On a machine the install
script set up, Bricklogger is already in the environment and the constraint is
checked against it, and in a container
[the image stands in for it](../docker.md#plugins).

### The SDK

**`bricklogger.sdk` is the public surface.** Everything a plugin needs is
importable from that package, and nothing else in Bricklogger is: the other
modules may change between releases without notice, and a plugin that
reaches into them is on its own.

| From `bricklogger.sdk` | Names |
|------------------------|-------|
| The contract | `Source`, `Destination`, `Observation`, `PointMetadata`, `Outcome`, `AssignedPoint`, `Sink`, `StatusChannel`, `GraphReader`, and the closed vocabularies `ValueType`, `NullReason`, `OutcomeState` and `InstanceState` as types, with `VALUE_TYPES` and `NULL_REASONS` as sets |
| The declaration | `SourceDeclaration`, `DestinationDeclaration`, `CollectionMethod`, `ToolDeclaration`, `ToolOffer`, `Vocabulary` |
| Configuration values | `Duration`, the type a setting such as a timeout is declared with: it takes `30s`, `5m`, `1h` or `7d` in YAML and is a `timedelta` in the plugin; `DURATION_HELP`, the one-line description of that form, and `parse_duration` and `format_duration` for code that handles the text itself, such as a tool parameter |
| Prefixes | `expand`, which turns a prefixed name such as `brick:Point` into its URI with the prefixes the graph declares (`GraphReader.prefixes`), and `UnknownPrefix`, which it raises for a prefix none declares |
| `bricklogger.sdk.bacnet` | For a source whose system exposes BACnet objects, on site or through a cloud API: the object types grouped by the value type their present value maps to (`ANALOG_TYPES`, `BINARY_TYPES`, `MULTISTATE_TYPES`, `INTEGER_TYPES`, `STRING_TYPES`, `DATETIME_TYPES`), the reason `UNSUPPORTED` for one with no counterpart, and the map from BACnet engineering units to QUDT (`UNIT_MAP` under the namespace `QUDT`) with `protocol_unit`, which gives the protocol's own designation where the map has none. The BACnet/IP source uses the same tables |
| `bricklogger.sdk.testing` | `graph_from_turtle`, `Collector`, `run_source`, `assigned`; see [testing a plugin](#testing-a-plugin) |

### Compatibility

The contract is stable within a **minor version** of Bricklogger: a change to
it raises the minor version, and patch releases never touch it. A plugin
therefore pins the minor version it was built for, `bricklogger>=0.2,<0.3`,
and works with every patch release of it. The author releases the plugin
again for a new minor version after checking it against the changes the release notes of that version list. The version `bricklogger plugins`
shows for a plugin is the plugin's own distribution version.

### The declaration

The declaration is what the [architecture](../architecture.md#declaration)
defines: for a source the type name, a description, the configuration schema,
the reference types it understands, a vocabulary when Brick's reference
schema has no type for its system, the collection methods beyond `poll`, the
tools, and the factory that makes an instance; for a destination the type
name, the description, the configuration schema, whether it stores metadata,
and the factory. Three things matter in practice:

- **Schemas are pydantic models**, and every setting carries a **one-line
  `description`**. That text is all the CLI's `plugins <type>`, `init`, the
  web interface's forms and the assistant behind the MCP server know about the
  setting, so it is written for the operator who fills it in. Reject unknown
  keys with `extra="forbid"`, so a misspelt key is an error naming it rather
  than a setting silently ignored.
- **A secret is marked** with the JSON Schema format `password`, as in the
  example below; the CLI then asks for it without echo, keeps its value in
  the [`env` file](configuration.md#the-env-file) and refuses to write it into
  a configuration file, and the assistant takes it only as `${VARIABLE}`.
- **The factory is the class itself** when its constructor takes `name`,
  `config` and, for a source, `graph`. The `config` it receives is the
  validated instance of the plugin's own schema, never YAML.

A source whose system has no reference type in Brick's reference schema
declares a [vocabulary](../architecture.md#declaration): the prefix and
namespace of its own reference type and a Turtle document shipped in the
package, which the daemon loads with Brick's ontology at every activation. The vocabulary of the [iBOS source](https://github.com/CX1-ApS/bricklogger-ibos) shows the shape.

### A source, phase by phase

The [six phases](../architecture.md#the-phases-of-the-contract) of the
contract, seen from the author's side:

1. **Declaration.** As above.
2. **Configuration binding.** The daemon validates the instance and calls the
   factory; the plugin never reads YAML. Keep the constructor cheap and free
   of network access: it runs at every plan computation, and again in the CLI
   when a tool runs without a daemon.
3. **Binding to the model.** `resources()` returns the exclusive resources the
   instance claims as opaque strings, such as `udp:0.0.0.0:47808`, or nothing.
   `claim()` finds the references of the declared types in the graph through
   `self.graph.query`, decides from the configuration which the instance
   serves, and returns their **point URIs**. Both run without touching the
   network, at every reload and activation. The query is plain SPARQL over the
   union of the model, the ontology, the inferred graph and the value overlay;
   declare the prefixes you use with `PREFIX` lines, or build them from
   `self.graph.prefixes`, and read the result as pyoxigraph returns it.
4. **Operation.** `assign(points)` hands over **full desired state** and may be
   called before `start`, and from another thread at any time; the plugin
   works out what changed. Each `AssignedPoint` carries the URI, the method
   name, its parameters, which for `poll` is `interval` as a `timedelta`, and
   `last_observation`, where a source with history resumes. `start(sink,
   status)` runs in a thread of its own and **returns only after `stop` has
   been called**; a loop that returns on its own is treated as a crash, and
   the daemon restarts the instance with backoff. In the loop the source
   reports an `Outcome` per point on the status channel first and again when
   it changes, pushes `Observation`s into the sink with **timezone-aware UTC
   timestamps** from the closed
   [value-type vocabulary](../architecture.md#the-observation), a `null` with
   its reason when there is no valid value, delivers `PointMetadata` when it
   knows units and state texts, and raises and clears warnings with codes of
   its own. A new model gives a new `claim` and a new `assign`, so references
   are resolved again from the graph, not remembered from the first round.
5. **Protocol tools.** `run_tool(name, parameters)` receives the parameters
   already validated against the tool's schema, as a plain dictionary, and
   returns a JSON-serialisable result. The daemon calls it only while the
   instance is running; without a daemon the CLI constructs the source and
   calls it in-process, so a tool must work on a source that was never
   started.
6. **Stop.** `stop()` is called from another thread and only asks; `start`
   then finishes within `stop_timeout` from `daemon.yaml`, default 10 seconds,
   flushing what it holds into the sink. A source that misses the deadline is
   abandoned and reported.

### The example

A complete source, `bricklogger-httpjson`: every point carries a
`ref:TimeseriesReference`, and its `ref:hasTimeseriesId` is the key under which
an HTTP endpoint answers `{"value": 21.5, "time": "2026-09-19T10:00:00+00:00"}`.
The instance claims every such reference, polls each point at its rule's
interval and reads one point per request. It is short because the contract
asks for little; what a real protocol adds is its own.

```python
"""bricklogger_httpjson: points read one by one from a JSON endpoint."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel, ConfigDict, Field

from bricklogger.sdk import (
    AssignedPoint,
    Duration,
    GraphReader,
    Observation,
    Outcome,
    Sink,
    Source,
    SourceDeclaration,
    StatusChannel,
)

QUERY = """
PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
SELECT ?point ?id WHERE {
  ?point ref:hasExternalReference ?reference .
  ?reference a ref:TimeseriesReference ; ref:hasTimeseriesId ?id .
}
"""


class HttpJsonConfig(BaseModel):
    """One ``httpjson`` instance in ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        description="The endpoint; a point is read at <url>/<timeseries id>"
    )
    token: str | None = Field(
        default=None,
        json_schema_extra={"format": "password"},
        description="A bearer token, given as ${VARIABLE} with the value in "
        "the env file",
    )
    timeout: Duration = Field(
        default=timedelta(seconds=5), description="How long one read may take"
    )


class HttpJsonSource(Source):
    def __init__(self, name: str, config: HttpJsonConfig, graph: GraphReader) -> None:
        super().__init__(name, config, graph)
        self.settings = config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._points: dict[str, AssignedPoint] = {}
        self._changed = False

    # --- binding to the model ------------------------------------------------

    def resources(self) -> Iterable[str]:
        return []  # nothing exclusive: two instances may read the same endpoint

    def claim(self) -> set[str]:
        return set(self._ids())

    def _ids(self) -> dict[str, str]:
        """Point URI to timeseries id, for every reference of our type."""
        return {
            str(row["point"].value): str(row["id"].value)
            for row in self.graph.query(QUERY)
        }

    # --- operation -----------------------------------------------------------

    def assign(self, points: Sequence[AssignedPoint]) -> None:
        with self._lock:
            self._points = {point.uri: point for point in points}
            self._changed = True

    def start(self, sink: Sink, status: StatusChannel) -> None:
        headers = {"Authorization": f"Bearer {self.settings.token}"}
        client = httpx.Client(
            base_url=self.settings.url,
            headers=headers if self.settings.token else {},
            timeout=self.settings.timeout.total_seconds(),
        )
        ids: dict[str, str] = {}
        due: dict[str, float] = {}
        with client:
            while not self._stop.is_set():
                with self._lock:
                    if self._changed:  # a new assignment: resolve again, report
                        self._changed = False
                        ids = self._ids()
                        status.outcomes(
                            Outcome(uri, "active")
                            if uri in ids
                            else Outcome(uri, "rejected", "no reference in the graph")
                            for uri in self._points
                        )
                        due = {uri: 0.0 for uri in self._points if uri in ids}
                    now = time.monotonic()
                    ready = [self._points[uri] for uri, at in due.items() if at <= now]
                    for point in ready:
                        interval: timedelta = point.parameters["interval"]
                        due[point.uri] = now + interval.total_seconds()
                if ready:
                    sink.observations(
                        [self._read(client, point, ids) for point in ready]
                    )
                self._stop.wait(0.5)

    def _read(
        self, client: httpx.Client, point: AssignedPoint, ids: dict[str, str]
    ) -> Observation:
        try:
            answer = client.get(f"/{ids[point.uri]}").raise_for_status().json()
            return Observation(
                point.uri,
                datetime.fromisoformat(answer["time"]),
                "number",
                float(answer["value"]),
            )
        except Exception:
            return Observation(
                point.uri, datetime.now(UTC), "null", reason="read_error"
            )

    def stop(self) -> None:
        self._stop.set()


SOURCE = SourceDeclaration(
    type_name="httpjson",
    description="Points read one by one from a JSON endpoint, by timeseries id.",
    config_schema=HttpJsonConfig,
    reference_types=("ref:TimeseriesReference",),
    factory=HttpJsonSource,
)
```

Three things the example does that every source must: it resolves its
references from the graph **again on every assignment**, since a new model
may have changed them; it reports an outcome for every assigned point before
the first read; and its loop **blocks until `stop`**, returning nothing on its
own. What it leaves out, a real source adds: metadata with the unit and the
value type once it has read them, a `device` report per system it talks to,
warnings with codes of its own, and tools such as a `read` of one point for
the CLI.

### A destination

A destination is the same declaration and binding, and a simpler contract:
`start()` connects and prepares storage and raises if it cannot, `write(batch)`
writes one batch and raises on failure, `write_metadata(entries)` stores point
metadata when the declaration says the destination does, and `stop()` flushes
and closes. The one rule to design around is that **delivery is at least
once**: a batch that was written but not acknowledged arrives again, so the
write is idempotent, keyed on point and time as
[TimescaleDB](destinations.md#timescaledb) does. Every destination instance
runs in its own thread, its writes come from a spool the daemon owns, and a
destination that is down never slows collection; the
[architecture](../architecture.md#the-destination-contract) has the details.

```python
DESTINATION = DestinationDeclaration(
    type_name="csvfiles",
    description="One CSV file per day, appended in batches.",
    config_schema=CsvFilesConfig,
    stores_metadata=False,
    factory=CsvFilesDestination,
)
```

A destination that cannot store metadata declares `stores_metadata=False` and
receives measurements only; status shows which destinations store it.

## Testing a plugin

A plugin lives outside Bricklogger's repository, so the SDK carries what its
tests need, in `bricklogger.sdk.testing`:

| Name | What it does |
|------|--------------|
| `graph_from_turtle(text, directory=None, *, vocabularies=None)` | Stores and activates the model exactly as the daemon does, with validation, inference, Brick's ontology and the vocabularies of the installed sources, or the ones given, and returns a `GraphReader` with the model's prefixes. The graph lives in `directory`, or in a temporary one. A model that does not conform raises, as an upload would be rejected |
| `Collector()` | A sink and a status channel in one object that remembers everything, thread-safe: `observations_for(point)`, `metadata_for(point)`, `outcome_of(point)`, `warnings` by code, `devices`, `states`, and `wait_until(predicate, timeout=5.0)`, which polls until the predicate returns something true and returns it, or raises |
| `run_source(source, points, *, timeout=5.0)` | A context manager: assigns `points`, starts the source in a thread with a fresh `Collector` and yields the collector; on exit calls `stop` and waits `timeout` seconds. It raises when the source did not stop in time or when its loop raised |
| `assigned(uri, method="poll", **parameters)` | An `AssignedPoint`; a string `interval` such as `"1s"` is parsed as a duration |

The example's test, against a local HTTP server from `pytest-httpserver`:

```python
from bricklogger.sdk.testing import assigned, graph_from_turtle, run_source
from bricklogger_httpjson import SOURCE, HttpJsonConfig

MODEL = """
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix ex: <https://example.com/bldg#> .

ex:Room a brick:Room .
ex:ZAT a brick:Zone_Air_Temperature_Sensor ; brick:isPointOf ex:Room ;
    ref:hasExternalReference [ a ref:TimeseriesReference ; ref:hasTimeseriesId "zat" ] .
"""
POINT = "https://example.com/bldg#ZAT"


def test_claims_and_reads(tmp_path, httpserver):
    httpserver.expect_request("/zat").respond_with_json(
        {"value": 21.5, "time": "2026-09-19T10:00:00+00:00"}
    )
    graph = graph_from_turtle(MODEL, tmp_path)
    config = HttpJsonConfig(url=httpserver.url_for("/"))
    source = SOURCE.create("demo", config, graph)

    assert source.claim() == {POINT}

    with run_source(source, [assigned(POINT, interval="1s")]) as collected:
        observation = collected.wait_until(lambda: collected.observations_for(POINT))[0]
    assert observation.type == "number"
    assert observation.value == 21.5
    assert collected.outcome_of(POINT).state == "active"
```

Test the declaration too: that `SOURCE.type_name` is the entry point's name,
that the schema rejects an unknown key, and that every setting has a
description. Those are the three things the catalogue, `init` and the
assistant rely on. A destination needs no harness: construct it with a
validated configuration, call `start`, `write` and `stop`, and write the same
batch twice to prove the idempotency.

## Distributing a plugin

```bash
uv build
```

builds the wheel into `dist/`. It is installed on a machine as described under
[installing a plugin](#installing-a-plugin): with
`bricklogger plugins add ./dist/<wheel>` while it is tried, and by name once
it is published: `uv publish` uploads what `uv build` built to PyPI, which is
how the [iBOS source](https://github.com/CX1-ApS/bricklogger-ibos) is
released. The plugin's version is its
own and is what `bricklogger plugins` shows.

The plugin documents itself: its README says what its reference looks like,
what its settings mean and which tools it offers, since this documentation
covers the built-in plugins only. The one-line descriptions in its schemas
travel further than the README does, because they are what the CLI, the web
interface and the assistant show at the moment a setting is filled in.
