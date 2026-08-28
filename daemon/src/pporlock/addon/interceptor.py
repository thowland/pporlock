"""The mitmproxy addon — SPEC-1 §3.1.

Hook methods are deliberately thin: normalize, decide, apply, record. All
decisions live in ``engine/``, which is what makes them testable without a proxy
(REQ TST-001) and what confines mitmproxy version churn to this package
(REQ DD-2).

Sprint 2 wires baseline interception: the ClientHello exclusion decision, flow
identity, passthrough recording, and counters. The rules engine arrives in
Sprint 7 and plugs into the marked seams; the capture ring buffer and control
server arrive in Sprint 3 and 4 behind the ``sink`` interface.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from ..config import Config
from ..engine.cost import ModuleCostIndex, decide_offload
from ..engine.evaluator import Evaluator, TimeBudget
from ..engine.exclusions import ExclusionList, load_exclusions
from ..engine.provenance import NoteCode, Provenance, ProvenanceBuilder
from ..engine.ruleset import RuleSet
from ..errors import ProxyControlError
from . import apply as apply_mod
from . import normalize

#: How long to wait for mitmproxy to actually bind or release the listener.
#: Twenty polls of 50ms — long enough for a local bind, short enough that a
#: caller is not left hanging on a listener that is never going to change.
PROXY_STATE_POLLS = 20
PROXY_STATE_POLL_INTERVAL_S = 0.05


class FlowSink(Protocol):
    """Where completed flows go.

    An interface rather than a concrete buffer so Sprint 2 can run with a
    counting stub and Sprint 3 can drop the ring buffer in without touching the
    addon.
    """

    def record_http(
        self, request: Any, response: Any, provenance: Provenance, timing: dict[str, float]
    ) -> None: ...

    def record_passthrough(
        self, host: str | None, ip: str | None, provenance: Provenance, timing: dict[str, float]
    ) -> None: ...

    def record_websocket_message(self, message: Any) -> None: ...

    def record_websocket_close(self, flow_id: str, close_code: Any) -> None: ...


class NullSink:
    """Counts flows and discards them. Replaced by the ring buffer in Sprint 3."""

    def __init__(self) -> None:
        self.http = 0
        self.passthrough = 0
        self.websocket_messages = 0

    def record_http(
        self, request: Any, response: Any, provenance: Provenance, timing: dict[str, float]
    ) -> None:
        self.http += 1

    def record_passthrough(
        self, host: str | None, ip: str | None, provenance: Provenance, timing: dict[str, float]
    ) -> None:
        self.passthrough += 1

    def record_websocket_message(self, message: Any) -> None:
        self.websocket_messages += 1

    def record_websocket_close(self, flow_id: str, close_code: Any) -> None:
        """No-op: there is nothing to close on a sink that discards."""


class Counters:
    """Flow tallies surfaced through GET /state (SPEC-0 §6.4)."""

    __slots__ = ("blocked", "errors", "flows_total", "modified", "passthrough")

    def __init__(self) -> None:
        self.flows_total = 0
        self.blocked = 0
        self.modified = 0
        self.passthrough = 0
        self.errors = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "flows_total": self.flows_total,
            "blocked": self.blocked,
            "modified": self.modified,
            "passthrough": self.passthrough,
            "errors": self.errors,
        }


class Interceptor:
    """The addon. One instance per proxy process."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        sink: FlowSink | None = None,
        exclusions: ExclusionList | None = None,
        profile: str = "default",
        control: Any = None,
        evaluator: Evaluator | None = None,
    ) -> None:
        self.config = config or Config()
        self.sink: FlowSink = sink or NullSink()
        self.exclusions = exclusions if exclusions is not None else load_exclusions()
        self.profile = profile
        self.counters = Counters()
        # Per-module timing for GET /metrics (REQ PRF-007). Accumulated as flows
        # complete so the inline-classified metrics route never has to walk the
        # ring buffer to answer.
        self.module_cost = ModuleCostIndex()
        self.started_at = time.time()
        self._ws_indexes: dict[str, int] = {}
        # Set by the runner when the control server should be started from
        # running(). Left None in tests and in bare-addon use.
        self.control: Any = control
        self.control_server: Any = None
        # The evaluator holds an immutable rule-set snapshot. Reload swaps the
        # whole evaluator rather than mutating it, so an in-flight flow finishes
        # against the snapshot it started with (REQ MOD-004).
        # Development toggles (REQ PXY-043). Both alter traffic in ways that
        # make normal behaviour unreproducible, so both default off and every
        # flow processed while one is on carries a note saying so.
        self.dev_toggles: dict[str, bool] = {"anticache": False, "anticomp": False}
        # Last commanded listener state. Corroborated against mitmproxy's own
        # listener addresses whenever there is a master to ask.
        self._proxy_running = True
        self.evaluator = (
            evaluator if evaluator is not None else Evaluator(RuleSet(), exclusions=self.exclusions)
        )

    # -- lifecycle -------------------------------------------------------

    def running(self) -> None:
        """Called once the proxy is up.

        The control server starts here, on the proxy's own event loop, which is
        what removes any need for IPC or locking between hooks and handlers
        (REQ DD-3, API-001).
        """
        if self.control is None:
            return
        import asyncio

        from ..control.server import ControlServer

        self.control_server = ControlServer(self.control, self.config)
        asyncio.create_task(self.control_server.start())  # noqa: RUF006

    def done(self) -> None:
        """Called on shutdown."""

    # -- interception hooks ----------------------------------------------

    def tls_clienthello(self, data: Any) -> None:
        """Exclusion decision (REQ PXY-013).

        Setting ``ignore_connection`` tunnels the connection undecrypted, so
        there is no downstream failure to handle. The connection is still
        recorded as a passthrough — host and timing, no content — because
        excluded traffic must be visible even though it is not readable
        (REQ PXY-015).
        """
        sni = normalize.sni_of(data)
        ip = normalize.peer_ip_of(data)
        decision = self.exclusions.decide(sni, ip)
        if not decision.excluded:
            return

        data.ignore_connection = True
        self.counters.passthrough += 1

        builder = ProvenanceBuilder(self.profile)
        builder.note(
            NoteCode.PASSTHROUGH_EXCLUDED,
            f"tunneled undecrypted: {decision.pattern}",
            pattern=decision.pattern,
            reason=decision.comment,
            source=decision.source,
        )
        self.sink.record_passthrough(sni, ip, builder.build(), {"started_at": time.time()})

    def request(self, flow: Any) -> None:
        """Request-side evaluation: short-circuit rules, then header rules.

        Every flow carries provenance from birth, whether or not any rule
        matched (REQ CAP-013).
        """
        started = time.perf_counter()
        flow_id = _flow_id(flow)
        builder = ProvenanceBuilder(self.profile)

        request = normalize.normalize_request(flow, flow_id=flow_id, body=flow.request.content)
        budget = TimeBudget(self.config.budget.per_flow_ms)
        self._apply_dev_toggles(flow, builder)
        decision = self.evaluator.evaluate_request(request, builder, budget)

        if decision.kill:
            # Opt-in only: flow.kill() is the wrong default because a page's
            # JavaScript reacts badly to a failed fetch (REQ PXY-031).
            flow.kill()
            self.counters.blocked += 1
        elif apply_mod.apply_request_mutation(flow, decision.mutation):
            if decision.short_circuit is not None:
                self.counters.blocked += 1
            else:
                self.counters.modified += 1

        # Re-normalise after applying, so the captured request is the one that
        # actually went out. Provenance explains what changed; the record should
        # show the result, not the input — otherwise the panel displays a
        # request that was never sent.
        if not decision.kill and not decision.mutation.is_empty():
            request = normalize.normalize_request(
                flow, flow_id=flow_id, tab_id=request.tab_id, body=flow.request.content
            )

        _stash(flow, "request", request)
        _stash(flow, "builder", builder)
        _stash(flow, "started", started)
        _stash(flow, "wants_body", decision.wants_body)
        _stash(flow, "budget", budget)

        self.counters.flows_total += 1

    def responseheaders(self, flow: Any) -> None:
        """The buffering decision, which can only be made here (REQ PXY-021).

        Deciding to stream means the body is never held in memory, so any body
        transform for this flow becomes impossible — which is why the engine
        records that it was skipped rather than doing nothing quietly.
        """
        if flow.response is None:
            return

        request = flow.metadata.get("pporlock.request")
        builder = flow.metadata.get("pporlock.builder")
        if request is None or builder is None:
            return

        length: int | None
        try:
            length = int(flow.response.headers.get("content-length", "") or 0) or None
        except (TypeError, ValueError):
            length = None

        # Response headers are applied HERE, not in response(). Once a response
        # streams, mitmproxy has already put its headers on the wire, so a
        # mutation computed later is recorded as applied and changes nothing.
        response = normalize.normalize_response(flow, flow_id=_flow_id(flow))
        header_decision = self.evaluator.evaluate_response_headers(request, response, builder)
        if apply_mod.apply_response_mutation(flow, header_decision.mutation):
            self.counters.modified += 1

        decision = self.evaluator.decide_buffering(
            request,
            flow.response.headers.get("content-type"),
            length,
            bool(flow.metadata.get("pporlock.wants_body", True)),
            builder,
        )
        if not decision.buffer:
            apply_mod.set_stream(flow, True)

    def _apply_dev_toggles(self, flow: Any, builder: ProvenanceBuilder) -> None:
        """Apply anticache and anticomp, and record that they were on.

        `anticache` strips conditional-request headers so a full body comes back
        every time — without it a rewrite rule appears not to fire, because the
        browser is being handed a 304 with no body to rewrite.

        `anticomp` strips Accept-Encoding so bodies arrive uncompressed. Useful
        while debugging, and off in normal use because it inflates transfer
        volume for no benefit.

        Both are recorded on every flow they touch (REQ PXY-044). A capture
        taken under anticomp is not a capture of normal behaviour, and nothing
        downstream can tell unless the flow says so.
        """
        active = [name for name, on in self.dev_toggles.items() if on]
        if not active:
            return

        if self.dev_toggles["anticache"]:
            for header in ("if-none-match", "if-modified-since", "if-match", "if-range"):
                if header in flow.request.headers:
                    del flow.request.headers[header]
        if self.dev_toggles["anticomp"]:
            if "accept-encoding" in flow.request.headers:
                del flow.request.headers["accept-encoding"]

        builder.note(
            NoteCode.DEV_TOGGLE_ACTIVE,
            f"{' + '.join(active)} active; this flow does not reflect normal behaviour",
            toggles=active,
        )

    def _should_offload(self, request: Any, response: Any) -> bool:
        """Whether this flow's body work belongs on a worker thread.

        Sprint 9 classified the work; this is what honours the classification.
        Without it the decision would be advisory — recorded in provenance and
        ignored in practice — and a document-parsing transform on a large page
        would stall every other connection the browser has open.
        """
        return any(
            decide_offload(
                str(t.get("kind", "")), response.body_size, self.evaluator.offload_threshold
            ).offload
            for rule in self.evaluator.ruleset.matching_response_body(request, response)
            for t in _rule_transforms(rule)
        )

    async def response(self, flow: Any) -> None:
        """Response-side evaluation and flow completion.

        Async because expensive body work has to genuinely leave the event loop
        (REQ PXY-024, DD-3). A synchronous hook submitting to a thread pool and
        blocking on the result would stall the loop exactly as much as doing the
        work inline — the await is the entire point.
        """
        builder: ProvenanceBuilder = _unstash(flow, "builder") or ProvenanceBuilder(self.profile)
        request = _unstash(flow, "request")
        started: float | None = _unstash(flow, "started")

        streamed = bool(getattr(flow.response, "stream", False))
        response = normalize.normalize_response(
            flow,
            flow_id=_flow_id(flow),
            body=None if streamed else flow.response.content,
            streamed=streamed,
        )

        if request is not None:
            # Headers were already applied in responseheaders(); only body work
            # remains here.
            budget = _unstash(flow, "budget")
            if self._should_offload(request, response):
                loop = asyncio.get_running_loop()
                decision = await loop.run_in_executor(
                    None,
                    self.evaluator.evaluate_response_body,
                    request,
                    response,
                    builder,
                    budget,
                )
            else:
                decision = self.evaluator.evaluate_response_body(request, response, builder, budget)
            if apply_mod.apply_response_mutation(flow, decision.mutation):
                self.counters.modified += 1
                # Re-normalise so the captured response is the one the browser
                # actually received. Provenance explains what changed; the
                # record should show the result, not the input — the same
                # reasoning as the request side in Sprint 9.
                response = normalize.normalize_response(
                    flow,
                    flow_id=_flow_id(flow),
                    body=None if streamed else flow.response.content,
                    streamed=streamed,
                )

        _unstash(flow, "wants_body")

        elapsed_ms = (time.perf_counter() - started) * 1000 if started is not None else 0.0
        provenance = builder.build(elapsed_ms)
        # Two consumers, two shapes. `/metrics` wants every module that spent
        # time, including the pseudo-modules that file rules carry; the module
        # library wants the four columns its contract declares, per installed
        # module. One walk each, both O(entries), in the same place the flow
        # counters already increment (REQ PRF-007).
        self.module_cost.record(provenance)
        if self.evaluator.registry is not None:
            self.evaluator.registry.record_provenance(provenance)
        self.sink.record_http(request, response, provenance, {"pporlock_ms": elapsed_ms})

    def error(self, flow: Any) -> None:
        """An upstream or client error. Counted, not swallowed."""
        self.counters.errors += 1

    def websocket_message(self, flow: Any) -> None:
        """WebSocket frames are captured, never modified in v1 (REQ PXY-051)."""
        if not flow.websocket or not flow.websocket.messages:
            return
        flow_id = _flow_id(flow)
        index = self._ws_indexes.get(flow_id, 0)
        message = normalize.normalize_ws_message(
            flow, flow.websocket.messages[-1], flow_id=flow_id, index=index
        )
        self._ws_indexes[flow_id] = index + 1
        self.sink.record_websocket_message(message)

    def websocket_end(self, flow: Any) -> None:
        flow_id = _flow_id(flow)
        self._ws_indexes.pop(flow_id, None)
        close_code = getattr(getattr(flow, "websocket", None), "close_code", None)
        recorder = getattr(self.sink, "record_websocket_close", None)
        if recorder is not None:
            recorder(flow_id, close_code)

    def replace_ruleset(self, ruleset: RuleSet) -> None:
        """Swap in a new rule set without restarting the proxy (REQ MOD-004).

        A whole new Evaluator, not a mutation: an in-flight flow keeps the
        reference it started with, so no locking is needed and no flow can
        observe a half-applied change.

        Everything the outgoing evaluator was configured with carries over. The
        module registry and the transform registry especially: dropping them
        here would silently stop every Python hook and every module-registered
        transform the first time anyone edited a rule.
        """
        self.evaluator = self.evaluator.clone_with(
            ruleset=ruleset, registry=self.evaluator.registry
        )

    # -- proxy listener control (SPEC-0 §6.4, OI-3) ----------------------

    def _proxyserver(self) -> Any:
        """mitmproxy's own listener addon, or None when we are not under a master.

        This — and ``mitmproxy.ctx`` — is the whole of the version-churn surface
        for start/stop, and it is confined to this adapter by design (SPEC-1
        §2.1). ``options.server`` is what mitmproxy itself uses to decide
        whether to hold listeners open; setting it is not a back door.
        """
        try:
            from mitmproxy import ctx
        except ImportError:  # pragma: no cover - mitmproxy is a hard dependency
            return None
        master = getattr(ctx, "master", None)
        if master is None:
            return None
        return master

    @property
    def proxy_listening(self) -> bool:
        """Whether the proxy listener is currently accepting connections.

        The last commanded state, corroborated by mitmproxy's own listener
        addresses when there is a master to ask. ``/state`` reports this rather
        than "an Interceptor object exists", which is what made OI-3's silent
        discard invisible.
        """
        addon = _proxyserver_addon(self._proxyserver())
        if addon is None:
            return self._proxy_running
        return _has_listeners(addon)

    async def set_proxy_running(self, running: bool) -> bool:
        """Start or stop the proxy listener, and report what actually happened.

        mitmproxy applies the option change on a task of its own, so this waits
        for the listener to reach the requested state rather than assuming it.
        Returning before that would reproduce exactly the bug this closes: a
        200 for an effect that had not happened.
        """
        master = self._proxyserver()
        addon = _proxyserver_addon(master)
        if master is None or addon is None:
            raise ProxyControlError(
                "the proxy listener is not managed by this process, so it "
                "cannot be started or stopped through the control API",
                requested=running,
            )

        master.options.update(server=running)
        for _ in range(PROXY_STATE_POLLS):
            if _has_listeners(addon) == running:
                self._proxy_running = running
                return True
            await asyncio.sleep(PROXY_STATE_POLL_INTERVAL_S)

        raise ProxyControlError(
            f"the proxy listener did not {'start' if running else 'stop'} within "
            f"{PROXY_STATE_POLLS * PROXY_STATE_POLL_INTERVAL_S:.1f}s",
            requested=running,
        )

    # -- state -----------------------------------------------------------

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at


