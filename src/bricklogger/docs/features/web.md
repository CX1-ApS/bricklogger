# Web

The web interface is the second door into the same functionality as the CLI.
It is served by **`bricklogger serve`**, a process of its own that talks to the
daemon's [HTTP API](api.md) exactly as the CLI does, and it has **full parity**
with the CLI: every command has its screen, and nothing can be done in only one
of them. The one exception is starting the daemon, which needs the CLI's
`daemon start` or a service manager, because the web interface needs a running
daemon to talk to.

## How it is served

- **A thin client over the API.** The browser talks only to `serve`. `serve`
  renders the pages and forwards API calls to the daemon under the same paths,
  `/api/v1/...`, adding the API token itself when the daemon requires one. The
  browser never sees the token, and there is no cross-origin traffic.
- **Server-rendered.** Pages are rendered on the server with FastAPI, Jinja2
  and HTMX, which gives partial updates and forms without a JavaScript build.
  Three libraries ship as static files: CodeMirror for the YAML editor,
  Yasgui for the SPARQL page, which works directly against the SPARQL endpoint
  because it speaks the standard protocol, and Cytoscape.js for the explorer's
  graph. Everything is inside the Python package, so installing Bricklogger
  installs the web interface too.
- **Forms from schemas.** Instance configuration and the parameters of plugin
  tools are pydantic schemas, which the API exposes as JSON Schema. The web
  interface generates its forms from them, as the CLI generates its flags, so
  a new plugin brings its screens with it.
- **Polling.** Status, points, instances and notifications are refreshed
  every five seconds with HTMX, a running job every second. No API surface
  exists for the web interface alone: the forwarded API answers under
  `/api/v1/...` exactly as the daemon does, and a daemon that does not answer
  gives `503` there.
- **Same site only.** Every request that changes something is refused when the
  browser marks it as coming from another site, so a page elsewhere cannot
  drive a logged-in session; reads are unaffected.
- **When the daemon is away.** Every screen says so plainly in its place and
  keeps polling; the status line and the health in the top bar turn to the
  signal colour.

- **A liveness answer.** `serve` answers `GET /health/live` with `200`, its
  process ID and the time it started while it runs, so `bricklogger status`
  can say whether the web interface is up, and since when, without rendering a
  page. It needs no login and tells nothing more.

## Access

