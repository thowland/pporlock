"""The SSE hub. SPEC-0 §7, SPEC-1 §7.3, REQ API-022.

The rule these protect: a slow subscriber must never slow the proxy. The hub
runs on the proxy's own event loop, so backpressure from a stalled DevTools
panel would become backpressure on browsing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pporlock.capture.records import FlowRecord
from pporlock.control.events import (
    EVENT_TYPES,
    FLOW_EVENTS,
    QUEUE_MAXSIZE,
    Event,
    EventFilter,
    EventHub,
    Subscriber,
)

from .test_ring import make_record


class TestEventFormat:
    def test_sse_frame_shape(self) -> None:
        frame = Event(type="flow.completed", data={"a": 1}, seq=7, ts="t").to_sse().decode()
        assert frame.startswith("id: 7\n")
        assert "event: flow.completed\n" in frame
        assert frame.endswith("\n\n")

    def test_data_is_json_with_the_envelope(self) -> None:
        frame = Event(type="state.changed", data={"a": 1}, seq=1, ts="t").to_sse().decode()
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["type"] == "state.changed"
        assert payload["seq"] == 1
        assert payload["data"] == {"a": 1}

    def test_id_carries_the_sequence_for_resume(self) -> None:
        """Last-Event-ID is how a reconnecting client learns it missed events."""
        assert "id: 42" in Event(type="x", data={}, seq=42).to_sse().decode()

    def test_every_spec_event_type_is_known(self) -> None:
        assert EVENT_TYPES == {
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

    def test_flow_events_are_the_filterable_subset(self) -> None:
        assert FLOW_EVENTS < EVENT_TYPES


class TestFiltering:
    def test_no_filter_allows_everything(self) -> None:
        assert EventFilter().allows(Event("flow.completed", {}), make_record("f0"))

    def test_kind_filter(self) -> None:
        f = EventFilter(kinds=frozenset({"state.changed"}))
        assert f.allows(Event("state.changed", {}), None)
        assert not f.allows(Event("flow.completed", {}), make_record("f0"))

    def test_flow_filter_applies_to_flow_events(self) -> None:
        from pporlock.capture.filters import FlowFilter

        f = EventFilter(flow_filter=FlowFilter(host="a.example"))
        assert f.allows(Event("flow.completed", {}), make_record("f0", host="a.example"))
        assert not f.allows(Event("flow.completed", {}), make_record("f1", host="b.example"))

    def test_system_events_are_never_filtered_away(self) -> None:
        """They tell the client about the system, not about one flow. A user
        filtering on a host still needs to know a module was quarantined."""
        from pporlock.capture.filters import FlowFilter

        f = EventFilter(flow_filter=FlowFilter(host="nothing-matches"), tab_id=999)
        assert f.allows(Event("module.quarantined", {}), None)
        assert f.allows(Event("state.changed", {}), None)

    def test_tab_filter(self) -> None:
        f = EventFilter(tab_id=7)
        assert f.allows(Event("flow.completed", {}), make_record("f0", tab_id=7))
        assert not f.allows(Event("flow.completed", {}), make_record("f1", tab_id=8))

    def test_from_query_parses_kinds(self) -> None:
        f = EventFilter.from_query({"kinds": "flow.completed,state.changed"})
        assert f.kinds == frozenset({"flow.completed", "state.changed"})

    def test_from_query_drops_unknown_kinds(self) -> None:
        assert EventFilter.from_query({"kinds": "not-a-type"}).kinds is None

    def test_from_query_parses_tab_id(self) -> None:
        assert EventFilter.from_query({"tab_id": "7"}).tab_id == 7

    def test_from_query_ignores_a_bad_tab_id(self) -> None:
        assert EventFilter.from_query({"tab_id": "abc"}).tab_id is None

    def test_from_query_carries_the_flow_vocabulary(self) -> None:
        assert EventFilter.from_query({"host": "a.example"}).flow_filter.host == "a.example"


class TestSubscriberBackpressure:
    def test_offer_never_blocks(self) -> None:
        """The publisher runs on the proxy's loop. It must not await anything."""
        subscriber = Subscriber(EventFilter(), maxsize=2)
        for i in range(100):
            subscriber.offer(Event("flow.completed", {}, seq=i))
        assert subscriber.queue.qsize() <= 2

    def test_overflow_drops_oldest_and_counts(self) -> None:
        subscriber = Subscriber(EventFilter(), maxsize=2)
        for i in range(5):
            subscriber.offer(Event("flow.completed", {}, seq=i))
        assert subscriber.dropped == 3

    def test_newest_events_survive(self) -> None:
        """Dropping the oldest keeps the view closest to the present."""
        subscriber = Subscriber(EventFilter(), maxsize=2)
        for i in range(5):
            subscriber.offer(Event("flow.completed", {}, seq=i))
        remaining = [subscriber.queue.get_nowait().seq for _ in range(2)]
        assert remaining == [3, 4]

    def test_gap_is_produced_after_drops(self) -> None:
        subscriber = Subscriber(EventFilter(), maxsize=1)
        for i in range(4):
            subscriber.offer(Event("flow.completed", {}, seq=i))
        gap = subscriber.take_gap(10)
        assert gap is not None
        assert gap.type == "stream.gap"
        assert gap.data["to_seq"] == 10

    def test_no_gap_without_drops(self) -> None:
        assert Subscriber(EventFilter()).take_gap(1) is None

    def test_gap_resets_after_being_taken(self) -> None:
        subscriber = Subscriber(EventFilter(), maxsize=1)
        for i in range(4):
            subscriber.offer(Event("flow.completed", {}, seq=i))
        subscriber.take_gap(10)
        assert subscriber.take_gap(11) is None

    def test_default_queue_absorbs_a_page_load(self) -> None:
        """A busy page load is a few hundred events; the queue must not drop on
        ordinary traffic, only on a genuinely stalled client."""
        assert QUEUE_MAXSIZE >= 256


