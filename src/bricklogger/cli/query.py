"""``bricklogger query``: a read-only SPARQL query against the working graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from bricklogger.cli.client import ApiClient
from bricklogger.cli.context import cli_context
from bricklogger.cli.output import fail, stdout
from bricklogger.model.prefixes import compact

RESULTS_JSON = "application/sparql-results+json"
FORMATS = {
    "json": RESULTS_JSON,
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xml": "application/sparql-results+xml",
    "turtle": "text/turtle",
}


def query_command(
    ctx: typer.Context,
    query: Annotated[
        str, typer.Argument(help="A SPARQL query, or @FILE to read it from a file.")
    ],
    fmt: Annotated[
        str | None,
        typer.Option("--format", help="Raw output as csv, tsv, xml, json or turtle."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="The SPARQL results JSON unchanged.")
    ] = False,
) -> None:
    """Run a read-only SPARQL query; the model's prefixes and Brick's are declared."""
    if query.startswith("@"):
        path = Path(query[1:])
        if not path.is_file():
            raise fail(f"{path} does not exist")
        text = path.read_text(encoding="utf-8")
    else:
        text = query
    if fmt is not None and fmt not in FORMATS:
        raise fail(f"unknown format {fmt!r}; choose one of {', '.join(FORMATS)}")
    client = ApiClient.from_context(cli_context(ctx))
    accept = FORMATS[fmt] if fmt else (RESULTS_JSON if json_output else None)
    response = client.raw(
        "POST",
        "/v1/sparql",
        content=text,
        content_type="application/sparql-query",
        accept=accept,
    )
    if fmt is not None or json_output:
        typer.echo(response.text, nl=not response.text.endswith("\n"))
        return
    media = response.headers.get("content-type", "").split(";")[0].strip()
    if media != RESULTS_JSON:
        typer.echo(response.text, nl=not response.text.endswith("\n"))
        return
    _echo_results(response.json(), client.prefixes())


def _echo_results(results: dict[str, Any], prefixes: dict[str, str]) -> None:
    if "boolean" in results:
        typer.echo("true" if results["boolean"] else "false")
        return
    variables = results.get("head", {}).get("vars", [])
    bindings = results.get("results", {}).get("bindings", [])
    if not bindings:
        typer.echo("no results")
        return
    table = Table(box=None)
    for variable in variables:
        table.add_column(f"?{variable}", overflow="fold")
    for binding in bindings:
        table.add_row(
            *(_render(binding.get(variable), prefixes) for variable in variables)
        )
    stdout.print(table)
    typer.echo(f"{len(bindings)} result{'' if len(bindings) == 1 else 's'}")


def _render(term: dict[str, Any] | None, prefixes: dict[str, str]) -> str:
    if term is None:
        return ""
    kind = term.get("type")
    value = str(term.get("value", ""))
    if kind == "uri":
        return compact(value, prefixes)
    if kind == "bnode":
        return f"_:{value}"
    return value


def dumps(results: Any) -> str:
    return json.dumps(results, indent=2)
