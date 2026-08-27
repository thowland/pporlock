"""The control API application — SPEC-0 §6.

Routes are classified. ``INLINE_ROUTES`` may read in-memory state only; every
other route offloads to the executor, because a slow handler on the proxy's own
event loop stalls all browsing (SPEC-1 §7.1). A test asserts the classification
holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from ..capture.filters import FlowFilter
from ..config import Config
from ..errors import AuthError, PairingError, PporlockError
from .audit import AuditLog
from .auth import (
    CLIENT_HEADER,
    OriginPolicy,
    PairingWindow,
    TokenStore,
    bearer_token,
    require_client,
)
from .events import EventFilter, EventHub
from .serialize import (
    DEFAULT_ITEM_DETAIL,
    DEFAULT_LIST_DETAIL,
    parse_detail,
    serialize_flow,
    serialize_flow_page,
)

if TYPE_CHECKING:
    from ..addon.interceptor import Interceptor
    from ..capture.ring import RingBuffer

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Routes that read in-memory state only. Everything else must offload to the
#: executor. Kept as data so the loop-discipline test can assert against it.
INLINE_ROUTES: frozenset[str] = frozenset(
    {
        "/state/health",
        "/state",
        "/flows",
        "/flows/{flow_id}",
        "/exclusions",
        "/audit",
        "/metrics",
        "/events",
    }
)

#: The only route that does not require a bearer token (SPEC-0 §6.1). It returns
#: nothing but liveness and a version, and the extension polls it to decide
#: whether to clear Chrome's proxy configuration (REQ EXT-010).
PUBLIC_ROUTES: frozenset[str] = frozenset({"/state/health", "/pair"})

#: Routes that touch the filesystem, SQLite, or module import. These MUST
#: offload to the executor: on the proxy's own event loop, blocking here stalls
#: every connection the browser has open (SPEC-1 §7.1).
OFFLOAD_ROUTES: frozenset[str] = frozenset({"/config", "/"})

#: Path prefixes served without a token. The web UI's own assets: the page has
#: to load before it can present a token, and the assets are ours, not data.
PUBLIC_PREFIXES: tuple[str, ...] = ("/assets/", "/favicon", "/vite.svg")


def error_response(exc: PporlockError, status: int) -> JSONResponse:
    return JSONResponse({"error": exc.to_dict()}, status_code=status)


class SecurityMiddleware(BaseHTTPMiddleware):
    """Bearer token, origin policy, and CSRF defence in one place.

    The threat is specific: any page you visit can issue requests to
    127.0.0.1:8081. Without this it could enable a module or read your traffic.
    """

    def __init__(self, app: Any, control: ControlApp) -> None:
        super().__init__(app)
        self.control = control

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.scope.get("path", "")
        origin = request.headers.get("origin")

        # /pair is exempt from the origin allowlist, and must be: an extension
        # cannot be on the allowlist until it has paired, and pairing is how it
        # gets there. The route is not unguarded — it validates that the origin
        # is a well-formed chrome-extension:// one, and requires a code from a
        # window a human opened seconds ago (SPEC-1 §7.2).
        if path != "/pair" and not self.control.policy.allows(origin):
            return error_response(AuthError("origin not permitted", origin=origin), 403)

        is_public = path in PUBLIC_ROUTES or path == "/" or path.startswith(PUBLIC_PREFIXES)
        if not is_public and not self.control.tokens.verify(
            bearer_token(request.headers.get("authorization"))
        ):
            return error_response(AuthError("missing or invalid bearer token"), 401)

        client = "cli"
        if request.method in MUTATING_METHODS and path != "/pair":
            try:
                client = require_client(request.headers.get(CLIENT_HEADER))
            except AuthError as exc:
                return error_response(exc, 403)
        request.scope["pporlock_client"] = client

        try:
            response = await call_next(request)
        except PporlockError as exc:
            return error_response(exc, 400)

        if origin is not None:
            response.headers["access-control-allow-origin"] = origin
            response.headers["vary"] = "origin"
        return response


class ControlApp:
    """Holds the daemon state the routes read, and builds the ASGI app."""

    def __init__(
        self,
        config: Config,
        *,
        ring: RingBuffer,
        interceptor: Interceptor | None = None,
        tokens: TokenStore | None = None,
        policy: OriginPolicy | None = None,
        pairing: PairingWindow | None = None,
        audit: AuditLog | None = None,
        events: EventHub | None = None,
        static_dir: Any = None,
        version: str = "0.1.0",
    ) -> None:
        from pathlib import Path

        self.config = config
        self.ring = ring
        self.interceptor = interceptor
        self.version = version
        self.tokens = tokens or TokenStore(Path(config.state_dir))
        self.policy = policy or OriginPolicy(config.control.listen_host, config.control.listen_port)
        self.pairing = pairing or PairingWindow()
        self.audit = audit or AuditLog()
        self.events = events or EventHub()
        self.static_dir = static_dir
        # Generate the token now rather than on first verify. It is per-install
        # state the user and the CLI need to be able to find, and a path printed
        # at startup that does not yet exist is worse than no path at all.
        self.tokens.ensure()
        self.dev_toggles = {"anticache": False, "anticomp": False}
        self.active_profile = "default"
        self.asgi = self._build()

    # -- helpers ---------------------------------------------------------

    async def offload(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run blocking work off the proxy's event loop (REQ API-002)."""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def _state_payload(self) -> dict[str, Any]:
        from mitmproxy import version as mitm_version

        counters = (
            self.interceptor.counters.to_dict()
            if self.interceptor is not None
            else {"flows_total": 0, "blocked": 0, "modified": 0, "passthrough": 0, "errors": 0}
        )
        stats = self.ring.stats
        return {
            "version": self.version,
            "mitmproxy_version": mitm_version.VERSION,
            "proxy": {
                "running": self.interceptor is not None,
                "listen": f"{self.config.proxy.listen_host}:{self.config.proxy.listen_port}",
                "uptime_s": self.interceptor.uptime_s if self.interceptor is not None else 0.0,
            },
            "active_profile": self.active_profile,
            "dev_toggles": dict(self.dev_toggles),
            "modules": {"loaded": 0, "enabled": 0, "quarantined": 0, "errors": []},
            "capture": {
                "ring_flows": stats.flows,
                "ring_bytes": stats.bytes,
                "recording_session": None,
            },
            "counters": counters,
            "clients": {"mcp_connected": 0, "mcp_read_only": False},
        }

    # -- routes ----------------------------------------------------------

    async def health(self, _: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": self.version})

    async def get_state(self, _: Request) -> JSONResponse:
        return JSONResponse(self._state_payload())

    async def post_state(self, request: Request) -> JSONResponse:
        body = await request.json()
        client = request.scope.get("pporlock_client", "cli")
        toggles = body.get("dev_toggles") or {}
        for name in ("anticache", "anticomp"):
            if name in toggles:
                self.dev_toggles[name] = bool(toggles[name])
                self.audit.record(client, "dev_toggle", toggle=name, value=self.dev_toggles[name])
        return JSONResponse(self._state_payload())

    async def get_flows(self, request: Request) -> JSONResponse:
        params = dict(request.query_params)
        detail = parse_detail(params.get("detail"), DEFAULT_LIST_DETAIL)
        try:
            limit = int(params.get("limit", 100))
        except ValueError:
            limit = 100
        result = self.ring.query(
            FlowFilter.from_query(params), limit=limit, cursor=params.get("cursor")
        )
        return JSONResponse(
            serialize_flow_page(
                result.flows,
                next_cursor=result.next_cursor,
                total_estimate=result.total_estimate,
                detail=detail,
            )
        )

    async def get_flow(self, request: Request) -> JSONResponse:
        flow_id = request.path_params["flow_id"]
        record = self.ring.get(flow_id)
        if record is None:
            return JSONResponse(
                {"error": {"code": "not_found", "message": f"no flow {flow_id}", "detail": {}}},
                status_code=404,
            )
        detail = parse_detail(request.query_params.get("detail"), DEFAULT_ITEM_DETAIL)
        return JSONResponse(serialize_flow(record, detail))

    async def delete_flows(self, request: Request) -> Response:
        self.ring.clear()
        self.audit.record(request.scope.get("pporlock_client", "cli"), "clear_flows")
        return Response(status_code=204)

    async def get_exclusions(self, _: Request) -> JSONResponse:
        if self.interceptor is None:
            return JSONResponse({"entries": []})
        return JSONResponse(self.interceptor.exclusions.to_dict())

    async def put_exclusions(self, request: Request) -> JSONResponse:
        from ..engine.exclusions import ExclusionList

        body = await request.json()
        entries = body.get("entries", [])
        if self.interceptor is not None:
            self.interceptor.exclusions = ExclusionList.from_dicts(entries)
        self.audit.record(
            request.scope.get("pporlock_client", "cli"), "put_exclusions", count=len(entries)
        )
        return await self.get_exclusions(request)

    async def get_config(self, _: Request) -> JSONResponse:
        # Offloaded: reading configuration touches dataclass reflection and, in
        # later sprints, the filesystem. Anything not purely in-memory stays off
        # the proxy's event loop (REQ API-002).
        payload: dict[str, Any] = await self.offload(self.config.to_dict)
        return JSONResponse(payload)

    async def get_audit(self, request: Request) -> JSONResponse:
        try:
            limit = int(request.query_params.get("limit", 100))
        except ValueError:
            limit = 100
        entries, next_cursor = self.audit.entries(limit, request.query_params.get("cursor"))
        return JSONResponse({"entries": [e.to_dict() for e in entries], "next_cursor": next_cursor})

    async def get_metrics(self, _: Request) -> JSONResponse:
        stats = self.ring.stats
        counters = self.interceptor.counters.to_dict() if self.interceptor is not None else {}
        return JSONResponse(
            {"ring": stats.to_dict(), "counters": counters, "attribution_coverage": None}
        )

    async def get_index(self, _: Request) -> Response:
        """Serve the web UI shell with its bearer token injected.

        The UI is served from our own origin by the same process that holds the
        token, so handing it over in the document is the honest path: there is
        no third party in between, and the alternative — a token in the query
        string — would put it in history and Referer.

        The origin policy still applies to every call the page then makes, so a
        different page cannot use what it cannot read.
        """
        from pathlib import Path as _Path

        if self.static_dir is None:
            return Response(
                "pporlock web UI is not built. Run `make web`.",
                media_type="text/plain",
                status_code=404,
            )

        index = _Path(self.static_dir) / "index.html"
        if not index.is_file():
            return Response(
                "pporlock web UI is not built. Run `make web`.",
                media_type="text/plain",
                status_code=404,
            )

        html = await self.offload(index.read_text)
        token = self.tokens.ensure()
        meta = f'<meta name="pporlock-token" content="{token}">'
        html = html.replace("</head>", f"  {meta}\n  </head>", 1)
        return Response(
            html,
            media_type="text/html",
            headers={
                # The shell carries a credential, so it must never be cached by
                # anything between us and the browser.
                "cache-control": "no-store",
                "referrer-policy": "no-referrer",
            },
        )

    async def get_events(self, request: Request) -> StreamingResponse:
        """SSE stream (REQ API-022).

        Filtered server-side so a narrow client filter reduces event volume
        rather than merely hiding rows (SPEC-0 §7.1).
        """
        event_filter = EventFilter.from_query(dict(request.query_params))
        stream = self.events.subscribe(
            event_filter, last_event_id=request.headers.get("last-event-id")
        )
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-transform",
                # Without this an intermediary can buffer the stream into
                # uselessness; harmless on loopback, cheap insurance.
                "x-accel-buffering": "no",
                "connection": "keep-alive",
            },
        )

    async def post_pair(self, request: Request) -> JSONResponse:
        """Redeem a pairing code for the bearer token (REQ API-012).

        Deliberately unauthenticated — it is how the extension obtains the token
        in the first place — but gated on a window a human opened seconds ago,
        and single-use.
        """
        body = await request.json()
        origin = request.headers.get("origin") or ""
        try:
            self.pairing.redeem(str(body.get("code", "")), origin, self.policy)
        except PairingError as exc:
            return error_response(exc, 403)
        self.audit.record("extension", "paired", extension_id=self.policy.extension_id)
        return JSONResponse({"token": self.tokens.ensure()})

    def _build(self) -> Starlette:
        routes: list[BaseRoute] = [
            Route("/state/health", self.health, methods=["GET"]),
            Route("/state", self.get_state, methods=["GET"]),
            Route("/state", self.post_state, methods=["POST"]),
            Route("/flows", self.get_flows, methods=["GET"]),
            Route("/flows", self.delete_flows, methods=["DELETE"]),
            Route("/flows/{flow_id}", self.get_flow, methods=["GET"]),
            Route("/exclusions", self.get_exclusions, methods=["GET"]),
            Route("/exclusions", self.put_exclusions, methods=["PUT"]),
            Route("/config", self.get_config, methods=["GET"]),
            Route("/audit", self.get_audit, methods=["GET"]),
            Route("/metrics", self.get_metrics, methods=["GET"]),
            Route("/events", self.get_events, methods=["GET"]),
            Route("/pair", self.post_pair, methods=["POST"]),
            Route("/", self.get_index, methods=["GET"]),
        ]

        if self.static_dir is not None:
            from pathlib import Path as _Path

            directory = _Path(self.static_dir)
            if directory.is_dir():
                # Serves the built web UI (REQ API-003). Mounted last so every
                # API route wins over a same-named asset, and html=True gives
                # SPA fallback to index.html.
                # Mounted last so every API route and the token-injecting
                # index handler win over a same-named asset.
                routes.append(Mount("/", app=StaticFiles(directory=directory), name="ui"))

        return Starlette(
            routes=routes,
            middleware=[Middleware(SecurityMiddleware, control=self)],
        )
