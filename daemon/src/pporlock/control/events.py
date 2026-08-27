"""Server-Sent Events hub — SPEC-0 §7, SPEC-1 §7.3, REQ API-022.

The rule that shapes this module: **a slow subscriber must never slow the
proxy.** The hub runs on the proxy's own event loop, so a DevTools panel that
stalls — a laptop that slept, a tab that was throttled — must not apply
backpressure to traffic. Each subscriber therefore gets a bounded queue, and an
overflowing one loses its oldest events and is told so with ``stream.gap``
rather than being allowed to block the publisher.

Losing events loudly is the correct trade. A client that receives ``stream.gap``
refetches; a client that silently misses flows shows a lie.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..addon.normalize import now_iso
from ..capture.filters import FlowFilter
from ..capture.records import FlowRecord

#: Per-subscriber queue depth. A busy page load produces a few hundred events;
#: this absorbs that without letting a dead subscriber grow without bound.
QUEUE_MAXSIZE = 512

EVENT_TYPES = frozenset(
    {
        "flow.started",
        "flow.completed",
        "flow.updated",
        "websocket.message",
        "state.changed",
        "module.error",
        "module.quarantined",
        "session.changed",
        "stream.gap",
    }
)

#: Events that describe a specific flow and can therefore be filtered per-tab.
FLOW_EVENTS = frozenset({"flow.started", "flow.completed", "flow.updated", "websocket.message"})


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: dict[str, Any]
    seq: int = 0
    ts: str = ""

    def to_sse(self) -> bytes:
        """Serialize to the SSE wire format.

        ``id:`` carries the sequence so a reconnecting client can send
        Last-Event-ID and be told whether it missed anything (SPEC-0 §7.2).
        """
        payload = json.dumps({"type": self.type, "seq": self.seq, "ts": self.ts, "data": self.data})
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n".encode()


@dataclass(slots=True)
class EventFilter:
    """Server-side filtering, so a narrow client filter reduces event volume
    rather than merely hiding rows (SPEC-0 §7.1)."""

    kinds: frozenset[str] | None = None
    flow_filter: FlowFilter = field(default_factory=FlowFilter)
    tab_id: int | None = None

    def allows(self, event: Event, record: FlowRecord | None) -> bool:
        if self.kinds is not None and event.type not in self.kinds:
            return False
        if event.type not in FLOW_EVENTS:
            # State and module events are never filtered away: they tell the
            # client something about the system, not about one flow.
            return True
        if record is None:
            return True
        if self.tab_id is not None and record.tab_id != self.tab_id:
            return False
        return self.flow_filter.matches(record)

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> EventFilter:
        raw_kinds = params.get("kinds")
        kinds: frozenset[str] | None = None
        if raw_kinds:
            requested = {k.strip() for k in str(raw_kinds).split(",") if k.strip()}
            kinds = frozenset(requested & EVENT_TYPES) or None

        tab_id: int | None = None
        if params.get("tab_id"):
            try:
                tab_id = int(params["tab_id"])
            except (TypeError, ValueError):
                tab_id = None

        return cls(
            kinds=kinds,
            flow_filter=FlowFilter.from_query(params),
            tab_id=tab_id,
        )


class Subscriber:
    """One connected client."""

    __slots__ = ("_last_delivered_seq", "dropped", "filter", "queue")

    def __init__(self, event_filter: EventFilter, maxsize: int = QUEUE_MAXSIZE) -> None:
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self.filter = event_filter
        self.dropped = 0
        self._last_delivered_seq = 0

    def offer(self, event: Event) -> None:
        """Enqueue without ever blocking the publisher.

        On overflow the oldest event is discarded and the drop counted. The
        subscriber is told about the gap on its next read rather than at this
        moment, because emitting from here would mean growing the queue we just
        found full.
        """
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - racy, harmless
                pass
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - racy, harmless
                self.dropped += 1

    def take_gap(self, upcoming_seq: int) -> Event | None:
        """A stream.gap describing what was dropped, if anything."""
        if self.dropped == 0:
            return None
        gap = Event(
            type="stream.gap",
            data={"from_seq": self._last_delivered_seq, "to_seq": upcoming_seq},
            seq=upcoming_seq,
            ts=now_iso(),
        )
        self.dropped = 0
        return gap

    def note_delivered(self, seq: int) -> None:
        self._last_delivered_seq = seq


class EventHub:
    """Fan-out to connected subscribers."""

    __slots__ = ("_seq", "_subscribers")

    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._seq = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def last_seq(self) -> int:
        return self._seq

    def publish(
        self, event_type: str, data: dict[str, Any], record: FlowRecord | None = None
    ) -> Event:
        """Publish to every interested subscriber. Never blocks, never raises."""
        self._seq += 1
        event = Event(type=event_type, data=data, seq=self._seq, ts=now_iso())
        for subscriber in self._subscribers:
            if subscriber.filter.allows(event, record):
                subscriber.offer(event)
        return event

    def publish_flow(self, event_type: str, record: FlowRecord, data: dict[str, Any]) -> Event:
        return self.publish(event_type, data, record)

    def _add(self, subscriber: Subscriber) -> None:
        self._subscribers.add(subscriber)

    def _remove(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    async def subscribe(
        self,
        event_filter: EventFilter,
        *,
        last_event_id: str | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[bytes]:
        """Yield SSE frames until the client disconnects.

        A heartbeat comment keeps intermediaries and the browser's own idle
        handling from closing a quiet stream — without it, a proxy watching an
        idle tab looks broken.
        """
        subscriber = Subscriber(event_filter)
        self._add(subscriber)

        # A client reconnecting with a sequence behind ours missed events while
        # it was away. We cannot replay from here (the hub is not a log), so we
        # say so rather than letting it believe it saw everything.
        if last_event_id:
            try:
                resumed_from = int(last_event_id)
            except ValueError:
                resumed_from = 0
            if resumed_from < self._seq:
                yield Event(
                    type="stream.gap",
                    data={"from_seq": resumed_from, "to_seq": self._seq},
                    seq=self._seq,
                    ts=now_iso(),
                ).to_sse()

        try:
            yield b": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=heartbeat_seconds
                    )
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue

                gap = subscriber.take_gap(event.seq)
                if gap is not None:
                    yield gap.to_sse()
                subscriber.note_delivered(event.seq)
                yield event.to_sse()
        finally:
            self._remove(subscriber)
