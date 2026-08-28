"""The evaluator and stub synthesis. SPEC-1 §4.3, §4.7.

Everything here runs with no proxy and no network, which is the point of the
pure engine (REQ TST-001) and what makes the dry runner possible (REQ CAP-031).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pporlock.engine.evaluator import Evaluator, TimeBudget, _resolve_asset
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.provenance import Action, NoteCode, Outcome, Phase, ProvenanceBuilder
from pporlock.engine.ruleset import RuleSet
from pporlock.engine.stubs import (
    TRANSPARENT_GIF,
    StubLibrary,
    auto_for,
    infer_dest_from_accept,
)
from pporlock.errors import AssetPathError, RuleValidationError


def req(**kwargs: object) -> NormalizedRequest:
    base: dict[str, object] = {
        "flow_id": "f",
        "timestamp": "t",
        "scheme": "https",
        "method": "GET",
        "host": "cdn.vendor.example",
        "port": 443,
        "path": "/a.js",
        "url": "https://cdn.vendor.example/a.js",
        "dest": "script",
    }
    base.update(kwargs)
    return NormalizedRequest(**base)  # type: ignore[arg-type]


def resp(**kwargs: object) -> NormalizedResponse:
    base: dict[str, object] = {
        "flow_id": "f",
        "timestamp": "t",
        "status": 200,
        "headers": (("content-type", "text/html"),),
        "body": b"<html></html>",
    }
    base.update(kwargs)
    return NormalizedResponse(**base)  # type: ignore[arg-type]


def evaluator(rules: list[dict[str, Any]] | None = None, **kwargs: Any) -> Evaluator:
    return Evaluator(RuleSet.from_rules(rules or [], module="m"), **kwargs)


def builder() -> ProvenanceBuilder:
    return ProvenanceBuilder("default")


BLOCK = {"name": "block-vendor", "action": "block", "match": {"host": "*.vendor.example"}}


class TestStubDerivation:
    """REQ PXY-032 — the table is normative and implemented exactly once."""

    @pytest.mark.parametrize(
        "dest,status,content_type",
        [
            ("script", 200, "application/javascript"),
            ("image", 200, "image/gif"),
            ("empty", 200, "application/json"),
            ("iframe", 200, "text/html"),
            ("style", 200, "text/css"),
            ("document", 403, "text/html"),
            ("font", 204, None),
            ("media", 204, None),
            ("something-new", 204, None),
        ],
    )
    def test_derivation(self, dest: str | None, status: int, content_type: str | None) -> None:
        # An explicit Accept keeps this testing the Sec-Fetch-Dest table alone;
        # the absent-header fallback is covered separately.
        synthetic = auto_for(dest, req(headers=(("accept", "application/vnd.x"),)), rule="r")
        assert synthetic.status == status
        assert synthetic.content_type == content_type

    def test_an_image_gets_a_real_transparent_gif(self) -> None:
        """A layout can depend on a tracking pixel having dimensions."""
        assert auto_for("image", req()).body == TRANSPARENT_GIF

    def test_an_xhr_gets_valid_json_not_an_empty_body(self) -> None:
        """response.json() on nothing throws, which is the failure we prevent."""
        import json

        assert json.loads(auto_for("empty", req()).body) == {}

    def test_a_blocked_navigation_is_visible_to_the_user(self) -> None:
        """A 403 with a page, not a network error that looks like connectivity."""
        synthetic = auto_for("document", req(), rule="my-rule")
        assert synthetic.status == 403
        assert b"my-rule" in synthetic.body
        assert b"cdn.vendor.example" in synthetic.body

    def test_synthesised_responses_are_never_cached(self) -> None:
        """The rule can be edited a second later; a cached stub would outlive it."""
        headers = dict(auto_for("script", req()).headers)
        assert "no-store" in headers["cache-control"]

    def test_synthesised_responses_are_labelled(self) -> None:
        assert dict(auto_for("script", req()).headers)["x-pporlock"] == "blocked"


class TestAcceptFallback:
    """Sec-Fetch-Dest is only sent on secure contexts.

    On a plain-HTTP page Chrome omits the Sec-Fetch headers entirely, which was
    measured directly: a blocked tracking pixel received a 204 and never
    rendered. Accept is always sent and covers the cases it can distinguish.
    """

    def _req(self, accept: str) -> NormalizedRequest:
        return req(dest=None, headers=(("accept", accept),))

    @pytest.mark.parametrize(
        "accept,expected_type",
        [
            ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8", "image/gif"),
            ("text/css,*/*;q=0.1", "text/css"),
            ("*/*", "application/javascript"),
        ],
    )
    def test_infers_from_accept(self, accept: str, expected_type: str) -> None:
        assert auto_for(None, self._req(accept)).content_type == expected_type

    def test_a_document_accept_still_gets_the_explanatory_page(self) -> None:
        synthetic = auto_for(None, self._req("text/html,application/xhtml+xml"))
        assert synthetic.status == 403

    def test_an_ambiguous_accept_resolves_toward_script(self) -> None:
        """*/* cannot separate a script from an XHR. An empty script body is
        harmless, and a blocked tracker requested with */* is overwhelmingly a
        script tag."""
        assert auto_for(None, self._req("*/*")).content_type == "application/javascript"

    def test_sec_fetch_dest_still_wins_when_present(self) -> None:
        request = req(dest="image", headers=(("accept", "text/html"),))
        assert auto_for("image", request).content_type == "image/gif"

    def test_an_unrecognised_accept_falls_through_to_204(self) -> None:
        assert auto_for(None, self._req("application/vnd.custom")).status == 204

    def test_no_accept_at_all_resolves_toward_script(self) -> None:
        assert auto_for(None, req(dest=None)).content_type == "application/javascript"

    @pytest.mark.parametrize(
        "accept,expected",
        [
            ("text/html", "document"),
            ("text/css", "style"),
            ("image/png", "image"),
            ("font/woff2", "font"),
            ("*/*", None),
            ("", None),
        ],
    )
    def test_inference_directly(self, accept: str, expected: str | None) -> None:
        assert infer_dest_from_accept(accept) == expected

    def test_none_accept(self) -> None:
        assert infer_dest_from_accept(None) is None


class TestStubLibrary:
    def test_ships_the_stubs_the_spec_names(self) -> None:
        """REQ PXY-033 — where most of the value in tracker suppression sits."""
        library = StubLibrary()
        for name in ("analytics", "gtm", "ga", "facebook-pixel", "noop"):
            assert library.has(name), name

    def test_a_named_stub_defines_the_globals_a_page_expects(self) -> None:
        # A page calling analytics.track() on a script that failed to load
        # throws; one that loads this proceeds.
        body = StubLibrary().named("analytics").body
        assert b"analytics" in body
        assert b"track" in body

    def test_the_gtm_stub_preserves_datalayer(self) -> None:
        """GTM's own snippet pushes onto it before this loads."""
        assert b"dataLayer" in StubLibrary().named("gtm").body

    def test_an_unknown_stub_fails_loudly_and_lists_what_exists(self) -> None:
        with pytest.raises(RuleValidationError, match="unknown stub"):
            StubLibrary().named("no-such-stub")

    def test_auto_resolves_from_dest(self) -> None:
        assert StubLibrary().resolve("auto", req(dest="image")).content_type == "image/gif"

    def test_none_resolves_as_auto(self) -> None:
        assert StubLibrary().resolve(None, req(dest="image")).content_type == "image/gif"

    def test_an_inline_stub_specification(self) -> None:
        synthetic = StubLibrary().resolve(
            {"status": 418, "content_type": "text/plain", "body": "nope"}, req()
        )
        assert synthetic.status == 418
        assert synthetic.body == b"nope"

    def test_an_invalid_specification_is_rejected(self) -> None:
        with pytest.raises(RuleValidationError, match="invalid stub"):
            StubLibrary().resolve(42, req())

    def test_an_empty_directory_yields_no_stubs(self, tmp_path: Path) -> None:
        assert StubLibrary([tmp_path]).names == ()


