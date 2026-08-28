"""The control API application — SPEC-0 §6.

Routes are classified. ``INLINE_ROUTES`` may read in-memory state only; every
other route offloads to the executor, because a slow handler on the proxy's own
event loop stalls all browsing (SPEC-1 §7.1). A test asserts the classification
holds.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from ..capture.attribution import AttributionIndex, coverage_of, entry_from_dict
from ..capture.dryrun import DryRunner, DryRunRequest
from ..capture.export import EXPORT_FORMATS, export_session
from ..capture.filters import FlowFilter
from ..capture.records import FlowRecord
from ..capture.redact import FieldPathError, Redactor, resolve_field
from ..capture.session import SessionMeta, SessionStore
from ..capture.suggest import suggest_rule
from ..config import Config, save_config, update_config
from ..engine.evaluator import Evaluator
from ..engine.modules.loader import (
    ASSETS_DIR,
    MANIFEST_NAME,
    MODULE_NAME_PATTERN,
    WRITABLE_FILES,
)
from ..engine.modules.validate import validate_module_files
from ..engine.profiles import DEFAULT_PROFILE, Profile
from ..engine.rules_file import rules_to_dicts
from ..engine.ruleset import RuleSet
from ..errors import (
    AuthError,
    ConfigError,
    PairingError,
    PporlockError,
    ProxyControlError,
    RuleValidationError,
    SessionError,
)
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
    from ..engine.modules.registry import ModuleRegistry, ReloadResult
    from ..engine.profiles import ProfileManager

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Routes that read in-memory state only. Everything else must offload to the
#: executor. Kept as data so the loop-discipline test can assert against it.
INLINE_ROUTES: frozenset[str] = frozenset(
    {
        "/state/health",
        "/state",
        "/flows",
        "/flows/{flow_id}",
        "/flows/{flow_id}/suggest-rule",
        "/exclusions",
        "/audit",
        "/metrics",
        "/events",
        "/attribution",
        "/pair/begin",
        "/rules",
    }
)

#: The only route that does not require a bearer token (SPEC-0 §6.1). It returns
#: nothing but liveness and a version, and the extension polls it to decide
#: whether to clear Chrome's proxy configuration (REQ EXT-010).
PUBLIC_ROUTES: frozenset[str] = frozenset({"/state/health", "/pair"})

#: Routes that touch the filesystem, SQLite, or module import. These MUST
#: offload to the executor: on the proxy's own event loop, blocking here stalls
#: every connection the browser has open (SPEC-1 §7.1).
#:
#: Classification is per path, not per method, because a path is what the
#: router knows. A path whose mutating methods write files is listed here even
#: where its GET only reads memory.
OFFLOAD_ROUTES: frozenset[str] = frozenset(
    {
        "/config",
        "/",
        "/modules",
        "/modules/reload",
        "/modules/{name}",
        "/profiles",
        "/profiles/{name}",
        "/profiles/{name}/activate",
        "/sessions",
        "/sessions/{session_id}",
        "/sessions/{session_id}/stop",
        "/sessions/{session_id}/flows",
        "/sessions/{session_id}/export",
        "/sessions/{session_id}/dryrun",
        "/validate",
    }
)

#: Files a module write may contain. Defined by the loader, because "what a
#: module is made of" is the loader's fact and a second copy of it here would
#: drift.
WRITABLE_MODULE_FILES: frozenset[str] = WRITABLE_FILES

#: The keys ``POST /state`` accepts (SPEC-0 §6.4, ``StatePatch`` in the
#: OpenAPI). Anything else is a 400 rather than a silent discard: the contract
#: and the code have to agree, and the failure mode of disagreeing is a caller
#: told its request worked (OI-3).
STATE_PATCH_KEYS: frozenset[str] = frozenset({"dev_toggles", "proxy_running"})

#: Session ids that mean "the live ring buffer" to the dry runner. Not a real
#: session, so it never reaches the session store.
LIVE_SESSION_IDS: frozenset[str] = frozenset({"live", "ring"})

#: How long after a request from a client we still call it active (REQ MCP-033).
CLIENT_ACTIVE_TTL_S = 60.0

#: Path prefixes served without a token. The web UI's own assets: the page has
#: to load before it can present a token, and the assets are ours, not data.
PUBLIC_PREFIXES: tuple[str, ...] = ("/assets/", "/favicon", "/vite.svg")


class ClientActivity:
    """Which client kinds have been heard from lately (REQ MCP-033, OI-4).

    ``clients.mcp_connected`` was hard-coded to zero, so the web UI's MCP
    activity indicator had nothing to read. There is no registration endpoint
    and no place in the protocol for one, but every MCP request already carries
    ``X-Pporlock-Client: mcp`` — so "was the MCP server active in the last
    minute" is answerable from what the daemon already sees, needs no new
    surface, and is the more useful signal in any case: an idle stdio connection
    the user has forgotten about is not what the indicator is for.

    What this cannot observe is the MCP server's ``--read-only`` flag. Nothing
    on the wire carries it, so ``mcp_read_only`` is reported false and the
    indicator says "active", not "read/write". Inferring it from the absence of
    mutating calls would be a guess that reads as a fact.
    """

    __slots__ = ("_seen", "ttl")

    def __init__(self, ttl: float = CLIENT_ACTIVE_TTL_S) -> None:
        self.ttl = ttl
        self._seen: dict[str, float] = {}

    def touch(self, client: str, *, now: float | None = None) -> None:
        self._seen[client] = time.time() if now is None else now

    def last_seen(self, client: str) -> float | None:
        return self._seen.get(client)

    def is_active(self, client: str, *, now: float | None = None) -> bool:
        seen = self._seen.get(client)
        if seen is None:
            return False
        current = time.time() if now is None else now
        return (current - seen) <= self.ttl

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        seen = self._seen.get("mcp")
        return {
            "mcp_connected": 1 if self.is_active("mcp", now=now) else 0,
            # Not observable from the control API; see the class docstring.
            "mcp_read_only": False,
            "mcp_last_seen": (
                datetime.fromtimestamp(seen, tz=UTC).isoformat() if seen is not None else None
            ),
            "active_ttl_s": self.ttl,
        }


def error_response(exc: PporlockError, status: int) -> JSONResponse:
    return JSONResponse({"error": exc.to_dict()}, status_code=status)


def _read_module_files(path: Path) -> dict[str, Any]:
    """A module's source and its asset listing, for the editor.

    Assets are listed rather than returned: they are arbitrary bytes of
    arbitrary size, and a listing is what an editor needs to show a tree.
    """
    files = {
        name: (path / name).read_text()
        for name in sorted(WRITABLE_MODULE_FILES)
        if (path / name).is_file()
    }
    assets_dir = path / ASSETS_DIR
    assets = (
        sorted(str(p.relative_to(assets_dir)) for p in assets_dir.rglob("*") if p.is_file())
        if assets_dir.is_dir()
        else []
    )
    return {"files": files, "assets": assets}


def _remove_module_dir(directory: Path) -> None:
    """Delete a module's directory. Already absent counts as done — the caller
    asked for it to be gone, and it is."""
    shutil.rmtree(directory, ignore_errors=True)


def _write_module_files(directory: Path, files: dict[str, str]) -> None:
    """Write a module's files, removing any the caller left out.

    A replace that left the old ``module.py`` behind would keep running code the
    author believes they deleted.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted(WRITABLE_MODULE_FILES):
        target = directory / name
        if name in files:
            target.write_text(str(files[name]))
        elif target.is_file():
            target.unlink()


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
        elif not is_public:
            # Non-mutating requests are not *required* to identify themselves,
            # but they do, and it is what makes the activity indicator work on a
            # client that has only ever read (REQ MCP-033).
            declared = request.headers.get(CLIENT_HEADER)
            if declared:
                try:
                    client = require_client(declared)
                except AuthError:
                    client = "cli"
        request.scope["pporlock_client"] = client
        self.control.clients.touch(client)

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
        registry: ModuleRegistry | None = None,
        profiles: ProfileManager | None = None,
        static_dir: Any = None,
        base_ruleset: RuleSet | None = None,
        sessions: SessionStore | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.config = config
        self.ring = ring
        self.interceptor = interceptor
        # Both optional: the CLI wires them, but a daemon with no module root —
        # and every test that only cares about flows — is a legitimate state,
        # and the module routes answer "nothing here" rather than failing.
        self.registry = registry
        self.profiles = profiles
        # The rules from rules.yaml, kept so reinstalling the module rules does
        # not delete them. They are the user's own and nothing in the module
        # lifecycle owns them.
        self.base_ruleset = base_ruleset if base_ruleset is not None else RuleSet()
        self.version = version
        self.tokens = tokens or TokenStore(Path(config.state_dir))
        self.policy = policy or OriginPolicy(config.control.listen_host, config.control.listen_port)
        self.pairing = pairing or PairingWindow()
        self.audit = audit or AuditLog()
        # Who has been talking to us lately, for the MCP activity indicator
        # (REQ MCP-033, OI-4).
        self.clients = ClientActivity()
        self.events = events or EventHub()
        self.attribution = AttributionIndex()
        self.static_dir = static_dir
        # One Redactor, shared by the serializer and the session writer. Sharing
        # it is what makes PUT /config's effect immediate in both places rather
        # than in whichever one happened to be rebuilt (REQ CAP-044).
        self.redactor = Redactor(config.redaction)
        self.sessions = sessions or SessionStore(
            Path(config.state_dir).expanduser() / "sessions",
            self.redactor,
            max_bytes=config.capture.session_max_bytes,
            max_body_bytes=config.capture.max_body_bytes,
            version=version,
        )
        # Generate the token now rather than on first verify. It is per-install
        # state the user and the CLI need to be able to find, and a path printed
        # at startup that does not yet exist is worse than no path at all.
        self.tokens.ensure()
        self.dev_toggles = {"anticache": False, "anticomp": False}
        self.active_profile = profiles.active_name if profiles else DEFAULT_PROFILE
        self.asgi = self._build()

    # -- helpers ---------------------------------------------------------

    async def offload(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run blocking work off the proxy's event loop (REQ API-002)."""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    @staticmethod
    def _not_found(message: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "not_found", "message": message, "detail": {}}},
            status_code=404,
        )

    def _registry_or_raise(self) -> ModuleRegistry:
        """The registry, or a loud failure.

        Not an ``assert``: asserts are stripped under ``python -O``, which would
        turn this invariant into an ``AttributeError`` on ``None`` several
        frames away from the cause. Every public caller checks for ``None`` and
        returns 404 first, so reaching here means a route was wired up without
        that check — a programming error, and it should say so.
        """
        if self.registry is None:
            raise RuntimeError("module route reached with no registry configured")
        return self.registry

    def _profiles_or_raise(self) -> ProfileManager:
        """The profile store, or a loud failure. See _registry_or_raise."""
        if self.profiles is None:
            raise RuntimeError("profile route reached with no profile store configured")
        return self.profiles

    def _client(self, request: Request) -> str:
        return str(request.scope.get("pporlock_client", "cli"))

    def _module_summary(self) -> dict[str, Any]:
        modules = self.registry.modules if self.registry is not None else ()
        return {
            "loaded": len(modules),
            "enabled": sum(1 for m in modules if m.enabled),
            "quarantined": sum(1 for m in modules if m.state == "quarantined"),
            "errors": [m.error.to_dict() | {"module": m.name} for m in modules if m.error],
        }

    def _module_dir(self, name: str) -> Path:
        """The directory a module named ``name`` lives in.

        The name is validated against the loader's own pattern before it is
        joined to the root, which is also what keeps ``../`` out of a path the
        caller supplies.
        """
        if not re.match(MODULE_NAME_PATTERN, name):
            raise ConfigError(f"{name!r} is not a valid module name", module=name)
        return Path(self.config.modules.root) / name

    def reload_modules(self) -> ReloadResult:
        """Rebuild the module set from disk. Blocking — always offloaded."""
        registry = self._registry_or_raise()
        transforms = self.interceptor.evaluator.transforms if self.interceptor else None
        return registry.reload(transforms, self.active_profile)

    def apply_modules(self) -> None:
        """Install the active modules' rules on the running proxy.

        A whole new snapshot, so an in-flight flow finishes against the module
        set it started with (REQ MOD-004). The active profile narrows which
        modules contribute (REQ MOD-043).
        """
        if self.registry is None or self.interceptor is None:
            return
        module_filter = self.profiles.module_filter() if self.profiles else None
        self.interceptor.replace_ruleset(
            RuleSet.combine(self.base_ruleset, self.registry.build_ruleset(module_filter))
        )
        # The evaluator interleaves Python hooks with declarative rules, so it
        # needs the registry as well as the rules built from it (REQ MOD-023).
        self.interceptor.evaluator.registry = self.registry

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
                "running": self.interceptor is not None and self.interceptor.proxy_listening,
                "listen": f"{self.config.proxy.listen_host}:{self.config.proxy.listen_port}",
                "uptime_s": self.interceptor.uptime_s if self.interceptor is not None else 0.0,
            },
            "active_profile": self.active_profile,
            "dev_toggles": dict(self.dev_toggles),
            "modules": self._module_summary(),
            "capture": {
                "ring_flows": stats.flows,
                "ring_bytes": stats.bytes,
                "recording_session": self.sessions.recording_session,
            },
            "counters": counters,
            "clients": self.clients.to_dict(),
        }

    # -- routes ----------------------------------------------------------

    async def health(self, _: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": self.version})

    async def get_state(self, _: Request) -> JSONResponse:
        return JSONResponse(self._state_payload())

    async def post_state(self, request: Request) -> JSONResponse:
        """Apply a StatePatch (SPEC-0 §6.4).

        Every key the contract declares is implemented, and every key it does
        not is refused. The route used to read ``dev_toggles`` and drop the
        rest, answering 200 with a payload saying the proxy was still running —
        so an agent calling ``proxy_stop`` was told it had worked (OI-3). A
        route that reports success for an effect it did not have is worse than
        one that refuses.
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ConfigError("the state patch must be a mapping")
        unknown = set(body) - STATE_PATCH_KEYS
        if unknown:
            raise ConfigError(
                f"cannot set {', '.join(sorted(unknown))} on /state; "
                f"the patchable keys are {', '.join(sorted(STATE_PATCH_KEYS))}",
                setting=sorted(unknown)[0],
            )

        client = request.scope.get("pporlock_client", "cli")
        toggles = body.get("dev_toggles") or {}
        if not isinstance(toggles, dict):
            raise ConfigError("dev_toggles must be a mapping", setting="dev_toggles")
        unknown_toggles = set(toggles) - {"anticache", "anticomp"}
        if unknown_toggles:
            raise ConfigError(
                f"unknown dev toggle: {', '.join(sorted(unknown_toggles))}",
                setting="dev_toggles",
            )
        for name in ("anticache", "anticomp"):
            if name in toggles:
                self.dev_toggles[name] = bool(toggles[name])
                # The addon owns the behaviour; the app mirrors it for /state.
                if self.interceptor is not None:
                    self.interceptor.dev_toggles[name] = self.dev_toggles[name]
                self.audit.record(client, "dev_toggle", toggle=name, value=self.dev_toggles[name])

        if "proxy_running" in body:
            wanted = bool(body["proxy_running"])
            if self.interceptor is None:
                return error_response(
                    ProxyControlError(
                        "no proxy is attached to this control server, so its "
                        "listener cannot be started or stopped",
                        requested=wanted,
                    ),
                    409,
                )
            try:
                await self.interceptor.set_proxy_running(wanted)
            except ProxyControlError as exc:
                return error_response(exc, 409)
            self.audit.record(client, "set_proxy_running", running=wanted)

        self.events.publish("state.changed", {"dev_toggles": dict(self.dev_toggles)})
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
                redactor=self.redactor,
            )
        )

    async def get_flow(self, request: Request) -> JSONResponse:
        flow_id = request.path_params["flow_id"]
        record = self.ring.get(flow_id)
        if record is None:
            return self._not_found(f"no flow {flow_id}")

        unmask = request.query_params.get("unmask")
        if unmask:
            return self._unmask(request, record, unmask)

        detail = parse_detail(request.query_params.get("detail"), DEFAULT_ITEM_DETAIL)
        return JSONResponse(serialize_flow(record, detail, self.redactor))

    def _unmask(self, request: Request, record: FlowRecord, field_path: str) -> JSONResponse:
        """Reveal one masked value from the live ring buffer (REQ CAP-043).

        Three constraints, and each one is doing separate work:

        * **Live only.** This handler is reachable from ``/flows/{id}`` and
          nothing else. A session flow cannot arrive here — and if one somehow
          did, there would be nothing to reveal, because the value was masked
          before it reached the file (REQ CAP-045).
        * **UI only.** The client header is required and must say ``ui``. The
          MCP interface has no unmask capability by construction (REQ MCP-003);
          this is the server-side half of that, so a future MCP build cannot
          acquire one by calling a URL.
        * **One value.** A field path names a single header occurrence or a
          single JSON field. There is no bulk form.
        """
        try:
            client = require_client(request.headers.get(CLIENT_HEADER))
        except AuthError as exc:
            return error_response(exc, 403)
        if client != "ui":
            return error_response(
                AuthError(
                    "unmasking is available only from the pporlock web UI",
                    client=client,
                    reason="unmask_ui_only",
                ),
                403,
            )

        try:
            value = resolve_field(record, field_path)
        except FieldPathError as exc:
            return JSONResponse(
                {"error": {"code": "not_found", "message": str(exc), "detail": {}}},
                status_code=404,
            )

        # REQ MCP-031. The field path is recorded; the value never is. An audit
        # log that quoted what it protected would be the leak it exists to make
        # visible.
        self.audit.record(client, "unmask", flow_id=record.flow_id, field_path=field_path)
        return JSONResponse(
            {"flow_id": record.flow_id, "field_path": field_path, "value": value},
            headers={"cache-control": "no-store"},
        )

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

    def _config_path(self) -> Path:
        return Path(self.config.state_dir).expanduser() / "config.yaml"

    async def put_config(self, request: Request) -> JSONResponse:
        """Change the settable configuration sections (REQ CAP-044).

        The redaction pattern lists are the reason this route exists: they must
        be editable, and the effective configuration must be readable back, or
        "redaction is configurable" is a claim nobody can check. ``GET /config``
        returns the full effective configuration, so the UI can show exactly
        what is in force.

        Redaction takes effect immediately, because the Redactor is shared.
        Buffering, capture, budget, and logging are persisted and applied at
        next start — the proxy's guards are built when it starts and rebuilding
        them under live traffic is a Sprint 16 concern, not a silent one.
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ConfigError("the request body must be a mapping of sections")

        updated = await self.offload(update_config, self.config, dict(body))
        self.config = updated
        # Swapped whole rather than mutated field by field: the writer thread
        # reads this object, and a half-applied pattern list is a window in
        # which a header is neither on the old list nor the new one.
        self.redactor.cfg = updated.redaction
        self.sessions.max_bytes = updated.capture.session_max_bytes

        await self.offload(save_config, updated, self._config_path())
        self.audit.record(self._client(request), "put_config", sections=sorted(body))
        self.events.publish("state.changed", {"config": sorted(body)})
        payload: dict[str, Any] = await self.offload(self.config.to_dict)
        return JSONResponse(payload)

    # -- sessions (REQ CAP-020, CAP-021, CAP-024) ------------------------

    async def get_sessions(self, _: Request) -> JSONResponse:
        sessions: list[SessionMeta] = await self.offload(self.sessions.list)
        return JSONResponse([s.to_dict() for s in sessions])

    async def post_sessions(self, request: Request) -> JSONResponse:
        """Start recording. Opt-in and off by default (REQ CAP-020)."""
        body = await request.json()
        name = str(body.get("name") or "")
        try:
            meta: SessionMeta = await self.offload(self._start_session, name)
        except SessionError as exc:
            return error_response(exc, 409)
        self.audit.record(self._client(request), "start_session", session_id=meta.session_id)
        self.events.publish("session.changed", {"session_id": meta.session_id, "state": meta.state})
        return JSONResponse(meta.to_dict(), status_code=201)

    def _start_session(self, name: str) -> SessionMeta:
        return self.sessions.start(name, profile=self.active_profile)

    async def get_session(self, request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        meta = await self._session_meta(session_id)
        if meta is None:
            return self._not_found(f"no session {session_id}")
        return JSONResponse(meta.to_dict())

    async def _session_meta(self, session_id: str) -> SessionMeta | None:
        try:
            return await self.offload(self.sessions.get, session_id)  # type: ignore[no-any-return]
        except SessionError:
            return None

    async def patch_session(self, request: Request) -> JSONResponse:
        """Rename a session (REQ CAP-021). Nothing else is patchable: a session
        is a recording, and everything else about it is a fact about what
        happened."""
        session_id = request.path_params["session_id"]
        body = await request.json()
        unknown = set(body) - {"name"}
        if unknown:
            raise ConfigError(f"cannot patch {', '.join(sorted(unknown))}")
        try:
            meta = await self.offload(self.sessions.rename, session_id, str(body.get("name") or ""))
        except SessionError:
            meta = None
        if meta is None:
            return self._not_found(f"no session {session_id}")
        self.audit.record(self._client(request), "rename_session", session_id=session_id)
        return JSONResponse(meta.to_dict())

    async def post_session_stop(self, request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        try:
            meta = await self.offload(self.sessions.stop, session_id)
        except SessionError as exc:
            return error_response(exc, 409)
        self.audit.record(
            self._client(request),
            "stop_session",
            session_id=session_id,
            flows=meta.flow_count,
            dropped=meta.dropped,
        )
        self.events.publish("session.changed", {"session_id": session_id, "state": "stopped"})
        return JSONResponse(meta.to_dict())

    async def delete_session(self, request: Request) -> Response:
        session_id = request.path_params["session_id"]
        try:
            deleted = await self.offload(self.sessions.delete, session_id)
        except SessionError:
            deleted = False
        if not deleted:
            return self._not_found(f"no session {session_id}")
        self.audit.record(self._client(request), "delete_session", session_id=session_id)
        self.events.publish("session.changed", {"session_id": session_id, "state": "deleted"})
        return Response(status_code=204)

    async def get_session_flows(self, request: Request) -> JSONResponse:
        """A page of a session's flows, same filter vocabulary as /flows.

        No ``unmask`` parameter is accepted, and adding one here would have
        nothing to reveal: these records were redacted before they reached the
        file (REQ CAP-045).
        """
        session_id = request.path_params["session_id"]
        params = dict(request.query_params)
        detail = parse_detail(params.get("detail"), DEFAULT_LIST_DETAIL)
        try:
            limit = int(params.get("limit", 100))
        except ValueError:
            limit = 100
        try:
            result = await self.offload(
                self._session_query,
                session_id,
                FlowFilter.from_query(params),
                limit,
                params.get("cursor"),
            )
        except SessionError as exc:
            return error_response(exc, 404)
        return JSONResponse(
            serialize_flow_page(
                result.flows,
                next_cursor=result.next_cursor,
                total_estimate=result.total_estimate,
                detail=detail,
            )
        )

    def _session_query(
        self, session_id: str, flow_filter: FlowFilter, limit: int, cursor: str | None
    ) -> Any:
        return self.sessions.reader(session_id).query(flow_filter, limit=limit, cursor=cursor)

    async def get_session_export(self, request: Request) -> JSONResponse:
        """HAR or pporlock-native export (REQ CAP-024).

        Reads the session file, which is already redacted, so an export cannot
        carry a raw secret regardless of who asks for it.
        """
        session_id = request.path_params["session_id"]
        fmt = (request.query_params.get("format") or "pporlock").strip().lower()
        if fmt not in EXPORT_FORMATS:
            raise ConfigError(
                f"unknown export format {fmt!r}; expected one of "
                f"{', '.join(sorted(EXPORT_FORMATS))}"
            )
        try:
            payload = await self.offload(self._export, session_id, fmt)
        except SessionError as exc:
            return error_response(exc, 404)
        self.audit.record(
            self._client(request), "export_session", session_id=session_id, format=fmt
        )
        return JSONResponse(
            payload,
            headers={
                "content-disposition": f'attachment; filename="{session_id}.{fmt}.json"',
                "cache-control": "no-store",
            },
        )

    def _export(self, session_id: str, fmt: str) -> dict[str, Any]:
        return export_session(self.sessions.reader(session_id), fmt)

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
        # REQ PRF-007. Accumulated on the addon as flows complete, not computed
        # here: this route is inline-classified and may only read memory.
        modules = self.interceptor.module_cost.to_list() if self.interceptor is not None else []
        return JSONResponse(
            {
                "ring": stats.to_dict(),
                "counters": counters,
                # An expensive module should be identifiable rather than merely
                # suspected, so this is ordered most-expensive first and carries
                # max alongside mean: a module that is slow on one page and fast
                # on four hundred disappears into an average.
                "modules": modules,
                # The Sprint 6 decision criterion is measured against this, so
                # it lives in the product rather than in a one-off script.
                # Coverage is over flows; the index's own counters are join
                # diagnostics and count attempts (see AttributionStats).
                "attribution": {
                    **self.attribution.stats.to_dict(),
                    **coverage_of(self.ring.query(limit=1000).flows).to_dict(),
                },
            }
        )

    async def get_rules(self, _: Request) -> JSONResponse:
        """The rules currently in force, as loaded."""
        ruleset = self.interceptor.evaluator.ruleset if self.interceptor else RuleSet()
        return JSONResponse({"rules": rules_to_dicts(ruleset), "count": len(ruleset)})

    async def put_rules(self, request: Request) -> JSONResponse:
        """Replace the rule set. Takes effect without restarting the proxy.

        The new set is compiled first and only swapped in if it compiles
        cleanly, so a bad edit leaves the running rules untouched rather than
        emptying them. The swap itself replaces an immutable snapshot, so an
        in-flight flow finishes against the rules it started with (REQ MOD-004).
        """
        body = await request.json()
        raw = body.get("rules")
        if not isinstance(raw, list):
            raise RuleValidationError("'rules' must be a list")

        ruleset = await self.offload(
            lambda: RuleSet.from_rules(raw, module=str(body.get("module") or "api"))
        )

        if self.interceptor is not None:
            self.interceptor.replace_ruleset(ruleset)

        self.audit.record(
            request.scope.get("pporlock_client", "cli"), "put_rules", count=len(ruleset)
        )
        self.events.publish("state.changed", {"rules": len(ruleset)})
        return JSONResponse({"rules": rules_to_dicts(ruleset), "count": len(ruleset)})

    # -- modules (REQ API-023) -------------------------------------------

    async def get_modules(self, _: Request) -> JSONResponse:
        """Every module the daemon knows about, loaded or failed.

        Failures are listed rather than omitted: a module missing from the list
        because it would not parse is how an author concludes the daemon never
        saw it (REQ MOD-005).
        """
        if self.registry is None:
            return JSONResponse([])
        return JSONResponse([m.to_dict() for m in self.registry.modules])

    async def get_module(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        module = self.registry.get(name) if self.registry is not None else None
        if module is None:
            return self._not_found(f"no module {name}")
        payload = module.to_dict()
        payload.update(await self.offload(_read_module_files, module.path))
        return JSONResponse(payload)

    async def post_modules(self, request: Request) -> JSONResponse:
        """Create a module — disabled (REQ MCP-030).

        A module that ran the moment it was written would make "write it, then
        read what it does before letting it near your traffic" impossible, which
        is the one review step an agent-authored module gets.
        """
        if self.registry is None:
            return self._not_found("no module registry is configured")
        body = await request.json()
        name = str(body.get("name") or "")
        if self.registry.get(name) is not None:
            raise ConfigError(f"module {name!r} already exists", module=name)
        return await self._install_module(
            request, name, body.get("files"), enabled=False, status=201
        )

    async def put_module(self, request: Request) -> JSONResponse:
        """Replace a module's files. Never enables it (REQ MCP-030).

        Enablement is API state, not manifest state: an update that flipped a
        module on because the new manifest said ``enabled: true`` would be a
        write turning into a deployment.
        """
        if self.registry is None:
            return self._not_found("no module registry is configured")
        name = request.path_params["name"]
        existing = self.registry.get(name)
        body = await request.json()
        return await self._install_module(
            request,
            name,
            body.get("files"),
            enabled=bool(existing.enabled) if existing else False,
            status=200,
        )

    async def _install_module(
        self, request: Request, name: str, files: Any, *, enabled: bool, status: int
    ) -> JSONResponse:
        registry = self._registry_or_raise()
        directory = self._module_dir(name)
        if not isinstance(files, dict) or not files:
            raise ConfigError("'files' must be a non-empty mapping of filename to contents")
        unknown = set(files) - WRITABLE_MODULE_FILES
        if unknown:
            raise ConfigError(f"cannot write {', '.join(sorted(unknown))}", module=name)
        if MANIFEST_NAME not in files:
            raise ConfigError(f"{MANIFEST_NAME} is required", module=name)

        await self.offload(_write_module_files, directory, dict(files))
        await self.offload(self.reload_modules)
        registry.set_enabled(name, enabled)
        self.apply_modules()

        module = registry.get(name)
        if module is None:
            return self._not_found(f"no module {name}")
        self.audit.record(self._client(request), "write_module", module=name, enabled=enabled)
        self.events.publish("state.changed", {"modules": self._module_summary()})
        return JSONResponse(module.to_dict(), status_code=status)

    async def patch_module(self, request: Request) -> JSONResponse:
        """Set enabled and/or priority. Nothing else.

        Narrow on purpose: everything else about a module lives in files, and a
        PATCH that could rewrite behaviour would bypass the reload that makes a
        change visible in the module's load state.
        """
        name = request.path_params["name"]
        module = self.registry.get(name) if self.registry is not None else None
        if module is None or self.registry is None:
            return self._not_found(f"no module {name}")

        body = await request.json()
        unknown = set(body) - {"enabled", "priority"}
        if unknown:
            raise ConfigError(f"cannot patch {', '.join(sorted(unknown))}", module=name)

        if "enabled" in body:
            self.registry.set_enabled(name, bool(body["enabled"]))
        if "priority" in body:
            try:
                self.registry.set_priority(name, int(body["priority"]))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"priority must be an integer: {body['priority']!r}") from exc

        self.apply_modules()
        # REQ MCP-031 — enabling a module changes what happens to traffic, so
        # who did it and when is recorded.
        self.audit.record(
            self._client(request),
            "patch_module",
            module=name,
            enabled=module.enabled,
            priority=module.priority,
        )
        self.events.publish("state.changed", {"modules": self._module_summary()})
        return JSONResponse(module.to_dict())

    async def delete_module(self, request: Request) -> Response:
        name = request.path_params["name"]
        module = self.registry.get(name) if self.registry is not None else None
        if module is None or self.registry is None:
            return self._not_found(f"no module {name}")
        await self.offload(_remove_module_dir, self._module_dir(name))
        await self.offload(self.reload_modules)
        self.apply_modules()
        self.audit.record(self._client(request), "delete_module", module=name)
        self.events.publish("state.changed", {"modules": self._module_summary()})
        return Response(status_code=204)

    async def post_modules_reload(self, request: Request) -> JSONResponse:
        """Reload every module from disk (REQ MOD-024).

        The whole set, not one module: modules are ordered against each other by
        priority, so a partial reload would leave an ordering nobody declared.
        """
        if self.registry is None:
            return self._not_found("no module registry is configured")
        result: ReloadResult = await self.offload(self.reload_modules)
        self.apply_modules()
        self.audit.record(
            self._client(request),
            "reload_modules",
            loaded=result.loaded,
            errors=len(result.errors),
        )
        self.events.publish("state.changed", {"modules": self._module_summary()})
        return JSONResponse(result.to_dict())

    # -- validation, suggestion, dry run (REQ API-027, CAP-030-033) ------

    async def post_validate(self, request: Request) -> JSONResponse:
        """Validate a candidate module without installing it (REQ API-027).

        Writes nothing, installs nothing, and does not execute the candidate's
        Python — it compiles it. See ``engine/modules/validate.py`` for why that
        distinction is the point rather than a shortcut.
        """
        body = await request.json()
        if not isinstance(body, dict):
            raise ConfigError("the validation request body must be a mapping")
        files = body.get("files")
        if not isinstance(files, dict) or not files:
            raise ConfigError("'files' must be a non-empty mapping of filename to contents")
        # No name given — the web UI's editor validates a file before it has
        # named it — means "use the manifest's own", not "call it candidate and
        # then report a name mismatch against a name the caller never chose".
        raw_name = body.get("name")
        name = str(raw_name) if raw_name else None
        report = await self.offload(
            validate_module_files, name, {k: str(v) for k, v in files.items()}
        )
        return JSONResponse(report.to_dict())

    async def post_suggest_rule(self, request: Request) -> JSONResponse:
        """A candidate rule matching one flow (REQ WUI-008, MCP-014)."""
        flow_id = request.path_params["flow_id"]
        record = self.ring.get(flow_id)
        if record is None:
            return self._not_found(f"no flow {flow_id}")
        body = await request.json()
        if not isinstance(body, dict):
            raise ConfigError("the suggestion request body must be a mapping")
        payload = suggest_rule(record, str(body.get("intent") or ""))
        self.audit.record(
            self._client(request), "suggest_rule", flow_id=flow_id, intent=payload["intent"]
        )
        return JSONResponse(payload)

    def dry_runner(self) -> DryRunner:
        """The dry runner, built from live daemon state.

        The evaluator handed over is the one the proxy is using, so the clone it
        makes inherits every setting live traffic is evaluated under
        (REQ CAP-031). With no proxy attached — a control server started without
        one — a default evaluator still gives the module system somewhere to
        run, which is what makes the route usable from tests and from the CLI.
        """
        evaluator = self.interceptor.evaluator if self.interceptor is not None else Evaluator()
        return DryRunner(
            evaluator,
            installed_root=Path(self.config.modules.root).expanduser(),
            redactor=self.redactor,
            budget_ms=self.config.budget.per_flow_ms,
        )

    async def post_session_dryrun(self, request: Request) -> JSONResponse:
        """Replay a session — or the live ring — through candidate modules.

        ``session_id`` may be ``live``, which replays the ring buffer. The two
        sources are the same ``FlowRecord`` shape, so this is a different
        iterable and nothing else; a separate route for it would have been a
        second code path to keep honest for no gain.

        **This executes the candidate module's Python hooks** (REQ CAP-032). It
        is stated in the tool descriptions, in the module authoring guide, and
        in the web UI's dry-run panel, because dry-running an agent-authored
        module runs that agent's code on this machine.
        """
        session_id = request.path_params["session_id"]
        body = await request.json()
        dry_request = DryRunRequest.from_dict(body)

        if session_id in LIVE_SESSION_IDS:
            flows: list[FlowRecord] = self.ring.query(limit=dry_request.limit).flows
        else:
            try:
                flows = await self.offload(self._session_flows_for_dryrun, session_id, dry_request)
            except SessionError as exc:
                return error_response(exc, 404)

        result = await self.offload(self._run_dry_run, flows, dry_request)
        self.audit.record(
            self._client(request),
            "dry_run",
            session_id=session_id,
            modules=[m.name for m in dry_request.candidate_modules]
            + list(dry_request.use_installed),
            flows=result["summary"]["flows_evaluated"],
        )
        return JSONResponse(result)

    def _session_flows_for_dryrun(
        self, session_id: str, dry_request: DryRunRequest
    ) -> list[FlowRecord]:
        reader = self.sessions.reader(session_id)
        flows: list[FlowRecord] = []
        for record in reader.iter_all():
            if len(flows) >= dry_request.limit:
                break
            flows.append(record)
        return flows

    def _run_dry_run(self, flows: list[FlowRecord], dry_request: DryRunRequest) -> dict[str, Any]:
        return self.dry_runner().run(flows, dry_request)

    # -- profiles (REQ API-024) ------------------------------------------

    async def get_profiles(self, _: Request) -> JSONResponse:
        if self.profiles is None:
            return JSONResponse([])
        profiles = await self.offload(self.profiles.all_profiles)
        return JSONResponse([p.to_dict() for p in profiles])

    async def get_profile(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if self.profiles is None:
            return self._not_found(f"no profile {name}")
        profile = await self.offload(self.profiles.get, name)
        if profile is None:
            return self._not_found(f"no profile {name}")
        return JSONResponse(profile.to_dict())

    async def post_profiles(self, request: Request) -> JSONResponse:
        if self.profiles is None:
            return self._not_found("no profile store is configured")
        body = await request.json()
        return await self._save_profile(request, Profile.from_dict(dict(body)), status=201)

    async def put_profile(self, request: Request) -> JSONResponse:
        """Replace a profile. The path names it; a body that disagrees loses."""
        if self.profiles is None:
            return self._not_found("no profile store is configured")
        body = dict(await request.json())
        body["name"] = request.path_params["name"]
        return await self._save_profile(request, Profile.from_dict(body), status=200)

    async def _save_profile(
        self, request: Request, profile: Profile, *, status: int
    ) -> JSONResponse:
        profiles = self._profiles_or_raise()
        saved = await self.offload(profiles.save, profile)
        self.audit.record(self._client(request), "save_profile", profile=saved.name)
        return JSONResponse(saved.to_dict(), status_code=status)

    async def delete_profile(self, request: Request) -> Response:
        """Delete a profile. Refuses ``default`` (REQ MOD-041)."""
        name = request.path_params["name"]
        if self.profiles is None:
            return self._not_found(f"no profile {name}")
        try:
            deleted = await self.offload(self.profiles.delete, name)
        except ConfigError as exc:
            # There must always be a profile to fall back to, so this is a
            # conflict with an invariant rather than a malformed request.
            return error_response(exc, 409)
        if not deleted:
            return self._not_found(f"no profile {name}")
        self.active_profile = self.profiles.active_name
        self.apply_modules()
        self.audit.record(self._client(request), "delete_profile", profile=name)
        self.events.publish("state.changed", {"active_profile": self.active_profile})
        return Response(status_code=204)

    async def post_profile_activate(self, request: Request) -> JSONResponse:
        """Switch profiles without restarting the daemon (REQ MOD-042).

        A profile is a working context, not just a module list, so its dev
        toggles come with it — applying them separately is the step an operator
        forgets.
        """
        name = request.path_params["name"]
        if self.profiles is None:
            return self._not_found(f"no profile {name}")
        try:
            profile = await self.offload(self.profiles.activate, name)
        except ConfigError:
            return self._not_found(f"no profile {name}")

        self.active_profile = profile.name
        for toggle, value in profile.dev_toggles.items():
            self.dev_toggles[toggle] = bool(value)
            if self.interceptor is not None:
                self.interceptor.dev_toggles[toggle] = bool(value)
        self.apply_modules()

        self.audit.record(self._client(request), "activate_profile", profile=profile.name)
        self.events.publish("state.changed", {"active_profile": profile.name})
        return JSONResponse(self._state_payload())

    async def post_attribution(self, request: Request) -> JSONResponse:
        """Batched (request -> tab) associations from the extension.

        Best-effort and non-blocking: a malformed entry is dropped rather than
        failing the batch, and nothing here can delay a flow.
        """
        body = await request.json()
        raw_entries = body.get("entries") or []
        parsed = [e for e in (entry_from_dict(r) for r in raw_entries) if e is not None]
        accepted = self.attribution.submit(parsed)

        # Backfill anything already in the ring that we can now attribute, and
        # tell subscribed clients so a row they already rendered gets updated
        # rather than staying wrong (SPEC-0 §3.6, §7.3).
        backfilled = self.backfill_attribution()
        return JSONResponse(
            {
                "accepted": accepted,
                "rejected": len(raw_entries) - accepted,
                "backfilled": backfilled,
            }
        )

    def backfill_attribution(self, limit: int = 200) -> int:
        """Attribute recent unattributed flows. Returns how many were updated."""
        updated = 0
        for record in self.ring.query(limit=limit).flows:
            if record.tab_id is not None or record.request is None:
                continue
            tab_id = self.attribution.resolve(record.request.method, record.request.url)
            if tab_id is None:
                continue
            self.ring.update(record.flow_id, tab_id=tab_id)
            updated += 1
            self.events.publish_flow(
                "flow.updated",
                record,
                {"flow_id": record.flow_id, "changed": {"tab_id": tab_id}},
            )
        return updated

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

    async def post_pair_begin(self, request: Request) -> JSONResponse:
        """Open a pairing window and return the code (REQ API-012).

        Authenticated: only something that can already read the token — the CLI
        or the web UI — may open a window. That is what makes the code itself
        safe to read aloud: it is worthless without a human having just asked
        for it, and it expires in two minutes.
        """
        code = self.pairing.open()
        self.audit.record(
            request.scope.get("pporlock_client", "cli"),
            "pairing_window_opened",
            ttl_seconds=self.pairing.ttl,
        )
        return JSONResponse({"code": code, "expires_in": self.pairing.ttl})

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
            Route("/flows/{flow_id}/suggest-rule", self.post_suggest_rule, methods=["POST"]),
            Route("/exclusions", self.get_exclusions, methods=["GET"]),
            Route("/exclusions", self.put_exclusions, methods=["PUT"]),
            Route("/config", self.get_config, methods=["GET"]),
            Route("/config", self.put_config, methods=["PUT"]),
            # Registered before /sessions/{session_id} so "stop", "flows", and
            # "export" are never read as session ids.
            Route("/sessions/{session_id}/stop", self.post_session_stop, methods=["POST"]),
            Route("/sessions/{session_id}/flows", self.get_session_flows, methods=["GET"]),
            Route("/sessions/{session_id}/export", self.get_session_export, methods=["GET"]),
            Route("/sessions/{session_id}/dryrun", self.post_session_dryrun, methods=["POST"]),
            Route("/sessions", self.get_sessions, methods=["GET"]),
            Route("/sessions", self.post_sessions, methods=["POST"]),
            Route("/sessions/{session_id}", self.get_session, methods=["GET"]),
            Route("/sessions/{session_id}", self.patch_session, methods=["PATCH"]),
            Route("/sessions/{session_id}", self.delete_session, methods=["DELETE"]),
            Route("/audit", self.get_audit, methods=["GET"]),
            Route("/metrics", self.get_metrics, methods=["GET"]),
            Route("/events", self.get_events, methods=["GET"]),
            Route("/rules", self.get_rules, methods=["GET"]),
            Route("/rules", self.put_rules, methods=["PUT"]),
            Route("/validate", self.post_validate, methods=["POST"]),
            # Registered before /modules/{name}, or a reload would be read as a
            # module called "reload".
            Route("/modules/reload", self.post_modules_reload, methods=["POST"]),
            Route("/modules", self.get_modules, methods=["GET"]),
            Route("/modules", self.post_modules, methods=["POST"]),
            Route("/modules/{name}", self.get_module, methods=["GET"]),
            Route("/modules/{name}", self.put_module, methods=["PUT"]),
            Route("/modules/{name}", self.patch_module, methods=["PATCH"]),
            Route("/modules/{name}", self.delete_module, methods=["DELETE"]),
            Route("/profiles", self.get_profiles, methods=["GET"]),
            Route("/profiles", self.post_profiles, methods=["POST"]),
            Route("/profiles/{name}/activate", self.post_profile_activate, methods=["POST"]),
            Route("/profiles/{name}", self.get_profile, methods=["GET"]),
            Route("/profiles/{name}", self.put_profile, methods=["PUT"]),
            Route("/profiles/{name}", self.delete_profile, methods=["DELETE"]),
            Route("/attribution", self.post_attribution, methods=["POST"]),
            Route("/pair/begin", self.post_pair_begin, methods=["POST"]),
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
