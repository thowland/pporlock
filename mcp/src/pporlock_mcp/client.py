"""HTTP client for the pporlock control API (REQ MCP-001, MCP-002).

This module is the *only* place the MCP server talks to the daemon, and it does
so exactly the way any other client does: loopback HTTP, bearer token, origin
header, client tag. It imports nothing from the daemon package (SPEC-1 §11.1) —
there is no shared process, no shared database file, and no shared filesystem
state beyond the token file, which is read and never written.

Two things here are load-bearing:

* ``X-Pporlock-Client: mcp`` is sent on every request. On mutating requests the
  daemon *requires* it (SPEC-0 §6.1) and it is what makes the audit log's origin
  field trustworthy rather than a guess (REQ MCP-031).
* ``unmask`` is stripped and refused at this layer, not at the tool layer, so no
  future tool can reintroduce it by passing parameters through (REQ MCP-003,
  CAP-043).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from .errors import ConfigurationError, ControlApiError, GuardrailError

DEFAULT_BASE_URL = "http://127.0.0.1:8081"
DEFAULT_STATE_DIR = Path("~/.pporlock")
CLIENT_HEADER = "X-Pporlock-Client"
CLIENT_NAME = "mcp"
DEFAULT_TIMEOUT_S = 30.0

#: Query parameters this server will never send, whatever a caller asks for.
#: ``unmask`` is the UI-only reveal path of SPEC-0 §9.3; MCP has no equivalent
#: and must not acquire one by parameter passthrough (REQ MCP-003, CAP-043).
FORBIDDEN_PARAMS = frozenset({"unmask", "unredact", "reveal", "unmask_field"})


def read_token(state_dir: str | Path | None = None) -> str:
    """Read the per-install bearer token.

    The MCP server runs as the user, so unlike the extension it reads the token
    file directly rather than pairing (SPEC-1 §11.1). ``PPORLOCK_TOKEN`` wins
    when set, which is what makes the server startable in a test or a sandbox
    without a daemon install.
    """
    env = os.environ.get("PPORLOCK_TOKEN")
    if env:
        return env.strip()

    base = (
        Path(state_dir)
        if state_dir is not None
        else Path(os.environ.get("PPORLOCK_STATE_DIR", str(DEFAULT_STATE_DIR)))
    )
    path = base.expanduser() / "token"
    try:
        token = path.read_text().strip()
    except OSError as exc:
        raise ConfigurationError(
            f"cannot read the pporlock token at {path}: {exc}. "
            "Is the daemon installed and has it run once?"
        ) from exc
    if not token:
        raise ConfigurationError(f"the pporlock token at {path} is empty")
    return token


def _origin(base_url: str) -> str:
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def assert_no_forbidden_params(params: dict[str, Any]) -> None:
    """REQ MCP-003 — refuse anything that would reveal a redacted value."""
    offenders = sorted(k for k in params if k.lower() in FORBIDDEN_PARAMS)
    if offenders:
        raise GuardrailError(
            f"the MCP interface cannot unmask redacted values (refused: {', '.join(offenders)}). "
            "Unmasking is available only in the web UI, on live flows, per value.",
            requirement="MCP-003/CAP-043",
        )


class ControlClient:
    """Thin, typed wrapper over the control API.

    Constructed with an ``httpx.AsyncClient`` so tests can inject a
    ``MockTransport`` and never start a daemon.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout_s)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Origin": _origin(self.base_url),
            # REQ MCP-031: every request is tagged, so the audit log's origin
            # column is recorded, not inferred.
            CLIENT_HEADER: CLIENT_NAME,
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        """Issue one control API call and return the decoded body.

        Returns ``None`` for a 204. Raises ``ControlApiError`` for any non-2xx,
        carrying the daemon's uniform error body (SPEC-0 §6.2).
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        assert_no_forbidden_params(clean)
        if isinstance(json, dict):
            assert_no_forbidden_params(json)

        try:
            response = await self._http.request(
                method,
                f"{self.base_url}{path}",
                params=clean or None,
                json=json,
                headers=self._headers(json_body=json is not None),
            )
        except httpx.HTTPError as exc:
            raise ControlApiError(
                0,
                None,
                f"cannot reach the pporlock daemon at {self.base_url}: {exc}. "
                "Start it with `pporlock start`.",
            ) from exc

        if response.status_code >= 400:
            body: dict[str, Any] | None
            try:
                decoded = response.json()
                body = decoded if isinstance(decoded, dict) else {"error": {"raw": decoded}}
            except ValueError:
                body = None
            message = f"{method} {path} failed with HTTP {response.status_code}"
            if body and isinstance(body.get("error"), dict):
                message = str(body["error"].get("message") or message)
            raise ControlApiError(response.status_code, body, message)

        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any | None = None, **params: Any) -> Any:
        return await self.request(
            "POST", path, params=params, json=json if json is not None else {}
        )

    async def put(self, path: str, json: Any) -> Any:
        return await self.request("PUT", path, json=json)

    async def patch(self, path: str, json: Any) -> Any:
        return await self.request("PATCH", path, json=json)

    async def delete(self, path: str) -> Any:
        return await self.request("DELETE", path)
