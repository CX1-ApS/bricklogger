"""Every text the web interface shows, in one place, so a translation can be
added later without touching the screens. Keys are stable identifiers; values
are English."""

from __future__ import annotations

TEXTS: dict[str, str] = {
    # plugins
    "plugins.title": "Plugins",
    "plugins.type": "Type",
    "plugins.role": "Role",
    "plugins.version": "Version",
    "plugins.description": "Description",
    "plugins.instances": "Instances",
    "plugins.error": "could not load: {error}",
    "plugins.none": "no plugins are installed",
    "plugins.remove": "Remove",
    "plugins.remove.confirm": "Uninstall the distribution that provides {type}?",
    "plugins.remove.force": "Remove anyway",
    "plugins.remove.force.confirm": (
        "Uninstall anyway? The daemon will reject the configuration at its next "
        "start until the instances of {type} are gone."
    ),
    "plugins.add": "Install",
    "plugins.add.label": "Packages",
    "plugins.add.placeholder": "bricklogger-httpjson==0.2.1 ./plugin.whl",
    "plugins.add.help": (
        "Anything uv installs, separated by spaces: a name, name==version, a "
        "wheel on disk or a git URL. The package goes into the environment the "
        "web interface runs from, with its own uv; the page waits while it runs."
    ),
    "plugins.installed": "installed {packages}",
    "plugins.removed": "removed {distribution}{version}, which provided {type}",
    "plugins.removed.others": "with it went {types}, from the same distribution",
    "plugins.removed.dependencies": (
        "with it went {packages}, which nothing else needed"
    ),
    "plugins.refused": "refused: {message}",
    "plugins.newest": "Newest",
    "plugins.update": "Update to {version}",
    "plugins.update.confirm": (
        "Upgrade the plugin that provides {type} to {version}? The new version "
        "validates the configuration first, and the previous one is put back if "
        "it does not hold."
    ),
    "plugins.updated": "updated {moved}",
    "plugins.uptodate": "already up to date",
    "plugins.update.refused": "refused: {message}; the previous versions are back",
    "plugins.core": "Bricklogger {installed}",
    "plugins.core.newer": (
        "Bricklogger {installed}; {newest} is out, and is upgraded from the "
        "command line:"
    ),
    "plugins.core.command": "bricklogger update core",
    "plugins.checked": "newer releases looked for {when}",
    "plugins.unchecked": "newer releases not looked for yet",
    "plugins.check": "Check now",
    "plugins.look.error": "not looked up: {error}",
    "plugins.restart": (
        "The daemon reads its plugins when it starts; restart it to see this:"
    ),
    "plugins.restart.container": "docker compose restart daemon",
    "plugins.restart.host": (
        "bricklogger daemon restart (or: systemctl --user restart bricklogger)"
    ),
    "plugins.restart.mcp": (
        "The MCP server over HTTP reads them the same way; the web interface "
        "needs nothing."
    ),
    # layout
    "wordmark": "bricklogger_",
    "nav.overview": "Overview",
    "nav.points": "Points",
    "nav.instances": "Sources and destinations",
    "nav.model": "Model",
    "nav.explorer": "Explorer",
    "nav.config": "Configuration",
    "nav.plugins": "Plugins",
    "nav.notifications": "Notifications",
    "nav.query": "Query",
    "nav.daemon": "Daemon",
    "nav.collapse": "Collapse the sidebar",
    "nav.expand": "Expand the sidebar",
    "topbar.health": "daemon",
    "topbar.logout": "log out",
    "status.prompt": ">",
    "status.daemon": "daemon",
    "status.points": "points",
    "status.warnings": "warnings",
    # notifications
    "notify.title": "Notifications",
    "notify.state": "state",
    "notify.none": "none",
    "notify.on": "on",
    "notify.off": "off",
    "notify.dormant": "on, but dormant: no model is active",
    "notify.to": "to",
    "notify.server": "server",
    "notify.last": "last mail",
    "notify.never": "never",
    "notify.error": "last error",
    "notify.next": "next summary",
    "notify.waiting": "waiting to be sent",
    "notify.test": "Send test mail",
    "notify.test.sent": "Test mail sent to {to}; the server said: {answer}",
    "notify.test.failed": "The test mail could not be sent: {error}",
    "warnings.kind": "Kind",
    "status.observations": "observations",
    "status.unreachable": "daemon not reachable",
    # unreachable
    "unreachable.title": "The daemon does not answer",
    "unreachable.body": (
        "Nothing answers at {url}. Start the daemon with "
        "`bricklogger daemon start`, or check `api` in daemon.yaml. "
        "This page keeps trying."
    ),
    # login
    "login.title": "Log in",
    "login.password": "Password",
    "login.submit": "Log in",
    "login.wrong": "That is not the password.",
    "login.hint": "The password is web.password in daemon.yaml.",
    # overview
    "overview.title": "Overview",
    "overview.health": "Health",
    "overview.model": "Model",
    "overview.model.none": "no model active",
    "overview.model.version": "version {version}, activated {activated}",
    "overview.points": "Points",
    "overview.points.line": (
        "{accepted} accepted · {assigned} assigned · {active} active · "
        "{unsupported} unsupported · {rejected} rejected"
    ),
    "overview.observations": "Observations received",
    "overview.uptime": "Uptime",
    "overview.warnings": "Warnings",
    "overview.warnings.none": "no warnings",
    "warnings.code": "Code",
    "warnings.subject": "Subject",
    "warnings.message": "Message",
    "warnings.first_seen": "First seen",
    "warnings.count": "Count",
    "warnings.last_seen": "Last seen",
    "warnings.clear": "Clear",
    "warnings.clear_all": "Clear all",
    # points
    "points.title": "Points",
    "points.class": "Class",
    "points.filter.instance": "Instance",
    "points.filter.outcome": "Outcome",
    "points.filter.warning": "Warning",
    "points.filter.class": "Class",
    "points.filter.any": "any",
    "points.filter.apply": "Filter",
    "points.filter.clear": "Clear",
    "points.point": "Point",
    "points.name": "Name",
    "points.instance": "Instance",
    "points.method": "Method",
    "points.outcome": "Outcome",
    "points.last_value": "Last value",
    "points.at": "At",
    "points.warnings": "Warnings",
    "points.fallback": "fallback",
    "points.pending": "pending",
    "points.count": "{total} points, showing {shown} from {offset}",
    "points.none": "no points match",
    "points.previous": "previous",
    "points.next": "next",
    # daemon
    "daemon.title": "Daemon",
    "daemon.version": "Version",
    "daemon.started": "Started",
    "daemon.uptime": "Uptime",
    "daemon.config_dir": "Config directory",
    "daemon.data_dir": "Data directory",
    "daemon.api": "API",
    "daemon.reload": "Reload the configuration",
    "daemon.reload.help": (
        "Reads the four files from disk and applies them if they validate; "
        "an invalid configuration is rejected and the running one kept."
    ),
    "daemon.stop": "Stop the daemon",
    "daemon.stop.help": (
        "A graceful stop within stop_timeout. Starting again needs the CLI "
        "or the service manager."
    ),
    "daemon.stop.confirm": (
        "Stop the daemon? Starting it again needs the CLI or a service manager."
    ),
    "daemon.reloaded": "reloaded",
    "daemon.stopping": "stopping",
    "daemon.rejected": "rejected: {detail}",
    "daemon.warning": "warning: {message}",
    # sources and destinations
    "instances.title": "Sources and destinations",
    "instances.sources": "Sources",
    "instances.destinations": "Destinations",
    "instances.none": "none configured",
    "instances.start": "start",
    "instances.stop": "stop",
    "instances.restart": "restart",
    "instances.by_operator": "stopped by the operator",
    "instances.resources": "Resources",
    "instances.points": "Points",
    "instances.restarts": "Restarts",
    "instances.last_error": "Last error",
    "instances.metadata": "Stores metadata",
    "instances.written": "Written",
    "instances.spool": "Spool",
    "devices.device": "Device",
    "devices.reachable": "Reachable",
    "devices.last_success": "Last success",
    "devices.errors": "Errors",
    "devices.skipped": "Skipped rounds",
    "devices.last_error": "Last error",
    "tools.title": "Protocol tools",
    "tools.help": (
        "Each tool runs on the instance that owns the connection; the fields "
        "come from the plugin's declaration."
    ),
    "tools.run": "Run",
    "tools.download": "Download",
    "tools.empty": "nothing came back",
    "tools.rows": "{count} row(s)",
    # model
    "model.title": "Model",
    "model.upload": "Upload a model",
    "model.upload.file": "Model file",
    "model.upload.activate": "activate after storing",
    "model.upload.submit": "Upload",
    "model.versions": "Versions",
    "model.none": "no model has been uploaded",
    "model.version": "Version",
    "model.uploaded": "Uploaded",
    "model.format": "Format",
    "model.size": "Size",
    "model.active": "active",
    "model.last_activated": "Last activated",
    "model.activate": "activate",
    "model.export": "export",
    "model.export.inferred": "export with inferred graph and values",
    "model.diff": "Diff between two versions",
    "model.diff.a": "From version",
    "model.diff.b": "To version",
    "model.diff.show": "Show the diff",
    "model.diff.change": "Change",
    "model.diff.none": "no point differs between version {a} and {b}",
    "job.line": "{operation} job {id}: {state}",
    "job.stored": "stored version {version} ({size} bytes, {format})",
    "job.activated": (
        "activated version {version} in {seconds} s: {model} model triples, "
        "{inferred} inferred"
    ),
    "job.no_diff": "no point differs from version {previous}",
    "job.first": "no previously active version to compare with",
    # configuration
    "config.title": "Configuration",
    "config.empty": "The config directory is empty.",
    "config.init": "Write the example files",
    "config.missing": "does not exist yet",
    "config.validate": "Validate",
    "config.save": "Save and apply",
    "config.save.confirm": "Write {file}.yaml and apply it to the running daemon?",
    "config.help": (
        "Validation checks the whole directory with this file replaced; "
        "saving writes the text as it is and reloads the daemon."
    ),
    "config.valid": "valid",
    "config.applied": "written and applied",
    "config.invalid": "{count} error(s); nothing written",
    "config.initialised": "the example files were written",
    # explorer
    "explorer.title": "Explorer",
    "explorer.help": (
        "The active model as the building: what contains what, what the model "
        "lacks, and what the daemon has made of each point."
    ),
    "explorer.loading": "loading the model",
    "explorer.reload": "Reload",
    "explorer.version": "version {version}",
    "explorer.version.changed": "version {version} is now active",
    "explorer.no_model": "no model is active",
    "explorer.no_model.hint": "Upload one on the Model screen.",
    "explorer.failed": "the model could not be loaded: {detail}",
    "explorer.divider": "Drag, or use the arrow keys, to resize the panes",
    "explorer.tree": "Hierarchy",
    "explorer.graph": "Neighbourhood",
    "explorer.details": "Element",
    "explorer.counts": (
        "{entities} elements · {relations} relations · {findings} with findings"
    ),
    "explorer.search": "Search",
    "explorer.search.placeholder": "URI or name",
    "explorer.filter.kind": "Kind",
    "explorer.filter.class": "Class",
    "explorer.filter.finding": "Finding",
    "explorer.filter.outcome": "Outcome",
    "explorer.filter.warning": "Warning",
    "explorer.filter.instance": "Instance",
    "explorer.filter.any": "any",
    "explorer.filter.clear": "Clear",
    "explorer.filter.more": "More filters",
    "explorer.sort": "Sort",
    "explorer.sort.name": "name",
    "explorer.sort.class": "class",
    "explorer.sort.count": "points",
    "explorer.expand_all": "Expand",
    "explorer.collapse_all": "Collapse",
    "explorer.expand_capped": "too many to expand at once; expanded as far as it goes",
    "explorer.unplaced": "Unplaced",
    "explorer.empty": "no element matches",
    "explorer.points_beneath": "{count} points",
    "explorer.gathers": "gathers {count}",
    "explorer.beneath": "{count}",
    "explorer.depth": "Depth",
    "explorer.fit": "Fit",
    "explorer.png": "PNG",
    "explorer.centre": "Centre here",
    "explorer.capped": "{total} elements at depth {asked}; showing depth {shown}",
    "explorer.select": "Choose an element in the hierarchy.",
    "explorer.graph.narrow": "the graph needs a wider screen",
    "explorer.graph.label": "neighbourhood of {name}, {count} elements",
    "explorer.uri": "URI",
    "explorer.name": "Name",
    "explorer.kind": "Kind",
    "explorer.class": "Class",
    "explorer.types": "Types",
    "explorer.unit": "Unit",
    "explorer.references": "References",
    "explorer.findings": "Findings",
    "explorer.last_value": "Last known value",
    "explorer.value": "Value",
    "explorer.observed": "Observed",
    "explorer.runtime": "Runtime",
    "explorer.accepted": "accepted, no instance yet",
    "explorer.not_accepted": "no rule accepts this point",
    "explorer.warnings": "Warnings",
    "explorer.relations_out": "Relations out",
    "explorer.relations_in": "Relations in",
    "explorer.copy": "Copy",
    "explorer.copied": "copied",
    "explorer.show_points": "Show in Points",
    "explorer.open_query": "Describe in Query",
    "explorer.kind.location": "location",
    "explorer.kind.equipment": "equipment",
    "explorer.kind.point": "point",
    "explorer.kind.system": "system",
    "explorer.kind.other": "other",
    "explorer.finding.no_owner": (
        "a point of no equipment and of no location, located nowhere"
    ),
    "explorer.finding.no_reference": (
        "no external reference, so no source can address it"
    ),
    "explorer.finding.no_location": (
        "neither it nor anything it is part of has a location"
    ),
    "explorer.finding.no_relations": "tied to nothing else in the model",
    "explorer.finding.deprecated_class": (
        "one of its classes is deprecated in this version of Brick"
    ),
    # query
    "query.title": "Query",
    "query.endpoint": "endpoint",
    "query.help": (
        "Read-only SPARQL against the working graph: model, ontology, inferred "
        "graph and value overlay. The prefixes of the model and Brick are declared."
    ),
    # common
    "common.none": "none",
    "common.yes": "yes",
    "common.no": "no",
    "common.error": "error {status}: {detail}",
    "common.updated": "updated {time}",
    "common.cross_site": "cross-site requests are refused",
}


def text(key: str, **values: object) -> str:
    """A text by key, with ``{placeholders}`` filled in."""
    template = TEXTS.get(key, key)
    return template.format(**values) if values else template
