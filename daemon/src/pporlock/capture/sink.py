"""The ring-buffer flow sink — SPEC-1 §6.1.

Implements the addon's ``FlowSink`` protocol, replacing the Sprint 2 counting
stub. This is where normalized objects become FlowRecords and land in the ring.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..addon.normalize import now_iso
from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from ..engine.provenance import Action, NoteCode, Outcome, Provenance
from .records import FlowError, FlowRecord, Timing, truncate
from .ring import RingBuffer

#: How much WebSocket payload one flow record retains. Generous enough that an
#: ordinary page's socket is captured whole, small enough that a socket left
#: open for a day cannot be the reason the daemon runs out of memory.
DEFAULT_MAX_WS_BYTES = 1024 * 1024


def _trim_ws_messages(record: FlowRecord, max_bytes: int) -> int:
    """Drop the oldest frames until the retained payload fits.

    Returns the bytes released, for the ring's accounting. ``ws_dropped``
    counts the frames, so a truncated capture is never mistaken for a complete
    one — the same promise per-message truncation makes.
    """
    total = sum(len(m.payload) for m in record.ws_messages)
    released = 0
    dropped = 0
    while record.ws_messages and total - released > max_bytes:
        released += len(record.ws_messages.pop(0).payload)
        dropped += 1
    record.ws_dropped += dropped
    return released


class RingSink:
    """Writes completed flows into the ring buffer.

    Body caps are applied here rather than at read time so the buffer's memory
    bound reflects what is actually held (REQ CAP-003), and truncation is always
    flagged so a shortened body is never mistaken for a complete one.
    """

    __slots__ = ("max_body_bytes", "max_ws_bytes", "on_flow", "resolve_tab", "ring", "session")

    def __init__(
        self,
        ring: RingBuffer,
        *,
        max_body_bytes: int = 512 * 1024,
        max_ws_bytes: int = DEFAULT_MAX_WS_BYTES,
        on_flow: Any = None,
        resolve_tab: Any = None,
        session: Any = None,
    ) -> None:
        self.ring = ring
        self.max_body_bytes = max_body_bytes
        #: Total retained frame payload for one socket (REQ PXY-050). Bounds a
        #: record that grows for as long as the connection is open.
        self.max_ws_bytes = max_ws_bytes
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

        short_circuit = _short_circuit_action(provenance)
        # A `block` denied the client. `map_local` and `redirect` also end
        # evaluation early, and both hand the browser a response it uses — so
        # they are modifications, not blocks. Reporting them as blocked is what
        # made an enabled css-tamper look like it was breaking the page it was
        # styling (OI-26).
        blocked = short_circuit == "block"
        modified = _was_modified(provenance) or short_circuit in ("map_local", "redirect")

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
            short_circuit=short_circuit,
        )
        self._emit(record)

    def record_error(
        self,
        request: NormalizedRequest | None,
        provenance: Provenance,
        message: str,
        *,
        from_client: bool = False,
    ) -> None:
        """A flow that failed before completing (REQ CAP-002, OI-23).

        Recorded rather than merely counted. A request that 502s used to leave
        no trace in the ring at all: the browser showed a gateway error, the
        user opened the flow table built to explain it, and the request they
        were looking for was simply not there. The one flow you most need to
        see was the one flow the tool discarded.

        The record carries no response, because there was none. That is the
        honest shape — a row with a reason and no status, rather than a
        fabricated one.
        """
        tab_id = request.tab_id if request is not None else None
        if tab_id is None and request is not None and self.resolve_tab is not None:
            tab_id = self.resolve_tab(request.method, request.url)

        record = FlowRecord(
            flow_id=_flow_id_of(request, None),
            kind="http",
            started_at=request.timestamp if request is not None else now_iso(),
            completed_at=now_iso(),
            tab_id=tab_id,
            request=request,
            response=None,
            provenance=provenance,
            error=FlowError(message=message, from_client=from_client),
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
        # An explicit retention policy, then the accounting.
        #
        # Per-message truncation bounds one frame; nothing bounded how many
        # frames one connection accumulated, and appending to a list already in
        # the ring never moved the ring's byte counter — so a chatty socket grew
        # memory that `max_bytes` could not see and `stats` under-reported
        # (SEP_5_REVIEW F-05, REQ PXY-050, CAP-003, PRF-005).
        #
        # The newest frames are kept rather than the oldest: the question being
        # asked of a live socket is what it is doing now. Re-accounting alone
        # would let the active socket evict every other flow to make room for
        # itself, which is why the per-record cap comes first.
        released = _trim_ws_messages(record, self.max_ws_bytes)
        self.ring.adjust(record.flow_id, len(payload or b"") - released)
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


def _short_circuit_action(provenance: Provenance) -> str | None:
    """Which action ended request evaluation, by name.

    `short_circuited_by` records the rule, not what it did, so the action is
    recovered from the entry that rule wrote. Three actions can appear here and
    only one of them is a block (REQ MOD-012).
    """
    rule_id = provenance.short_circuited_by
    if rule_id is None:
        return None
    for entry in provenance.entries:
        if entry.rule_id == rule_id:
            return str(entry.action.value)
    # A short-circuit with no matching entry should not happen; naming the rule
    # without claiming an action is better than guessing "block".
    return None


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
    # Unique, not merely descriptive. This used to be `unknown-<iso timestamp>`,
    # which is second-resolution: two flows with neither a request nor a
    # response in the same second collided on one id and the ring kept one of
    # them. Latent while only complete flows were recorded; reachable as soon as
    # failures were (OI-23), and a page failing to load produces many at once —
    # exactly the case where losing all but one is worst.
    return f"unknown-{uuid4().hex}"


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