def _has_listeners(addon: Any) -> bool:
    """Whether mitmproxy currently holds a listening socket.

    ``listen_addrs`` is a method in mitmproxy 12 and has been a property in
    other releases, so both are accepted here — this is the adapter, and that is
    what it is for (SPEC-1 §2.1).
    """
    addrs = getattr(addon, "listen_addrs", None)
    if callable(addrs):
        addrs = addrs()
    return bool(addrs)


def _proxyserver_addon(master: Any) -> Any:
    """mitmproxy's proxyserver addon, or None if it is not loaded."""
    if master is None:
        return None
    addons = getattr(master, "addons", None)
    if addons is None:
        return None
    try:
        return addons.get("proxyserver")
    except Exception:  # pragma: no cover - defensive across mitmproxy versions
        return None


def _flow_id(flow: Any) -> str:
    """mitmproxy already assigns each flow a stable UUID; reuse it.

    Minting our own would mean maintaining a second identity map for no benefit,
    and the two would eventually disagree.
    """
    return str(flow.id)


def _stash(flow: Any, key: str, value: Any) -> None:
    """Carry per-flow state between hooks.

    mitmproxy gives each flow a metadata dict for exactly this. Keeping our keys
    namespaced avoids collisions with other addons.
    """
    flow.metadata[f"pporlock.{key}"] = value


def _unstash(flow: Any, key: str) -> Any:
    return flow.metadata.pop(f"pporlock.{key}", None)


def _rule_transforms(rule: Any) -> list[dict[str, Any]]:
    """A rule's transform blocks, single or list."""
    single = rule.params.get("transform")
    many = rule.params.get("transforms")
    out: list[dict[str, Any]] = []
    if isinstance(single, dict):
        out.append(single)
    if isinstance(many, list):
        out.extend(item for item in many if isinstance(item, dict))
    return out
