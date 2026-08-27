"""Provenance. SPEC-0 §4, REQ CAP-010 through CAP-013.

The completeness tests here matter more than they look: every outcome and every
note code must have a representation, because a client that silently drops one
reintroduces exactly the invisible failure provenance exists to prevent.
"""

from __future__ import annotations

import json

import pytest

from pporlock.engine.provenance import (
    NOTE_SEVERITY,
    Action,
    NoteCode,
    Outcome,
    Phase,
    Provenance,
    ProvenanceBuilder,
    Severity,
)


@pytest.fixture
def builder() -> ProvenanceBuilder:
    return ProvenanceBuilder("ad-blocking", ("block-vendors", "strip-sri"))


class TestEnumCompleteness:
    def test_every_phase_in_spec_is_present(self) -> None:
        """SPEC-0 §4.2 — the fixed pipeline order (REQ PXY-020)."""
        assert [p.value for p in Phase] == [
            "clienthello",
            "request_short_circuit",
            "request_headers",
            "buffering_decision",
            "response_headers",
            "response_body",
            "websocket",
        ]

    def test_every_action_in_the_taxonomy_is_present(self) -> None:
        """REQ PXY-030 — six actions, no more."""
        assert {a.value for a in Action} == {
            "passthrough",
            "block",
            "map_local",
            "redirect",
            "headers",
            "body",
        }

    def test_every_outcome_in_spec_is_present(self) -> None:
        """SPEC-0 §4.3."""
        assert {o.value for o in Outcome} == {
            "applied",
            "no_change",
            "skipped_streamed",
            "skipped_budget",
            "skipped_short_circuit",
            "skipped_disabled",
            "error",
        }

    def test_every_note_code_has_a_canonical_severity(self) -> None:
        """A note without a severity cannot be styled, so it would render as noise."""
        missing = [c for c in NoteCode if c not in NOTE_SEVERITY]
        assert not missing, f"note codes with no severity: {missing}"

    def test_severity_map_has_no_stray_entries(self) -> None:
        assert set(NOTE_SEVERITY) == set(NoteCode)

    def test_the_dangerous_notes_are_at_least_warnings(self) -> None:
        """These weaken a page's own protections and must never be info-level.

        A CSP relaxation or an SRI strip that renders as informational will be
        scrolled past, which defeats the in-page banner (REQ EXT-020).
        """
        for code in (
            NoteCode.CSP_MODIFIED,
            NoteCode.SRI_STRIPPED,
            NoteCode.SCRIPT_INJECTED,
            NoteCode.DEV_TOGGLE_ACTIVE,
        ):
            assert NOTE_SEVERITY[code] in (Severity.WARNING, Severity.ERROR)

    def test_failure_notes_are_errors(self) -> None:
        for code in (
            NoteCode.MODULE_ERROR,
            NoteCode.MODULE_QUARANTINED,
            NoteCode.MAP_LOCAL_MISSING,
        ):
            assert NOTE_SEVERITY[code] is Severity.ERROR


class TestBuilder:
    def test_seq_increments_in_evaluation_order(self, builder: ProvenanceBuilder) -> None:
        for _ in range(3):
            builder.record(
                phase=Phase.RESPONSE_BODY,
                module="m",
                rule_id="m:0",
                action=Action.BODY,
                outcome=Outcome.APPLIED,
            )
        assert [e.seq for e in builder.build().entries] == [0, 1, 2]

    def test_entry_count_tracks_records(self, builder: ProvenanceBuilder) -> None:
        assert builder.entry_count == 0
        builder.record(
            phase=Phase.REQUEST_HEADERS,
            module="m",
            rule_id="m:0",
            action=Action.HEADERS,
            outcome=Outcome.NO_CHANGE,
        )
        assert builder.entry_count == 1

    def test_detail_kwargs_land_in_the_entry(self, builder: ProvenanceBuilder) -> None:
        entry = builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module="block-vendors",
            rule_id="block-vendors:2",
            action=Action.BLOCK,
            outcome=Outcome.APPLIED,
            stub="auto",
            derived_from_dest="script",
        )
        assert entry.detail == {"stub": "auto", "derived_from_dest": "script"}

    def test_note_severity_defaults_to_the_canonical_one(self, builder: ProvenanceBuilder) -> None:
        note = builder.note(NoteCode.CSP_MODIFIED, "removed CSP")
        assert note.severity is Severity.WARNING

    def test_note_severity_can_be_overridden(self, builder: ProvenanceBuilder) -> None:
        note = builder.note(NoteCode.BODY_TRUNCATED, "huge", severity=Severity.WARNING)
        assert note.severity is Severity.WARNING

    def test_short_circuit_is_recorded(self, builder: ProvenanceBuilder) -> None:
        """'An earlier rule ate it' is the most common debugging confusion."""
        builder.short_circuit("block-vendors:2")
        assert builder.build().short_circuited_by == "block-vendors:2"

    def test_short_circuit_is_none_by_default(self, builder: ProvenanceBuilder) -> None:
        assert builder.build().short_circuited_by is None

    def test_set_modules_replaces_the_list(self, builder: ProvenanceBuilder) -> None:
        builder.set_modules(("only-one",))
        assert builder.build().evaluated_modules == ("only-one",)

    def test_build_is_repeatable_and_does_not_reset(self, builder: ProvenanceBuilder) -> None:
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        assert len(builder.build().entries) == 1
        assert len(builder.build().entries) == 1

    def test_total_ms_is_carried_through(self, builder: ProvenanceBuilder) -> None:
        assert builder.build(4.8).total_ms == 4.8


