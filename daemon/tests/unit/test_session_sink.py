"""The sink's session hook and WebSocket capture.

SPEC-1 §6.1, §6.3. REQ CAP-020/023, PXY-050/051/052.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from pporlock.capture.redact import Redactor, is_masked
from pporlock.capture.ring import RingBuffer
from pporlock.capture.session import SessionStore
from pporlock.capture.sink import RingSink
from pporlock.control.serialize import serialize_flow
from pporlock.engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from pporlock.engine.provenance import Provenance
from pporlock.engine.ruleset import WS_ACTION_PREFIX, RuleSet
from pporlock.errors import RuleValidationError

from .test_schema_conformance import validator_for
from .test_session import BEARER_SECRET, COOKIE_SECRET


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions", Redactor())


@pytest.fixture
def sink(store: SessionStore) -> RingSink:
    return RingSink(RingBuffer(), session=store)


def request_with_secrets(flow_id: str = "f0") -> NormalizedRequest:
    return NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="GET",
        host="api.example.com",
        port=443,
        path="/v1/me",
        url="https://api.example.com/v1/me",
        headers=(("cookie", COOKIE_SECRET), ("authorization", BEARER_SECRET)),
    )


class TestSinkFeedsTheSession:
    def test_nothing_is_recorded_when_no_session_is_running(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        """REQ CAP-020 — recording is opt-in and off by default."""
        sink.record_http(
            request_with_secrets(),
            None,
            Provenance(profile="default"),
            {"pporlock_ms": 1.0},
        )
        assert len(sink.ring) == 1
        assert store.list() == []

    def test_a_completed_flow_reaches_the_recording_session(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        meta = store.start("live")
        sink.record_http(
            request_with_secrets(),
            NormalizedResponse(flow_id="f0", timestamp="t", status=200),
            Provenance(profile="default"),
            {"pporlock_ms": 1.0},
        )
        stopped = store.stop(meta.session_id)
        assert stopped.flow_count == 1

    def test_what_the_session_stored_is_masked(self, sink: RingSink, store: SessionStore) -> None:
        """REQ CAP-045 — the write-time guarantee, exercised through the sink
        rather than the writer, because this is the path a real flow takes."""
        meta = store.start("live")
        sink.record_http(
            request_with_secrets(),
            None,
            Provenance(profile="default"),
            {"pporlock_ms": 1.0},
        )
        store.stop(meta.session_id)

        stored = store.reader(meta.session_id).get("f0")
        assert stored is not None and stored.request is not None
        assert is_masked(stored.request.header("cookie") or "")
        assert is_masked(stored.request.header("authorization") or "")

        raw = store.path_for(meta.session_id).read_bytes()
        assert COOKIE_SECRET.encode() not in raw
        assert BEARER_SECRET.encode() not in raw

    def test_the_ring_keeps_the_raw_values(self, sink: RingSink, store: SessionStore) -> None:
        """Which is what makes unmasking possible for live flows (REQ CAP-043)."""
        meta = store.start("live")
        sink.record_http(
            request_with_secrets(), None, Provenance(profile="default"), {"pporlock_ms": 1.0}
        )
        store.stop(meta.session_id)
        live = sink.ring.get("f0")
        assert live is not None and live.request is not None
        assert live.request.header("cookie") == COOKIE_SECRET

    def test_a_passthrough_flow_is_recorded_too(self, sink: RingSink, store: SessionStore) -> None:
        """REQ PXY-015 — an excluded connection is visible, and a session that
        omitted it would not explain why nothing from that host was captured."""
        meta = store.start("live")
        sink.record_passthrough("bank.example", "1.2.3.4", Provenance(profile="default"), {})
        stopped = store.stop(meta.session_id)
        assert stopped.flow_count == 1


class TestWebSocketCapture:
    """REQ PXY-050/051."""

    def message(self, index: int, payload: bytes) -> WebSocketMessage:
        return WebSocketMessage(
            flow_id="ws0",
            index=index,
            timestamp=f"2026-08-27T14:00:0{index}.000Z",
            direction="outbound" if index % 2 == 0 else "inbound",
            opcode="text",
            payload=payload,
        )

    def test_frames_accumulate_on_one_flow(self, sink: RingSink) -> None:
        for i in range(3):
            sink.record_websocket_message(self.message(i, b"hello"))
        record = sink.ring.get("ws0")
        assert record is not None
        assert record.kind == "websocket"
        assert len(record.ws_messages) == 3
        assert [m.direction for m in record.ws_messages] == [
            "outbound",
            "inbound",
            "outbound",
        ]

    def test_frames_are_capped_and_the_cut_is_flagged(self, store: SessionStore) -> None:
        """REQ CAP-003 — a shortened payload is never mistaken for a complete
        one."""
        sink = RingSink(RingBuffer(), max_body_bytes=4, session=store)
        sink.record_websocket_message(self.message(0, b"0123456789"))
        record = sink.ring.get("ws0")
        assert record is not None
        assert record.ws_messages[0].payload == b"0123"
        assert record.ws_messages[0].truncated is True

    def test_close_is_recorded(self, sink: RingSink) -> None:
        """Without it a recorded socket is indistinguishable from one still
        open, which is the question being asked when updates stop."""
        sink.record_websocket_message(self.message(0, b"hi"))
        sink.record_websocket_close("ws0", 1000)
        record = sink.ring.get("ws0")
        assert record is not None
        assert record.ws_closed is True
        assert record.ws_close_code == 1000

    def test_closing_an_unknown_flow_is_a_no_op(self, sink: RingSink) -> None:
        sink.record_websocket_close("never-seen", 1006)

    def test_frames_reach_the_session_with_payloads_redacted(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        meta = store.start("ws")
        sink.record_websocket_message(
            self.message(0, json.dumps({"access_token": "s3cr3t-value"}).encode())
        )
        sink.record_websocket_close("ws0", 1000)
        store.stop(meta.session_id)

        stored = store.reader(meta.session_id).get("ws0")
        assert stored is not None
        assert len(stored.ws_messages) == 1
        assert is_masked(json.loads(stored.ws_messages[0].payload)["access_token"])
        assert stored.ws_closed is True
        assert b"s3cr3t-value" not in store.path_for(meta.session_id).read_bytes()

    def test_every_frame_is_persisted_not_just_the_last(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        meta = store.start("ws")
        for i in range(5):
            sink.record_websocket_message(self.message(i, f"frame-{i}".encode()))
        store.stop(meta.session_id)
        stored = store.reader(meta.session_id).get("ws0")
        assert stored is not None
        assert [m.payload for m in stored.ws_messages] == [f"frame-{i}".encode() for i in range(5)]


class TestWebSocketActionNamespaceIsReserved:
    """REQ PXY-052. Adding ws actions later must not be a breaking change."""

    def test_no_ws_action_exists_yet(self) -> None:
        """REQ PXY-051 — inspection-only in v1."""
        from pporlock.engine.provenance import Action

        assert not any(a.value.startswith(WS_ACTION_PREFIX) for a in Action)

    def test_a_ws_prefixed_action_is_refused_as_reserved(self) -> None:
        with pytest.raises(RuleValidationError, match="reserved"):
            RuleSet.from_rules([{"name": "r", "action": "ws_send", "text": "x"}], module="m")

    def test_the_reservation_message_names_the_namespace(self) -> None:
        with pytest.raises(RuleValidationError) as excinfo:
            RuleSet.from_rules([{"name": "r", "action": "ws_rewrite"}], module="m")
        assert WS_ACTION_PREFIX in str(excinfo.value)

    def test_an_ordinary_unknown_action_still_reports_the_valid_list(self) -> None:
        with pytest.raises(RuleValidationError, match="valid actions are"):
            RuleSet.from_rules([{"name": "r", "action": "teleport"}], module="m")


class TestSerializedShapesStillValidate:
    """SPEC-0 is the contract; redaction and sessions must not break it."""

    @pytest.fixture
    def flow_validator(self) -> Draft202012Validator:
        return validator_for("flow.schema.json")

    def _record(self, sink: RingSink) -> Any:
        sink.record_http(
            request_with_secrets(),
            NormalizedResponse(
                flow_id="f0",
                timestamp="t",
                status=200,
                headers=(("set-cookie", COOKIE_SECRET),),
                body=json.dumps({"password": "p"}).encode(),
            ),
            Provenance(profile="default"),
            {"pporlock_ms": 1.0},
        )
        return sink.ring.get("f0")

    def test_a_redacted_flow_validates(
        self, sink: RingSink, flow_validator: Draft202012Validator
    ) -> None:
        payload = serialize_flow(self._record(sink), "bodies", Redactor())
        flow_validator.validate(payload)
        assert payload["redacted"] is True

    def test_a_session_flow_validates(
        self, sink: RingSink, store: SessionStore, flow_validator: Draft202012Validator
    ) -> None:
        meta = store.start("s")
        self._record(sink)
        store.stop(meta.session_id)
        stored = store.reader(meta.session_id).get("f0")
        assert stored is not None
        flow_validator.validate(serialize_flow(stored, "bodies"))

    def test_a_session_websocket_flow_validates(
        self, sink: RingSink, store: SessionStore, flow_validator: Draft202012Validator
    ) -> None:
        meta = store.start("s")
        sink.record_websocket_message(
            WebSocketMessage(
                flow_id="ws0",
                index=0,
                timestamp="2026-08-27T14:00:00.000Z",
                direction="inbound",
                opcode="text",
                payload=b"hi",
            )
        )
        store.stop(meta.session_id)
        stored = store.reader(meta.session_id).get("ws0")
        assert stored is not None
        flow_validator.validate(serialize_flow(stored, "bodies"))


class TestExportEdges:
    """REQ CAP-024. The shapes HAR cannot carry, and what we do instead."""

    def test_a_passthrough_flow_is_omitted_from_har(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        """HAR has no entry shape for a tunnelled connection, and inventing a
        200 for one would make the export say something that did not happen."""
        from pporlock.capture.export import export_har, export_native

        meta = store.start("s")
        sink.record_passthrough("bank.example", "1.2.3.4", Provenance(profile="default"), {})
        store.stop(meta.session_id)

        reader = store.reader(meta.session_id)
        assert export_har(reader)["log"]["entries"] == []
        # The native export keeps it: a session that silently dropped the
        # excluded host would not explain why nothing from it was captured.
        assert len(export_native(reader)["flows"]) == 1

    def test_a_websocket_flow_is_omitted_from_har_but_kept_natively(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        from pporlock.capture.export import export_har, export_native

        meta = store.start("s")
        sink.record_websocket_message(
            WebSocketMessage(
                flow_id="ws0",
                index=0,
                timestamp="2026-08-27T14:00:00.000Z",
                direction="inbound",
                opcode="text",
                payload=b"hi",
            )
        )
        store.stop(meta.session_id)

        reader = store.reader(meta.session_id)
        assert export_har(reader)["log"]["entries"] == []
        assert export_native(reader)["flows"][0]["kind"] == "websocket"

    def test_binary_bodies_are_base64_in_har(self, sink: RingSink, store: SessionStore) -> None:
        from pporlock.capture.export import export_har

        meta = store.start("s")
        sink.record_http(
            request_with_secrets(),
            NormalizedResponse(flow_id="f0", timestamp="t", status=200, body=b"\xff\xfe\x00binary"),
            Provenance(profile="default"),
            {"pporlock_ms": 1.0},
        )
        store.stop(meta.session_id)

        entry = export_har(store.reader(meta.session_id))["log"]["entries"][0]
        assert entry["response"]["content"]["encoding"] == "base64"

    def test_a_request_body_becomes_har_post_data(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        from pporlock.capture.export import export_har

        request = NormalizedRequest(
            flow_id="f0",
            timestamp="2026-08-27T14:00:00.000Z",
            scheme="https",
            method="POST",
            host="api.example.com",
            port=443,
            path="/v1/login",
            url="https://api.example.com/v1/login",
            headers=(("content-type", "application/json"),),
            body=b'{"user":"tim"}',
        )
        meta = store.start("s")
        sink.record_http(request, None, Provenance(profile="default"), {"pporlock_ms": 1.0})
        store.stop(meta.session_id)

        entry = export_har(store.reader(meta.session_id))["log"]["entries"][0]
        assert entry["request"]["postData"]["mimeType"] == "application/json"
        assert entry["request"]["postData"]["text"] == '{"user":"tim"}'

    def test_a_binary_request_body_is_flagged_base64(
        self, sink: RingSink, store: SessionStore
    ) -> None:
        from pporlock.capture.export import export_har

        request = NormalizedRequest(
            flow_id="f0",
            timestamp="2026-08-27T14:00:00.000Z",
            scheme="https",
            method="POST",
            host="api.example.com",
            port=443,
            path="/upload",
            url="https://api.example.com/upload",
            body=b"\xff\xfe\x00",
        )
        meta = store.start("s")
        sink.record_http(request, None, Provenance(profile="default"), {"pporlock_ms": 1.0})
        store.stop(meta.session_id)

        entry = export_har(store.reader(meta.session_id))["log"]["entries"][0]
        assert entry["request"]["postData"]["encoding"] == "base64"

    def test_an_unknown_format_raises(self, store: SessionStore) -> None:
        from pporlock.capture.export import export_session

        meta = store.start("s")
        store.stop(meta.session_id)
        with pytest.raises(ValueError, match="unknown export format"):
            export_session(store.reader(meta.session_id), "pcap")