class TestClientHello:
    def test_an_excluded_host_is_tunneled(self) -> None:
        ev = evaluator(
            exclusions=ExclusionList([ExclusionEntry("*.apple.com", "update: OS", "default")])
        )
        b = builder()
        decision = ev.evaluate_clienthello("swscan.apple.com", None, b)
        assert decision.passthrough
        assert b.build().has_note(NoteCode.PASSTHROUGH_EXCLUDED)

    def test_an_ordinary_host_is_decrypted(self) -> None:
        b = builder()
        assert not evaluator().evaluate_clienthello("example.com", None, b).passthrough
        assert b.build().notes == ()

    def test_the_note_carries_why(self) -> None:
        ev = evaluator(
            exclusions=ExclusionList([ExclusionEntry("*.chase.com", "sensitive: financial")])
        )
        b = builder()
        ev.evaluate_clienthello("www.chase.com", None, b)
        note = b.build().notes[0]
        assert note.detail["pattern"] == "*.chase.com"
        assert "financial" in note.detail["reason"]


class TestBlock:
    def test_blocks_a_matching_request(self) -> None:
        b = builder()
        decision = evaluator([BLOCK]).evaluate_request(req(), b)
        assert decision.blocked
        assert decision.short_circuit is not None
        assert decision.short_circuit.status == 200

    def test_does_not_block_a_non_matching_request(self) -> None:
        b = builder()
        decision = evaluator([BLOCK]).evaluate_request(req(host="safe.example"), b)
        assert not decision.blocked

    def test_provenance_names_the_rule_and_the_derived_type(self) -> None:
        """REQ CAP-011 — the primary debugging affordance."""
        b = builder()
        evaluator([BLOCK]).evaluate_request(req(), b)
        provenance = b.build()
        entry = provenance.entries[0]
        assert entry.rule_name == "block-vendor"
        assert entry.rule_id == "m:0"
        assert entry.action is Action.BLOCK
        assert entry.outcome is Outcome.APPLIED
        assert entry.phase is Phase.REQUEST_SHORT_CIRCUIT
        assert entry.detail["derived_from_dest"] == "script"
        assert entry.detail["synthesized_content_type"] == "application/javascript"

    def test_short_circuited_by_is_recorded(self) -> None:
        """'An earlier rule ate it' is the most common source of confusion."""
        b = builder()
        evaluator([BLOCK]).evaluate_request(req(), b)
        assert b.build().short_circuited_by == "m:0"

    def test_kill_mode_is_opt_in(self) -> None:
        """REQ PXY-031 — killing is the wrong default."""
        b = builder()
        decision = evaluator([{**BLOCK, "mode": "kill"}]).evaluate_request(req(), b)
        assert decision.kill
        assert decision.short_circuit is None
        assert b.build().entries[0].detail["mode"] == "kill"

    def test_a_named_stub_is_served(self) -> None:
        b = builder()
        decision = evaluator([{**BLOCK, "stub": "gtm"}]).evaluate_request(req(), b)
        assert decision.short_circuit is not None
        assert b"dataLayer" in decision.short_circuit.body

    def test_an_unknown_stub_is_an_error_not_a_crash(self) -> None:
        """Attributed to the rule, and the flow still proceeds."""
        b = builder()
        decision = evaluator([{**BLOCK, "stub": "nope"}]).evaluate_request(req(), b)
        assert not decision.blocked
        provenance = b.build()
        assert provenance.entries[0].outcome is Outcome.ERROR
        assert provenance.has_note(NoteCode.MODULE_ERROR)

    def test_first_match_wins(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {**BLOCK, "name": "first", "stub": {"status": 201}},
                {**BLOCK, "name": "second", "stub": {"status": 202}},
            ]
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.short_circuit is not None
        assert decision.short_circuit.status == 201