class TestHub:
    def test_sequence_increments(self) -> None:
        hub = EventHub()
        assert hub.publish("state.changed", {}).seq == 1
        assert hub.publish("state.changed", {}).seq == 2

    def test_publish_with_no_subscribers_is_fine(self) -> None:
        assert EventHub().publish("state.changed", {}).type == "state.changed"

    async def test_subscriber_receives_events(self) -> None:
        hub = EventHub()
        received: list[str] = []

        async def consume() -> None:
            # A long heartbeat: this test is about delivery, not timing, and a
            # short one races the publish below.
            async for chunk in hub.subscribe(EventFilter(), heartbeat_seconds=5):
                received.append(chunk.decode())
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        hub.publish("state.changed", {"a": 1})
        await asyncio.wait_for(task, timeout=2)

        assert received[0].startswith(": connected")
        assert "state.changed" in received[1]

    async def test_filtered_subscriber_misses_non_matching_flows(self) -> None:
        from pporlock.capture.filters import FlowFilter

        hub = EventHub()
        received: list[str] = []

        async def consume() -> None:
            filt = EventFilter(flow_filter=FlowFilter(host="wanted.example"))
            async for chunk in hub.subscribe(filt, heartbeat_seconds=5):
                received.append(chunk.decode())
                if len(received) >= 2:
                    break

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        hub.publish_flow("flow.completed", make_record("f0", host="other.example"), {})
        hub.publish_flow("flow.completed", make_record("f1", host="wanted.example"), {})
        await asyncio.wait_for(task, timeout=2)

        assert "wanted.example" not in received[0]
        assert received[1].count("event: flow.completed") == 1

    async def test_heartbeat_keeps_a_quiet_stream_alive(self) -> None:
        """A proxy watching an idle tab must not look broken."""
        hub = EventHub()
        chunks: list[bytes] = []

        async def consume() -> None:
            async for chunk in hub.subscribe(EventFilter(), heartbeat_seconds=0.02):
                chunks.append(chunk)
                if len(chunks) >= 2:
                    break

        await asyncio.wait_for(asyncio.create_task(consume()), timeout=2)
        assert chunks[1] == b": heartbeat\n\n"

    async def test_reconnect_behind_the_sequence_is_told_about_the_gap(self) -> None:
        """The hub is not a log, so it cannot replay — but it must not let the
        client believe it saw everything (SPEC-0 §7.2)."""
        hub = EventHub()
        for _ in range(5):
            hub.publish("state.changed", {})

        chunks: list[bytes] = []

        async def consume() -> None:
            async for chunk in hub.subscribe(
                EventFilter(), last_event_id="2", heartbeat_seconds=0.02
            ):
                chunks.append(chunk)
                break

        await asyncio.wait_for(asyncio.create_task(consume()), timeout=2)
        assert b"stream.gap" in chunks[0]

    async def test_reconnect_at_the_head_gets_no_gap(self) -> None:
        hub = EventHub()
        hub.publish("state.changed", {})
        chunks: list[bytes] = []

        async def consume() -> None:
            async for chunk in hub.subscribe(
                EventFilter(), last_event_id="1", heartbeat_seconds=0.02
            ):
                chunks.append(chunk)
                break

        await asyncio.wait_for(asyncio.create_task(consume()), timeout=2)
        assert chunks[0] == b": connected\n\n"

    async def test_malformed_last_event_id_is_tolerated(self) -> None:
        hub = EventHub()
        hub.publish("state.changed", {})
        chunks: list[bytes] = []

        async def consume() -> None:
            async for chunk in hub.subscribe(
                EventFilter(), last_event_id="garbage", heartbeat_seconds=0.02
            ):
                chunks.append(chunk)
                break

        await asyncio.wait_for(asyncio.create_task(consume()), timeout=2)
        assert b"stream.gap" in chunks[0]

    async def test_subscriber_is_removed_on_disconnect(self) -> None:
        hub = EventHub()

        async def consume() -> None:
            async for _ in hub.subscribe(EventFilter(), heartbeat_seconds=0.02):
                break

        await asyncio.wait_for(asyncio.create_task(consume()), timeout=2)
        await asyncio.sleep(0)
        assert hub.subscriber_count == 0

    async def test_a_stalled_subscriber_does_not_block_the_publisher(self) -> None:
        """The load-bearing property. A client that never reads must not be able
        to slow a publish call running on the proxy's event loop."""
        hub = EventHub()

        async def stalled() -> None:
            gen = hub.subscribe(EventFilter(), heartbeat_seconds=30)
            await gen.__anext__()
            await asyncio.sleep(5)

        task = asyncio.create_task(stalled())
        await asyncio.sleep(0.05)

        started = asyncio.get_running_loop().time()
        for _ in range(QUEUE_MAXSIZE * 3):
            hub.publish("flow.completed", {}, make_record("f0"))
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.0, "publishing blocked on a stalled subscriber"
        task.cancel()


@pytest.fixture
def _record() -> FlowRecord:
    return make_record("f0")
