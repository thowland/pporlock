"""Python structures validate against the published JSON Schemas.

SPEC-0 is the source of truth for cross-component shapes. The daemon builds
those shapes in Python and the clients consume TypeScript generated from the
same schemas, so the only thing keeping the two honest is this: what Python
emits must validate against what the schema declares.

Without this, the schemas drift into documentation and the contract stops being
a contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pporlock.engine.provenance import (
    Action,
    NoteCode,
    Outcome,
    Phase,
    ProvenanceBuilder,
)

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


def _registry() -> Registry:
    """All schemas, registered by $id, so cross-file $ref resolves."""
    registry = Registry()
    for path in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def validator_for(filename: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / filename).read_text())
    return Draft202012Validator(schema, registry=_registry())


@pytest.fixture(scope="module")
def provenance_validator() -> Draft202012Validator:
    return validator_for("provenance.schema.json")


@pytest.fixture(scope="module")
def flow_validator() -> Draft202012Validator:
    return validator_for("flow.schema.json")


def test_schema_directory_is_populated() -> None:
    assert list(SCHEMA_DIR.glob("*.schema.json")), f"no schemas in {SCHEMA_DIR}"


class TestProvenanceConformance:
    def test_empty_provenance_validates(self, provenance_validator: Draft202012Validator) -> None:
        """Every flow carries provenance, including one that matched nothing."""
        payload = ProvenanceBuilder("default").build().to_dict()
        provenance_validator.validate(payload)

    def test_populated_provenance_validates(
        self, provenance_validator: Draft202012Validator
    ) -> None:
        b = ProvenanceBuilder("ad-blocking", ("block-vendors", "strip-sri"))
        b.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module="block-vendors",
            rule_id="block-vendors:2",
            rule_name="block-analytics-vendor",
            action=Action.BLOCK,
            outcome=Outcome.APPLIED,
            duration_ms=0.3,
            stub="auto",
            derived_from_dest="script",
        )
        b.note(NoteCode.CSP_MODIFIED, "removed CSP", module="relax-csp")
        b.short_circuit("block-vendors:2")
        provenance_validator.validate(b.build(4.8).to_dict())

    @pytest.mark.parametrize("phase", list(Phase))
    def test_every_phase_is_accepted_by_the_schema(
        self, provenance_validator: Draft202012Validator, phase: Phase
    ) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=phase, module="m", rule_id="m:0", action=Action.BODY, outcome=Outcome.APPLIED
        )
        provenance_validator.validate(b.build().to_dict())

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_every_outcome_is_accepted_by_the_schema(
        self, provenance_validator: Draft202012Validator, outcome: Outcome
    ) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=outcome,
        )
        provenance_validator.validate(b.build().to_dict())

    @pytest.mark.parametrize("action", list(Action))
    def test_every_action_is_accepted_by_the_schema(
        self, provenance_validator: Draft202012Validator, action: Action
    ) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=action,
            outcome=Outcome.APPLIED,
        )
        provenance_validator.validate(b.build().to_dict())

    @pytest.mark.parametrize("code", list(NoteCode))
    def test_every_note_code_is_accepted_by_the_schema(
        self, provenance_validator: Draft202012Validator, code: NoteCode
    ) -> None:
        """The Python enum and the schema enum must not drift apart.

        This is the specific failure that would let the daemon emit a note the
        UI cannot render.
        """
        b = ProvenanceBuilder("p")
        b.note(code, "message")
        provenance_validator.validate(b.build().to_dict())

    def test_schema_rejects_an_unknown_outcome(
        self, provenance_validator: Draft202012Validator
    ) -> None:
        """Guards the guard: the validator must actually reject bad data."""
        payload = ProvenanceBuilder("p").build().to_dict()
        payload["entries"] = [
            {
                "seq": 0,
                "phase": "response_body",
                "module": "m",
                "rule_id": "m:0",
                "action": "body",
                "outcome": "invented_outcome",
                "duration_ms": 0.0,
            }
        ]
        assert not provenance_validator.is_valid(payload)

    def test_schema_rejects_an_unknown_note_code(
        self, provenance_validator: Draft202012Validator
    ) -> None:
        payload = ProvenanceBuilder("p").build().to_dict()
        payload["notes"] = [{"code": "invented", "severity": "info", "message": "x"}]
        assert not provenance_validator.is_valid(payload)

    def test_schema_rejects_a_missing_required_field(
        self, provenance_validator: Draft202012Validator
    ) -> None:
        payload = ProvenanceBuilder("p").build().to_dict()
        del payload["profile"]
        assert not provenance_validator.is_valid(payload)


class TestFlowConformance:
    """The FlowRecord shape the API and session storage will emit (SPEC-0 §3.4)."""

    def _flow(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "flow_id": "01JB2K7Q9X4M8Z0V3T5R7W1Y2A",
            "kind": "http",
            "started_at": "2026-08-27T14:03:22.417Z",
            "completed_at": "2026-08-27T14:03:22.694Z",
            "tab_id": 481,
            "request": {
                "method": "GET",
                "scheme": "https",
                "host": "cdn.example.com",
                "port": 443,
                "path": "/a/analytics.js",
                "query": [["v", "3"]],
                "url": "https://cdn.example.com/a/analytics.js?v=3",
                "http_version": "HTTP/2",
                "dest": "script",
                "headers": [["accept", "*/*"]],
                "body_size": 0,
                "body": None,
                "body_truncated": False,
            },
            "response": {
                "status": 200,
                "reason": "OK",
                "http_version": "HTTP/2",
                "headers": [["content-type", "application/javascript"]],
                "content_type": "application/javascript",
                "body_size": 48213,
                "body": None,
                "body_encoding": None,
                "body_truncated": False,
                "streamed": False,
            },
            "timing": {"upstream_ms": 210.7, "pporlock_ms": 4.8, "total_ms": 277.0},
            "modified": True,
            "blocked": False,
            "provenance": ProvenanceBuilder("default").build().to_dict(),
            "redacted": True,
        }
        record.update(overrides)
        return record

    def test_full_http_flow_validates(self, flow_validator: Draft202012Validator) -> None:
        flow_validator.validate(self._flow())

    def test_summary_flow_without_bodies_validates(
        self, flow_validator: Draft202012Validator
    ) -> None:
        """Summary detail omits bodies entirely (SPEC-0 §6.3)."""
        flow = self._flow()
        del flow["request"]["body"]
        del flow["response"]["body"]
        flow_validator.validate(flow)

    def test_unattributed_flow_validates(self, flow_validator: Draft202012Validator) -> None:
        """tab_id is null until attribution backfills, and may stay null."""
        flow_validator.validate(self._flow(tab_id=None))

    def test_passthrough_flow_validates(self, flow_validator: Draft202012Validator) -> None:
        """An excluded connection is recorded with no content (REQ PXY-015)."""
        flow_validator.validate(
            {
                "flow_id": "01JB2K7Q9X4M8Z0V3T5R7W1Y2B",
                "kind": "passthrough",
                "started_at": "2026-08-27T14:03:22.417Z",
                "modified": False,
                "blocked": False,
                "provenance": ProvenanceBuilder("default").build().to_dict(),
                "redacted": True,
            }
        )

    def test_websocket_flow_validates(self, flow_validator: Draft202012Validator) -> None:
        flow = self._flow(kind="websocket")
        flow["websocket"] = {
            "closed": False,
            "close_code": None,
            "message_count": 1,
            "messages": [
                {
                    "index": 0,
                    "timestamp": "2026-08-27T14:03:25.001Z",
                    "direction": "outbound",
                    "opcode": "text",
                    "size": 218,
                    "payload": "hello",
                    "payload_encoding": "utf8",
                    "truncated": False,
                }
            ],
        }
        flow_validator.validate(flow)

    def test_streamed_response_validates(self, flow_validator: Draft202012Validator) -> None:
        flow = self._flow()
        flow["response"]["streamed"] = True
        flow["response"]["body"] = None
        flow_validator.validate(flow)

    def test_schema_rejects_an_unknown_kind(self, flow_validator: Draft202012Validator) -> None:
        assert not flow_validator.is_valid(self._flow(kind="carrier-pigeon"))

    def test_schema_rejects_an_extra_field(self, flow_validator: Draft202012Validator) -> None:
        """Strict: a field nobody agreed on must not travel silently."""
        assert not flow_validator.is_valid(self._flow(surprise="value"))

    def test_schema_rejects_a_flow_with_no_provenance(
        self, flow_validator: Draft202012Validator
    ) -> None:
        """REQ CAP-013. Provenance is not optional on any flow, anywhere."""
        flow = self._flow()
        del flow["provenance"]
        assert not flow_validator.is_valid(flow)

    def test_schema_rejects_a_malformed_header_pair(
        self, flow_validator: Draft202012Validator
    ) -> None:
        flow = self._flow()
        flow["request"]["headers"] = [["only-one-element"]]
        assert not flow_validator.is_valid(flow)