`serve` binds to **localhost by default**, on port 8421. When it is
deliberately bound elsewhere, `web.password` must be set — validation refuses
the configuration otherwise — and the user logs in once with it and receives a
session cookie that lasts twelve hours. On a loopback binding no login is
asked for, whether or not a password is set, in line with the API's token
rule. There is one password and no user accounts. TLS is a reverse proxy's
job, as for the API. The settings live in
[`daemon.yaml`](configuration.md#daemonyaml) under `web`, and the flags of
[`serve`](cli.md#serve) override them when it runs on another machine.

## Screens

| Screen | Content | CLI |
|--------|---------|-----|
| Overview | Health, the daemon summary and the warning list with first and last seen, a Clear per warning and one for all | `status`, `status warnings`, `status warnings clear` |
| Points | The paged points view with its filters and the last valid value per point | `points` |
| Sources and destinations | Instances with state, devices and outcomes; start, stop and restart; the plugin's tools as forms with their results; a tool whose result is a document as a download — `<instance>-<tool>[-<values>]-<date>.json` — either from a form of its own or, when the plugin offers it on another tool's rows, as a button on each row of that result and one for the whole document above the rows | `sources`, `destinations` |
| Model | Versions with activation history; upload with the job's progress and the resulting diff; activate, diff and export | `model` |
| Explorer | The active model as a hierarchy of building, floor, room or zone, equipment and points, with sorting, filtering and search; a graph of the selected element's neighbourhood; a detail panel; findings and the runtime state per point in the signal colour | `model tree` |
| Configuration | The four files in a YAML editor, validation before saving with errors shown at their keys, and `init` on an empty directory | `daemon config`, `sources config`, `destinations config`, `rules config` (`show` and `edit`), `init` |
| Plugins | The installed plugins with type, role, version, description and instances, and the error of one that could not load; the newest release beside Bricklogger and each plugin, with an Update per plugin row; a package to install, a Remove per row, and the note that the daemon must be restarted | `plugins`, `plugins add`, `plugins remove`, `update status`, `update <type>` |
| Notifications | Whether they are on, the recipients and the server, the last mail and the last error, when the next summary is due, and what waits in the open window; a Send test mail that reports what the mail server answered | `notify status`, `notify test` |
| Query | SPARQL against the working graph with Yasgui: editor, results table and downloads. `?query=` fills the editor, which is how the explorer hands a query over | `query` |
| Daemon | Reload and stop, with the daemon's version, uptime and directories | `daemon reload`, `daemon stop` |

A job — upload or activation — shows its steps as they happen and ends with
the diff or the error. An instance stopped from the web shows as stopped in
the CLI and the other way round, because both change the same state through
the same API.

**Notifications** have a screen of their own: whether they are on, the
recipients and the server, the last mail and the last error, when the next
summary is due, and what waits in the open window, with a **Send test mail**
button that reports what the mail server answered and keeps the answer on the
screen until the next test. That is `notify status` and `notify test`. The
screen says plainly when notifications are off, and why, so a missing model is
not mistaken for a quiet building. The overview carries nothing about mail: a
mail that could not be sent raises `notify_failed`, which stands in the warning
list there like any other warning. The settings themselves are edited in
`daemon.yaml` on the configuration screen, as every other setting is; what the
mails contain is described on the [notifications page](notifications.md).

**Plugins** has a screen of its own: the catalogue as the daemon sees it, a
field for the packages to install and a Remove on every row. Installing and
removing here is [the same operation as `plugins add` and `plugins remove`](plugins.md#installing-a-plugin),
run by `serve` in **its own process**: the package goes into the environment
`serve` runs from — in a container, the plugin volume it shares with the
daemon — with uv, and the screen waits while uv runs. It can do what the CLI
could do from the same login, and since `serve` runs as the login that
installed Bricklogger, that is everything the CLI can. Pointed at a daemon on another machine with `--api`, the screen
installs on the machine `serve` runs on, which is not where that daemon looks,
so that case too belongs to the CLI beside the daemon. A removal is refused
while an instance of the type is configured, naming the instances, and then
offers to remove anyway, as `--force` does.

Beside Bricklogger and each plugin the screen shows the newest release that
fits, from the daemon's daily [check](cli.md#update), and a Check now looks
it up as `update status` does. A plugin row with a newer release has an
Update, which is `update <type>` run the same way as an install, with the
same validation and the same return to the previous version when it fails.
Bricklogger itself is upgraded from the CLI, with the command the screen
names: `serve` would be replacing the code it runs.

No action here restarts the daemon: the result says that the daemon reads its
plugins when it starts and names the command to restart it, and the catalogue
shows the daemon's set until then. The MCP server has
[no tool for this](mcp.md#tools), on purpose.

## Model explorer

The explorer is where the model is read with the eye. A point list says what
is logged; the explorer says what the building looks like and where each
point sits in it, which is what reveals a point hung on the wrong equipment,
a room no one modelled a location for, or a sensor the source will never
find. It draws the [entity document](api.md#entities), and
[`model tree`](cli.md#model) prints the same hierarchy as text.

- **The active model only.** The explorer reads the working graph, which
  holds the active version with its inference, so a stored version has to be
  activated before it can be explored.
- **The tree.** The document says of every relation whether it contains, and
  which end is the contained one, and the tree follows from that alone:

    1. **Bodies and groupings.** A body holds what is inside it; a grouping
       only gathers what is elsewhere. A grouping is a system, a collection
       or an element whose class descends from `brick:Zone` or `rec:Zone`,
       and the [document](api.md#entities) marks it as one, so that no client
       needs to know Brick's vocabulary to draw the tree. The hierarchy is
       built of bodies alone: a grouping is never a container and never sits
       inside the tree. An HVAC zone is not a step on the way from the
       building to the sensor — it is a cut across it, and a spatial tree has
       no place to put it without breaking the chain.
    2. **One container.** A point sits under the equipment or location it is
       a point of, else where it is located. Equipment sits where it is
       located, else under what it is part of. A location or anything else
       sits under what it is part of, else where it is located. Of several
       candidates the first by URI decides — a last resort that keeps the
       same model drawing the same tree, never a rule the reader should have
       to reason about.
    3. **Three bands.** Locations without a container are the roots of the
       building. After them stand the groupings, **gathered under class
       headings in two levels**: first the family that makes an element a
       grouping — `brick:System`, `rec:Collection`, `brick:Zone` or
       `rec:Zone` — and under it the element's own class, so that
       `brick:System` holds `brick:Ventilation_Air_System` which holds the
       ventilation systems, and one click folds every system away. An
       element whose class is the family itself sits directly under it.
       Brick's classes in between are not levels of their own: the band is
       two deep in every model, however deep a class happens to sit in the
       ontology, and nothing above the families — `brick:Collection`,
       `rec:Resource` and the like — is ever drawn. A heading carries its
       class and how many elements are beneath it, it is there even for a
       single one so that folding is predictable, and headings are ordered
       by class. Last comes the root named `Unplaced`, which holds
       everything else without a container — a point of nothing, equipment
       with neither a location nor a parent, a device — and folds like the
       others, though it is itself a finding worth reading. So a room that
       only a zone gathered stands among the roots, and an air handling unit
       that only a system gathered stands under `Unplaced` beside the
       `no_location` finding it already carried: what the model leaves out is
       shown rather than covered up by a grouping that looked like a place.
       A point whose only owner is a grouping goes there too — a zone's CO2
       sensor is modelled the way Brick intends, and the model still gives it
       no place in the building, which is what `Unplaced` reports. The tree
       says where the model puts a thing, and never more than that.
    4. **Once.** An element appears once. Its other containment relations
       stay relations and are shown in the graph and the detail panel.
    5. **Cycles** are broken at the member first by URI, which becomes
       unplaced.

- **What a grouping gathers.** A grouping's line carries the number of
  elements it gathers, and it opens: in the explorer to a dimmed list of its
  members, in the CLI with `model tree --root <grouping>`, which lists them
  with their own subtrees. That list is a view of the grouping, not a place in
  the building — the members keep their own place in the tree above, the
  dimmed rows count towards nothing, and rule 4 stands. A grouping that
  gathers another grouping shows it among its members like any other element.
  A class heading is not an element of the model and cannot be selected; it
  only folds, and `--root` does not take one.

- **The graph.** The selected element with its neighbours out to a depth of
  one, two or three, over every relation and not only the containing ones, so
  `feeds` and the rest become visible where the tree cannot show them.
  Containment is drawn as a solid edge and everything else dashed, each
  labelled with its predicate; the shape says the kind. A neighbourhood
  larger than three hundred elements is drawn at a smaller depth and says so.
- **Three panes that give way.** The dividers between the hierarchy, the
  graph and the detail panel can be dragged, and the widths are remembered in
  the browser, because how much room a tree needs depends on the model, not on
  us. Dragging is for a pointer; the dividers also answer the arrow keys. On a
  narrower window the panes stack instead and the dividers go away.
- **The detail panel.** The selected element's URI, name, kind, classes,
  unit, reference types, findings with their meaning, every relation in and
  out, and for a point its instance, method, outcome and warnings. A point
  that has delivered carries its **last known value with the time it was
  observed**, so the model can be read against what the building is actually
  saying — a temperature of 19.6 under a sensor confirms the reference reaches
  the right object, and a value from last Tuesday says it no longer does. The
  value is the one the graph holds, texts and all, and it is as fresh as the
  moment the document was loaded; [Points](#screens) is where values refresh
  themselves. From here the point can be opened in [Points](#screens) or
  described in [Query](#screens).
- **Filtering and sorting.** Filters on kind, class — subclasses included, as
  in a rule's selector — finding, warning code, outcome, instance, and a text
  search over URI and name. What matches is shown with its ancestors, which
  stay as dimmed context so the path is never lost. Siblings are sorted by
  name, by class or by the number of points beneath them. The filters live in
  the address, so a filtered view can be kept or handed on.
- **Two lenses on the same tree.** The [findings](daemon.md#model-findings)
  are what the model says about itself, and the runtime state — outcome,
  instance and warnings — is what the daemon has made of it. Both mark their
  element in the signal colour, and both are filters, so "every point without
  a reference" and "every point no instance claims" are one click each.
- **Loaded once.** The document is fetched when the screen opens and on
  **Reload**, not every five seconds like the other screens: it is the size
  of the model and changes only at activation. The status line keeps its own
  cadence, and when it reports another active version the explorer says so
  and offers to load it.

## Visual identity

Bricklogger is part of the CX2 project and follows its visual identity, so
that it looks like the other interfaces on the same edge server and its
screens can be reused there:

- **Paper and ink.** White background, near-black text and lines, a dim grey
  for secondary text and hairlines between table rows. No shadows, gradients
  or filled areas, and no rounded corners beyond small pill badges: the
  interface is built from 1px lines, typography and whitespace.
- **One signal colour.** Orange `#E35D28` marks only what needs attention —
  `degraded` health, a `failed` instance, a warning count, a rejected point.
  It is never the default colour of buttons or links.
- **Type.** IBM Plex Sans for headings and running text; IBM Plex Mono for
  everything the machine says — values, timestamps, status, codes, tags,
  table headers and the wordmark. Numbers are set with tabular figures.
- **Wordmark.** `bricklogger_` in IBM Plex Mono SemiBold, underscore
  included, in the top bar.
- **Layout.** A top bar with the wordmark and the daemon's health; a
  collapsible sidebar with the screens; the content in the middle; and a
  status line at the bottom in terminal style, e.g.
  `> daemon: ok · last observation 12:04:31 · spool 0`. On a phone the sidebar
  becomes bottom tabs.
- **Terminal touches, sparingly.** The `>` prompt in the status line and in
  empty states, monospace status fields, plain tables. No retro effects.
- **Offline by design.** Fonts, HTMX, CodeMirror, Yasgui and Cytoscape.js
  ship inside the package, so the interface works on an edge server without
  internet access. IBM Plex is licensed under the SIL Open Font License.
- **Language.** The texts are English, like the CLI, the codes and the
  documentation, and are kept in one place so that a translation can be added
  later without touching the screens.