class TestQueries:
    def test_modules_fired_lists_only_applied(self) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="applied-one",
            rule_id="a:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="skipped-one",
            rule_id="s:0",
            action=Action.BODY,
            outcome=Outcome.SKIPPED_STREAMED,
        )
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="no-change-one",
            rule_id="n:0",
            action=Action.BODY,
            outcome=Outcome.NO_CHANGE,
        )
        assert b.build().modules_fired() == ["applied-one"]

    def test_modules_fired_deduplicates_and_preserves_order(self) -> None:
        b = ProvenanceBuilder("p")
        for rule_id in ("m:0", "m:1"):
            b.record(
                phase=Phase.RESPONSE_BODY,
                module="m",
                rule_id=rule_id,
                action=Action.BODY,
                outcome=Outcome.APPLIED,
            )
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="z",
            rule_id="z:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        assert b.build().modules_fired() == ["m", "z"]

    def test_has_note(self) -> None:
        b = ProvenanceBuilder("p")
        b.note(NoteCode.RESPONSE_STREAMED, "too big")
        prov = b.build()
        assert prov.has_note(NoteCode.RESPONSE_STREAMED)
        assert not prov.has_note(NoteCode.CSP_MODIFIED)

    def test_max_severity_is_none_without_notes(self) -> None:
        assert ProvenanceBuilder("p").build().max_severity() is None

    def test_max_severity_picks_the_worst(self) -> None:
        b = ProvenanceBuilder("p")
        b.note(NoteCode.RESPONSE_STREAMED, "info-level")
        b.note(NoteCode.MODULE_ERROR, "error-level")
        b.note(NoteCode.CSP_MODIFIED, "warning-level")
        assert b.build().max_severity() is Severity.ERROR

    def test_errored_is_true_for_an_error_outcome(self) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=Outcome.ERROR,
        )
        assert b.build().errored

    def test_errored_is_true_for_an_error_note(self) -> None:
        b = ProvenanceBuilder("p")
        b.note(NoteCode.MAP_LOCAL_MISSING, "no such file")
        assert b.build().errored

    def test_errored_is_false_for_a_clean_flow(self) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.REQUEST_HEADERS,
            module="m",
            rule_id="m:0",
            action=Action.HEADERS,
            outcome=Outcome.APPLIED,
        )
        b.note(NoteCode.RESPONSE_STREAMED, "streamed")
        assert not b.build().errored


class TestSerialization:
    def test_round_trips_through_json(self) -> None:
        """The wire form must be plain JSON — enums serialize as their values."""
        b = ProvenanceBuilder("ad-blocking", ("block-vendors",))
        b.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module="block-vendors",
            rule_id="block-vendors:2",
            rule_name="block-analytics-vendor",
            action=Action.BLOCK,
            outcome=Outcome.APPLIED,
            duration_ms=0.3,
            stub="auto",
        )
        b.note(NoteCode.CSP_MODIFIED, "removed CSP", module="relax-csp")
        b.short_circuit("block-vendors:2")

        payload = json.loads(json.dumps(b.build(4.8).to_dict()))

        assert payload["profile"] == "ad-blocking"
        assert payload["short_circuited_by"] == "block-vendors:2"
        assert payload["total_ms"] == 4.8
        entry = payload["entries"][0]
        assert entry["phase"] == "request_short_circuit"
        assert entry["action"] == "block"
        assert entry["outcome"] == "applied"
        assert entry["rule_name"] == "block-analytics-vendor"
        assert entry["detail"] == {"stub": "auto"}
        note = payload["notes"][0]
        assert note["code"] == "csp_modified"
        assert note["severity"] == "warning"
        assert note["module"] == "relax-csp"

    def test_empty_provenance_serializes(self) -> None:
        """Every flow carries provenance, including one that matched nothing."""
        payload = Provenance(profile="default").to_dict()
        assert payload == {
            "profile": "default",
            "evaluated_modules": [],
            "entries": [],
            "notes": [],
            "total_ms": 0.0,
            "short_circuited_by": None,
        }

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_every_outcome_serializes(self, outcome: Outcome) -> None:
        b = ProvenanceBuilder("p")
        b.record(
            phase=Phase.RESPONSE_BODY,
            module="m",
            rule_id="m:0",
            action=Action.BODY,
            outcome=outcome,
        )
        assert json.loads(json.dumps(b.build().to_dict()))["entries"][0]["outcome"] == outcome.value

    @pytest.mark.parametrize("code", list(NoteCode))
    def test_every_note_code_serializes(self, code: NoteCode) -> None:
        b = ProvenanceBuilder("p")
        b.note(code, "message")
        payload = json.loads(json.dumps(b.build().to_dict()))["notes"][0]
        assert payload["code"] == code.value
        assert payload["severity"] in {"info", "warning", "error"}
