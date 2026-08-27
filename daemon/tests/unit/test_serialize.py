"""Wire serialization and detail levels. SPEC-0 §3.4, §6.3."""

from __future__ import annotations

import json

import pytest

from pporlock.capture.records import FlowRecord
from pporlock.control.serialize import parse_detail, serialize_flow, serialize_flow_page
from pporlock.engine.models import WebSocketMessage
from pporlock.engine.provenance import (
    Action,
    NoteCode,
    Outcome,
    Phase,
    ProvenanceBuilder,
)

from .test_ring import make_record
from .test_schema_conformance import validator_for


@pytest.fixture(scope="module")
def flow_validator():
    return validator_for("flow.schema.json")


class TestDetailParsing:
    @pytest.mark.parametrize("value", ["summary", "full", "bodies"])
    def test_valid_levels(self, value: str) -> None:
        assert parse_detail(value, "summary") == value

    def test_case_insensitive(self) -> None:
        assert parse_detail("FULL", "summary") == "full"

    def test_absent_uses_the_default(self) -> None:
        assert parse_detail(None, "full") == "full"

    def test_unknown_falls_back_rather_than_erroring(self) -> None:
        """A bad detail level is a client bug that should not cost the user an
        answer, and the fallback is always the cheaper option."""
        assert parse_detail("everything", "summary") == "summary"


class TestDetailLevels:
    def test_summary_omits_bodies(self) -> None:
        record = make_record("f0", body=b"a body")
        payload = serialize_flow(record, "summary")
        assert "body" not in payload["response"]

    def test_full_includes_the_key_but_not_the_body(self) -> None:
        """So a client can tell 'no body' from 'body withheld at this level'."""
        payload = serialize_flow(make_record("f0", body=b"a body"), "full")
        assert payload["response"]["body"] is None

    def test_bodies_includes_the_body(self) -> None:
        payload = serialize_flow(make_record("f0", body=b"a body"), "bodies")
        assert payload["response"]["body"] == "a body"
        assert payload["response"]["body_encoding"] == "utf8"

    def test_binary_body_is_base64(self) -> None:
        payload = serialize_flow(make_record("f0", body=b"\xff\xfe"), "bodies")
        assert payload["response"]["body_encoding"] == "base64"

    def test_summary_collapses_provenance_entries_but_keeps_notes(self) -> None:
        """The flag icons in the flow table are drawn from note codes, so those
        survive; a hundred-rule chain per row would dominate a list response."""
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        builder.note(NoteCode.CSP_MODIFIED, "removed")
        payload = serialize_flow(make_record("f0", provenance=builder.build()), "summary")
        assert payload["provenance"]["entries"] == []
        assert payload["provenance"]["notes"][0]["code"] == "csp_modified"

    def test_full_keeps_provenance_entries(self) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        payload = serialize_flow(make_record("f0", provenance=builder.build()), "full")
        assert len(payload["provenance"]["entries"]) == 1


class TestAlwaysPresent:
    def test_a_flow_with_no_provenance_still_gets_the_key(self) -> None:
        """REQ CAP-013 — provenance is not optional on any flow, anywhere."""
        payload = serialize_flow(make_record("f0"), "full")
        assert payload["provenance"]["profile"] == "default"

    def test_timing_is_always_present(self) -> None:
        assert "pporlock_ms" in serialize_flow(make_record("f0"))["timing"]

    def test_redaction_flag_is_reported(self) -> None:
        assert serialize_flow(make_record("f0"))["redacted"] is False


class TestKinds:
    def test_passthrough_carries_host_and_reason(self, flow_validator) -> None:
        """REQ PXY-015 — visible even though it is not readable."""
        record = FlowRecord(
            flow_id="pt",
            kind="passthrough",
            started_at="2026-08-27T14:00:00.000Z",
            passthrough_host="www.chase.com",
            passthrough_pattern="*.chase.com",
            passthrough_reason="sensitive: financial",
        )
        payload = serialize_flow(record)
        assert payload["passthrough"]["host"] == "www.chase.com"
        assert payload["passthrough"]["pattern"] == "*.chase.com"
        assert payload["passthrough"]["reason"] == "sensitive: financial"
        flow_validator.validate(payload)

    def test_websocket_messages_are_omitted_at_summary(self) -> None:
        record = make_record("ws")
        record.kind = "websocket"
        record.ws_messages.append(
            WebSocketMessage(
                flow_id="ws",
                index=0,
                timestamp="t",
                direction="outbound",
                opcode="text",
                payload=b"hi",
            )
        )
        assert "messages" not in serialize_flow(record, "summary")["websocket"]
        assert serialize_flow(record, "summary")["websocket"]["message_count"] == 1

    def test_websocket_payload_only_at_bodies_detail(self) -> None:
        record = make_record("ws")
        record.kind = "websocket"
        record.ws_messages.append(
            WebSocketMessage(
                flow_id="ws",
                index=0,
                timestamp="t",
                direction="inbound",
                opcode="text",
                payload=b"hi",
            )
        )
        assert serialize_flow(record, "full")["websocket"]["messages"][0]["payload"] is None
        assert serialize_flow(record, "bodies")["websocket"]["messages"][0]["payload"] == "hi"


class TestSchemaConformance:
    """What the daemon puts on the wire must validate against the contract."""

    @pytest.mark.parametrize("detail", ["summary", "full", "bodies"])
    def test_http_flow_validates_at_every_detail_level(
        self,
        flow_validator,
        detail: str,
    ) -> None:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        builder.note(NoteCode.SRI_STRIPPED, "stripped")
        record = make_record("f0", body=b"<html></html>", provenance=builder.build())
        flow_validator.validate(serialize_flow(record, detail))  # type: ignore[arg-type]

    def test_page_is_json_serializable(self) -> None:
        page = serialize_flow_page([make_record("f0")], next_cursor=None, total_estimate=1)
        assert json.loads(json.dumps(page))["total_estimate"] == 1
