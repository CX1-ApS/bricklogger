"""The daemon's HTTP API.

Every operation lives under ``/v1`` except the two health endpoints; errors
are ``application/problem+json``; a token is required when the API is bound
beyond localhost. See ``docs/features/api.md``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from bricklogger.config import CONFIG_FILES, FilesExist, parse_text, write_examples
from bricklogger.config.schema import is_loopback
from bricklogger.config.validation import ValidationResult
from bricklogger.daemon.core import (
    Daemon,
    ExportUnavailable,
    InstanceNotRunning,
    NotFound,
    ToolFailed,
)
from bricklogger.daemon.jobs import Job
from bricklogger.daemon.sparql import QueryError
from bricklogger.model import ModelNotFound, ModelUnreadable
from bricklogger.ops.status import live_answer

PROBLEM = "application/problem+json"
YAML = "application/yaml"

RDF_MEDIA_TYPES = {
    "text/turtle": "turtle",
    "application/n-triples": "nt",
    "application/rdf+xml": "xml",
    "application/ld+json": "json-ld",
    "text/n3": "n3",
    "application/trig": "trig",
    "application/n-quads": "nquads",
}
MEDIA_TYPE_OF = {fmt: media for media, fmt in RDF_MEDIA_TYPES.items()}


def problem(
    status: int, title: str, detail: str | None = None, **extra: Any
) -> JSONResponse:
    """An RFC 9457 problem document."""
    body: dict[str, Any] = {
        "type": f"urn:bricklogger:problem:{title.lower().replace(' ', '-')}",
        "title": title,
        "status": status,
    }
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def validation_problem(result: ValidationResult) -> JSONResponse:
    count = len(result.errors)
    return problem(
        422,
        "Configuration is invalid",
        f"{count} error{'' if count == 1 else 's'}",
        errors=[issue.as_dict() for issue in result.errors],
    )


def job_response(job: Job) -> JSONResponse:
    """A 202 with the job and where to follow it."""
    return JSONResponse(
        job.as_dict(), status_code=202, headers={"Location": f"/v1/jobs/{job.id}"}
    )


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def config_file_name(file: str) -> str:
    if file not in CONFIG_FILES:
        raise HTTPException(
            404,
            f"unknown configuration file {file!r}; one of {', '.join(CONFIG_FILES)}",
        )
    return file


def create_app(
    daemon: Daemon, request_stop: Callable[[], None] | None = None
) -> FastAPI:
    """The API over a started daemon; ``request_stop`` ends the serving process."""
    app = FastAPI(
        title="Bricklogger", version=daemon.status()["version"], docs_url=None
    )
    settings = daemon.configuration.daemon.api if daemon.configuration else None
    token = settings.token if settings and not is_loopback(settings.host) else None

    @app.middleware("http")
    async def require_token(request: Request, call_next: Callable[..., Any]) -> Any:
        if token is not None and not request.url.path.startswith("/health"):
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return problem(401, "Unauthorized", "a valid bearer token is required")
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "key": ".".join(str(part) for part in error["loc"]),
                "message": str(error["msg"]),
            }
            for error in exc.errors()
        ]
        count = len(errors)
        return problem(
            422,
            "Request is invalid",
            f"{count} error{'' if count == 1 else 's'} in the request",
            errors=errors,
        )

    # --- health and daemon ---------------------------------------------------

    @app.get("/health/live")
    def live() -> dict[str, Any]:
        return live_answer(daemon.started_at)

    @app.get("/health")
    def health() -> JSONResponse:
        state = daemon.health()
        return JSONResponse(
            {"health": state}, status_code=503 if state == "degraded" else 200
        )

    @app.post("/v1/daemon/reload")
    def reload() -> JSONResponse:
        result = daemon.reload()
        if not result.valid:
            return validation_problem(result)
        return JSONResponse(result.as_dict())

    @app.post("/v1/daemon/stop")
    def stop() -> dict[str, str]:
        if request_stop is None:
            raise HTTPException(503, "this daemon cannot be stopped through the API")
        request_stop()
        return {"stopping": "ok"}

    # --- status and points ---------------------------------------------------

    @app.get("/v1/status")
    def status() -> dict[str, Any]:
        return daemon.status()

    @app.get("/v1/status/sources")
    def status_sources() -> list[dict[str, Any]]:
        return daemon.status_sources()

    @app.get("/v1/status/destinations")
    def status_destinations() -> list[dict[str, Any]]:
        return daemon.status_destinations()

    @app.get("/v1/status/warnings")
    def status_warnings() -> list[dict[str, Any]]:
        return daemon.status_warnings()

    @app.delete("/v1/status/warnings")
    def clear_warnings(
        code: str | None = None, subject: str | None = None
    ) -> dict[str, int]:
        """Clear warnings by hand: all, those with a code, or one with a subject."""
        return {"cleared": daemon.clear_warnings(code, subject)}

    # --- notifications -------------------------------------------------------

    @app.get("/v1/notifications")
    def notifications() -> dict[str, Any]:
        return daemon.notification_status()

    @app.post("/v1/notifications/test")
    def notifications_test() -> JSONResponse:
        """Send a test mail; the one operation here that leaves the machine."""
        result = daemon.send_test_notification()
        if result["sent"]:
            return JSONResponse(result)
        return problem(
            502,
            "The test mail could not be sent",
            str(result.get("error", "")),
            to=result.get("to", []),
        )

    @app.get("/v1/points")
    def points(
        instance: str | None = None,
        outcome: str | None = None,
        warning: str | None = None,
        brick_class: str | None = Query(None, alias="class"),
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return daemon.points(
            instance=instance,
            outcome=outcome,
            warning=warning,
            brick_class=brick_class,
            limit=limit,
            offset=offset,
        )

    @app.get("/v1/entities")
    def entities(
        kind: Literal["location", "equipment", "point", "system", "other"]
        | None = None,
        brick_class: str | None = Query(None, alias="class"),
        finding: Literal[
            "no_owner",
            "no_reference",
            "no_location",
            "no_relations",
            "deprecated_class",
        ]
        | None = None,
        warning: str | None = None,
        outcome: Literal["active", "unsupported", "rejected", "pending"] | None = None,
        instance: str | None = None,
        search: str | None = None,
        root: str | None = None,
        depth: int | None = Query(None, ge=0),
    ) -> Response:
        """The active model as one document; see docs/features/api.md."""
        try:
            return JSONResponse(
                daemon.entities(
                    kind=kind,
                    brick_class=brick_class,
                    finding=finding,
                    warning=warning,
                    outcome=outcome,
                    instance=instance,
                    search=search,
                    root=root,
                    depth=depth,
                )
            )
        except NotFound as exc:
            return problem(404, "Entity not found", str(exc))

    # --- configuration -------------------------------------------------------

    @app.get("/v1/config")
    def config_all() -> JSONResponse:
        """All four files as one document, environment variables left as written."""
        texts = daemon.config_texts()
        data: dict[str, Any] = {}
        issues = []
        for name in CONFIG_FILES:
            parsed, file_issues = parse_text(name, texts[name])
            data[name] = parsed
            issues.extend(file_issues)
        if issues:
            return problem(
                422,
                "Configuration is not readable",
                str(issues[0]),
                errors=[issue.as_dict() for issue in issues],
            )
        return JSONResponse(data)

    @app.get("/v1/config/{file}")
    def config_one(file: str, request: Request) -> Response:
        """One file as YAML text, or parsed when JSON is asked for."""
        name = config_file_name(file)
        text = daemon.config_texts()[name]
        if wants_json(request):
            parsed, issues = parse_text(name, text)
            if issues:
                return problem(
                    422,
                    "Configuration is not readable",
                    str(issues[0]),
                    errors=[issue.as_dict() for issue in issues],
                )
            return JSONResponse(parsed)
        return Response(text or "", media_type=YAML)

    @app.put("/v1/config/{file}")
    async def config_replace(file: str, request: Request) -> JSONResponse:
        """Replace one file with the text in the body; validated first, then applied."""
        name = config_file_name(file)
        try:
            text = (await request.body()).decode("utf-8")
        except UnicodeDecodeError:
            return problem(400, "Body is not UTF-8")
        result = await run_in_threadpool(daemon.replace_config_file, name, text)
        if not result.valid:
            return validation_problem(result)
        return JSONResponse(result.as_dict())

    @app.post("/v1/config/validate")
    async def config_validate(request: Request) -> JSONResponse:
        """Validate the files on disk, or a proposed set given as {file: text}."""
        body = await request.body()
        texts: dict[str, str | None] | None = None
        if body.strip():
            try:
                proposed = json.loads(body)
            except ValueError:
                return problem(400, "Body is not JSON")
            if not isinstance(proposed, dict) or any(
                key not in CONFIG_FILES or not (value is None or isinstance(value, str))
                for key, value in proposed.items()
            ):
                return problem(
                    422,
                    "Proposed files are malformed",
                    "an object mapping daemon, sources, destinations or rules "
                    "to YAML text is expected",
                )
            texts = proposed
        result = await run_in_threadpool(daemon.validate_config, texts)
        return JSONResponse(result.as_dict())

    @app.post("/v1/config/init")
    def config_init() -> JSONResponse:
        """Write the example files; refused when any of them exists."""
        try:
            written = write_examples(daemon.config_dir)
        except FilesExist as exc:
            return problem(409, "Configuration exists", str(exc))
        return JSONResponse({"written": [str(path) for path in written]})

    # --- sparql --------------------------------------------------------------

    def run_sparql(query: str, request: Request) -> Response:
        try:
            data, media = daemon.sparql(query, request.headers.get("accept", ""))
        except QueryError as exc:
            return problem(400, "Query is invalid", str(exc))
        return Response(data, media_type=media)

    @app.get("/v1/sparql")
    def sparql_get(request: Request, query: str = Query(...)) -> Response:
        return run_sparql(query, request)

    @app.post("/v1/sparql")
    async def sparql_post(request: Request) -> Response:
        """The query as application/sparql-query, or form-encoded in ``query``."""
        header = request.headers.get("content-type", "")
        media = header.split(";")[0].strip().lower()
        body = await request.body()
        if media == "application/sparql-query":
            query: str | None = body.decode("utf-8", errors="replace")
        elif media == "application/x-www-form-urlencoded":
            form = parse_qs(body.decode("utf-8", errors="replace"))
            values = form.get("query") or []
            query = values[0] if values else None
        else:
            return problem(
                415,
                "Unsupported query encoding",
                "application/sparql-query or a form-encoded query is expected",
            )
        if not query or not query.strip():
            return problem(400, "Query is missing")
        return await run_in_threadpool(run_sparql, query, request)

    # --- plugins -------------------------------------------------------------

    @app.get("/v1/plugins")
    def plugins() -> list[dict[str, Any]]:
        return daemon.plugins()

    @app.get("/v1/plugins/{type_name}")
    def plugin(type_name: str) -> JSONResponse:
        try:
            return JSONResponse(daemon.plugin(type_name))
        except NotFound as exc:
            return problem(404, "Plugin not found", str(exc))

    @app.post("/v1/plugins/{type_name}/instances/{name}/tools/{tool}")
    async def run_tool(
        type_name: str, name: str, tool: str, request: Request
    ) -> JSONResponse:
        """Run a tool on the instance; the parameters are the JSON body."""
        body = await request.body()
        parameters: Any = {}
        if body.strip():
            try:
                parameters = json.loads(body)
            except ValueError:
                return problem(400, "Body is not JSON")
        if not isinstance(parameters, dict):
            return problem(400, "Parameters are malformed", "a JSON object is expected")
        try:
            result = await run_in_threadpool(
                daemon.run_tool, type_name, name, tool, parameters
            )
        except NotFound as exc:
            return problem(404, "Tool not found", str(exc))
        except InstanceNotRunning as exc:
            return problem(409, "Instance not running", str(exc))
        except ValidationError as exc:
            errors = [
                {
                    "key": ".".join(str(part) for part in error["loc"]),
                    "message": str(error["msg"]),
                }
                for error in exc.errors()
            ]
            return problem(
                422,
                "Tool parameters are invalid",
                f"{len(errors)} error{'' if len(errors) == 1 else 's'}",
                errors=errors,
            )
        except ToolFailed as exc:
            return problem(500, "Tool failed", str(exc))
        return JSONResponse(result)

    @app.post("/v1/plugins/{type_name}/instances/{name}/{action}")
    def control_instance(type_name: str, name: str, action: str) -> JSONResponse:
        """Start, stop or restart an instance; a stop persists until a start."""
        try:
            return JSONResponse(daemon.control_instance(type_name, name, action))
        except NotFound as exc:
            return problem(404, "Instance not found", str(exc))
        except InstanceNotRunning as exc:
            return problem(409, "Instance cannot run", str(exc))

    # --- models and jobs -----------------------------------------------------

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return daemon.models()

    @app.post("/v1/models", status_code=202)
    async def upload_model(request: Request, activate: bool = True) -> JSONResponse:
        """Store an upload as a job; the body is the model, named by Content-Type."""
        header = request.headers.get("content-type", "text/turtle")
        media = header.split(";")[0].strip().lower()
        fmt = RDF_MEDIA_TYPES.get(media)
        if fmt is None:
            return problem(
                415,
                "Unsupported RDF serialisation",
                f"{media}; one of {', '.join(RDF_MEDIA_TYPES)}",
            )
        data = await request.body()
        if not data.strip():
            return problem(400, "Body is empty", "the model is expected in the body")
        return job_response(daemon.upload_model(data, fmt, activate=activate))

    @app.post("/v1/models/{version}/activate", status_code=202)
    def activate_model(version: int) -> JSONResponse:
        try:
            job = daemon.activate_model(version)
        except ModelNotFound as exc:
            return problem(404, "Model version not found", str(exc))
        return job_response(job)

    @app.get("/v1/models/diff")
    def models_diff(a: int, b: int) -> JSONResponse:
        try:
            return JSONResponse(daemon.diff(a, b).as_dict())
        except ModelNotFound as exc:
            return problem(404, "Model version not found", str(exc))
        except ModelUnreadable as exc:
            return problem(422, "Model is unreadable", str(exc))

    @app.get("/v1/models/{version}")
    def export_model(
        version: int,
        inferred: bool = False,
        values: bool = False,
        timeseries: str | None = None,
    ) -> Response:
        try:
            data, fmt = daemon.export(
                version, inferred=inferred, values=values, timeseries=timeseries
            )
        except ModelNotFound as exc:
            return problem(404, "Model version not found", str(exc))
        except NotFound as exc:
            return problem(404, "No time-series references", str(exc))
        except ExportUnavailable as exc:
            return problem(409, "Export unavailable", str(exc))
        return Response(
            data, media_type=MEDIA_TYPE_OF.get(fmt, "application/octet-stream")
        )

    @app.get("/v1/jobs/{job_id}")
    def job(job_id: str) -> JSONResponse:
        found = daemon.jobs.get(job_id)
        if found is None:
            return problem(404, "Job not found", job_id)
        return JSONResponse(found.as_dict())

    return app
