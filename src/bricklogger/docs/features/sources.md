# Sources

Sources are [plugins](../architecture.md#plugins-sources-and-destinations) —
the built-in ones follow the same contract as [custom plugins](plugins.md). Bricklogger
comes with one, BACnet/IP; other sources are installed as plugins, such as the
[iBOS source](https://github.com/CX1-ApS/bricklogger-ibos) for the iBOS Data
API, and document themselves. Every source declares which [collection methods](configuration.md#collection-method-method)
it supports; `poll` exists for all sources.

## BACnet/IP

The first version's source. Its collection method is **`poll`**: the points
are read at the rule's interval. Change of Value (COV), where the source
subscribes to changes on the device instead of polling, follows in a later
version as a method of its own.

A BACnet read carries no time, so the source **stamps every observation
itself** with the machine's clock when the response is received.

### Read only

Bricklogger **never writes to a BACnet device.** The only services it sends
are Who-Is, ReadProperty and ReadPropertyMultiple. There is no WriteProperty,
no WritePropertyMultiple, no SubscribeCOV, no ReinitializeDevice and no time
synchronisation, neither in the daemon nor in any of the
[protocol tools](#protocol-tools), and the
[source contract](../architecture.md#the-phases-of-the-contract) has no way to
express a command: a source is handed points and returns observations. No rule
can be written that changes anything in the building, because the rule schema
has nothing to write with.

The instance is itself a BACnet device on the network, with a device object
and a network port object, so that it can be found and addressed like any
other. It answers WriteProperty to **its own two objects**, because the
protocol stack it is built on serves that service, and it exposes **no
commandable points**: there is no present value and no priority array anywhere
in it, so nothing in the building can be driven through Bricklogger. What
another device can reach that way are the logger's own settings, such as its
APDU timeout and its retry count.

### Configuration

```yaml
# sources.yaml
bacnet_main:
  type: bacnet-ip
  address: 192.168.10.5/24        # required: local address with prefix length
  device_instance: 1200           # required: the instance's own device object
  device_name: bricklogger-main   # default: the instance name
  vendor_id: 999                  # default: 999 until a vendor identifier is assigned
  devices: ["1000-1999", 2500]    # optional: claim scope by device instance
  subnet: 192.168.10.0/24         # optional: claim scope by the graph's IP
  bbmd:                           # optional: register as a foreign device
    address: 10.0.0.1
    ttl: 60s
  timeout: 3s                     # default
  retries: 2                      # default
  max_in_flight: 8                # default: requests on the wire at once
```

| Key | Requirement | Content |
|-----|-------------|---------|
| `address` | required | The local IPv4 address with prefix length, e.g. `192.168.10.5/24`; the prefix gives the broadcast address. A port other than 47808 is appended: `192.168.10.5/24:47809` |
| `device_instance` | required | The number of the instance's own device object. It must be unique on the BACnet network, so there is no default |
| `device_name` | optional | The name of the instance's own device object; default the instance name |
| `vendor_id` | optional | The vendor identifier the instance's own device object reports. Vendor identifiers are assigned by ASHRAE; the default `999` stands in until Bricklogger has one of its own |
| `devices` | optional | The device instances the instance claims: a list of numbers and ranges written `"low-high"` |
| `subnet` | optional | The instance claims only devices whose IP in the graph lies in the subnet; a device without an IP in the graph is then not claimed |
| `bbmd.address` | optional | A BBMD to register with as a foreign device, for broadcast discovery across routers |
| `bbmd.ttl` | default `60s` | How often the registration is renewed |
| `timeout` | default `3s` | How long a request waits for an answer before a retry |
| `retries` | default `2` | Retries per request before the read counts as failed |
| `max_in_flight` | default `8` | How many requests the instance may have on the wire at once, across every device and every search |

A device is in scope when it satisfies **every** restriction given. Without
`devices` and `subnet` the instance claims every device with a BACnet
reference — the simple installation configures nothing. The exclusive
[resource](../architecture.md#isolation-and-resources) the instance declares
is `udp:<address>:<port>`, so two instances on the same socket are caught at
validation.

### The reference

A point is addressed through Brick's external reference: the point carries
`ref:hasExternalReference` to a reference node, and the source accepts that
node in its **property form**:

```turtle
ex:AHU_01_SAT a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [
        a ref:BACnetReference ;
        bacnet:object-identifier "analog-input,3" ;
        bacnet:objectOf ex:AHU_01_Controller ;
        ref:read-property "present-value"      # optional; this is the default
    ] .

ex:AHU_01_Controller a bacnet:BACnetDevice ;
    bacnet:device-instance 1201 ;
    bacnet:hasPort [
        a bacnet:Port ;
        bacnet:ip-address "C0A80A14"^^xsd:hexBinary   # optional; 192.168.10.20
    ] .
```

- **The object:** `bacnet:object-identifier` names it as `type,instance` with
  the standard's hyphenated type names, e.g. `analog-input,3` or
  `multi-state-value,12`. The literal may be plain or typed
  `bacnet:objectIdentifier`. The source recognises a BACnet reference by this
  property, whether or not the node is typed `ref:BACnetReference` — Brick's
  own example leaves the type to inference.
- **The device:** reached through `bacnet:objectOf` or `bacnet:contains`;
  Brick's example uses the first and its shape the second, so the source
  accepts both. Two BACnet vocabularies occur in models,
  `http://data.ashrae.org/bacnet/2020#` in the bundled Brick and
  `http://data.ashrae.org/bacnet/` in Brick's current schema, and the source
  accepts both. The device node must carry `bacnet:device-instance`. A port
  with `bacnet:ip-address` is optional and is used as a hint when
  [finding the device](#finding-the-device).
- **The property:** `ref:read-property` names the property to read and
  defaults to Present_Value. Any readable property can be named, e.g.
  `reliability` or `out-of-service`, in the standard's hyphenated spelling or
  as the ASHRAE vocabulary's IRI — both forms occur in models. Array elements
  cannot be addressed in the first version.
- `bacnet:object-name`, `bacnet:description` and `bacnet:units` on the
  reference are informational and play no part in addressing.

The source reads the reference, the device and its port from the graph itself
with the [read-only SPARQL access](../architecture.md#graph-access-for-sources)
every source has; the daemon hands over nothing about references.

**What is rejected.** A reference the source recognises but cannot use gives
the point the outcome `rejected` with a reason that names the problem, and the
point is not collected until the model is fixed. That covers the URI form
(`ref:BACnetURI`), a reference with only an object name, a malformed object
identifier, a device node without `bacnet:device-instance`, a
`bacnet:object-type` that contradicts the type in the identifier, an unknown
property name, and a point with two BACnet references of which neither is
marked `ref:preferred true` — where one is marked, it is used. The rule is
one: a reference that is not complete and unambiguous is not read, because a
point logged from the wrong object under the right name is worse than a
visible gap.

### Finding the device

The source finds a device on the network **by its device instance** with
Who-Is; an address from the graph is a hint, never the truth:

- If the graph carries an IP for the device, a **unicast Who-Is** goes to that
  address first. The I-Am confirms that the instance lives there, no
  broadcast is needed, and it works across subnets.
- Otherwise — or if the hinted address does not answer — a **broadcast
  Who-Is** for that instance goes out on the local network, or through the
  BBMD in the instance's configuration. An instance bound to a single address
  without a network, a `/32`, has no broadcast address and reaches only
  devices the graph gives an IP.
- The address that answers is **cached** and looked up again when the device
  stops answering, so a device that is re-addressed comes back by itself.
  Devices behind BACnet routers are reached through the routed address their
  I-Am carries.

### Polling

The source owns the rhythm, and what it puts on the network is bounded. A
logger on a building's own network must never be the reason something else
stops answering:

- Points on the same device with the same interval are read together in one
  **ReadPropertyMultiple**, so they share one request and one timestamp. A
  device that does not support it is read with ReadProperty, one property at a
  time.
- **One outstanding request per device and interval.** Within a round the
  requests follow one another; a device whose points are accepted at two
  different intervals is polled by two rounds, which can overlap.
- **At most `max_in_flight` requests are on the wire at once**, counted per
  instance and whatever the number of devices; the default is **8**.
  Everything the instance sends passes that ceiling, the searches included, so
  a network never sees more of Bricklogger's requests at a time than the
  number configured. It is the setting to lower on a fragile network or one
  where everything sits behind a single router.
- **First rounds are spread** over the interval, at most a minute, so a rule
  with hundreds of devices does not start them all in the same instant. Each
  device keeps its offset afterwards, so the load stays even instead of
  gathering at every interval boundary.
- **Reads never pile up.** If a device's round is still running when the next
  is due, the next round is skipped. Skipped rounds are counted per device in
  status, and a device that keeps missing its interval gets the warning
  `poll_overrun`. The interval is thus a minimum, and every observation carries
  the time it was actually received, so a device that cannot keep up is
  visible both in status and in the time series.

### Value types and units

The value type follows the **datatype of the property as read**, not the
object type alone, because `ref:read-property` can name any property. For a
standard property the datatype is known from the standard before the first
read; for a proprietary property it is the datatype of the first successful
read.

| BACnet datatype | Value type | Texts and notes |
|-----------------|------------|-----------------|
| REAL, DOUBLE | `number` | Analog objects, Large_Analog_Value, Loop. NaN and infinity become `null` with reason `no_value` |
| Unsigned, INTEGER | `integer` | Integer_Value, Positive_Integer_Value, Accumulator |
| BOOLEAN | `boolean` | e.g. Out_Of_Service |
| Present_Value of a binary object | `boolean` | `active` is true; texts from Active_Text and Inactive_Text when the object has them |
| Present_Value of a multi-state object | `enum` | The ordinal is BACnet's own value, counting from 1; texts from State_Text |
| ENUMERATED elsewhere | `enum` | e.g. Reliability; the ordinal is the enumeration value, the texts are the standard's names |
| CharacterString | `string` | |
| DateTime | `datetime` | BACnet's DateTime carries no zone; it is read as the machine's local time and stored in UTC, since device and logger share a building |
| BitString, OctetString, ObjectIdentifier, Date or Time alone, lists and structures | — | The point is `rejected`: the datatype has no counterpart in the vocabulary |

Bit strings are not a type, and with the property form a single bit cannot be
addressed, so there is nothing to decompose them into. A status word that is
wanted as a time series is modelled as its own boolean points where the device
offers them.

**Units.** The protocol's unit is the object's Units property, one of
BACnet's engineering units. The source translates it into Brick's unit
vocabulary (QUDT) where the standard's unit has a counterpart —
`degrees-celsius` to `unit:DEG_C`, `kilowatt-hours` to `unit:KiloW-HR` and so
on — and otherwise delivers the BACnet name itself, e.g. a proprietary unit as
`proprietary-<number>`. `no-units`, and objects without a Units property, give
no protocol unit. As for every source, the graph's unit stays primary, a
disagreement gives the `unit_conflict` warning, and values are never
converted.

**When metadata is read.** Units, State_Text, Active_Text and Inactive_Text
are read once when a point is assigned and delivered as the point's metadata.
For a point that reads another property than Present_Value, the value type and
the enumeration's texts follow the first successful read. Metadata is read
again whenever the device has been rediscovered after being unreachable, since
a device may have been reconfigured while it was gone.

### Status flags

Every BACnet object carries four status flags. When Present_Value is read,
the source maps them to observations as follows; a rule that reads another
property, such as Reliability, gets that property's value whatever the flags
say, because logging the status is what such a point is for:

| Flag | Observation |
|------|-------------|
| IN_ALARM | The value is delivered unchanged |
| FAULT | `null` with reason `fault` |
| OUT_OF_SERVICE | `null` with reason `out_of_service` |
| OVERRIDDEN | `null` with reason `overridden` |

The status flags are read together with the value. If the status itself is to
be logged as a time series, it is modelled as its own point in the graph:
Brick's BACnet reference can, with `ref:read-property`, point at e.g.
Reliability or Out_Of_Service instead of Present_Value.

### Failed reads

If the device does not answer — timeout or offline — every poll attempt
yields `null` with reason `unreachable`, and so does every attempt before the
device has been found on the network. If it answers with an error — unknown
object, unknown property, access denied — the attempt yields `null` with
reason `read_error`.

A device that has been lost is looked for again with a Who-Is, but **not on
every round**. The search backs off: the first retry after 30 seconds, then
doubling to at most 5 minutes, and the count resets the moment the device
answers. A search that finds nothing at the address the model gives has to
**broadcast**, which every device on the network must process and which a
BBMD forwards to other subnets, so a controller switched off for a working day
must not become a broadcast every few seconds. The points keep yielding
`unreachable` at their own interval while the search waits, so status and the
time series are unchanged by the backoff; only the searching is.

### Protocol tools

The tools run with `bricklogger sources bacnet-ip <tool>` — through the daemon
when it runs, in-process otherwise — as described for
[protocol tools](../architecture.md#protocol-tools) in general. Every tool
returns a structured result that the CLI renders as a table or emits as JSON.

| Tool | Parameters | Result |
|------|------------|--------|
| `discover` | `--low` and `--high` bound the device instances asked for (default: all); `--duration` is the listening time (default `3s`); `--address` sends the Who-Is to one address instead of broadcasting, for an interface without a broadcast address or a device or BBMD on another network | The devices that answered: instance, address, name, vendor and model |
| `read` | `--device` as instance number or address, `--object` identifier, `--property` (default `present-value`), `--index` for an array element | The value as the vocabulary sees it, BACnet's own datatype and the status flags; an error the device answers with is returned as `error` |
| `objects` | `--device`; `--values` adds Present_Value and Units | The device's object list: identifier, name and type, optionally value, unit and its QUDT counterpart |
| `resolve` | `--point`, a URI in full or prefixed form | How the point's reference resolves — device instance, the address found, object and property, whether the device is in the instance's scope — and the value with its status flags, or the problem with the reference |
| `pointlist` | `--device` for one device, in scope or not; `--low` and `--high` bound the search as for `discover`; `--values` adds each object's Present_Value | The point list: the devices the instance claims, or the one device, with the objects on each, as one JSON document to keep as a file |

`discover`, `read`, `objects` and `pointlist` need nothing but the instance's
configuration, so they also run without a daemon; a device given by instance
number is found with a Who-Is, so on an interface that cannot broadcast it
is given by address. `resolve` needs the running
daemon, because the working graph is the daemon's; without one the command
says so plainly, like `status` and `points`. It is the shortest path from a
warning in status to its cause.

No tool writes to a device. The first version reads only.

#### The point list

The point list is what the network has, without a single sample: the devices in
the instance's scope and the objects on each — an inventory to file with the
building's documentation, or to hand to whoever builds the Brick model. On a
BACnet site there is rarely another one. It is a
[document](../architecture.md#protocol-tools), so the CLI writes it as JSON, to
standard output or to a file with `-o`:

```
bricklogger sources bacnet_main pointlist -o building-a.json
```

In the web interface it is offered on the `discover` listing: run `discover`,
and every device row carries a Download for that device's point list, with a
Download for the whole list above the table. The tool has no form of its own
there.

Without arguments the list covers **the devices the instance claims**, so the
Who-Is is bounded by the `devices` ranges and the answers are filtered by
`devices` and `subnet` both. `--low` and `--high` widen or narrow the search
deliberately, as they do for `discover`, and `--device` covers one device
whether or not the instance claims it. `--values` adds each object's
Present_Value, which costs one more reading per object.

```json
{
  "instance": "bacnet_main",
  "address": "192.168.10.5/24:47808",
  "exported_at": "2026-09-15T13:05:12+00:00",
  "counts": {"devices": 1, "objects": 2},
  "devices": [
    {
      "instance": 1201,
      "address": "192.168.10.20",
      "name": "AHU-01 controller",
      "vendor": "Siemens",
      "model": "PXC36.1-E.D",
      "objects": [
        {"type": "analog-input", "instance": 3, "name": "AHU01_SAT", "unit": "degreesCelsius", "qudt": "http://qudt.org/vocab/unit/DEG_C"},
        {"type": "binary-input", "instance": 1, "name": "AHU01_FAN", "unit": null, "qudt": null}
      ]
    }
  ]
}
```

The header names the instance, its own address, the time of the export in UTC
and the counts; then follow the devices, each with its objects. The fields are
those of `discover` and `objects`, so the document reads as the two lists
nested. A device that stops answering halfway through is kept with the objects
it managed to give and an `error` beside them, because half a site's inventory
is worth more than none.

**It asks one thing at a time.** The sweep is one Who-Is, then per device one
read of the object list and one ReadPropertyMultiple per twenty objects, with
never more than a single request outstanding. A whole site therefore takes a
while and puts no more on the network at any instant than one poll round does,
which is why it needs no ceiling of its own.

## Which points the source serves

A source instance **claims** the points whose external references it can
reach. It finds the references of its type in the Brick graph itself, through
the [read-only SPARQL access](../architecture.md#graph-access-for-sources)
every source has, and decides the claim from its own configuration —
address, subnet, device range — without touching the network, so the
binding can be shown and validated before collection starts. If there is no
network restriction in the configuration, the instance claims all points with
a reference of its type. If several instances claim the same accepted point,
that is a validation error; if none does, the point is not collected and
status warns. The source may query the graph at any time, and every model
activation binds it anew, so it resolves its references again against the new
model. The mechanics and the arbitration are described in the
[architecture](../architecture.md#which-source-owns-which-point).

## What a source delivers

A source pushes **observations** up into the daemon — point, timestamp, type
and value. **The timestamp belongs to the source:** the daemon never stamps
anything itself. If the protocol carries no time, the source stamps with the
machine's clock when the response is received; if the source receives
timestamped samples, it passes the sample's timestamp on. Timestamps are
always timezone-aware and in UTC, and a plugin that passes on device time
must say so in its documentation.

A source only delivers a value it can **vouch for** as the point's value. If
the device itself disputes the value, or the read fails, the source delivers
`null` with a reason from the closed reason vocabulary in the
[architecture](../architecture.md#observations-and-metadata). Every poll
attempt of a **live** source thereby yields exactly one observation. A
**history source**, which copies samples the system has already recorded,
delivers nothing for a failed request and fetches the stretch when the next
one succeeds; the [iBOS source](https://github.com/CX1-ApS/bricklogger-ibos) is one.

In addition the source delivers **point metadata** for what only the protocol
knows: the value type, the state texts for `enum`, the texts for `boolean`
and the protocol's own unit designation — translated into Brick's unit
vocabulary (QUDT) where possible, otherwise as the protocol's own
designation. The source never converts values. Both vocabularies are closed
and defined in the
[architecture](../architecture.md#observations-and-metadata); a source cannot
add its own types or fields, but must map its protocol's values into the set.

For every assigned point the source reports an outcome on the status channel —
`active`, `unsupported` or `rejected` with a reason — initially and whenever
it changes. The daemon applies the rule's `fallback` on `unsupported`.

The source never talks to the destinations. It knows neither their number nor
their kind.
