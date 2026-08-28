"""The ring-buffer sink. SPEC-1 §6.1."""

from __future__ import annotations

from typing import Any

from pporlock.capture.ring import RingBuffer
from pporlock.capture.sink import RingSink
from pporlock.engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from pporlock.engine.provenance import (
    Action,
    NoteCode,
    Outcome,
    Phase,
    ProvenanceBuilder,
)


def request(flow_id: str = "f0", body: bytes | None = None) -> NormalizedRequest:
    return NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="GET",
        host="a.example",
        port=443,
        path="/x",
        url="https://a.example/x",
        body=body,
    )


def response(flow_id: str = "f0", body: bytes | None = None) -> NormalizedResponse:
    return NormalizedResponse(
        flow_id=flow_id, timestamp="2026-08-27T14:00:00.100Z", status=200, body=body
    )


class TestHttpRecording:
    def test_records_a_flow(self) -> None:
        ring = RingBuffer()
        RingSink(ring).record_http(
            request(), response(), ProvenanceBuilder("default").build(), {"pporlock_ms": 1.5}
        )
        record = ring.get("f0")
        assert record is not None
        assert record.kind == "http"
        assert record.timing.pporlock_ms == 1.5

    def test_bodies_are_capped_and_flagged(self) -> None:
        """REQ CAP-003 — the ring's memory bound must reflect what is held."""
        ring = RingBuffer()
        RingSink(ring, max_body_bytes=10).record_http(
            request(body=b"x" * 100),
            response(body=b"y" * 100),
            ProvenanceBuilder("default").build(),
            {},
        )
        record = ring.get("f0")
        assert record is not None
        assert record.request is not None
        assert record.response is not None
        assert len(record.request.body or b"") == 10
        assert record.request.body_truncated
        assert record.response.body_truncated

    def test_bodies_within_the_cap_are_untouched(self) -> None:
        ring = RingBuffer()
        RingSink(ring, max_body_bytes=1000).record_http(
            request(body=b"small"), response(), ProvenanceBuilder("default").build(), {}
        )
        record = ring.get("f0")
        assert record is not None
        assert record.request is not None
        assert not record.request.body_truncated

    def test_blocked_is_derived_from_short_circuit(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.short_circuit("block-vendors:2")
        ring = RingBuffer()
        RingSink(ring).record_http(request(), response(), builder.build(), {})
        record = ring.get("f0")
        assert record is not None
        assert record.blocked

    def test_modified_is_derived_from_applied_entries(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        ring = RingBuffer()
        RingSink(ring).record_http(request(), response(), builder.build(), {})
        record = ring.get("f0")
        assert record is not None
        assert record.modified

    def test_a_skipped_rule_does_not_count_as_modified(self) -> None:
        """A transform that was skipped changed nothing, and the flow table's
        'modified' flag must say so."""
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.SKIPPED_STREAMED,
        )
        ring = RingBuffer()
        RingSink(ring).record_http(request(), response(), builder.build(), {})
        record = ring.get("f0")
        assert record is not None
        assert not record.modified

    def test_a_response_without_a_request_still_records(self) -> None:
        ring = RingBuffer()
        RingSink(ring).record_http(None, response("f9"), ProvenanceBuilder("default").build(), {})
        assert ring.get("f9") is not None

    def test_on_flow_callback_fires(self) -> None:
        """Sprint 4 hangs the SSE hub off this."""
        seen: list[str] = []
        ring = RingBuffer()
        sink = RingSink(ring, on_flow=lambda r: seen.append(r.flow_id))
        sink.record_http(request(), response(), ProvenanceBuilder("default").build(), {})
        assert seen == ["f0"]


class TestPassthroughRecording:
    def test_carries_host_pattern_and_reason(self) -> None:
        """REQ PXY-015 — the user must be able to see why it was tunneled."""
        builder = ProvenanceBuilder("default")
        builder.note(
            NoteCode.PASSTHROUGH_EXCLUDED,
            "tunneled",
            pattern="*.chase.com",
            reason="sensitive: financial",
        )
        ring = RingBuffer()
        RingSink(ring).record_passthrough("www.chase.com", None, builder.build(), {})
        record = ring.query().flows[0]
        assert record.kind == "passthrough"
        assert record.passthrough_host == "www.chase.com"
        assert record.passthrough_pattern == "*.chase.com"
        assert "financial" in (record.passthrough_reason or "")

    def test_carries_no_content(self) -> None:
        """Visible, but not readable."""
        ring = RingBuffer()
        RingSink(ring).record_passthrough(
            "a.example", None, ProvenanceBuilder("default").build(), {}
        )
        record = ring.query().flows[0]
        assert record.request is None
        assert record.response is None

    def test_ip_only_passthrough(self) -> None:
        ring = RingBuffer()
        RingSink(ring).record_passthrough(
            None, "10.1.2.3", ProvenanceBuilder("default").build(), {}
        )
        assert ring.query().flows[0].passthrough_ip == "10.1.2.3"


class TestWebSocketRecording:
    def _message(
        self, flow_id: str = "ws0", index: int = 0, payload: bytes = b"hi"
    ) -> WebSocketMessage:
        return WebSocketMessage(
            flow_id=flow_id,
            index=index,
            timestamp="2026-08-27T14:00:00.000Z",
            direction="outbound",
            opcode="text",
            payload=payload,
        )

    def test_creates_a_record_for_an_unseen_flow(self) -> None:
        ring = RingBuffer()
        RingSink(ring).record_websocket_message(self._message())
        record = ring.get("ws0")
        assert record is not None
        assert record.kind == "websocket"
        assert len(record.ws_messages) == 1

    def test_appends_to_an_existing_flow(self) -> None:
        ring = RingBuffer()
        sink = RingSink(ring)
        sink.record_websocket_message(self._message(index=0))
        sink.record_websocket_message(self._message(index=1, payload=b"there"))
        record = ring.get("ws0")
        assert record is not None
        assert [m.index for m in record.ws_messages] == [0, 1]

    def test_payloads_are_capped(self) -> None:
        ring = RingBuffer()
        RingSink(ring, max_body_bytes=4).record_websocket_message(self._message(payload=b"x" * 50))
        record = ring.get("ws0")
        assert record is not None
        assert record.ws_messages[0].truncated


class TestAttributionJoin:
    """SPEC-0 §3.6 — both orderings happen, and both must work."""

    def test_resolves_the_tab_as_the_flow_is_recorded(self) -> None:
        """The usual ordering: the extension observes at onBeforeRequest, so its
        association arrives before the flow completes."""
        ring = RingBuffer()
        RingSink(ring, resolve_tab=lambda _m, _u: 42).record_http(
            request(), response(), ProvenanceBuilder("default").build(), {}
        )
        record = ring.get("f0")
        assert record is not None
        assert record.tab_id == 42

    def test_leaves_the_tab_unset_when_nothing_matches(self) -> None:
        ring = RingBuffer()
        RingSink(ring, resolve_tab=lambda _m, _u: None).record_http(
            request(), response(), ProvenanceBuilder("default").build(), {}
        )
        record = ring.get("f0")
        assert record is not None
        assert record.tab_id is None

    def test_does_not_overwrite_a_tab_already_known(self) -> None:
        import dataclasses

        ring = RingBuffer()
        known = dataclasses.replace(request(), tab_id=7)
        RingSink(ring, resolve_tab=lambda _m, _u: 99).record_http(
            known, response(), ProvenanceBuilder("default").build(), {}
        )
        record = ring.get("f0")
        assert record is not None
        assert record.tab_id == 7

    def test_works_without_a_resolver(self) -> None:
        ring = RingBuffer()
        RingSink(ring).record_http(request(), response(), ProvenanceBuilder("default").build(), {})
        record = ring.get("f0")
        assert record is not None
        assert record.tab_id is None


class TestModifiedFlag:
    """The flags column is how you scan a hundred rows for the one that broke,
    so BLK and MOD must not both appear on a flow that was simply blocked."""

    def _record(self, builder: ProvenanceBuilder) -> Any:
        ring = RingBuffer()
        RingSink(ring).record_http(request(), response(), builder.build(), {})
        return ring.get("f0")

    def test_a_blocked_flow_is_not_also_modified(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module="m",
            rule_id="m:0",
            action=Action.BLOCK,
            outcome=Outcome.APPLIED,
        )
        builder.short_circuit("m:0")
        record = self._record(builder)
        assert record is not None
        assert record.blocked
        assert not record.modified

    def test_a_header_change_counts_as_modified(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_HEADERS,
            module="m",
            rule_id="m:0",
            action=Action.HEADERS,
            outcome=Outcome.APPLIED,
        )
        record = self._record(builder)
        assert record is not None
        assert record.modified
        assert not record.blocked

    def test_a_body_change_counts_as_modified(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        record = self._record(builder)
        assert record is not None
        assert record.modified

    def test_a_redirect_is_neither_blocked_nor_modified_content(self) -> None:
        """It changed where the request went, not what came back."""
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module="m",
            rule_id="m:0",
            action=Action.REDIRECT,
            outcome=Outcome.APPLIED,
        )
        record = self._record(builder)
        assert record is not None
        assert not record.modified

    def test_a_header_rule_that_changed_nothing_is_not_modified(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_HEADERS,
            module="m",
            rule_id="m:0",
            action=Action.HEADERS,
            outcome=Outcome.NO_CHANGE,
        )
        record = self._record(builder)
        assert record is not None
        assert not record.modified