class TestMapLocal:
    def test_serves_a_local_file(self, tmp_path: Path) -> None:
        (tmp_path / "stub.js").write_text("window.x = 1;")
        b = builder()
        ev = evaluator(
            [{"name": "map", "action": "map_local", "file": "stub.js"}], asset_root=tmp_path
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.short_circuit is not None
        assert decision.short_circuit.body == b"window.x = 1;"
        assert decision.short_circuit.content_type == "text/javascript"

    def test_a_missing_file_fails_loudly(self, tmp_path: Path) -> None:
        """REQ PXY-034 — silence looks exactly like a rule that did not match."""
        b = builder()
        ev = evaluator(
            [{"name": "map", "action": "map_local", "file": "nope.js"}], asset_root=tmp_path
        )
        decision = ev.evaluate_request(req(), b)
        assert not decision.blocked
        provenance = b.build()
        assert provenance.entries[0].outcome is Outcome.ERROR
        assert provenance.has_note(NoteCode.MAP_LOCAL_MISSING)

    def test_no_asset_root_is_reported(self) -> None:
        b = builder()
        ev = evaluator([{"name": "map", "action": "map_local", "file": "x.js"}])
        ev.evaluate_request(req(), b)
        assert b.build().has_note(NoteCode.MAP_LOCAL_MISSING)

    def test_content_type_can_be_overridden(self, tmp_path: Path) -> None:
        (tmp_path / "data").write_text("{}")
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "map",
                    "action": "map_local",
                    "file": "data",
                    "content_type": "application/json",
                }
            ],
            asset_root=tmp_path,
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.short_circuit is not None
        assert decision.short_circuit.content_type == "application/json"

    def test_a_traversal_attempt_is_refused(self, tmp_path: Path) -> None:
        """implementation-plan.md §2.5 — path traversal."""
        b = builder()
        ev = evaluator(
            [{"name": "map", "action": "map_local", "file": "../../etc/passwd"}],
            asset_root=tmp_path,
        )
        ev.evaluate_request(req(), b)
        assert b.build().has_note(NoteCode.MAP_LOCAL_MISSING)


