"""The web interface: a FastAPI application that renders the screens with Jinja2
and HTMX and forwards API calls to the daemon under ``/api``. Served by
``bricklogger serve``. See ``docs/features/web.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, TimestampSigner

from bricklogger import __version__
from bricklogger.config import CONFIG_FILES, resolve_config_dir
from bricklogger.ops.environment import InstancesConfigured, add_plugins, remove_plugin
from bricklogger.ops.errors import OperationError
from bricklogger.ops.plugin_volume import plugin_directory
from bricklogger.web.client import ApiProblem, DaemonClient, DaemonUnavailable, shorten
from bricklogger.web.forms import (
    cell,
    document_filename,
    fields_of,
    offers_on,
    shape,
    values_from_form,
)
from bricklogger.web.texts import TEXTS, text

PACKAGE_DIR = Path(__file__).parent
SESSION_COOKIE = "bricklogger_session"
SESSION_MAX_AGE = 12 * 3600
POLL_SECONDS = 5
FILTERS = ("instance", "outcome", "warning", "class")
INSTANCE_ACTIONS = ("start", "stop", "restart")
MUTATING = frozenset({"POST", "PUT", "DELETE", "PATCH"})
RDF_MEDIA_TYPES = {
    "ttl": "text/turtle",
    "turtle": "text/turtle",
    "nt": "application/n-triples",
    "rdf": "application/rdf+xml",
    "xml": "application/rdf+xml",
    "owl": "application/rdf+xml",
    "jsonld": "application/ld+json",
    "json": "application/ld+json",
    "n3": "text/n3",
    "trig": "application/trig",
    "nq": "application/n-quads",
}

SCREENS = (
    ("overview", "/", "nav.overview"),
    ("points", "/points", "nav.points"),
    ("instances", "/instances", "nav.instances"),
    ("model", "/model", "nav.model"),
    ("explorer", "/explorer", "nav.explorer"),
    ("config", "/config", "nav.config"),
    ("plugins", "/plugins", "nav.plugins"),
    ("notifications", "/notifications", "nav.notifications"),
    ("query", "/query", "nav.query"),
    ("daemon", "/daemon", "nav.daemon"),
)


def create_web_app(
    api_url: str,
    token: str | None = None,
    password: str | None = None,
    secret: str | None = None,
    config_dir: Path | None = None,
) -> FastAPI:
    """The web interface over one daemon; a password makes every page need a login.

    ``config_dir`` is what the plugin screen's removal checks for configured
    instances — this process's configuration, as the CLI's is.
    """
    client = DaemonClient(api_url, token)
    plugins_config_dir = config_dir if config_dir is not None else resolve_config_dir()
    signer = TimestampSigner(secret or secrets.token_hex(32))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await client.close()

    app = FastAPI(
        title="Bricklogger web", version=__version__, docs_url=None, lifespan=lifespan
    )
    app.state.client = client
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.globals.update(
        t=text,
        short=shorten,
        cell=cell,
        screens=SCREENS,
        version=__version__,
        poll=POLL_SECONDS,
        api_url=api_url,
        login_required=password is not None,
    )
    templates.env.filters["when"] = format_time

    def logged_in(request: Request) -> bool:
        if password is None:
            return True
        cookie = request.cookies.get(SESSION_COOKIE)
        if not cookie:
            return False
        try:
            signer.unsign(cookie, max_age=SESSION_MAX_AGE)
        except BadSignature:
            return False
        return True

    @app.middleware("http")
    async def require_login(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if request.method in MUTATING and cross_site(request):
            return Response(
                text("common.cross_site"), status_code=403, media_type="text/plain"
            )
        if (
            password is not None
            and not path.startswith(("/static/", "/login", "/health/"))
            and not logged_in(request)
        ):
            if request.headers.get("hx-request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    async def frame() -> dict[str, Any]:
        """What the layout needs on every page: the daemon's status, or none."""
        try:
            return {"status": await client.get("/v1/status")}
        except (DaemonUnavailable, ApiProblem):
            return {"status": None}

    async def render(
        request: Request, template: str, screen: str | None, **context: Any
    ) -> HTMLResponse:
        if "status" not in context:
            context.update(await frame())
        return templates.TemplateResponse(
            request, template, {"screen": screen, "now": datetime.now(UTC), **context}
        )

    def problem_text(exc: Exception) -> str:
        if isinstance(exc, ApiProblem):
            lines = [text("common.error", status=exc.status, detail=exc.detail)]
            lines += [f"  {describe_error(entry)}" for entry in exc.errors]
            return "\n".join(lines)
        return text("status.unreachable")

    async def problem_partial(
        request: Request, template: str, screen: str, exc: Exception, **context: Any
    ) -> HTMLResponse:
        """A partial that shows why an action did not happen."""
        return await render(
            request, template, screen, problem_text=problem_text(exc), **context
        )

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        """Alive while the process runs; what `bricklogger status` asks."""
        return {"status": "ok"}

    # --- login ---------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if password is None or logged_in(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(request, "login.html", {"wrong": False})

    @app.post("/login", response_class=HTMLResponse)
    async def login(
        request: Request, password_given: str = Form(alias="password")
    ) -> Response:
        if password is None:
            return RedirectResponse("/", status_code=303)
        if not hmac.compare_digest(password_given.encode(), password.encode()):
            return templates.TemplateResponse(request, "login.html", {"wrong": True})
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            signer.sign(b"ok").decode(),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # --- the API, forwarded --------------------------------------------------

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def forward(path: str, request: Request) -> Response:
        """The daemon's API under /api, with the token added; Yasgui uses it."""
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in ("content-type", "accept")
        }
        try:
            upstream = await client.request(
                request.method,
                f"/{path}",
                params=request.query_params,
                content=await request.body(),
                headers=headers,
            )
        except DaemonUnavailable as exc:
            return Response(
                '{"type": "urn:bricklogger:problem:daemon-unavailable", '
                '"title": "Daemon unavailable", "status": 503, '
                f'"detail": "{exc}"}}',
                status_code=503,
                media_type="application/problem+json",
            )
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    # --- overview ------------------------------------------------------------

    async def overview_context() -> dict[str, Any]:
        try:
            status = await client.get("/v1/status")
            warnings = await client.get("/v1/status/warnings")
        except (DaemonUnavailable, ApiProblem):
            return {"status": None, "warnings": [], "reachable": False, "prefixes": {}}
        return {
            "status": status,
            "warnings": warnings,
            "reachable": True,
            "prefixes": await client.prefixes(),
        }

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request) -> Response:
        return await render(
            request, "overview.html", "overview", **await overview_context()
        )

    @app.get("/partials/overview", response_class=HTMLResponse)
    async def overview_partial(request: Request) -> Response:
        return await render(
            request, "partials/overview.html", "overview", **await overview_context()
        )

    @app.post("/actions/warnings/clear", response_class=HTMLResponse)
    async def clear_warnings(
        request: Request, code: str = Form(""), subject: str = Form("")
    ) -> Response:
        """Clear one warning, those with a code, or all; the overview follows."""
        with contextlib.suppress(ApiProblem, DaemonUnavailable):
            await client.delete("/v1/status/warnings", code=code, subject=subject)
        return await render(
            request, "partials/overview.html", "overview", **await overview_context()
        )

    # --- notifications -------------------------------------------------------

    async def notifications_context() -> dict[str, Any]:
        context: dict[str, Any] = {
            "notifications": None,
            "reachable": True,
            "problem": None,
        }
        try:
            context["notifications"] = await client.get("/v1/notifications")
        except DaemonUnavailable:
            context["reachable"] = False
        except ApiProblem as problem:
            context["problem"] = problem
        return context

    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications(request: Request) -> Response:
        return await render(
            request,
            "notifications.html",
            "notifications",
            **await notifications_context(),
        )

    @app.get("/partials/notifications", response_class=HTMLResponse)
    async def notifications_partial(request: Request) -> Response:
        return await render(
            request,
            "partials/notifications.html",
            "notifications",
            **await notifications_context(),
        )

    @app.post("/actions/notifications/test", response_class=HTMLResponse)
    async def test_notification(request: Request) -> Response:
        """Send a test mail and show what the server answered, under the button."""
        outcome: dict[str, Any] | None
        try:
            outcome = await client.post("/v1/notifications/test")
        except ApiProblem as failure:
            outcome = {"sent": False, "error": failure.detail}
        except DaemonUnavailable:
            outcome = None
        return await render(
            request, "partials/notify_test.html", "notifications", test_result=outcome
        )

    # --- points --------------------------------------------------------------

    async def points_context(request: Request) -> dict[str, Any]:
        filters = {key: request.query_params.get(key, "") for key in FILTERS}
        limit = _int(request.query_params.get("limit"), 50, 1, 1000)
        offset = _int(request.query_params.get("offset"), 0, 0, 10**9)
        query = urlencode({**{k: v for k, v in filters.items() if v}, "limit": limit})
        context: dict[str, Any] = {
            "filters": filters,
            "query": query,
            "offset": offset,
            "limit": limit,
            "page": None,
            "problem": None,
            "instances": [],
            "prefixes": {},
        }
        try:
            page = await client.get("/v1/points", limit=limit, offset=offset, **filters)
            sources = await client.get("/v1/status/sources")
        except DaemonUnavailable:
            context["reachable"] = False
            return context
        except ApiProblem as exc:
            context.update(reachable=True, problem=exc)
            return context
        context.update(
            reachable=True,
            page=page,
            instances=[row["name"] for row in sources],
            prefixes=await client.prefixes(),
        )
        return context

    @app.get("/points", response_class=HTMLResponse)
    async def points(request: Request) -> Response:
        return await render(
            request, "points.html", "points", **await points_context(request)
        )

    @app.get("/partials/points", response_class=HTMLResponse)
    async def points_partial(request: Request) -> Response:
        return await render(
            request, "partials/points.html", "points", **await points_context(request)
        )

    # --- sources and destinations -------------------------------------------

    async def instances_context(
        message: str | None = None, signal: bool = False
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "sources": [],
            "destinations": [],
            "message": message,
            "signal": signal,
        }
        try:
            context["sources"] = await client.get("/v1/status/sources")
            context["destinations"] = await client.get("/v1/status/destinations")
        except (DaemonUnavailable, ApiProblem):
            context["reachable"] = False
            return context
        context["reachable"] = True
        return context

    async def declaration_of(type_name: str) -> dict[str, Any]:
        try:
            found = await client.get(f"/v1/plugins/{type_name}")
        except (DaemonUnavailable, ApiProblem):
            return {}
        return dict(found) if isinstance(found, dict) else {}

    async def tools_context(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        declarations: dict[str, dict[str, Any]] = {}
        tools: list[dict[str, Any]] = []
        for source in sources:
            type_name = str(source["type"])
            if type_name not in declarations:
                declarations[type_name] = await declaration_of(type_name)
            for tool in declarations[type_name].get("tools", []):
                if tool.get("offered_on"):
                    continue  # offered on another tool's rows, no form of its own
                tools.append(
                    {
                        "instance": source["name"],
                        "type": type_name,
                        "tool": tool,
                        "fields": fields_of(tool.get("parameters") or {}),
                    }
                )
        return tools

    @app.get("/instances", response_class=HTMLResponse)
    async def instances(request: Request) -> Response:
        context = await instances_context()
        tools = await tools_context(context["sources"]) if context["reachable"] else []
        return await render(
            request, "instances.html", "instances", tools=tools, **context
        )

    @app.get("/partials/instances", response_class=HTMLResponse)
    async def instances_partial(request: Request) -> Response:
        return await render(
            request, "partials/instances.html", "instances", **await instances_context()
        )

    @app.post(
        "/actions/instances/{type_name}/{name}/tools/{tool}",
        response_class=HTMLResponse,
    )
    async def run_tool(
        request: Request, type_name: str, name: str, tool: str
    ) -> Response:
        declaration = await declaration_of(type_name)
        spec = next(
            (t for t in declaration.get("tools", []) if t["name"] == tool), None
        )
        fields = fields_of(spec.get("parameters") or {}) if spec else []
        form = await request.form()
        values = values_from_form(fields, form)
        try:
            result = await client.post(
                f"/v1/plugins/{type_name}/instances/{name}/tools/{tool}", json=values
            )
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(
                request,
                "partials/tool_result.html",
                "instances",
                exc,
                rows=None,
                columns=[],
                mapping=None,
                raw=None,
                offers=[],
            )
        return await render(
            request,
            "partials/tool_result.html",
            "instances",
            problem_text=None,
            offers=offers_on(declaration.get("tools", []), tool),
            tool_type=type_name,
            tool_instance=name,
            **shape(result),
        )

    @app.post("/actions/instances/{type_name}/{name}/tools/{tool}/document")
    async def download_document(
        request: Request, type_name: str, name: str, tool: str
    ) -> Response:
        """A document tool's result as a file the browser saves; a problem
        renders the screen with the message instead."""
        declaration = await declaration_of(type_name)
        spec = next(
            (t for t in declaration.get("tools", []) if t["name"] == tool), None
        )
        if declaration and (spec is None or not spec.get("document")):
            return Response(status_code=404)
        fields = fields_of(spec.get("parameters") or {}) if spec else []
        values = values_from_form(fields, await request.form())
        try:
            result = await client.post(
                f"/v1/plugins/{type_name}/instances/{name}/tools/{tool}", json=values
            )
        except (ApiProblem, DaemonUnavailable) as exc:
            context = await instances_context(problem_text(exc), signal=True)
            tools = (
                await tools_context(context["sources"]) if context["reachable"] else []
            )
            return await render(
                request, "instances.html", "instances", tools=tools, **context
            )
        filename = document_filename(name, tool, values, datetime.now(UTC).date())
        return Response(
            content=json.dumps(result, indent=2, default=str) + "\n",
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post(
        "/actions/instances/{type_name}/{name}/{action}", response_class=HTMLResponse
    )
    async def control_instance(
        request: Request, type_name: str, name: str, action: str
    ) -> Response:
        if action not in INSTANCE_ACTIONS:
            return Response(status_code=404)
        try:
            row = await client.post(
                f"/v1/plugins/{type_name}/instances/{name}/{action}"
            )
        except (ApiProblem, DaemonUnavailable) as exc:
            context = await instances_context(problem_text(exc), signal=True)
        else:
            context = await instances_context(f"{name}: {row.get('state')}")
        return await render(request, "partials/instances.html", "instances", **context)

    # --- model ---------------------------------------------------------------

    async def model_context() -> dict[str, Any]:
        try:
            models = await client.get("/v1/models")
        except (DaemonUnavailable, ApiProblem):
            return {"models": None, "reachable": False, "prefixes": {}}
        versions = [v["version"] for v in models.get("versions", [])]
        return {
            "models": models,
            "reachable": True,
            "prefixes": await client.prefixes(),
            "diff_a": versions[-2]
            if len(versions) > 1
            else (versions[0] if versions else 1),
            "diff_b": versions[-1] if versions else 1,
        }

    @app.get("/model", response_class=HTMLResponse)
    async def model(request: Request) -> Response:
        return await render(request, "model.html", "model", **await model_context())

    @app.get("/partials/model/versions", response_class=HTMLResponse)
    async def model_versions(request: Request) -> Response:
        return await render(
            request, "partials/model_versions.html", "model", **await model_context()
        )

    async def job_partial(request: Request, job: Mapping[str, Any]) -> HTMLResponse:
        return await render(
            request,
            "partials/job.html",
            "model",
            job=job,
            prefixes=await client.prefixes(),
            problem_text=None,
        )

    @app.post("/actions/model/upload", response_class=HTMLResponse)
    async def model_upload(
        request: Request, file: UploadFile, activate: str | None = Form(None)
    ) -> Response:
        data = await file.read()
        extension = (file.filename or "").rsplit(".", 1)[-1].lower()
        media = RDF_MEDIA_TYPES.get(extension, "text/turtle")
        try:
            job = await client.json(
                "POST",
                "/v1/models",
                content=data,
                headers={"Content-Type": media},
                params={"activate": "true" if activate else "false"},
            )
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(request, "partials/job.html", "model", exc)
        return await job_partial(request, job)

    @app.post("/actions/model/{version}/activate", response_class=HTMLResponse)
    async def model_activate(request: Request, version: int) -> Response:
        try:
            job = await client.post(f"/v1/models/{version}/activate")
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(request, "partials/job.html", "model", exc)
        return await job_partial(request, job)

    @app.get("/partials/job/{job_id}", response_class=HTMLResponse)
    async def job_state(request: Request, job_id: str) -> Response:
        try:
            job = await client.get(f"/v1/jobs/{job_id}")
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(request, "partials/job.html", "model", exc)
        return await job_partial(request, job)

    @app.get("/model/diff", response_class=HTMLResponse)
    async def model_diff(request: Request, a: int, b: int) -> Response:
        try:
            diff = await client.get("/v1/models/diff", a=a, b=b)
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(request, "partials/diff.html", "model", exc)
        return await render(
            request,
            "partials/diff.html",
            "model",
            diff=diff,
            a=a,
            b=b,
            prefixes=await client.prefixes(),
            problem_text=None,
        )

    # --- configuration -------------------------------------------------------

    @app.get("/config", response_class=HTMLResponse)
    async def config(request: Request, file: str = "daemon") -> Response:
        if file not in CONFIG_FILES:
            return Response(
                text("common.error", status=404, detail=f"unknown file {file!r}"),
                status_code=404,
            )
        try:
            answers = {
                name: await client.request("GET", f"/v1/config/{name}")
                for name in CONFIG_FILES
            }
        except DaemonUnavailable:
            return await render(
                request,
                "config.html",
                "config",
                reachable=False,
                files=CONFIG_FILES,
                file=file,
            )
        bodies = {
            name: answer.text if answer.status_code < 400 else ""
            for name, answer in answers.items()
        }
        return await render(
            request,
            "config.html",
            "config",
            reachable=True,
            files=CONFIG_FILES,
            file=file,
            text_body=bodies[file],
            missing=not bodies[file].strip(),
            empty_directory=not any(body.strip() for body in bodies.values()),
        )

    @app.post("/actions/config/validate", response_class=HTMLResponse)
    async def config_validate(
        request: Request, file: str = Form(...), body: str = Form(alias="text")
    ) -> Response:
        if file not in CONFIG_FILES:
            return Response(status_code=404)
        try:
            result = await client.post("/v1/config/validate", json={file: body})
        except (ApiProblem, DaemonUnavailable) as exc:
            return await problem_partial(
                request, "partials/config_result.html", "config", exc
            )
        return await render(
            request,
            "partials/config_result.html",
            "config",
            result=result,
            applied=False,
            problem_text=None,
        )

    @app.post("/actions/config/save", response_class=HTMLResponse)
    async def config_save(
        request: Request, file: str = Form(...), body: str = Form(alias="text")
    ) -> Response:
        if file not in CONFIG_FILES:
            return Response(status_code=404)
        try:
            result = await client.json(
                "PUT",
                f"/v1/config/{file}",
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/yaml"},
            )
        except ApiProblem as exc:
            if exc.status == 422 and exc.errors:
                return await render(
                    request,
                    "partials/config_result.html",
                    "config",
                    result={"valid": False, "errors": exc.errors, "warnings": []},
                    applied=False,
                    problem_text=None,
                )
            return await problem_partial(
                request, "partials/config_result.html", "config", exc
            )
        except DaemonUnavailable as exc:
            return await problem_partial(
                request, "partials/config_result.html", "config", exc
            )
        return await render(
            request,
            "partials/config_result.html",
            "config",
            result=result,
            applied=True,
            problem_text=None,
        )

    @app.post("/actions/config/init")
    async def config_init() -> Response:
        with contextlib.suppress(ApiProblem, DaemonUnavailable):
            await client.post("/v1/config/init")
        return RedirectResponse("/config", status_code=303)

    # --- explorer ------------------------------------------------------------

    @app.get("/explorer", response_class=HTMLResponse)
    async def explorer(request: Request) -> Response:
        """The model explorer; the document itself is fetched by the browser."""
        context = await frame()
        status = context["status"]
        model = (status or {}).get("model", {}).get("active")
        return await render(
            request,
            "explorer.html",
            "explorer",
            reachable=status is not None,
            model_version=model["version"] if model else None,
            config={
                "endpoint": "/api/v1/entities",
                "modelVersion": model["version"] if model else None,
                "pointsUrl": "/points",
                "queryUrl": "/query",
                "texts": {
                    key.removeprefix("explorer."): value
                    for key, value in TEXTS.items()
                    if key.startswith("explorer.")
                },
            },
            **context,
        )

    # --- query ---------------------------------------------------------------

    @app.get("/query", response_class=HTMLResponse)
    async def query(request: Request, query: str | None = None) -> Response:
        prefixes = await client.prefixes()
        declarations = "".join(
            f"PREFIX {prefix}: <{iri}>\n" for prefix, iri in sorted(prefixes.items())
        )
        body = query or (
            "\nSELECT ?point ?class WHERE {\n  ?point a ?class .\n"
            "  ?class rdfs:subClassOf* brick:Point .\n} LIMIT 100\n"
        )
        return await render(
            request, "query.html", "query", default_query=declarations + body
        )

    # --- daemon --------------------------------------------------------------

    async def daemon_context(
        message: str | None = None, signal: bool = False
    ) -> dict[str, Any]:
        context = await frame()
        context.update(
            reachable=context["status"] is not None, message=message, signal=signal
        )
        return context

    @app.get("/daemon", response_class=HTMLResponse)
    async def daemon(request: Request) -> Response:
        return await render(request, "daemon.html", "daemon", **await daemon_context())

    @app.post("/actions/daemon/reload", response_class=HTMLResponse)
    async def daemon_reload(request: Request) -> Response:
        try:
            result = await client.post("/v1/daemon/reload")
        except ApiProblem as exc:
            lines = [text("daemon.rejected", detail=exc.detail)]
            lines += [f"  {describe_error(entry)}" for entry in exc.errors]
            context = await daemon_context("\n".join(lines), signal=True)
        except DaemonUnavailable:
            context = await daemon_context(text("status.unreachable"), signal=True)
        else:
            lines = [text("daemon.reloaded")]
            lines += [
                text("daemon.warning", message=w.get("message", ""))
                for w in result.get("warnings", [])
            ]
            context = await daemon_context("\n".join(lines))
        return await render(request, "partials/daemon.html", "daemon", **context)

    @app.post("/actions/daemon/stop", response_class=HTMLResponse)
    async def daemon_stop(request: Request) -> Response:
        try:
            await client.post("/v1/daemon/stop")
        except ApiProblem as exc:
            context = await daemon_context(
                text("daemon.rejected", detail=exc.detail), signal=True
            )
        except DaemonUnavailable:
            context = await daemon_context(text("status.unreachable"), signal=True)
        else:
            context = await daemon_context(text("daemon.stopping"))
        return await render(request, "partials/daemon.html", "daemon", **context)

    # --- plugins ---------------------------------------------------------------
    # The catalogue is the daemon's; installing and removing happen in this
    # process, against the environment it runs from, as `plugins add` and
    # `plugins remove` do. uv runs in a thread, so the other pages keep answering.

    async def plugins_context(**extra: Any) -> dict[str, Any]:
        context = await frame()
        rows: list[dict[str, Any]] | None
        try:
            rows = list(await client.get("/v1/plugins"))
        except (DaemonUnavailable, ApiProblem):
            rows = None
        in_container = plugin_directory() is not None
        context.update(
            reachable=context["status"] is not None,
            rows=rows,
            restart_hint=text(
                "plugins.restart.container" if in_container else "plugins.restart.host"
            ),
            message=None,
            signal=False,
            restart=False,
            offer_force=None,
        )
        context.update(extra)
        return context

    @app.get("/plugins", response_class=HTMLResponse)
    async def plugins(request: Request) -> Response:
        return await render(
            request, "plugins.html", "plugins", **await plugins_context()
        )

    @app.post("/actions/plugins/add", response_class=HTMLResponse)
    async def plugins_add(request: Request, packages: str = Form("")) -> Response:
        try:
            added = await asyncio.to_thread(add_plugins, packages.split())
        except OperationError as exc:
            context = await plugins_context(
                message=text("plugins.refused", message=exc.message), signal=True
            )
        else:
            context = await plugins_context(
                message=text("plugins.installed", packages=", ".join(added.packages)),
                restart=True,
            )
        return await render(request, "partials/plugins.html", "plugins", **context)

    @app.post("/actions/plugins/{type_name}/remove", response_class=HTMLResponse)
    async def plugins_remove(
        request: Request, type_name: str, force: str | None = Form(None)
    ) -> Response:
        try:
            removed = await asyncio.to_thread(
                remove_plugin,
                type_name,
                config_dir=plugins_config_dir,
                force=force is not None,
            )
        except InstancesConfigured as exc:
            context = await plugins_context(
                message=text("plugins.refused", message=exc.message),
                signal=True,
                offer_force=type_name,
            )
        except OperationError as exc:
            context = await plugins_context(
                message=text("plugins.refused", message=exc.message), signal=True
            )
        else:
            lines = [
                text(
                    "plugins.removed",
                    distribution=removed.distribution,
                    version=f" {removed.version}" if removed.version else "",
                    type=type_name,
                )
            ]
            others = [name for name in removed.types if name != type_name]
            if others:
                lines.append(text("plugins.removed.others", types=", ".join(others)))
            if removed.dependencies:
                lines.append(
                    text(
                        "plugins.removed.dependencies",
                        packages=", ".join(removed.dependencies),
                    )
                )
            context = await plugins_context(message="\n".join(lines), restart=True)
        return await render(request, "partials/plugins.html", "plugins", **context)

    # --- the frame's own partial --------------------------------------------

    @app.get("/partials/statusline", response_class=HTMLResponse)
    async def statusline(request: Request) -> Response:
        return await render(request, "partials/statusline.html", None, oob=True)

    return app


def cross_site(request: Request) -> bool:
    """Whether a browser sent the request from another site.

    Browsers mark cross-site requests with ``Sec-Fetch-Site`` and ``Origin``;
    a request carrying neither did not come from a browser page and passes.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin:
        host = request.headers.get("host", "")
        origin_host = origin.split("://", 1)[-1].split("/", 1)[0]
        return origin_host.lower() != host.lower()
    return False


def describe_error(entry: Mapping[str, Any]) -> str:
    where = " ".join(
        str(entry[key])
        for key in ("file", "instance", "rule", "point", "subject", "focus", "key")
        if entry.get(key)
    )
    message = str(entry.get("message", ""))
    return f"{where}: {message}" if where else message


def format_time(value: object) -> str:
    """A timestamp as ``YYYY-MM-DD HH:MM:SS`` in UTC; anything else as it is."""
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            return value
    else:
        return "" if value is None else str(value)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(UTC)
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        number = int(value) if value else default
    except ValueError:
        return default
    return max(low, min(high, number))
