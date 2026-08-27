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
from ..engine.exclusions import ExclusionList, load_exclusions
from ..engine.provenance import NoteCode, Provenance, ProvenanceBuilder
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
    ) -> None:
        self.config = config or Config()
        self.sink: FlowSink = sink or NullSink()
        self.exclusions = exclusions if exclusions is not None else load_exclusions()
        self.profile = profile
        self.counters = Counters()
        self.started_at = time.time()
        self._ws_indexes: dict[str, int] = {}

    # -- lifecycle -------------------------------------------------------

    def running(self) -> None:
        """Called once the proxy is up. The control server starts here in Sprint 3."""

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
        """Request-side evaluation.

        Sprint 7 inserts short-circuit and header rules here. Today it
        establishes flow identity and the provenance record that every flow
        carries from birth (REQ CAP-013).
        """
        started = time.perf_counter()
        flow_id = _flow_id(flow)
        builder = ProvenanceBuilder(self.profile)

        request = normalize.normalize_request(flow, flow_id=flow_id, body=flow.request.content)

        # Seam: engine.evaluate_request(request, builder) -> RequestDecision.
        _stash(flow, "request", request)
        _stash(flow, "builder", builder)
        _stash(flow, "started", started)

        self.counters.flows_total += 1

    def responseheaders(self, flow: Any) -> None:
        """The buffering decision, which can only be made here (REQ PXY-021).

        Sprint 9 installs the size and content-type guard. Until then everything
        buffers, which is correct-but-slow rather than silently wrong.
        """

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

        if streamed:
            builder.note(
                NoteCode.RESPONSE_STREAMED,
                "response streamed; body transforms unavailable",
                reason="size",
            )

        # Seam: engine.evaluate_response(request, response, builder).

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