class TestAssetContainment:
    def test_a_relative_path_inside_the_root_resolves(self, tmp_path: Path) -> None:
        (tmp_path / "a.js").write_text("x")
        assert _resolve_asset(tmp_path, "a.js").name == "a.js"

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AssetPathError, match="must be relative"):
            _resolve_asset(tmp_path, "/etc/passwd")

    def test_traversal_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AssetPathError, match="escapes"):
            _resolve_asset(tmp_path, "../../etc/passwd")

    def test_a_symlink_pointing_out_is_refused(self, tmp_path: Path) -> None:
        """Containment is checked after symlink resolution — the case a naive
        prefix check misses."""
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        root = tmp_path / "assets"
        root.mkdir()
        (root / "link.txt").symlink_to(outside)
        with pytest.raises(AssetPathError, match="escapes"):
            _resolve_asset(root, "link.txt")


class TestRedirect:
    def test_rewrites_the_target(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "redir",
                    "action": "redirect",
                    "to": {"host": "localhost", "port": 8099, "scheme": "http"},
                }
            ]
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.mutation.redirect is not None
        assert decision.mutation.redirect.host == "localhost"
        assert decision.mutation.redirect.port == 8099
        assert decision.mutation.redirect.scheme == "http"

    def test_is_recorded(self) -> None:
        b = builder()
        ev = evaluator([{"name": "redir", "action": "redirect", "to": {"host": "h"}}])
        ev.evaluate_request(req(), b)
        assert b.build().entries[0].action is Action.REDIRECT


