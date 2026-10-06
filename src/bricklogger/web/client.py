"""The web interface's client for the daemon's API: asynchronous, adds the
token, and turns the two failure modes into exceptions the screens render."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import httpx

from bricklogger.model.prefixes import WELL_KNOWN, compact

PREFIX_TTL = 60.0


class DaemonUnavailable(Exception):
    """Nothing answers at the daemon's API binding."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"the daemon does not answer at {url}")


class ApiProblem(Exception):
    """The daemon answered with an error; ``body`` is the problem document."""

    def __init__(self, status: int, body: Mapping[str, Any]) -> None:
        self.status = status
        self.body = dict(body)
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        return str(
            self.body.get("detail") or self.body.get("title") or f"status {self.status}"
        )

    @property
    def errors(self) -> list[dict[str, Any]]:
        errors = self.body.get("errors")
        return (
            [e for e in errors if isinstance(e, dict)]
            if isinstance(errors, list)
            else []
        )


class DaemonClient:
    """Talks to one daemon; every screen goes through it."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=timeout
        )
        self._prefixes: dict[str, str] = dict(WELL_KNOWN)
        self._prefixes_read = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """A raw request; an unreachable daemon raises DaemonUnavailable."""
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DaemonUnavailable(self.base_url) from exc

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        """A JSON answer; an error answer raises ApiProblem."""
        response = await self.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {"detail": response.text}
            raise ApiProblem(
                response.status_code, body if isinstance(body, dict) else {}
            )
        if not response.content:
            return None
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.json(
            "GET", path, params={k: v for k, v in params.items() if v not in (None, "")}
        )

    async def post(self, path: str, json: Any = None, **params: Any) -> Any:
        return await self.json(
            "POST",
            path,
            json=json,
            params={k: v for k, v in params.items() if v not in (None, "")},
        )

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.json(
            "DELETE",
            path,
            params={k: v for k, v in params.items() if v not in (None, "")},
        )

    async def reachable(self) -> bool:
        try:
            response = await self._client.get("/health/live", timeout=2.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def prefixes(self) -> dict[str, str]:
        """The active model's prefixes, refreshed now and then, for shortening URIs."""
        now = time.monotonic()
        if now - self._prefixes_read > PREFIX_TTL:
            try:
                found = (await self.get("/v1/models")).get("prefixes")
            except (DaemonUnavailable, ApiProblem):
                found = None
            if isinstance(found, dict) and found:
                self._prefixes = {str(k): str(v) for k, v in found.items()}
            self._prefixes_read = now
        return self._prefixes


def shorten(uri: object, prefixes: Mapping[str, str]) -> str:
    """A URI in prefixed form when a prefix is known; anything else unchanged."""
    if not isinstance(uri, str):
        return "" if uri is None else str(uri)
    return compact(uri, prefixes)
