"""The ring-buffer flow sink — SPEC-1 §6.1.

Implements the addon's ``FlowSink`` protocol, replacing the Sprint 2 counting
stub. This is where normalized objects become FlowRecords and land in the ring.
"""

from __future__ import annotations

from typing import Any

from ..addon.normalize import now_iso
from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from ..engine.provenance import Action, NoteCode, Outcome, Provenance
from .records import FlowRecord, Timing, truncate
from .ring import RingBuffer


class RingSink:
    """Writes completed flows into the ring buffer.

    Body caps are applied here rather than at read time so the buffer's memory
    bound reflects what is actually held (REQ CAP-003), and truncation is always
    flagged so a shortened body is never mistaken for a complete one.
    """

    __slots__ = ("max_body_bytes", "on_flow", "resolve_tab", "ring", "session")

    def __init__(
        self,
        ring: RingBuffer,
        *,
        max_body_bytes: int = 512 * 1024,
        on_flow: Any = None,
        resolve_tab: Any = None,
        session: Any = None,
    ) -> None:
        self.ring = ring
        self.max_body_bytes = max_body_bytes
        # Sprint 13: the session store, when one is recording. Its ``enqueue``
        # is a non-blocking put onto a bounded queue — recording must never be
        # able to slow the loop that is serving the browser (REQ CAP-023).
        self.session = session
        # Sprint 4 hangs the SSE hub off this.
        self.on_flow = on_flow
        # Sprint 6: the attribution join, applied as the flow is recorded.
        #
        # Both orderings happen and both must work. The extension observes at
        # onBeforeRequest, so its association usually arrives *before* the flow
        # completes — that is this hook. When the flow wins the race instead,
        # the POST /attribution handler backfills. Joining in only one direction
        # leaves roughly half of all flows unattributed.
        self.resolve_tab = resolve_tab

    def _emit(self, record: FlowRecord) -> None:
        self.ring.add(record)
        if self.session is not None:
            self.session.enqueue(record)
        if self.on_flow is not None:
            self.on_flow(record)

    def record_http(
        self,
        request: NormalizedRequest | None,
        response: NormalizedResponse | None,
        provenance: Provenance,
        timing: dict[str, float],
    ) -> None:
        capped_request = request
        if request is not None and request.body is not None:
            body, cut = truncate(request.body, self.max_body_bytes)
            if cut:
                capped_request = replace_body(request, body, truncated=True)

        capped_response = response
        if response is not None and response.body is not None:
            body, cut = truncate(response.body, self.max_body_bytes)
            if cut:
                capped_response = replace_response_body(response, body, truncated=True)

        blocked = provenance.short_circuited_by is not None
        # "Modified" means headers or a body were changed. A short-circuited
        # flow was blocked, not modified — showing both flags on one row makes
        # the flags column, which is how you scan a hundred rows for the one
        # that went wrong, harder rather than easier to read.
        modified = _was_modified(provenance)

        tab_id = request.tab_id if request is not None else None
        if tab_id is None and request is not None and self.resolve_tab is not None:
            tab_id = self.resolve_tab(request.method, request.url)

        record = FlowRecord(
            flow_id=_flow_id_of(request, response),
            kind="http",
            started_at=request.timestamp if request is not None else now_iso(),
            completed_at=now_iso(),
            tab_id=tab_id,
            request=capped_request,
            response=capped_response,
            provenance=provenance,
            timing=Timing(pporlock_ms=timing.get("pporlock_ms")),
            modified=modified,
            blocked=blocked,
        )
        self._emit(record)

    def record_passthrough(
        self,
        host: str | None,
        ip: str | None,
        provenance: Provenance,
        timing: dict[str, float],
    ) -> None:
        """An excluded connection: visible, but not readable (REQ PXY-015)."""
        pattern: str | None = None
        reason: str | None = None
        for note in provenance.notes:
            if note.code is NoteCode.PASSTHROUGH_EXCLUDED:
                pattern = note.detail.get("pattern")
                reason = note.detail.get("reason")
                break
        record = FlowRecord(
            flow_id=f"pt-{host or ip}-{now_iso()}",
            kind="passthrough",
            started_at=now_iso(),
            completed_at=now_iso(),
            passthrough_host=host,
            passthrough_ip=ip,
            passthrough_pattern=pattern,
            passthrough_reason=reason,
            provenance=provenance,
        )
        self._emit(record)

    def record_websocket_message(self, message: WebSocketMessage) -> None:
        """Append to the owning flow, or start a WebSocket record for it."""
        record = self.ring.get(message.flow_id)
        if record is None:
            record = FlowRecord(
                flow_id=message.flow_id,
                kind="websocket",
                started_at=message.timestamp,
            )
            self.ring.add(record)
        record.kind = "websocket"
        payload, cut = truncate(message.payload, self.max_body_bytes)
        record.ws_messages.append(
            WebSocketMessage(
                flow_id=message.flow_id,
                index=message.index,
                timestamp=message.timestamp,
                direction=message.direction,
                opcode=message.opcode,
                payload=payload or b"",
                truncated=cut,
            )
        )
        # Re-enqueued whole rather than appended to: the session's flows row is
        # the record, and a frame that only reached the ring would leave the
        # recorded flow claiming fewer messages than actually crossed the wire.
        if self.session is not None:
            self.session.enqueue(record)

    def record_websocket_close(self, flow_id: str, close_code: int | None) -> None:
        """Mark a WebSocket flow closed (REQ PXY-050).

        Without this a recorded socket is indistinguishable from one still
        open, which is exactly the question being asked when a page stops
        receiving updates.
        """
        record = self.ring.get(flow_id)
        if record is None:
            return
        record.ws_closed = True
        record.ws_close_code = close_code
        if self.session is not None:
            self.session.enqueue(record)


#: Actions that change an existing message rather than replacing it.
_MODIFYING_ACTIONS = frozenset({Action.HEADERS, Action.BODY})


def _was_modified(provenance: Provenance) -> bool:
    """Whether any header or body was actually changed."""
    return any(
        entry.outcome is Outcome.APPLIED and entry.action in _MODIFYING_ACTIONS
        for entry in provenance.entries
    )


def _flow_id_of(request: NormalizedRequest | None, response: NormalizedResponse | None) -> str:
    if request is not None:
        return request.flow_id
    if response is not None:
        return response.flow_id
    return f"unknown-{now_iso()}"


def replace_body(
    request: NormalizedRequest, body: bytes | None, *, truncated: bool
) -> NormalizedRequest:
    import dataclasses

    return dataclasses.replace(request, body=body, body_truncated=truncated)


def replace_response_body(
    response: NormalizedResponse, body: bytes | None, *, truncated: bool
) -> NormalizedResponse:
    import dataclasses

    return dataclasses.replace(response, body=body, body_truncated=truncated)