class TestHeaderRules:
    def test_request_headers_are_applied(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "h",
                    "action": "headers",
                    "request": {"add": {"x-test": "1"}, "remove": ["cookie"]},
                }
            ]
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.mutation.add_headers == [("x-test", "1")]
        assert decision.mutation.remove_headers == ["cookie"]

    def test_response_headers_are_applied(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "csp",
                    "action": "headers",
                    "response": {"remove": ["content-security-policy"]},
                }
            ]
        )
        decision = ev.evaluate_response(req(), resp(), b)
        assert decision.mutation.remove_headers == ["content-security-policy"]

    def test_all_matching_rules_apply_in_order(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {"name": "one", "action": "headers", "response": {"remove": ["a"]}},
                {"name": "two", "action": "headers", "response": {"remove": ["b"]}},
            ]
        )
        decision = ev.evaluate_response(req(), resp(), b)
        assert decision.mutation.remove_headers == ["a", "b"]

    def test_a_rule_that_changes_nothing_records_no_change(self) -> None:
        b = builder()
        ev = evaluator([{"name": "h", "action": "headers", "response": {"remove": []}}])
        ev.evaluate_response(req(), resp(), b)
        assert b.build().entries[0].outcome is Outcome.NO_CHANGE

    def test_header_rules_still_run_on_a_short_circuited_request(self) -> None:
        """A rule adding a header the synthesised response should carry is
        legitimate; skipping it silently would be surprising."""
        b = builder()
        ev = evaluator(
            [
                BLOCK,
                {
                    "name": "h",
                    "action": "headers",
                    "request": {"add": {"x": "1"}},
                },
            ]
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.blocked
        assert decision.mutation.add_headers == [("x", "1")]


class TestBuffering:
    """REQ PXY-021 — decided here or never."""

    def test_streams_when_no_rule_wants_the_body(self) -> None:
        b = builder()
        decision = evaluator().decide_buffering(req(), "text/html", 100, False, b)
        assert not decision.buffer
        assert decision.reason == "no_transform"
        assert b.build().has_note(NoteCode.RESPONSE_STREAMED)

    def test_streams_a_body_over_the_size_threshold(self) -> None:
        b = builder()
        ev = evaluator(max_buffer_bytes=1000)
        decision = ev.decide_buffering(req(), "text/html", 5000, True, b)
        assert not decision.buffer
        assert decision.reason == "size"

    def test_streams_a_type_outside_the_allowlist(self) -> None:
        b = builder()
        decision = evaluator().decide_buffering(req(), "video/mp4", 100, True, b)
        assert not decision.buffer
        assert decision.reason == "content_type"

    def test_buffers_a_wanted_html_body(self) -> None:
        b = builder()
        decision = evaluator().decide_buffering(req(), "text/html; charset=utf-8", 100, True, b)
        assert decision.buffer

    def test_buffers_when_the_length_is_unknown(self) -> None:
        """A chunked response has no Content-Length; refusing to buffer it would
        make every chunked page untransformable."""
        assert evaluator().decide_buffering(req(), "text/html", None, True, builder()).buffer

    def test_the_reason_is_always_recorded(self) -> None:
        """REQ PXY-022 — a skipped transform must never be silent."""
        for content_type, length, wants in [
            ("text/html", 100, False),
            ("video/mp4", 100, True),
        ]:
            b = builder()
            evaluator().decide_buffering(req(), content_type, length, wants, b)
            note = b.build().notes[0]
            assert note.code is NoteCode.RESPONSE_STREAMED
            assert note.detail["reason"]


class TestResponseBody:
    def test_a_streamed_response_records_the_skip(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "nothing-here", "repl": "x"},
                }
            ]
        )
        ev.evaluate_response(req(), resp(streamed=True, body=None), b)
        assert b.build().entries[0].outcome is Outcome.SKIPPED_STREAMED

    def test_an_exhausted_budget_records_the_skip_and_a_note(self) -> None:
        """REQ PXY-026 — the flow is delivered, not dropped."""
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "nothing-here", "repl": "x"},
                }
            ]
        )
        budget = TimeBudget(1.0)
        budget.consume(5.0)
        ev.evaluate_response(req(), resp(), b, budget)
        provenance = b.build()
        assert provenance.entries[0].outcome is Outcome.SKIPPED_BUDGET
        assert provenance.has_note(NoteCode.TRANSFORM_BUDGET_EXCEEDED)


