"""The MCP server itself: the tools, resources and prompts an assistant gets,
each mapping to one operation the CLI has too. See ``docs/features/mcp.md``.

The server is built over an :class:`~bricklogger.ops.Operations`, so it works
through the daemon's API when one answers and on the files otherwise, exactly
as the CLI does. Nothing here prints: over stdio the standard output is the
protocol's.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import Field

from bricklogger.mcp.instructions import INSTRUCTIONS
from bricklogger.ops import OperationError, Operations, catalogue, config, docs
from bricklogger.ops import status as status_ops
from bricklogger.ops import tools as tool_ops
from bricklogger.ops.tree import SORTS, render_tree

T = TypeVar("T")

READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
REACHES_OUT = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITES = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
REPLACES = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

RESULTS_JSON = "application/sparql-results+json"

FileName = Annotated[str, Field(description="daemon, sources, destinations or rules")]
Role = Annotated[str, Field(description="source or destination")]
Name = Annotated[str, Field(description="The instance's name, the key in its file")]
Settings = Annotated[
    dict[str, Any] | None,
    Field(description="The plugin's settings by key; a secret only as ${VARIABLE}"),
]


def create_mcp_server(ops: Operations) -> MCPServer:
    """The server over one config directory and the daemon it names."""
    server = MCPServer("Bricklogger", instructions=INSTRUCTIONS)

    def attempt(operation: Callable[[], T]) -> T:
        """Run an operation; what goes wrong is told to the model as a tool error."""
        try:
            return operation()
        except OperationError as exc:
            raise ToolError(exc.describe()) from exc

    def resource(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except OperationError as exc:
            raise MCPError(INVALID_PARAMS, exc.describe()) from exc

    # --- reading -------------------------------------------------------------

    @server.tool(title="Show the configuration", annotations=READS)
    def get_config(
        file: Annotated[
            str | None,
            Field(
                description="daemon, sources, destinations or rules; all four "
                "when left out"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """One of the four configuration files as written, or all four.

        Environment variables are left as they stand, so no secret is shown.
        """

        def read() -> dict[str, Any]:
            with ops.session() as client:
                files = (
                    {file: config.read_file(ops, client, file)}
                    if file
                    else config.read_files(ops, client)
                )
                return {
                    "directory": str(ops.config_dir),
                    "through_daemon": client is not None,
                    "files": files,
                }

        return attempt(read)

    @server.tool(title="Validate the configuration", annotations=READS)
    def validate_config(
        proposed: Annotated[
            dict[str, str] | None,
            Field(
                description="Files to validate in place of those on disk, by "
                "name, as YAML text: the dry run before a write"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Validate the configuration as it stands, or with proposed files.

        Errors name the file, the instance or rule, the key and the message; a
        rule set that accepts points without a destination is a warning.
        """

        def run() -> dict[str, Any]:
            for name in proposed or {}:
                config.file_name(name)
            with ops.session() as client:
                return config.validate(ops, client, proposed)

        return attempt(run)

    @server.tool(title="List the installed plugins", annotations=READS)
    def list_plugins() -> list[dict[str, Any]]:
        """The catalogue: every installed plugin with type, role, version,
        description and its configured instances."""

        def run() -> list[dict[str, Any]]:
            with ops.session() as client:
                return catalogue.list_plugins(ops, client)

        return attempt(run)

    @server.tool(title="Describe a plugin", annotations=READS)
    def describe_plugin(
        type: Annotated[str, Field(description="The plugin type, e.g. bacnet-ip")],
    ) -> dict[str, Any]:
        """One plugin's declaration: its configuration schema with every setting
        described, its reference types, collection methods and protocol tools."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return catalogue.describe(ops, client, type)

        return attempt(run)

    @server.tool(title="Show a file's schema", annotations=READS)
    def get_schema(file: FileName) -> dict[str, Any]:
        """The JSON Schema of one configuration file: daemon and rules from their
        models, sources and destinations with one alternative per installed type."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return catalogue.file_schema(ops, client, file)

        return attempt(run)

    @server.tool(title="Read the documentation", annotations=READS)
    def read_docs(
        page: Annotated[
            str | None,
            Field(
                description="A page, e.g. features/configuration; the index of "
                "pages when left out"
            ),
        ] = None,
    ) -> str:
        """A page of the documentation as Markdown, or the list of pages."""
        return attempt(lambda: docs.page(page) if page else docs.index())

    @server.tool(title="Show the status", annotations=READS)
    def get_status() -> dict[str, Any]:
        """Whether the daemon, the web interface and the MCP server run, and the
        daemon summary when it answers: health, the active model, the point
        counts and every instance with its state."""
        return attempt(lambda: status_ops.probe(ops))

    @server.tool(title="List the warnings", annotations=READS)
    def list_warnings() -> list[dict[str, Any]]:
        """The warning list: code, kind, subject, message, first and last seen
        and count. Needs the running daemon."""

        def run() -> list[dict[str, Any]]:
            with ops.session() as client:
                rows: list[dict[str, Any]] = ops.require(client).get(
                    "/v1/status/warnings"
                )
                return rows

        return attempt(run)

    @server.tool(title="List points", annotations=READS)
    def list_points(
        instance: Annotated[
            str | None, Field(description="Points held by this instance")
        ] = None,
        outcome: Annotated[
            str | None, Field(description="active, unsupported, rejected or pending")
        ] = None,
        warning: Annotated[
            str | None,
            Field(description="Points carrying a warning whose code has this text"),
        ] = None,
        brick_class: Annotated[
            str | None, Field(description="Points whose Brick class has this text")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000, description="Page size")] = 50,
        offset: Annotated[int, Field(ge=0, description="Page start")] = 0,
    ) -> dict[str, Any]:
        """The paged points view: per point its URI, name, class, instance,
        method, outcome, last valid value and warnings. Needs the running daemon."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                page: dict[str, Any] = ops.require(client).get(
                    "/v1/points",
                    instance=instance,
                    outcome=outcome,
                    warning=warning,
                    **{"class": brick_class},
                    limit=limit,
                    offset=offset,
                )
                return page

        return attempt(run)

    @server.tool(title="Show the model as a tree", annotations=READS)
    def model_tree(
        kind: Annotated[
            str | None,
            Field(description="location, equipment, point, system or other"),
        ] = None,
        brick_class: Annotated[
            str | None,
            Field(description="Elements of this class, subclasses included"),
        ] = None,
        finding: Annotated[
            str | None, Field(description="Elements with this finding")
        ] = None,
        warning: Annotated[
            str | None, Field(description="Elements carrying this warning code")
        ] = None,
        outcome: Annotated[
            str | None, Field(description="active, unsupported, rejected or pending")
        ] = None,
        instance: Annotated[
            str | None, Field(description="Points held by this instance")
        ] = None,
        search: Annotated[
            str | None, Field(description="A text in the URI or the name")
        ] = None,
        root: Annotated[
            str | None, Field(description="Only this element and what is under it")
        ] = None,
        depth: Annotated[int | None, Field(ge=0, description="Levels to show")] = None,
        sort: Annotated[
            str, Field(description="Order siblings by name, class or count")
        ] = "name",
    ) -> str:
        """The active model as an indented hierarchy: building, floors, rooms or
        zones, equipment and points, with their prefixed URIs, classes, findings
        and runtime state. The way to find the URIs a rule's selector needs.
        Needs the running daemon."""

        def run() -> str:
            if sort not in SORTS:
                raise OperationError(
                    f"unknown sort {sort!r}; one of {', '.join(SORTS)}"
                )
            with ops.session() as client:
                document = ops.require(client).get(
                    "/v1/entities",
                    kind=kind,
                    **{"class": brick_class},
                    finding=finding,
                    warning=warning,
                    outcome=outcome,
                    instance=instance,
                    search=search,
                    root=root,
                    depth=depth,
                )
            return render_tree(document, sort, root)

        return attempt(run)

    @server.tool(title="Query the model", annotations=READS)
    def query(
        sparql: Annotated[
            str,
            Field(
                description="A read-only SPARQL query; the model's prefixes and "
                "Brick's are declared already"
            ),
        ],
    ) -> dict[str, Any]:
        """Run a SPARQL query against the working graph: the model, Brick's
        ontology, the inferred graph and the last known values. Needs the running
        daemon."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                response = ops.require(client).raw(
                    "POST",
                    "/v1/sparql",
                    content=sparql,
                    content_type="application/sparql-query",
                )
            media = response.headers.get("content-type", "").split(";")[0].strip()
            if media == RESULTS_JSON:
                return {"media_type": media, "results": response.json(), "text": None}
            return {"media_type": media, "results": None, "text": response.text}

        return attempt(run)

    @server.tool(title="Run a protocol tool", annotations=REACHES_OUT)
    def run_tool(
        instance: Name,
        tool: Annotated[
            str,
            Field(description="The tool's name, as describe_plugin lists it"),
        ],
        parameters: Annotated[
            dict[str, Any] | None,
            Field(description="The tool's parameters, as its schema names them"),
        ] = None,
    ) -> dict[str, Any]:
        """Run one of a source instance's protocol tools, such as discovering the
        devices on a network or resolving a point's reference. It reaches out onto
        the instance's network, through the daemon when one runs."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                result = tool_ops.run_instance_tool(
                    ops, client, instance, tool, parameters or {}
                )
            return {"instance": instance, "tool": tool, "result": result}

        return attempt(run)

    # --- writing -------------------------------------------------------------

    @server.tool(title="Write the example configuration", annotations=WRITES)
    def init_config() -> dict[str, Any]:
        """Write the four files with commented examples into an empty config
        directory. Refuses to overwrite a file that exists."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return {"written": config.write_example_files(ops, client)}

        return attempt(run)

    @server.tool(title="Replace a configuration file", annotations=REPLACES)
    def set_config(
        file: FileName,
        text: Annotated[str, Field(description="The whole file as YAML text")],
    ) -> dict[str, Any]:
        """Replace one file with the text given, validated as a whole and refused
        as a whole when it does not hold. Comments and layout are yours to keep."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return config.write_file(ops, client, file, text)

        return attempt(run)

    @server.tool(title="Add a source or destination", annotations=WRITES)
    def add_instance(
        role: Role,
        name: Name,
        type: Annotated[str, Field(description="The plugin type, e.g. bacnet-ip")],
        settings: Settings = None,
    ) -> dict[str, Any]:
        """Write a new instance from its settings, validated by the plugin's
        schema and spliced into the file, so comments and the other instances
        stay. A secret is given only as ${VARIABLE}."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                data, result = config.write_instance(
                    ops, client, role, "add", name, type, settings or {}
                )
            return {
                "file": config.file_of(role),
                "name": name,
                "instance": data,
                "validation": result,
            }

        return attempt(run)

    @server.tool(title="Change a source or destination", annotations=REPLACES)
    def edit_instance(
        role: Role,
        name: Name,
        settings: Annotated[
            dict[str, Any],
            Field(
                description="The settings to change; null removes a key, and a "
                "secret is given only as ${VARIABLE}"
            ),
        ],
        type: Annotated[
            str | None, Field(description="A new plugin type, rarely")
        ] = None,
    ) -> dict[str, Any]:
        """Change the settings given and leave the rest as they are."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                data, result = config.write_instance(
                    ops, client, role, "edit", name, type, settings
                )
            return {
                "file": config.file_of(role),
                "name": name,
                "instance": data,
                "validation": result,
            }

        return attempt(run)

    @server.tool(title="Remove a source or destination", annotations=REPLACES)
    def remove_instance(role: Role, name: Name) -> dict[str, Any]:
        """Remove an instance from its file; a running instance is stopped as the
        new configuration is applied."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                result = config.remove_instance_from_file(ops, client, role, name)
            return {"file": config.file_of(role), "name": name, "validation": result}

        return attempt(run)

    @server.tool(title="Replace the rule set", annotations=REPLACES)
    def set_rules(
        rules: Annotated[
            list[dict[str, Any]],
            Field(
                description="The whole rule set in order, each rule with action, "
                "one of match, match_regex or sparql, and method and interval "
                "for an accept rule"
            ),
        ],
    ) -> dict[str, Any]:
        """Replace the rule set with the rules given, validated by the rule schema
        and written as a fresh file; set_config is the way to keep comments."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return config.write_rules(ops, client, rules)

        return attempt(run)

    @server.tool(title="Reload the daemon", annotations=WRITES)
    def reload_daemon() -> dict[str, Any]:
        """Reload the configuration from disk, for edits made outside the API;
        an invalid one is refused and the running configuration kept."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                result: dict[str, Any] = ops.require(client).post("/v1/daemon/reload")
                return result

        return attempt(run)

    # --- resources -----------------------------------------------------------

    @server.resource(
        "bricklogger://config/{file}",
        mime_type="application/yaml",
        title="A configuration file as written",
    )
    def config_resource(file: str) -> str:
        """One of the four files as written, environment variables left as they
        stand."""

        def run() -> str:
            with ops.session() as client:
                return config.read_file(ops, client, file)

        return resource(run)

    @server.resource(
        "bricklogger://schema/{file}",
        mime_type="application/json",
        title="A configuration file's JSON Schema",
    )
    def schema_resource(file: str) -> dict[str, Any]:
        """The JSON Schema of daemon or rules, or the shape of an instance in
        sources or destinations with one alternative per installed type."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return catalogue.file_schema(ops, client, file)

        return resource(run)

    @server.resource(
        "bricklogger://plugins",
        mime_type="application/json",
        title="The installed plugins",
    )
    def plugins_resource() -> list[dict[str, Any]]:
        """The catalogue: type, role, version, description and instances."""

        def run() -> list[dict[str, Any]]:
            with ops.session() as client:
                return catalogue.list_plugins(ops, client)

        return resource(run)

    @server.resource(
        "bricklogger://plugins/{type}",
        mime_type="application/json",
        title="A plugin's declaration",
    )
    def plugin_resource(type: str) -> dict[str, Any]:
        """One plugin's declaration, as the API gives it."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return catalogue.describe(ops, client, type)

        return resource(run)

    @server.resource(
        "bricklogger://validation",
        mime_type="application/json",
        title="The validation result",
    )
    def validation_resource() -> dict[str, Any]:
        """The validation result for the configuration as it stands."""

        def run() -> dict[str, Any]:
            with ops.session() as client:
                return config.validate(ops, client)

        return resource(run)

    @server.resource(
        "bricklogger://docs", mime_type="text/markdown", title="The documentation"
    )
    def docs_index() -> str:
        """The pages of the documentation, with their titles."""
        return resource(docs.index)

    @server.resource(
        "bricklogger://docs/{+page}",
        mime_type="text/markdown",
        title="A documentation page",
    )
    def docs_page(page: str) -> str:
        """One page as Markdown, e.g. features/configuration."""
        return resource(lambda: docs.page(page))

    # --- prompts -------------------------------------------------------------

    @server.prompt(title="Set up a new machine")
    def setup() -> str:
        """The first configuration of a new machine."""
        return (
            "Set up Bricklogger on this machine with me. Start with get_status and "
            "get_config to see what is there, and list_plugins for the installed "
            "types. If the config directory is empty, write the examples with "
            "init_config, then replace them: ask me which sources and destinations "
            "to configure, and for each instance ask for the settings its schema "
            "requires (describe_plugin), one at a time, offering the defaults. Put "
            "every secret in the env file yourself: write it as ${VARIABLE} and tell "
            "me the variable's name, so I can set it. Write a rule set that logs "
            "what I ask for, validate, and end with what to do next: start the "
            "daemon and upload the model."
        )

    @server.prompt(title="Add a source or destination")
    def new_instance(
        role: Annotated[str, Field(description="source or destination")],
        type: Annotated[str, Field(description="The plugin type, e.g. bacnet-ip")],
    ) -> str:
        """Adding one instance of a given type."""
        return (
            f"Add a {role} of type {type}. Read its settings with describe_plugin "
            "and the documentation page for it with read_docs, then ask me for the "
            "required settings first and the optional ones with their defaults, one "
            "at a time. A secret is written as ${VARIABLE}; tell me the variable's "
            "name and never ask for its value. Validate before you write, write it "
            "with add_instance, and show me the instance as written."
        )

    @server.prompt(title="Nothing arrives")
    def troubleshoot() -> str:
        """Finding out why no data arrives."""
        return (
            "No data seems to arrive. Look at get_status first, then list_warnings, "
            "which lists what stands in the way with a code per cause; the daemon "
            "page of the documentation (read_docs features/daemon) explains every "
            "code. Then list_points with an outcome filter, and the instance's "
            "protocol tools through run_tool: discover the devices, resolve a point "
            "that should deliver. Tell me what you find before you change anything."
        )

    return server
