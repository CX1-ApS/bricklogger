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
- **Metadata arrives whole.** The daemon merges the graph's part and the
  source's part before it offers the entry, so `points` is written from one
  entry; a field the entry does not carry keeps its stored value.

The connection settings are shown in the
[configuration document](configuration.md#sourcesyaml-and-destinationsyaml).