class TestTimeBudget:
    def test_starts_unexhausted(self) -> None:
        assert not TimeBudget(250.0).exhausted

    def test_consumes(self) -> None:
        budget = TimeBudget(100.0)
        budget.consume(30.0)
        assert budget.spent == 30.0
        assert budget.remaining == 70.0

    def test_exhausts(self) -> None:
        budget = TimeBudget(10.0)
        budget.consume(10.0)
        assert budget.exhausted
        assert budget.remaining == 0.0


class TestAlwaysProvenance:
    def test_a_flow_matching_nothing_still_gets_a_record(self) -> None:
        """REQ CAP-013 — there is no path that produces a decision without one."""
        b = builder()
        evaluator().evaluate_request(req(), b)
        provenance = b.build()
        assert provenance.profile == "default"
        assert provenance.entries == ()
        assert provenance.short_circuited_by is None


class TestBudgetIsActuallyCharged:
    """A guard that cannot fire is not a guard.

    The budget was created and checked from Sprint 7 but never consumed, so
    skipped_budget could not occur in production. These assert it is charged.
    """

    def test_the_request_phase_charges_the_budget(self) -> None:
        budget = TimeBudget(1000.0)
        evaluator([BLOCK]).evaluate_request(req(), builder(), budget)
        assert budget.spent > 0

    def test_matching_a_large_rule_set_is_charged(self) -> None:
        """A budget that only counted body transforms would let the request
        phase overrun it unnoticed."""
        rules = [{**BLOCK, "name": f"r{i}", "match": {"host": f"*.no{i}.test"}} for i in range(200)]
        budget = TimeBudget(1000.0)
        evaluator(rules).evaluate_request(req(), builder(), budget)
        assert budget.spent > 0

    def test_the_body_phase_charges_the_budget(self) -> None:
        budget = TimeBudget(1000.0)
        ev = evaluator([{"name": "t", "action": "body", "transform": {"kind": "regex_sub"}}])
        ev.evaluate_response_body(req(), resp(), builder(), budget)
        assert budget.spent > 0

    def test_an_exhausted_budget_skips_the_remaining_transforms(self) -> None:
        budget = TimeBudget(0.0001)
        budget.consume(10.0)
        b = builder()
        ev = evaluator([{"name": "t", "action": "body", "transform": {"kind": "regex_sub"}}])
        ev.evaluate_response_body(req(), resp(), b, budget)
        assert b.build().entries[0].outcome is Outcome.SKIPPED_BUDGET

    def test_evaluation_without_a_budget_still_works(self) -> None:
        """The dry runner replays without one."""
        assert evaluator([BLOCK]).evaluate_request(req(), builder()).blocked


class TestOffloadIsRecorded:
    def test_a_body_rule_records_its_offload_decision(self) -> None:
        """REQ PXY-024 — the classification is visible in provenance rather than
        implicit, so an operator can see why something was slow."""
        b = builder()
        ev = evaluator([{"name": "t", "action": "body", "transform": {"kind": "inject_script"}}])
        ev.evaluate_response_body(req(), resp(), b)
        detail = b.build().entries[0].detail
        assert detail["offload"] is True
        assert detail["cost"] == "expensive"
        assert "offload_reason" in detail

    def test_a_small_scanning_transform_records_that_it_stayed_inline(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "nothing-here", "repl": "x"},
                }
            ]
        )
        ev.evaluate_response_body(req(), resp(), b)
        assert b.build().entries[0].detail["offload"] is False


