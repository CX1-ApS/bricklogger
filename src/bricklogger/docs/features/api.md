# API

The daemon's HTTP API is the one interface behind the CLI and the web
interface: every CLI command maps to one operation here, and `--json` prints
the responses unchanged. The API binds to localhost by default, as set in
[`daemon.yaml`](configuration.md#daemonyaml).

## Conventions

- **Base path.** Every operation lives under `/v1`. The two health endpoints
  live outside it, at `/health/live` and `/health`, as
  [defined for status](daemon.md#health).
- **JSON** in requests and responses, except where the content is a model, a
  SPARQL query or a configuration file. Field names are stable within `/v1`.
  Point URIs are returned in full; the CLI shortens them with the model's
  prefixes. The [entity document](#entities) is the one exception: it names
  every element several times, so it writes URIs in prefixed form and carries
  the prefixes itself.
- **Access.** On a loopback binding no credentials are needed. When the API
  is deliberately bound elsewhere, `api.token` must be set, and every request
  outside `/health` carries it as `Authorization: Bearer <token>`; a missing
  or wrong token gives `401`. The daemon does not terminate TLS: an exposed
  API belongs behind a reverse proxy that does.
- **Errors** are `application/problem+json` after RFC 9457: `type`, `title`,
  `status` and `detail`, plus an `errors` list for validation failures in
  which each entry names the file, the instance, rule or point, and the key it
  concerns.
- **Pagination** on the points view: `limit` (default 200, at most 1000) and
  `offset`, with `total` in the response. The [entity document](#entities) is
  not paged — a tree cut into pages is no tree — and is narrowed by its
  filters instead.
- **Writes take effect immediately**, because they are validated first. An
  invalid change is rejected as a whole with `422`, and the running
  configuration is kept.
- **Long operations run as jobs.** Model upload and activation answer `202`
  with a job, because validation and inference take from seconds to minutes;
  everything else answers synchronously, including protocol tools, which
  declare their own duration.

```json
{
  "type": "urn:bricklogger:problem:validation",
  "title": "Configuration is invalid",
  "status": 422,
  "detail": "2 errors",
  "errors": [
    { "file": "sources.yaml", "instance": "bacnet_main", "key": "address",
      "message": "not an IPv4 address with prefix length" },
    { "file": "rules.yaml", "rule": "Ventilation temperatures", "key": "interval",
      "message": "required with method poll" }
  ]
}
```

## Endpoints

### Health and daemon

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/health/live` | Alive: 200 while the daemon runs | — |
| GET | `/health` | Doing its job: 200 for `ok` and `idle`, 503 for `degraded` | — |
| POST | `/v1/daemon/reload` | Reload the configuration from disk, validated as a whole | `daemon reload` |
| POST | `/v1/daemon/stop` | Graceful stop within `stop_timeout` | `daemon stop` |

### Status and points

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/status` | The daemon summary | `status` |
| GET | `/v1/status/sources` | Sources per instance, with devices and outcomes | `sources status` |
| GET | `/v1/status/destinations` | Destinations per instance, with spool and writes | `destinations status` |
| GET | `/v1/status/warnings` | The flat warning list: code, kind, subject, message, first seen, last seen, count | `status warnings` |
| DELETE | `/v1/status/warnings` | Clears warnings by hand: all of them, those with `?code=`, or the one with `?code=` and `?subject=`; answers with the number cleared | `status warnings clear` |
| GET | `/v1/points` | The paged points view; filters `instance` and `outcome` on the whole value, `warning` and `class` on part of it | `points` |

The status tree and the points view are defined on the
[daemon page](daemon.md#status); the API returns them as JSON with those
fields. A page of points:

```json
{
  "total": 1834,
  "limit": 200,
  "offset": 0,
  "items": [
    {
      "uri": "https://example.com/bldg#AHU_01_SAT",
      "name": "AHU 01 supply air temperature",
      "instance": "bacnet_main",
      "method": "poll",
      "fallback_active": false,
      "outcome": "active",
      "last_observation": "2026-09-05T09:12:30Z",
      "last_valid": { "value": 18.4, "time": "2026-09-05T09:12:30Z" },
      "warnings": []
    }
  ]
}
```

### Notifications

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/notifications` | Whether notifications are on, the last mail with its time and recipients, the last error, when the next summary is due, and what waits in the open window | `notify status` |
| POST | `/v1/notifications/test` | Sends a test mail at once and answers with what the mail server said, the failure included | `notify test` |

What is sent and when is defined on the
[notifications page](notifications.md), and the settings under
`notifications` in [`daemon.yaml`](configuration.md#notifications) are read
and written through the [configuration endpoints](#configuration) like every
other setting. `POST /v1/notifications/test` sends on the configuration as
written, whether or not it is switched on, and answers `200` with the server's
reply or `502` with a problem document naming the failure; it is the one
operation here that reaches outside the machine.

### Configuration

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/config` | All four files as one JSON document, environment variables left as written | the web interface |
| GET | `/v1/config/{file}` | One file — `daemon`, `sources`, `destinations` or `rules` — as `application/yaml`, or parsed as JSON on `Accept: application/json` | `daemon config show`, `sources config show`, `destinations config show`, `rules config show` |
| PUT | `/v1/config/{file}` | Replace one file with the YAML text in the body: the directory is validated with the change applied, the text is written atomically and verbatim, and the change takes effect | `… config edit`, `sources add`, `edit`, `remove` |
| POST | `/v1/config/validate` | Validate without writing: the files on disk, or a proposed set of files given in the body | `validate` |
| POST | `/v1/config/init` | Write the four files with commented examples into the config directory; refused with `409` if any of them exists | `init --non-interactive` |

The files travel as **YAML text**, not as parsed data, so comments and layout
survive a round trip through the API. Secrets are never expanded in what the
API returns. A validation result carries `valid`, the `errors` and the
`warnings`; a configuration that accepts points but has no destination is a
warning, not an error.

### Models and jobs

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/models` | The versions: number, upload time, activation history and which one is active | `model list` |
| POST | `/v1/models` | Upload a model as `text/turtle`, or another RDF serialisation named by `Content-Type`; activates unless `?activate=false`. Answers `202` with a job | `model upload` |
| POST | `/v1/models/{version}/activate` | Activate a stored version. Answers `202` with a job | `model activate` |
| GET | `/v1/models/diff?a=&b=` | Points added, removed or changed between two versions, computed on the uploaded models | `model diff` |
| GET | `/v1/models/{version}` | The model as uploaded, as `text/turtle`; `?inferred=true` adds the inferred graph and `?values=true` the value overlay | `model export` |
| GET | `/v1/jobs/{id}` | The state of a job | followed by the CLI |

Versions are numbered in upload order. Model operations run **one at a
time**; a second one is queued behind the first. A job carries its `id`, the
`operation`, a `state` of `queued`, `running`, `done` or `failed`, the current
`step` — `validating`, `inferring` or `activating` — the times it `started`
and `finished`, and on completion either a `result` or a `problem` in the
error format above. Finished jobs stay readable until the daemon restarts.
The `202` answer carries the job and a `Location` header pointing at it.

A `result` carries the stored `version`, the validation `report` with its
warnings, whether the model was `activated`, the `previous` active version,
the `diff` to it, and for an activation the `activation` summary: how many
triples the model, the ontology and the inferred graph hold and how long it
took. An upload that does not conform stores nothing and fails with a
`422` problem whose `errors` list the violations. An upload in a
serialisation the daemon does not read is refused with `415` before a job
is created.

Export answers in the serialisation the version was uploaded in. With
`?inferred=true` or `?values=true` the answer is Turtle built from the
working graph, which holds these graphs for the active version only; for
another version the request is refused with `409`. `GET /v1/models` lists
every version with its upload time, whether it is active and when it was
activated, together with the prefixes of the active model.

### Entities

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/entities` | The active model as one document: its elements, the relations between them, what the model lacks and what the daemon has made of each point | `model tree` |

The document is what the [model explorer](web.md#model-explorer) draws and
`model tree` prints. It is a projection of the working graph, so it describes
the **active version** only; without one the answer is the empty document with
`version` null.

```json
{
  "version": 3,
  "prefixes": { "brick": "https://brickschema.org/schema/Brick#", "ex": "https://example.com/bldg#" },
  "classes": { "brick:Zone_Air_Temperature_Sensor": ["brick:Air_Temperature_Sensor", "brick:Temperature_Sensor", "brick:Sensor", "brick:Point"] },
  "counts": {
    "entities": 1712, "relations": 3488,
    "kinds": { "location": 94, "equipment": 41, "point": 1575, "system": 0, "other": 2 },
    "findings": { "no_owner": 3, "no_reference": 12, "no_location": 1, "no_relations": 2, "deprecated_class": 96 }
  },
  "entities": [
    {
      "uri": "ex:AHU_01_SAT",
      "kind": "point",
      "class": "brick:Supply_Air_Temperature_Sensor",
      "types": ["brick:Supply_Air_Temperature_Sensor"],
      "grouping": null,
      "name": "AHU 01 supply air temperature",
      "unit": "unit:DEG_C",
      "references": ["ref:BACnetReference"],
      "last_known_value": { "value": 18.4, "time": "2026-09-05T09:12:30Z" },
      "findings": [],
      "runtime": { "accepted": true, "instance": "bacnet_main", "method": "poll",
                   "fallback_active": false, "outcome": "active" },
      "warnings": [],
      "context": false
    }
  ],
  "relations": [
    { "subject": "ex:AHU_01_SAT", "predicate": "brick:isPointOf", "object": "ex:AHU_01",
      "role": "point", "child": "subject" },
    { "subject": "ex:AHU_01", "predicate": "brick:feeds", "object": "ex:VAV_1_17",
      "role": null, "child": null }
  ]
}
```

**The elements** are the nodes the model gives a type to. Reference nodes are
left out — they describe an address, not a thing in the building — and so are
the value overlay's nodes and everything the inference adds. `kind` is
`point`, `equipment`, `location` — a Brick location or a RealEstateCore space
— `system` or `other`, decided with the subclass hierarchy, so a plugin's own
class lands in the right one. `class` is the element's first asserted Brick or
RealEstateCore class and `types` all of its asserted types; `classes` maps
each of those to its ancestors, so a client can filter by class with
subclasses without asking again. `grouping` names the family that makes an
element one that gathers rather than holds — `brick:System`, `rec:Collection`,
`brick:Zone` or `rec:Zone` — and is null for a body. It is what keeps the
element out of the [hierarchy](web.md#model-explorer) and what its band is
headed by, and it is decided here over the subclass hierarchy so that no
client needs to know the vocabulary, as with `role` on the relations below. `name` is the label, else the local part of
the URI, as in the points view. `references` names the types of the point's
external references, `unit` the unit the graph gives it.

**The relations** are the triples the model asserts between two elements, as
written and without the inverses the inference adds. Each says whether it
contains and which end is the contained one, so a client needs no knowledge of
Brick's vocabulary to build the hierarchy:

| Predicate | `role` | `child` |
|-----------|--------|---------|
| `brick:isPartOf`, `rec:isPartOf` | `part` | `subject` |
| `brick:hasPart`, `rec:hasPart`, `rec:includes` | `part` | `object` |
| `brick:hasLocation`, `rec:locatedIn` | `location` | `subject` |
| `brick:isLocationOf`, `rec:isLocationOf` | `location` | `object` |
| `brick:isPointOf`, `rec:isPointOf` | `point` | `subject` |
| `brick:hasPoint`, `rec:hasPoint` | `point` | `object` |
| anything else, `brick:feeds` among them | `null` | `null` |

**The last known value** is read from the working graph's own value overlay —
`brick:lastKnownValue` with its `brick:value` and `brick:timestamp`, which the
daemon keeps in step with the runtime state — so it is the same value the
[SPARQL endpoint](#sparql) and `model export --values` answer with, and an
enumeration or a boolean appears as its text where the source knows one. It is
null for a point that has yet to deliver a valid value and for everything that
is not a point. The time is the observation's own, not the time of the
request: the document is a snapshot, so a value is as fresh as the moment the
document was built.

**The two lenses.** `findings` are what the model lacks, read from the graph
at every request and listed on the [daemon page](daemon.md#model-findings).
`runtime` is what the daemon has made of a point — whether a rule accepted it,
which instance holds it, with which method and to which outcome — and is null
for everything that is not a point; `warnings` carries the codes the daemon
holds against the element, `unclaimed` among them.

**Narrowing.** `kind`, `class` — subclasses included — `finding`, `warning`,
`outcome`, `instance` and `search`, which matches URI and name without regard
to case. `root` limits the document to one element and what sits under it — for a
grouping, which holds nothing in the tree, to what it gathers — and `depth` to
that many levels. The ancestors of a match are kept with `context`
true so the result is still a tree, and `counts` counts the matches, not the
context. An unknown `root` gives `404`, an unknown value for `kind`, `finding`
or `outcome` gives `422`, and a prefix no model declares simply matches
nothing.

### SPARQL

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/sparql?query=` | A read-only query after the SPARQL 1.1 Protocol | `query` |
| POST | `/v1/sparql` | The same, with the query as `application/sparql-query` or form-encoded `query` | `query` |

Queries run against the union of the four graphs of the
[working graph](../architecture.md#the-working-graph). Results come as
`application/sparql-results+json` by default and as CSV, TSV or XML by
`Accept`; CONSTRUCT and DESCRIBE results come as Turtle. Update requests are
refused: the graph is read-only input, and only the daemon writes the
overlay. Because the protocol is the standard one, curl, Yasgui and rdflib's
SPARQL store work against the endpoint unchanged.

The model's prefixes and Brick's are declared for the query, so `brick:` and
the model's own prefix can be used without a `PREFIX` line; a query that
declares them itself is left alone. A query that does not parse, or an
update, is refused with `400`; a body in another encoding than the two above
with `415`.

### Plugins

| Method | Path | Purpose | CLI |
|--------|------|---------|-----|
| GET | `/v1/plugins` | Installed plugins: type, role, version, description, instances, and `error`, the reason a plugin could not be loaded, `null` otherwise | `plugins` |
| GET | `/v1/plugins/{type}` | The declaration: reference types, vocabulary, collection methods with parameter schemas, configuration schema and tools — each with its parameter schema, whether its result is a document and, if so, on which tool's rows it is offered — schemas as JSON Schema. For a plugin that could not be loaded: type, role, version and `error`, without schemas | `plugins <type>` |
| POST | `/v1/plugins/{type}/instances/{name}/tools/{tool}` | Run a tool with its parameters as the JSON body; the structured result is returned unchanged | `sources NAME <tool>` |
| POST | `/v1/plugins/{type}/instances/{name}/start` | Start the instance; likewise `stop` and `restart`. A stop persists across reload and restart | `sources start NAME` |

The API always names the instance; the CLI fills it in when the type has
exactly one.

The list carries each plugin's description and its configured instances; a
plugin that could not be loaded carries the error in place of a description.
A tool answers with the plugin's own result; parameters the tool does not
accept give `422` with the offending keys, a tool on an instance that is
not running `409`, and a tool that fails `500` with the plugin's message.
Start, stop and restart answer with the instance's status entry; an instance
whose plugin could not be loaded answers `409` with the error. An unknown
type, instance, tool or action is `404`.
