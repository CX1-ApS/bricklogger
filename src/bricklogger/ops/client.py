"""The client for the daemon's API that the CLI and the MCP server share."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from bricklogger.config.daemon import load_daemon_settings
from bricklogger.config.issues import ConfigError
from bricklogger.ops.errors import ApiError, DaemonUnreachable, OperationError

if TYPE_CHECKING:
    import httpx

WHERE_KEYS = ("file", "instance", "rule", "point", "subject", "focus", "key")


class DaemonApi:
    """Talks to one daemon; every failure is an :class:`OperationError`."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 30.0) -> None:
        import httpx

        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout
        )

    @classmethod
    def from_directory(
        cls,
        config_dir: Path,
        api_url: str | None = None,
        token: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Self:
        """The daemon named by ``api_url``, else the binding in ``daemon.yaml``."""
        if api_url is None:
            try:
                settings = load_daemon_settings(config_dir, env)
            except ConfigError as exc:
                raise OperationError(f"daemon.yaml: {exc}") from exc
            api_url = f"http://{settings.api.host}:{settings.api.port}"
            token = token or settings.api.token
        return cls(api_url, token)

    def close(self) -> None:
        """Close the connections, so a daemon asked to stop is not kept waiting."""
        self._client.close()

    def reachable(self) -> bool:
        """Whether a daemon answers at the binding; quick, and never raises."""
        import httpx

        try:
            return self._client.get("/health/live", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def get(self, path: str, **params: Any) -> Any:
        return _json(self._request("GET", path, params=_clean(params)))

    def delete(self, path: str, **params: Any) -> Any:
        return _json(self._request("DELETE", path, params=_clean(params)))

    def get_text(self, path: str, accept: str | None = None, **params: Any) -> str:
        headers = {"Accept": accept} if accept else None
        return self._request("GET", path, params=_clean(params), headers=headers).text

    def get_bytes(self, path: str, **params: Any) -> bytes:
        return self._request("GET", path, params=_clean(params)).content

    def raw(
        self,
        method: str,
        path: str,
        *,
        content: bytes | str | None = None,
        content_type: str | None = None,
        accept: str | None = None,
        **params: Any,
    ) -> httpx.Response:
        """A request whose response the caller reads itself."""
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if accept:
            headers["Accept"] = accept
        return self._request(
            method, path, content=content, headers=headers, params=_clean(params)
        )

    def prefixes(self) -> dict[str, str]:
        """The active model's prefixes, for shortening URIs; empty when unknown."""
        try:
            found = self.get("/v1/models").get("prefixes")
        except OperationError:
            return {}
        return dict(found) if isinstance(found, dict) else {}

    def post(
        self,
        path: str,
        json: Any = None,
        *,
        content: bytes | str | None = None,
        content_type: str | None = None,
        **params: Any,
    ) -> Any:
        headers = {"Content-Type": content_type} if content_type else None
        return _json(
            self._request(
                "POST",
                path,
                json=json,
                content=content,
                headers=headers,
                params=_clean(params),
            )
        )

    def put(self, path: str, content: bytes | str, content_type: str) -> Any:
        return _json(
            self._request(
                "PUT", path, content=content, headers={"Content-Type": content_type}
            )
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        import httpx

        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise DaemonUnreachable(
                f"the daemon is not running or not reachable at {self.base_url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise DaemonUnreachable(
                f"the daemon at {self.base_url} did not answer: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise api_error(response)
        return response


def reachable_api(
    config_dir: Path,
    api_url: str | None = None,
    token: str | None = None,
    env: Mapping[str, str] | None = None,
) -> DaemonApi | None:
    """The client when ``api_url`` names a daemon or one answers at the binding.

    ``None`` means: work without a daemon. With ``api_url`` the daemon is
    required, so a client comes back whether it answers or not.
    """
    try:
        client = DaemonApi.from_directory(config_dir, api_url, token, env)
    except OperationError:
        if api_url is not None:
            raise
        return None
    if api_url is not None or client.reachable():
        return client
    client.close()
    return None


def api_error(response: httpx.Response) -> ApiError:
    """The problem document as an error, its message on the CLI's lines."""
    try:
        body = response.json()
    except ValueError:
        body = None
    return ApiError(
        response.status_code,
        body if isinstance(body, dict) else None,
        describe_problem(response),
    )


def describe_problem(response: httpx.Response) -> str:
    """The status and detail on one line; the error entries follow, one per line."""
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip() or response.reason_phrase
        return f"{response.status_code}: {text}"
    if not isinstance(body, dict):
        return f"{response.status_code}: {response.text.strip()}"
    detail = body.get("detail") or body.get("title") or response.reason_phrase
    lines = [f"{response.status_code}: {detail}"]
    for entry in body.get("errors") or []:
        if isinstance(entry, dict):
            lines.append("  " + format_error(entry))
    return "\n".join(lines)


def format_error(entry: Mapping[str, Any]) -> str:
    """One error entry of a problem document as ``where: message``."""
    where = " ".join(str(entry[key]) for key in WHERE_KEYS if entry.get(key))
    message = str(entry.get("message", ""))
    return f"{where}: {message}" if where else message


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    return response.json()


def _clean(params: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}