class TestResponsePhasesAreSeparate:
    """Headers must be applied at responseheaders, before anything is on the
    wire; body work happens later. Conflating them meant header mutations on a
    streamed response were recorded as applied and changed nothing."""

    def test_header_evaluation_does_not_touch_body_rules(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "nothing-here", "repl": "x"},
                }
            ]
        )
        ev.evaluate_response_headers(req(), resp(), b)
        assert b.build().entries == ()

    def test_strip_csp_is_applied_in_the_header_phase_despite_being_a_body_rule(
        self,
    ) -> None:
        """It operates on headers, and once a response streams its headers are
        already on the wire — so it has to run where a mutation can still take
        effect (Sprint 9's finding)."""
        b = builder()
        ev = evaluator([{"name": "csp", "action": "body", "transform": {"kind": "strip_csp"}}])
        decision = ev.evaluate_response_headers(
            req(),
            resp(headers=(("content-security-policy", "default-src 'self'"),)),
            b,
        )
        assert "content-security-policy" in decision.mutation.remove_headers
        assert b.build().has_note(NoteCode.CSP_MODIFIED)

    def test_a_headers_rule_removing_csp_says_so(self) -> None:
        """REQ EXT-020. A CSP removed by a headers rule weakens the page exactly
        as much as one removed by strip_csp.

        Only the transform used to emit the note, so a rule written the other
        way relaxed CSP with no note, no banner, and nothing in the panel — the
        precise invisible weakening the banner exists to prevent.
        """
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "relax",
                    "action": "headers",
                    "response": {"remove": ["Content-Security-Policy"]},
                }
            ]
        )
        ev.evaluate_response_headers(
            req(), resp(headers=(("content-security-policy", "default-src 'self'"),)), b
        )
        prov = b.build()
        assert prov.has_note(NoteCode.CSP_MODIFIED)
        # Reported under its canonical lowercase name whatever the rule wrote.
        assert any("content-security-policy" in n.message for n in prov.notes)

    def test_a_headers_rule_replacing_csp_also_says_so(self) -> None:
        """Loosening a policy is a modification, not merely removing one."""
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "loosen",
                    "action": "headers",
                    "response": {"set": {"content-security-policy": "default-src *"}},
                }
            ]
        )
        ev.evaluate_response_headers(req(), resp(), b)
        assert b.build().has_note(NoteCode.CSP_MODIFIED)

    def test_a_headers_rule_touching_other_headers_says_nothing(self) -> None:
        """The note means something specific. A note on every header rule would
        make the banner appear constantly and teach people to ignore it."""
        b = builder()
        ev = evaluator([{"name": "h", "action": "headers", "response": {"set": {"x-thing": "1"}}}])
        ev.evaluate_response_headers(req(), resp(), b)
        assert not b.build().has_note(NoteCode.CSP_MODIFIED)

    def test_a_request_side_csp_header_is_not_reported(self) -> None:
        """CSP is a response header. A request-side rule naming it is doing
        something else, and claiming the page was weakened would be wrong."""
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "req",
                    "action": "headers",
                    "request": {"set": {"content-security-policy": "x"}},
                }
            ]
        )
        ev.evaluate_request(req(), b)
        assert not b.build().has_note(NoteCode.CSP_MODIFIED)

    def test_body_evaluation_does_not_touch_header_rules(self) -> None:
        b = builder()
        ev = evaluator([{"name": "h", "action": "headers", "response": {"remove": ["x"]}}])
        ev.evaluate_response_body(req(), resp(), b)
        assert b.build().entries == ()

    def test_the_combined_form_runs_both(self) -> None:
        """Kept for callers holding a fully-buffered response — the dry runner
        replaying complete recorded flows."""
        b = builder()
        ev = evaluator(
            [
                {"name": "h", "action": "headers", "response": {"remove": ["x"]}},
                {"name": "t", "action": "body", "transform": {"kind": "strip_csp"}},
            ]
        )
        ev.evaluate_response(req(), resp(), b)
        actions = {str(e.action) for e in b.build().entries}
        assert actions == {"headers", "body"}


