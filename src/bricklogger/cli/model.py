"""``bricklogger model``: upload, list, activate, diff, export and tree.

Through the API when a daemon answers: upload and activation are jobs, and
the CLI follows them step by step. Without a daemon the commands work on the
data directory: an upload is validated and stored, and the active marker
tells the daemon what to activate at start. Export gives the model as
uploaded; the inferred graph and the values need the daemon's working graph,
and so does ``tree``, which reads the active model as a hierarchy.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from bricklogger.cli.client import ApiClient, format_error, reachable_client
from bricklogger.cli.context import cli_context
from bricklogger.cli.output import fail, print_json, stdout
from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.model.versions import ModelNotFound, ModelStore
from bricklogger.ops.tree import SORTS, render_tree

model_app = typer.Typer(
    no_args_is_help=True,
    help="Upload, list, activate, diff and export models, and show the hierarchy.",
)

JsonFlag = Annotated[
    bool, typer.Option("--json", help="Emit the API's JSON instead of a table.")
]

MEDIA_TYPES = {
    "turtle": "text/turtle",
    "nt": "application/n-triples",
    "xml": "application/rdf+xml",
    "json-ld": "application/ld+json",
    "n3": "text/n3",
    "trig": "application/trig",
    "nquads": "application/n-quads",
}
POLL_INTERVAL = 0.5


def _store(ctx: typer.Context) -> ModelStore:
    context = cli_context(ctx)
    try:
        settings = load_daemon_settings(context.config_dir)
    except ConfigError as exc:
        raise fail(f"daemon.yaml: {exc}") from exc
    return ModelStore(settings.data_dir)


def _follow(client: ApiClient, job: dict[str, Any], quiet: bool) -> dict[str, Any]:
    """Poll a job until it finishes, echoing each step as it starts."""
    last_step: str | None = None
    while job["state"] in ("queued", "running"):
        step = job.get("step")
        if not quiet and step and step != last_step:
            typer.echo(f"{step}...")
            last_step = step
        time.sleep(POLL_INTERVAL)
        job = client.get(f"/v1/jobs/{job['id']}")
    return job


def _result_of(job: dict[str, Any], json_output: bool) -> dict[str, Any]:
    """The result of a finished job; a failed one is reported and ends the command."""
    if json_output:
        print_json(job)
        if job["state"] != "done":
            raise typer.Exit(1)
        return dict(job["result"] or {})
    if job["state"] != "done":
        problem = job.get("problem") or {}
        typer.echo(
            f"failed: {problem.get('detail') or problem.get('title') or job['state']}",
            err=True,
        )
        for entry in problem.get("errors") or []:
            typer.echo("  " + format_error(entry), err=True)
        raise typer.Exit(1)
    return dict(job["result"] or {})


def _echo_activation(result: dict[str, Any]) -> None:
    version = result["version"]["version"]
    activation = result["activation"]
    typer.echo(
        f"activated version {version} in {activation['seconds']} s: "
        f"{activation['model_triples']} model triples, "
        f"{activation['inferred_triples']} inferred"
    )
    for warning in activation.get("warnings", []):
        typer.echo(f"warning: {warning['focus']}: {warning['message']}")
    _echo_diff(result.get("diff"), result.get("previous"))


@model_app.command("upload")
def upload(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(help="The model, e.g. building.ttl.")],
    no_activate: Annotated[
        bool, typer.Option("--no-activate", help="Store without activating.")
    ] = False,
    json_output: JsonFlag = False,
) -> None:
    """Validate the model, store it as a new version and activate it."""
    from rdflib.util import guess_format

    from bricklogger.model import ModelInvalid, upload_model

    context = cli_context(ctx)
    if not file.is_file():
        raise fail(f"{file} does not exist")
    fmt = guess_format(str(file)) or "turtle"
    client = reachable_client(context)
    if client is not None:
        job = client.post(
            "/v1/models",
            content=file.read_bytes(),
            content_type=MEDIA_TYPES.get(fmt, "text/turtle"),
            activate="false" if no_activate else None,
        )
        result = _result_of(_follow(client, job, json_output), json_output)
        if json_output:
            return
        version = result["version"]
        typer.echo(
            f"stored version {version['version']} "
            f"({version['size']} bytes, {version['format']})"
        )
        for warning in result["report"]["warnings"]:
            typer.echo(f"warning: {warning['focus']}: {warning['message']}")
        if result["activated"]:
            _echo_activation(result)
        else:
            typer.echo("stored, not activated (--no-activate)")
        return
    store = _store(ctx)
    try:
        result_offline = upload_model(
            store, file.read_bytes(), fmt, activate=not no_activate
        )
    except ModelInvalid as exc:
        if json_output:
            print_json(
                {"valid": False, "violations": exc.report.as_dict()["violations"]}
            )
        else:
            typer.echo("the model does not conform to Brick's shapes:", err=True)
            for violation in exc.report.violations:
                typer.echo(f"  {violation.focus}: {violation.message}", err=True)
        raise typer.Exit(1) from exc
    except OSError as exc:
        raise fail(str(exc)) from exc
    if json_output:
        print_json(result_offline.as_dict())
        return
    stored = result_offline.version
    typer.echo(f"stored version {stored.number} ({stored.size} bytes, {fmt})")
    for report_warning in result_offline.report.warnings:
        typer.echo(f"warning: {report_warning.focus}: {report_warning.message}")
    if result_offline.activated:
        typer.echo(
            f"marked version {stored.number} active; the daemon activates it at start"
        )
        _echo_diff(
            result_offline.diff.as_dict() if result_offline.diff else None,
            result_offline.previous,
        )
    else:
        typer.echo("not activated (--no-activate)")


@model_app.command("list")
def list_versions(ctx: typer.Context, json_output: JsonFlag = False) -> None:
    """The stored versions and which one is active."""
    context = cli_context(ctx)
    client = reachable_client(context)
    if client is not None:
        data = client.get("/v1/models")
        if json_output:
            print_json(data)
            return
        _echo_versions(data["versions"], data["active"])
        return
    store = _store(ctx)
    active = store.active()
    versions = [version.as_dict() for version in store.versions()]
    if json_output:
        print_json({"active": active, "versions": versions})
        return
    _echo_versions(versions, active)


def _echo_versions(versions: list[dict[str, Any]], active: int | None) -> None:
    if not versions:
        typer.echo("no model has been uploaded")
        return
    table = Table(box=None)
    table.add_column("Version", justify="right")
    table.add_column("Uploaded")
    table.add_column("Format")
    table.add_column("Size", justify="right")
    table.add_column("Active")
    table.add_column("Last activated")
    for version in versions:
        activations = version.get("activations") or []
        table.add_row(
            str(version["version"]),
            str(version["uploaded_at"]),
            str(version["format"]),
            str(version["size"]),
            "active" if version["version"] == active else "",
            str(activations[-1]) if activations else "",
        )
    stdout.print(table)


@model_app.command("activate")
def activate(
    ctx: typer.Context,
    version: Annotated[int, typer.Argument(help="The version number to activate.")],
    json_output: JsonFlag = False,
) -> None:
    """Activate a stored version with upload semantics: validate, swap, re-plan."""
    from bricklogger.model import diff_versions, validate_model

    context = cli_context(ctx)
    client = reachable_client(context)
    if client is not None:
        job = client.post(f"/v1/models/{version}/activate")
        result = _result_of(_follow(client, job, json_output), json_output)
        if not json_output:
            _echo_activation(result)
        return
    store = _store(ctx)
    try:
        stored = store.get(version)
        report = validate_model(store.read(version), stored.format)
    except ModelNotFound as exc:
        raise fail(str(exc)) from exc
    if not report.valid:
        typer.echo("the model does not conform to Brick's shapes:", err=True)
        for violation in report.violations:
            typer.echo(f"  {violation.focus}: {violation.message}", err=True)
        raise typer.Exit(1)
    previous = store.active()
    store.set_active(version)
    if json_output:
        print_json({"version": stored.as_dict(), "activated": False, "marked": True})
        return
    typer.echo(f"marked version {version} active; the daemon activates it at start")
    if previous is not None and previous != version:
        _echo_diff(diff_versions(store, previous, version).as_dict(), previous)


@model_app.command("diff")
def diff(
    ctx: typer.Context,
    a: Annotated[int, typer.Argument(help="The older version.")],
    b: Annotated[int, typer.Argument(help="The newer version.")],
    json_output: JsonFlag = False,
) -> None:
    """Points added, removed or changed between two versions."""
    from bricklogger.model import diff_versions

    context = cli_context(ctx)
    client = reachable_client(context)
    if client is not None:
        data = client.get("/v1/models/diff", a=a, b=b)
    else:
        store = _store(ctx)
        try:
            data = diff_versions(store, a, b).as_dict()
        except ModelNotFound as exc:
            raise fail(str(exc)) from exc
    if json_output:
        print_json(data)
        return
    _echo_diff(data, a)


@model_app.command("export")
def export(
    ctx: typer.Context,
    version: Annotated[
        int | None,
        typer.Option("--version", help="A stored version; default the active one."),
    ] = None,
    inferred: Annotated[
        bool, typer.Option("--inferred", help="Add the inferred graph.")
    ] = False,
    values: Annotated[
        bool, typer.Option("--values", help="Add the value overlay.")
    ] = False,
    timeseries: Annotated[
        bool,
        typer.Option(
            "--timeseries",
            help="Add the time-series references of the destination that stores "
            "the model.",
        ),
    ] = False,
    destination: Annotated[
        str | None,
        typer.Option(
            "--destination",
            help="With --timeseries: the destination instance, when several "
            "store the model.",
        ),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Write to a file.")
    ] = None,
) -> None:
    """The model as uploaded; the inferred graph, the values and the time-series
    references need the daemon."""
    if destination is not None and not timeseries:
        raise fail("--destination goes with --timeseries")
    context = cli_context(ctx)
    client = reachable_client(context)
    if client is not None:
        number = version
        if number is None:
            number = client.get("/v1/models")["active"]
            if number is None:
                raise fail("no model is active; give --version")
        data = client.get_bytes(
            f"/v1/models/{number}",
            inferred="true" if inferred else None,
            values="true" if values else None,
            timeseries=(destination or "true") if timeseries else None,
        )
    else:
        if inferred or values or timeseries:
            raise fail(
                "--inferred, --values and --timeseries need the running daemon's "
                "working graph"
            )
        store = _store(ctx)
        number = version if version is not None else store.active()
        if number is None:
            raise fail("no model is active; give --version")
        try:
            data = store.read(number)
        except ModelNotFound as exc:
            raise fail(str(exc)) from exc
    if output is not None:
        output.write_bytes(data)
        typer.echo(f"wrote version {number} to {output}")
    else:
        typer.echo(data.decode("utf-8", errors="replace"), nl=False)


@model_app.command("tree")
def tree(
    ctx: typer.Context,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="location, equipment, point, system or other."),
    ] = None,
    brick_class: Annotated[
        str | None,
        typer.Option(
            "--class",
            help="Elements of this class, subclasses included, e.g. brick:Sensor.",
        ),
    ] = None,
    finding: Annotated[
        str | None,
        typer.Option(
            "--finding", help="Elements with this finding, e.g. no_reference."
        ),
    ] = None,
    warning: Annotated[
        str | None,
        typer.Option("--warning", help="Elements carrying this warning code."),
    ] = None,
    outcome: Annotated[
        str | None,
        typer.Option("--outcome", help="active, unsupported, rejected or pending."),
    ] = None,
    instance: Annotated[
        str | None, typer.Option("--instance", help="Points held by this instance.")
    ] = None,
    search: Annotated[
        str | None, typer.Option("--search", help="A text in the URI or the name.")
    ] = None,
    root: Annotated[
        str | None,
        typer.Option("--root", help="Only this element and what is under it."),
    ] = None,
    depth: Annotated[
        int | None, typer.Option("--depth", help="How many levels to show.")
    ] = None,
    sort: Annotated[
        str, typer.Option("--sort", help="Order siblings by name, class or count.")
    ] = "name",
    json_output: JsonFlag = False,
) -> None:
    """The active model as a hierarchy, with findings and the runtime per point."""
    if sort not in SORTS:
        raise fail(f"unknown sort {sort!r}; one of {', '.join(SORTS)}")
    client = ApiClient.from_context(cli_context(ctx))
    data = client.get(
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
    if json_output:
        print_json(data)
        return
    _echo_tree(data, sort, root)


def _echo_tree(document: dict[str, Any], sort: str, root: str | None = None) -> None:
    typer.echo(render_tree(document, sort, root))


def _echo_diff(diff: dict[str, Any] | None, previous: int | None) -> None:
    if diff is None or previous is None:
        typer.echo("no previously active version to compare with")
        return
    total = sum(len(items) for items in diff.values())
    if total == 0:
        typer.echo(f"no point differs from version {previous}")
        return
    for kind in ("added", "removed", "changed"):
        for uri in diff[kind]:
            typer.echo(f"{kind:8} {uri}")
