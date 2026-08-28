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

import time
from typing import Any, Protocol

from ..config import Config
from ..engine.evaluator import Evaluator, TimeBudget
from ..engine.exclusions import ExclusionList, load_exclusions
from ..engine.provenance import NoteCode, Provenance, ProvenanceBuilder
from ..engine.ruleset import RuleSet
from . import apply as apply_mod
from . import normalize


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
        self.started_at = time.time()
        self._ws_indexes: dict[str, int] = {}
        # Set by the runner when the control server should be started from
        # running(). Left None in tests and in bare-addon use.
        self.control: Any = control
        self.control_server: Any = None
        # The evaluator holds an immutable rule-set snapshot. Reload swaps the
        # whole evaluator rather than mutating it, so an in-flight flow finishes
        # against the snapshot it started with (REQ MOD-004).
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

    def response(self, flow: Any) -> None:
        """Response-side evaluation and flow completion."""
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
            decision = self.evaluator.evaluate_response_body(
                request, response, builder, _unstash(flow, "budget")
            )
            if apply_mod.apply_response_mutation(flow, decision.mutation):
                self.counters.modified += 1

        _unstash(flow, "wants_body")

        elapsed_ms = (time.perf_counter() - started) * 1000 if started is not None else 0.0
        self.sink.record_http(
            request, response, builder.build(elapsed_ms), {"pporlock_ms": elapsed_ms}
        )

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
        self._ws_indexes.pop(_flow_id(flow), None)

    def replace_ruleset(self, ruleset: RuleSet) -> None:
        """Swap in a new rule set without restarting the proxy (REQ MOD-004).

        A whole new Evaluator, not a mutation: an in-flight flow keeps the
        reference it started with, so no locking is needed and no flow can
        observe a half-applied change.
        """
        self.evaluator = Evaluator(
            ruleset,
            exclusions=self.exclusions,
            stubs=self.evaluator.stubs,
            asset_root=self.evaluator.asset_root,
            buffer_types=self.evaluator.buffer_types,
            max_buffer_bytes=self.evaluator.max_buffer_bytes,
            offload_threshold=self.evaluator.offload_threshold,
        )

    # -- state -----------------------------------------------------------

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at


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