class TestBodyRewriting:
    """REQ PXY-040/041/042 — the transforms actually running."""

    def html(self, body: str = "<html><head></head><body><p>hi</p></body></html>") -> Any:
        return resp(
            headers=(("content-type", "text/html; charset=utf-8"),),
            body=body.encode(),
        )

    def test_a_transform_changes_the_body(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "hi", "replace": "bye"},
                }
            ]
        )
        decision = ev.evaluate_response_body(req(), self.html(), b, None)
        assert decision.mutation.body is not None
        assert b"bye" in decision.mutation.body

    def test_a_transform_that_matches_nothing_records_no_change(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "absent", "replace": "x"},
                }
            ]
        )
        decision = ev.evaluate_response_body(req(), self.html(), b, None)
        assert decision.mutation.body is None
        assert b.build().entries[0].outcome is Outcome.NO_CHANGE

    def test_a_failing_transform_is_attributed_and_not_fatal(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "[", "repl": "x"},
                }
            ]
        )
        ev.evaluate_response_body(req(), self.html(), b, None)
        provenance = b.build()
        assert provenance.entries[0].outcome is Outcome.ERROR
        assert provenance.has_note(NoteCode.MODULE_ERROR)

    def test_a_rewritten_document_always_has_its_integrity_stripped(self) -> None:
        """REQ PXY-040. The breakage is invisible from the proxy's side, so
        leaving it to the operator to remember would guarantee it is forgotten."""
        page = (
            '<html><head><script src="a.js" integrity="sha384-x"></script></head>'
            "<body><p>hi</p></body></html>"
        )
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "hi", "replace": "bye"},
                }
            ]
        )
        decision = ev.evaluate_response_body(req(), self.html(page), b, None)
        assert decision.mutation.body is not None
        assert b"integrity" not in decision.mutation.body

    def test_the_implicit_strip_is_recorded_not_silent(self) -> None:
        page = '<html><head><script integrity="sha384-x"></script></head><body>hi</body></html>'
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "hi", "replace": "bye"},
                }
            ]
        )
        ev.evaluate_response_body(req(), self.html(page), b, None)
        names = [e.rule_name for e in b.build().entries]
        assert "implicit SRI strip" in names

    def test_an_unrewritten_document_is_left_entirely_alone(self) -> None:
        """The implicit strip only applies to a document we already changed."""
        page = '<html><head><script integrity="sha384-x"></script></head><body>hi</body></html>'
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "absent", "replace": "x"},
                }
            ]
        )
        decision = ev.evaluate_response_body(req(), self.html(page), b, None)
        assert decision.mutation.body is None

    def test_injection_reuses_the_pages_nonce_end_to_end(self) -> None:
        """REQ PXY-041 — leaving the page's own protections intact."""
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "inject",
                    "action": "body",
                    "transform": {"kind": "inject_script", "inline": "console.log(1)"},
                }
            ]
        )
        response = resp(
            headers=(
                ("content-type", "text/html"),
                ("content-security-policy", "script-src 'nonce-xyz789'"),
            ),
            body=b"<html><head></head><body></body></html>",
        )
        decision = ev.evaluate_response_body(req(), response, b, None)
        assert decision.mutation.body is not None
        assert b'nonce="xyz789"' in decision.mutation.body
        assert b.build().has_note(NoteCode.SCRIPT_INJECTED)

    def test_several_transforms_apply_in_order(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transforms": [
                        {"kind": "replace_literal", "find": "hi", "replace": "mid"},
                        {"kind": "replace_literal", "find": "mid", "replace": "end"},
                    ],
                }
            ]
        )
        decision = ev.evaluate_response_body(req(), self.html(), b, None)
        assert decision.mutation.body is not None
        assert b"end" in decision.mutation.body

    def test_the_charset_is_honoured_when_re_encoding(self) -> None:
        b = builder()
        ev = evaluator(
            [
                {
                    "name": "t",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "hi", "replace": "café"},
                }
            ]
        )
        response = resp(
            headers=(("content-type", "text/html; charset=latin-1"),),
            body="<html><body>hi</body></html>".encode("latin-1"),
        )
        decision = ev.evaluate_response_body(req(), response, b, None)
        assert decision.mutation.body is not None
        assert "café" in decision.mutation.body.decode("latin-1")
