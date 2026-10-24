# Destinations

Destinations are [plugins](../architecture.md#plugins-sources-and-destinations)
on an equal footing with sources, written and installed as the
[plugins page](plugins.md) describes. Accepted points are written to **all**
configured destinations; there is no routing per rule.

## What a destination receives

A destination receives **observations** — point, timestamp, type and value —
in the closed value-type vocabulary defined in the
[architecture](../architecture.md#observations-and-metadata). It never sees a
protocol and does not know which source an observation came from. The
timestamp is set by the source; the destination stores it as it is. A `null`
observation carries its reason as its value.

In addition it is **offered** point metadata: name, class, equipment,
location, unit and the texts for `enum` and `boolean` points. If the
destination stores metadata, the time-series database can be read on its own
— `2` can be resolved to `Auto` in a dashboard without Bricklogger. How it is
stored is the destination's own choice.

If a destination cannot store metadata — e.g. a purely numeric write-only API
— **it opts out in its declaration**, and the daemon sends only measurements.
Status shows which destinations store metadata.

## The model beside the data

A destination can also store **the Brick model**, so that someone with access
to the database alone can use the model to find the data: select points with
SPARQL, then read their time series with the database's own query language.
A destination **opts in in its declaration**; one that does not receives no
model, and status shows which destinations store it.

It stores **every version that has been active**, with its upload time and
the times it was activated, as Turtle: the model as uploaded with one
addition — every point the destination holds data for carries Brick's
time-series reference:

```turtle
bldg:AHU1_SAT ref:hasExternalReference [
    a ref:TimeseriesReference ;
    ref:hasTimeseriesId "17"
] .
```

- **`ref:hasTimeseriesId` is the destination's own key** for the point's time
  series — for TimescaleDB its `point_id`. Each destination's copy carries its
  own keys, and since the model lies in the database it describes, the
  reference has no `ref:storedAt`.
- **A point has a reference once the destination has met it,** whether or not
  a rule still accepts it: a point that left the plan keeps its history and
  its reference, and a point that was never stored has none.
- **The model follows the plan.** The daemon offers the active version with
  every new plan — at start, on activation and on a configuration reload —
  and every version that has been active once when the instance starts. It
  travels through the spool behind the metadata, so the points it names
  already have their keys, and writing a version again replaces it.

The same references are in Bricklogger's own
[working graph](../architecture.md#the-working-graph), one graph per
destination, and `bricklogger model export --timeseries` adds them to an
export.

## What a destination promises

Observations arrive in **batches** through a persistent spool, and delivery is
**at-least-once**: a batch that was written but not acknowledged can arrive
again. A destination must therefore write **idempotently**. It must also be
able to fail: a write that raises is retried with backoff, and a destination
that cannot start is marked failed in status while the spool keeps filling.
Before each retry the destination is **stopped and started again**, so a
plugin never has to survive a connection the far end has closed — it opens in
`start` and closes in `stop`, and the daemon does the rest.
The contract is described in the
[architecture](../architecture.md#the-destination-contract).

## TimescaleDB

The first version's destination. It is designed to **take up as little space
as possible**: the time-series table carries only the measurements
themselves, and point metadata is never stored per measurement. Rows are keyed
uniquely on point and time, so a repeated batch is harmless.

A `null` observation becomes a row without a value, with the reason as a
**code in a column of its own**, which is NULL in all ordinary rows and
therefore takes up practically no space. The destination keeps no state per
point.

### Preparing the database

The destination needs a database with the `timescaledb` extension and a role
that owns it. [`init`](cli.md#init) sets that up as one of its steps, and
[`init --db`](cli.md#init) runs the step alone. It is the one place where
Bricklogger knows a destination by its type; the destination itself follows
the same contract as any other.

1. **Where.** `init` asks for the host, the port, the database and the role,
   offering `localhost`, `5432`, `bricklogger` and `bricklogger`. The role's
   password is **never asked for**: it is generated and written to the
   [`env` file](configuration.md#the-env-file) under the instance's name,
   `TSDB_PASSWORD` for `tsdb`, with `${TSDB_PASSWORD}` in the YAML. A password
   already in the file that still logs in is kept; otherwise a new one is set
   on the role.
2. **Installing**, when the host is `localhost`. `init` asks whether to
   install PostgreSQL with TimescaleDB on this machine. On a yes it looks
   first: when they are installed already it says that the packages will be
   upgraded and that PostgreSQL's configuration will be tuned again, and asks
   before it goes on. The install adds Timescale's package repository and
   installs PostgreSQL 18 with TimescaleDB, copies `postgresql.conf` beside
   itself as `postgresql.conf.<time>.bak`, runs `timescaledb-tune`, and
   enables and restarts the service. The commands run through `sudo` in the terminal, which asks
   for the password itself; Bricklogger never sees it.
3. **Preparing.** `init` creates the role and the database, owned by the
   role, and creates the extension **as the role**, so the role owns it too
   and the destination can [update it](#timescaledb) itself. On this machine it does so as `postgres` through `sudo`, with
   no password; on another host it asks for an admin login, `postgres` by
   default, with the password without echo, uses it for this step and keeps
   nothing.

`timescaledb-tune` sets PostgreSQL's memory and workers from the machine,
which suits a machine that does little else; `init` says what it changed, and
it is run again with `sudo timescaledb-tune`. The install supports Debian and
Ubuntu. On another distribution, without `sudo` rights or when a command
fails, `init` prints the commands that remain, for an administrator to run,
and goes on.

Every step can be run again: what exists is used as it is, and **no
database, table or row is ever deleted** — not by a replace either, which
writes the destination's settings anew and sets the role's password. Nor
does a database stop the setup: the files are written whatever it answers,
and `init --db` is the way to try again. A database looked after by someone
else needs none of this: they create the database, the extension and the
role as the [getting started](../getting-started.md#prepare-the-database)
page shows, and the instance is written with
[`destinations add`](cli.md#sources-and-destinations) and the password they
give.

In a container the database is the compose file's own and is prepared by its
own container, so the step installs nothing and asks for no admin: the
destination is written from the database variables the container is given,
as the [Docker page](../docker.md#the-first-start) describes.

### Schema

```sql
bricklogger_schema (version integer, applied_at timestamptz)  -- schema version
points             (point_id integer PK, uri text unique, name, class, equipment,
                    location, unit, protocol_unit, value_type, updated_at)
point_states       (point_id, ordinal smallint, text)        -- enum and boolean texts
reasons            (code smallint PK, name text)              -- fault, out_of_service, ...
observations       (time timestamptz, point_id integer,
                    value double precision, reason smallint)  -- hypertable
                    PRIMARY KEY (point_id, time)
observations_text  (time timestamptz, point_id integer,
                    value_text text, value_time timestamptz)  -- hypertable
                    PRIMARY KEY (point_id, time)
models             (version integer PK, uploaded_at timestamptz,
                    document text, written_at timestamptz)    -- Turtle with references
model_activations  (version integer, activated_at timestamptz)
                    PRIMARY KEY (version, activated_at)
```

- **One numeric value column.** `number`, `integer`, `boolean` and `enum` all
  go into `value` as a double: booleans as 0 and 1, enums as their ordinal,
  integers exact up to 2^53. `string` and `datetime` go to the small side
  table `observations_text`. `points.value_type` says what the number means.
- **`null` observations always go to `observations`**, with `value` NULL and
  the reason code set, whatever the point's type. The text table holds values
  only.
- **Point ids are assigned by the destination** when it first meets a URI,
  through metadata or an observation. Because URIs are stable across model
  versions, ids are too. `points` holds the current metadata; its history is
  the daemon's model versions, not the database's job.
- **`reasons` and `point_states` make the database self-describing.** A
  dashboard resolves `2` to `Auto` and a reason code to `fault` with a join,
  without Bricklogger.
- **Compression** is enabled on both hypertables, segmented by point and
  ordered by time, on chunks older than 7 days.
- **Indexes: the primary key only.** Every further index would cost about as
  much as the table itself.
- **No retention policy** in the first version. Raw data stays until someone
  decides otherwise.
- **The destination owns its schema:** it creates the tables at start if they
  are missing and migrates them between its own versions. The version last
  applied is recorded in `bricklogger_schema`.
- **Without the TimescaleDB extension** the destination still works. It tries
  to create the extension at start; if that is not possible it logs a warning
  and writes to plain PostgreSQL tables, without hypertables or compression.
  Installing the extension later does not convert tables that already hold
  data, so start with the extension in place.
- **The destination keeps the extension up to date.** At start it compares
  the version installed in the database with the newest the server offers,
  and when the installed one is older it runs
  `ALTER EXTENSION timescaledb UPDATE` and logs both versions. A new package
  or a new image therefore takes effect at the next start of the daemon,
  without anyone running SQL. An update needs the role to own the extension,
  as [`init`](#preparing-the-database) and the compose file's database
  arrange it; where it does not, the destination logs a warning with the
  command for an administrator and goes on with the version it has.
- **Metadata arrives whole.** The daemon merges the graph's part and the
  source's part before it offers the entry, so `points` is written from one
  entry; a field the entry does not carry keeps its stored value.
- **The model lies beside the data.** `models` holds every version that has
  been active, with a `ref:TimeseriesReference` per point whose
  `ref:hasTimeseriesId` is the point's `point_id`; `model_activations` holds
  when each was activated, and the active version is the one activated last.
  A version written again replaces its row.

Reading the database with the model alone — fetch the active version, query
it, then read the series:

```sql
SELECT m.document
FROM models m JOIN model_activations a USING (version)
ORDER BY a.activated_at DESC
LIMIT 1;
```

```sparql
PREFIX brick: <https://brickschema.org/schema/Brick#>
PREFIX ref: <https://brickschema.org/schema/Brick/ref#>
SELECT ?point ?id WHERE {
  ?point a brick:Supply_Air_Temperature_Sensor ;
         ref:hasExternalReference [ a ref:TimeseriesReference ;
                                    ref:hasTimeseriesId ?id ] .
}
```

```sql
SELECT time, point_id, value
FROM observations
WHERE point_id IN (17, 18) AND time > now() - interval '7 days';
```

The model is stored as uploaded, without the inferred graph, so a query that
should hit subclasses either runs an OWL-RL reasoner over the model and Brick
first or walks `rdfs:subClassOf*` itself.

The connection settings are shown in the
[configuration document](configuration.md#sourcesyaml-and-destinationsyaml).
