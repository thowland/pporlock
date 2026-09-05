"""Regression tests for the 5 September 2026 review — docs/SEP_5_REVIEW_FINDINGS.md.

One class per finding, named for it, because each of these was reported as a
defect that the whole existing suite could not see. Every test in this file
failed against `c38a9a5` and describes the *composed* behaviour the review said
was uncovered: a decision made in one phase and consumed in another, a bound
enforced at insertion but not at growth, a snapshot swapped underneath a flow.

They live together rather than being scattered into the topical files so that
the finding, its demonstration, and its fix stay legible as one thing. New tests
of the same *helpers* belong in the topical file; new tests of a *handoff*
belong here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pporlock.addon import apply as apply_mod
from pporlock.engine.evaluator import Evaluator
from pporlock.engine.models import NormalizedRequest, NormalizedResponse, RequestMutation
from pporlock.engine.provenance import Outcome, ProvenanceBuilder
from pporlock.engine.ruleset import RuleSet

from ..stubs import StubFlow, StubHeaders, StubRequest, StubResponse


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


def header_names(headers: StubHeaders) -> list[str]:
    return [k.decode().lower() for k, _ in headers.fields]


# --------------------------------------------------------------------- F-02 ---


class TestF02HeaderOrderSurvivesApplication:
    """Declaration order must decide the wire result, not operation type.

    REQ MOD-012, PXY-020, PXY-036. The evaluator visited rules in the right
    order and then folded them into three per-operation containers, which the
    adapter applied remove-then-set-then-add. Both declaration orders of an
    add and a remove therefore produced the same header — while provenance
    correctly reported two rules applied in the order written.
    """

    @staticmethod
    def apply_request(
        rules: list[dict[str, Any]], existing: list[tuple[str, str]] | None = None
    ) -> Any:
        flow = StubFlow(
            StubRequest(
                headers=StubHeaders([(k.encode(), v.encode()) for k, v in (existing or [])])
            )
        )
        decision = evaluator(rules).evaluate_request(req(), ProvenanceBuilder("default"))
        apply_mod.apply_request_mutation(flow, decision.mutation)
        return flow.request.headers

    def test_add_then_remove_leaves_the_header_gone(self) -> None:
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"add": {"x-review": "present"}}},
                {"name": "two", "action": "headers", "request": {"remove": ["x-review"]}},
            ]
        )
        assert "x-review" not in header_names(headers)

    def test_remove_then_add_leaves_the_header_present(self) -> None:
        """The mirror case. If both orders agree, order is not being honoured."""
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"remove": ["x-review"]}},
                {"name": "two", "action": "headers", "request": {"add": {"x-review": "present"}}},
            ],
            existing=[("x-review", "stale")],
        )
        assert headers.get("x-review") == "present"

    def test_set_then_remove_leaves_the_header_gone(self) -> None:
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"set": {"x-review": "v"}}},
                {"name": "two", "action": "headers", "request": {"remove": ["x-review"]}},
            ]
        )
        assert "x-review" not in header_names(headers)

    def test_remove_then_set_leaves_the_set_value(self) -> None:
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"remove": ["x-review"]}},
                {"name": "two", "action": "headers", "request": {"set": {"x-review": "v"}}},
            ],
            existing=[("x-review", "stale")],
        )
        assert headers.get("x-review") == "v"

    def test_add_then_set_collapses_to_the_set_value(self) -> None:
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"add": {"x-review": "a"}}},
                {"name": "two", "action": "headers", "request": {"set": {"x-review": "b"}}},
            ]
        )
        assert headers.get("x-review") == "b"
        assert header_names(headers).count("x-review") == 1

    def test_set_then_add_keeps_both_values(self) -> None:
        headers = self.apply_request(
            [
                {"name": "one", "action": "headers", "request": {"set": {"x-review": "a"}}},
                {"name": "two", "action": "headers", "request": {"add": {"x-review": "b"}}},
            ]
        )
        assert header_names(headers).count("x-review") == 2

    def test_module_priority_decides_between_conflicting_operations(self) -> None:
        """Priority orders rules across modules; it must order these too."""
        low = RuleSet.from_rules(
            [{"name": "add", "action": "headers", "request": {"add": {"x-review": "1"}}}],
            module="low",
            priority=10,
        )
        high = RuleSet.from_rules(
            [{"name": "strip", "action": "headers", "request": {"remove": ["x-review"]}}],
            module="high",
            priority=90,
        )
        flow = StubFlow(StubRequest(headers=StubHeaders()))
        decision = Evaluator(RuleSet.combine(low, high)).evaluate_request(
            req(), ProvenanceBuilder("default")
        )
        apply_mod.apply_request_mutation(flow, decision.mutation)
        assert "x-review" not in header_names(flow.request.headers)

    def test_a_hooks_operations_keep_their_order_when_merged(self) -> None:
        """A module returning a mutation is subject to the same promise."""
        mutation = RequestMutation()
        mutation.add("x-review", "present")
        mutation.remove("x-review")
        flow = StubFlow(StubRequest(headers=StubHeaders()))
        apply_mod.apply_request_mutation(flow, mutation)
        assert "x-review" not in header_names(flow.request.headers)


# --------------------------------------------------------------------- F-12 ---


class TestF12ProvenanceReportsWhatActuallyHappened:
    """`applied` must mean the wire changed. REQ CAP-010/012/013.

    The capture layer derives a flow's "was modified" state from applied
    provenance, so a no-op recorded as applied puts an untouched flow in the
    UI's modified filter — and a failed map_local named a short circuit that
    never happened.
    """

    def test_removing_an_absent_header_records_no_change(self) -> None:
        b = ProvenanceBuilder("default")
        ev = evaluator([{"name": "h", "action": "headers", "request": {"remove": ["x-absent"]}}])
        ev.evaluate_request(req(headers=()), b)
        assert b.build().entries[0].outcome is Outcome.NO_CHANGE

    def test_setting_a_header_to_the_value_it_already_has_records_no_change(self) -> None:
        b = ProvenanceBuilder("default")
        ev = evaluator([{"name": "h", "action": "headers", "request": {"set": {"accept": "*/*"}}}])
        ev.evaluate_request(req(headers=(("accept", "*/*"),)), b)
        assert b.build().entries[0].outcome is Outcome.NO_CHANGE

    def test_removing_a_header_that_is_present_still_records_applied(self) -> None:
        b = ProvenanceBuilder("default")
        ev = evaluator([{"name": "h", "action": "headers", "request": {"remove": ["cookie"]}}])
        ev.evaluate_request(req(headers=(("cookie", "a=1"),)), b)
        assert b.build().entries[0].outcome is Outcome.APPLIED

    def test_an_add_is_always_a_change(self) -> None:
        b = ProvenanceBuilder("default")
        ev = evaluator([{"name": "h", "action": "headers", "request": {"add": {"x": "1"}}}])
        ev.evaluate_request(req(headers=(("x", "1"),)), b)
        assert b.build().entries[0].outcome is Outcome.APPLIED

    def test_a_response_header_no_op_records_no_change(self) -> None:
        b = ProvenanceBuilder("default")
        ev = evaluator([{"name": "h", "action": "headers", "response": {"remove": ["x-absent"]}}])
        ev.evaluate_response_headers(req(), resp(), b)
        assert b.build().entries[0].outcome is Outcome.NO_CHANGE

    def test_a_failed_map_local_does_not_claim_the_flow_was_short_circuited(
        self, tmp_path: Path
    ) -> None:
        """REQ PXY-034. The rule matched, the file was missing, the request went
        upstream — provenance must not name a short circuit that never was."""
        b = ProvenanceBuilder("default")
        ev = evaluator(
            [{"name": "m", "action": "map_local", "file": "nope.js"}], asset_root=tmp_path
        )
        decision = ev.evaluate_request(req(), b)
        assert decision.short_circuit is None
        assert b.build().short_circuited_by is None

    def test_a_successful_map_local_does_name_the_rule(self, tmp_path: Path) -> None:
        (tmp_path / "there.js").write_text("ok")
        b = ProvenanceBuilder("default")
        ev = evaluator(
            [{"name": "m", "action": "map_local", "file": "there.js"}], asset_root=tmp_path
        )
        ev.evaluate_request(req(), b)
        assert b.build().short_circuited_by == "m:0"


# --------------------------------------------------------------------- F-09 ---


class TestF09TwoSidedHeaderRules:
    """One declared rule is one rule. REQ MOD-011, MOD-012, PXY-020."""

    def test_a_two_sided_rule_is_counted_once(self) -> None:
        ruleset = RuleSet.from_rules(
            [
                {
                    "name": "both",
                    "action": "headers",
                    "request": {"add": {"x": "1"}},
                    "response": {"remove": ["y"]},
                }
            ],
            module="m",
        )
        assert len(ruleset) == 1
        assert len(ruleset.all_rules) == 1

    def test_response_only_criteria_are_refused_on_a_rule_that_mutates_the_request(
        self,
    ) -> None:
        """REQ MOD-011. The request half cannot see the response, so a `status`
        criterion would let the request mutation fire regardless of it —
        silently, and against what the rule says."""
        from pporlock.errors import RuleValidationError

        with pytest.raises(RuleValidationError):
            RuleSet.from_rules(
                [
                    {
                        "name": "both",
                        "action": "headers",
                        "match": {"status": 500},
                        "request": {"add": {"x": "1"}},
                        "response": {"remove": ["y"]},
                    }
                ],
                module="m",
            )

    def test_a_response_only_rule_may_still_match_on_status(self) -> None:
        ruleset = RuleSet.from_rules(
            [
                {
                    "name": "resp",
                    "action": "headers",
                    "match": {"status": 500},
                    "response": {"remove": ["y"]},
                }
            ],
            module="m",
        )
        assert len(ruleset) == 1
        assert not ruleset.matching_response_headers(req(), resp(status=200))
        assert ruleset.matching_response_headers(req(), resp(status=500))


# --------------------------------------------------------------------- F-14 ---


class TestF14BodyDemandMeansTheBodyIsNeeded:
    """REQ PXY-021, PRF-004. `strip_csp` is declared as a body transform and
    applied to headers; a rule whose only transform is `strip_csp` must not
    make an HTML response eligible for buffering."""

    def test_strip_csp_alone_does_not_want_the_body(self) -> None:
        ruleset = RuleSet.from_rules(
            [{"name": "csp", "action": "body", "transform": {"kind": "strip_csp"}}], module="m"
        )
        assert not ruleset.wants_body(req())

    def test_strip_csp_beside_a_real_transform_still_wants_the_body(self) -> None:
        ruleset = RuleSet.from_rules(
            [
                {
                    "name": "both",
                    "action": "body",
                    "transforms": [
                        {"kind": "strip_csp"},
                        {"kind": "replace_literal", "find": "a", "replace": "b"},
                    ],
                }
            ],
            module="m",
        )
        assert ruleset.wants_body(req())

    def test_a_strip_csp_only_flow_is_streamed(self) -> None:
        ev = evaluator([{"name": "csp", "action": "body", "transform": {"kind": "strip_csp"}}])
        b = ProvenanceBuilder("default")
        decision = ev.decide_buffering(req(), "text/html", 1000, ev.wants_body(req()), b)
        assert not decision.buffer
        assert decision.reason == "no_transform"

    def test_strip_csp_still_removes_the_header_on_a_streamed_response(self) -> None:
        """The point of the finding is the buffering, not the transform: a
        streamed response must still lose its CSP."""
        ev = evaluator([{"name": "csp", "action": "body", "transform": {"kind": "strip_csp"}}])
        b = ProvenanceBuilder("default")
        streamed = resp(
            headers=(
                ("content-type", "text/html"),
                ("content-security-policy", "default-src 'self'"),
            ),
            body=None,
            streamed=True,
        )
        decision = ev.evaluate_response_headers(req(), streamed, b)
        assert "content-security-policy" in decision.mutation.remove_headers


# --------------------------------------------------------------------- F-07 ---


class TestF07TransformsValidateAtLoadTime:
    """REQ MOD-014/015. An unknown transform kind is a load error, not a
    surprise on the first matching request."""

    def test_an_unknown_transform_kind_is_refused_at_compile_time(self) -> None:
        from pporlock.engine.transforms import build_registry
        from pporlock.errors import RuleValidationError

        with pytest.raises(RuleValidationError, match="definitely_unknown"):
            RuleSet.from_rules(
                [
                    {
                        "name": "b",
                        "action": "body",
                        "transform": {"kind": "definitely_unknown", "bogus": 1},
                    }
                ],
                module="m",
                transforms=build_registry(),
            )

    def test_an_unknown_parameter_is_refused_at_compile_time(self) -> None:
        from pporlock.engine.transforms import build_registry
        from pporlock.errors import RuleValidationError

        with pytest.raises(RuleValidationError, match="bogus"):
            RuleSet.from_rules(
                [
                    {
                        "name": "b",
                        "action": "body",
                        "transform": {
                            "kind": "replace_literal",
                            "find": "a",
                            "replace": "b",
                            "bogus": 1,
                        },
                    }
                ],
                module="m",
                transforms=build_registry(),
            )

    def test_an_invalid_regex_is_refused_at_compile_time(self) -> None:
        from pporlock.engine.transforms import build_registry
        from pporlock.errors import RuleValidationError

        with pytest.raises(RuleValidationError):
            RuleSet.from_rules(
                [
                    {
                        "name": "b",
                        "action": "body",
                        "transform": {"kind": "regex_sub", "pattern": "(unclosed", "repl": "x"},
                    }
                ],
                module="m",
                transforms=build_registry(),
            )

    def test_a_valid_rule_still_compiles(self) -> None:
        from pporlock.engine.transforms import build_registry

        ruleset = RuleSet.from_rules(
            [
                {
                    "name": "b",
                    "action": "body",
                    "transform": {"kind": "regex_sub", "pattern": "a+", "repl": "x", "flags": "i"},
                }
            ],
            module="m",
            transforms=build_registry(),
        )
        assert len(ruleset) == 1

    def test_compilation_without_a_registry_still_works(self) -> None:
        """The registry is not always available at compile time — a module's own
        `on_load` may register a transform its rules use. Validation is a
        separate, explicitly ordered pass for that case."""
        assert (
            len(
                RuleSet.from_rules(
                    [{"name": "b", "action": "body", "transform": {"kind": "custom"}}], module="m"
                )
            )
            == 1
        )


# --------------------------------------------------------------------- F-08 ---


class TestF08RegexIsCompiledOnce:
    """REQ PXY-025, PRF-004. A rule's regex is compiled at load, not per flow."""

    def test_the_same_pattern_is_not_recompiled_per_call(self) -> None:
        from pporlock.engine.transforms.text import _compiled, regex_sub

        _compiled.cache_clear()
        params = {"kind": "regex_sub", "pattern": "a+", "repl": "x", "flags": ""}

        class P:
            @staticmethod
            def get(name: str, default: Any = None) -> Any:
                return params.get(name, default)

        for _ in range(5):
            regex_sub("aaa bbb aaa", P())
        assert _compiled.cache_info().misses == 1
        assert _compiled.cache_info().hits == 4


# --------------------------------------------------------------------- F-01 ---

HOOK_ONLY_PY = (
    "from pporlock.engine.models import ResponseMutation\n"
    "def on_response(req, resp, ctx):\n"
    "    if resp.body is None:\n"
    "        ctx.note('module_error', 'no body reached the hook', severity='error')\n"
    "        return None\n"
    "    return ResponseMutation(body=resp.body + b'<!--hooked-->')\n"
)


def hook_only_module(root: Path, name: str = "hooker", python: str = HOOK_ONLY_PY) -> Any:
    """A module that is *only* a Python hook — no declarative rules at all.

    The layout `docs/tutorial-python-module.md` presents as a supported tier.
    """
    import yaml

    from pporlock.engine.modules.context import MODULE_API_VERSION
    from pporlock.engine.modules.registry import ModuleRegistry

    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "module.yaml").write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0.0",
                "pporlock_api": MODULE_API_VERSION,
                "enabled": True,
                "rules": [],
            },
            sort_keys=False,
        )
    )
    (path / "module.py").write_text(python)
    registry = ModuleRegistry(root, state_path=root / "state.json")
    registry.reload()
    return registry


class TestF01PythonHooksParticipateInBufferingAndOffload:
    """REQ PXY-021/024/026, MOD-020/023, PRF-004/006.

    A hook-only module was invisible to every decision made before the body
    arrived: no body was demanded, the response streamed, and the hook was
    handed `body=None`. It ran, could do nothing, and nothing said so.
    """

    def test_a_hook_only_module_demands_the_body(self, tmp_path: Path) -> None:
        registry = hook_only_module(tmp_path)
        ev = Evaluator(RuleSet(modules=("hooker",)), registry=registry)
        assert ev.wants_body(req())

    def test_a_hook_only_flow_is_buffered_rather_than_streamed(self, tmp_path: Path) -> None:
        registry = hook_only_module(tmp_path)
        ev = Evaluator(RuleSet(modules=("hooker",)), registry=registry)
        b = ProvenanceBuilder("default")
        decision = ev.decide_buffering(req(), "text/html", 1000, ev.wants_body(req()), b)
        assert decision.buffer

    def test_the_hook_then_actually_receives_a_body(self, tmp_path: Path) -> None:
        registry = hook_only_module(tmp_path)
        ev = Evaluator(RuleSet(modules=("hooker",)), registry=registry)
        b = ProvenanceBuilder("default")
        decision = ev.evaluate_response_body(req(), resp(), b, None)
        assert decision.mutation.body == b"<html></html><!--hooked-->"

    def test_no_module_means_no_body_demand(self, tmp_path: Path) -> None:
        """The optimisation that makes the common case cheap must survive."""
        assert not Evaluator(RuleSet()).wants_body(req())

    def test_a_large_body_with_only_a_hook_is_offloaded(self, tmp_path: Path) -> None:
        registry = hook_only_module(tmp_path)
        ev = Evaluator(RuleSet(modules=("hooker",)), registry=registry, offload_threshold=1024)
        assert ev.should_offload(req(), resp(body=b"x" * 4096))

    def test_a_small_body_with_only_a_hook_stays_inline(self, tmp_path: Path) -> None:
        registry = hook_only_module(tmp_path)
        ev = Evaluator(RuleSet(modules=("hooker",)), registry=registry, offload_threshold=1024)
        assert not ev.should_offload(req(), resp(body=b"x" * 10))

    def test_a_module_with_no_response_hook_does_not_demand_a_body(self, tmp_path: Path) -> None:
        registry = hook_only_module(
            tmp_path, name="reqonly", python="def on_request(req, ctx):\n    return None\n"
        )
        ev = Evaluator(RuleSet(modules=("reqonly",)), registry=registry)
        assert not ev.wants_body(req())

    def test_the_budget_stops_hooks_rather_than_only_measuring_them(self, tmp_path: Path) -> None:
        """REQ PXY-026, PRF-006. The request phase used to be charged once,
        after every hook had run, so the budget could not stop anything."""
        from pporlock.engine.evaluator import TimeBudget
        from pporlock.engine.provenance import Outcome

        registry = hook_only_module(
            tmp_path,
            name="slow",
            python=(
                "import time\ndef on_request(req, ctx):\n    time.sleep(0.02)\n    return None\n"
            ),
        )
        ev = Evaluator(RuleSet(modules=("slow",)), registry=registry)
        budget = TimeBudget(total_ms=1.0)
        b = ProvenanceBuilder("default")
        ev.evaluate_request(req(), b, budget)
        assert budget.spent > 0

        # A second flow on an already-exhausted budget must not run the hook.
        spent = TimeBudget(total_ms=0.0)
        second = ProvenanceBuilder("default")
        ev.evaluate_request(req(), second, spent)
        assert any(e.outcome is Outcome.SKIPPED_BUDGET for e in second.build().entries)


# --------------------------------------------------------------------- F-06 ---


class TestF06ObservedSizeBoundsTheBody:
    """REQ PXY-021/022, PRF-004/005. A chunked response declares no length, so
    the declared-size check alone bounded nothing."""

    def test_a_body_that_arrives_over_the_cap_is_treated_as_streamed(self) -> None:
        ev = evaluator(max_buffer_bytes=100)
        b = ProvenanceBuilder("default")
        assert ev.enforce_observed_size(resp(body=b"x" * 500), b)
        note = b.build().notes[-1]
        assert note.detail["reason"] == "observed_size"
        assert note.detail["observed_bytes"] == 500

    def test_a_body_within_the_cap_is_left_alone(self) -> None:
        ev = evaluator(max_buffer_bytes=1000)
        assert not ev.enforce_observed_size(resp(body=b"x" * 500), ProvenanceBuilder("default"))

    def test_an_already_streamed_response_is_not_re_reported(self) -> None:
        ev = evaluator(max_buffer_bytes=10)
        streamed = resp(body=None, streamed=True)
        assert not ev.enforce_observed_size(streamed, ProvenanceBuilder("default"))

    def test_the_buffering_decision_records_that_no_length_was_declared(self) -> None:
        """So a flow that later exceeds the cap has both halves of the story."""
        ev = evaluator(
            [
                {
                    "name": "b",
                    "action": "body",
                    "transform": {"kind": "replace_literal", "find": "a", "replace": "b"},
                }
            ]
        )
        b = ProvenanceBuilder("default")
        ev.decide_buffering(req(), "text/html", None, True, b)
        entry = next(e for e in b.build().entries if e.detail.get("buffered"))
        assert entry.detail["declared_length"] is None


# --------------------------------------------------------------------- F-03 ---


class TestF03AFlowKeepsTheGenerationItStartedOn:
    """REQ MOD-004. An atomic swap of the global evaluator is not enough unless
    the flow retains the object it was assigned."""

    @staticmethod
    def interceptor(rules: list[dict[str, Any]]) -> Any:
        from pporlock.addon.interceptor import Interceptor

        return Interceptor(evaluator=evaluator(rules))

    def test_the_response_phase_uses_the_request_phases_evaluator(self) -> None:
        from pporlock.addon.interceptor import Interceptor

        generation_a = evaluator(
            [{"name": "a", "action": "headers", "response": {"add": {"x-gen": "a"}}}]
        )
        interceptor = Interceptor(evaluator=generation_a)

        flow = StubFlow(StubRequest(headers=StubHeaders()))
        interceptor.request(flow)

        # A reload lands between request and responseheaders.
        interceptor.replace_ruleset(
            RuleSet.from_rules(
                [{"name": "b", "action": "headers", "response": {"add": {"x-gen": "b"}}}],
                module="m",
            )
        )
        assert interceptor.evaluator is not generation_a

        flow.response = StubResponse(headers=StubHeaders())
        interceptor.responseheaders(flow)
        assert flow.response.headers.get("x-gen") == "a"

    def test_a_flow_started_after_the_reload_uses_the_new_generation(self) -> None:
        from pporlock.addon.interceptor import Interceptor

        interceptor = Interceptor(
            evaluator=evaluator(
                [{"name": "a", "action": "headers", "response": {"add": {"x-gen": "a"}}}]
            )
        )
        interceptor.replace_ruleset(
            RuleSet.from_rules(
                [{"name": "b", "action": "headers", "response": {"add": {"x-gen": "b"}}}],
                module="m",
            )
        )
        flow = StubFlow(StubRequest(headers=StubHeaders()))
        interceptor.request(flow)
        flow.response = StubResponse(headers=StubHeaders())
        interceptor.responseheaders(flow)
        assert flow.response.headers.get("x-gen") == "b"

    def test_a_flow_that_never_saw_request_falls_back_to_the_current_one(self) -> None:
        """A response synthesised by another addon, or a phase driven alone."""
        from pporlock.addon.interceptor import Interceptor

        interceptor = Interceptor(
            evaluator=evaluator(
                [{"name": "a", "action": "headers", "response": {"add": {"x-gen": "a"}}}]
            )
        )
        flow = StubFlow(StubRequest(headers=StubHeaders()))
        flow.response = StubResponse(headers=StubHeaders())
        flow.metadata["pporlock.request"] = req()
        flow.metadata["pporlock.builder"] = ProvenanceBuilder("default")
        interceptor.responseheaders(flow)
        assert flow.response.headers.get("x-gen") == "a"


# --------------------------------------------------------------------- F-11 ---


class TestF11ExecutorConfigurationReachesRuntime:
    """REQ PXY-024, PRF-004. Both settings are documented and published; both
    were ignored by the daemon that reads them."""

    def test_the_configured_threshold_reaches_the_evaluator(self, tmp_path: Path) -> None:
        from pporlock.cli.runner import build_evaluator
        from pporlock.config import Config

        config = Config()
        config.state_dir = str(tmp_path)
        config.modules.root = str(tmp_path / "modules")
        config.budget.executor_threshold_bytes = 4321
        built, *_ = build_evaluator(config)
        assert built.offload_threshold == 4321

    def test_the_interceptor_owns_a_pool_sized_from_configuration(self) -> None:
        from pporlock.addon.interceptor import Interceptor
        from pporlock.config import Config

        config = Config()
        config.budget.executor_workers = 3
        interceptor = Interceptor(config)
        assert interceptor.executor._max_workers == 3
        interceptor.done()

    def test_body_work_does_not_run_on_the_default_executor(self) -> None:
        """Control-plane filesystem work uses asyncio's default pool. Sharing it
        meant an expensive transform could delay a reload, and the reverse."""
        from pporlock.addon.interceptor import Interceptor
        from pporlock.config import Config

        interceptor = Interceptor(Config())
        assert interceptor.executor is not None
        interceptor.done()


# --------------------------------------------------------------------- F-04 ---


class TestF04ReloadPublishesAReplacement:
    """REQ MOD-004, MOD-024, DD-3. Reload used to empty the live registry and
    refill it one module at a time, on a worker thread, while traffic ran
    against the same object on the event loop."""

    @staticmethod
    def write(root: Path, name: str, python: str | None = None) -> None:
        import yaml

        from pporlock.engine.modules.context import MODULE_API_VERSION

        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "module.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": name,
                    "version": "1.0.0",
                    "pporlock_api": MODULE_API_VERSION,
                    "enabled": True,
                },
                sort_keys=False,
            )
        )
        if python is not None:
            (path / "module.py").write_text(python)

    def test_a_reader_never_sees_a_partially_loaded_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observation is taken *during* the load loop — the window the old
        code left open, when `_modules` had been emptied and was being refilled
        one module at a time."""
        from pporlock.engine.modules import registry as registry_mod
        from pporlock.engine.modules.registry import ModuleRegistry

        for name in ("alpha", "beta", "gamma"):
            self.write(tmp_path, name)
        registry = ModuleRegistry(tmp_path, state_path=tmp_path / "state.json")
        registry.reload()
        assert len(registry.modules) == 3

        seen: list[int] = []
        real_load_all = registry_mod.load_all

        def observing(root: Path) -> Any:
            for module in real_load_all(root):
                # What a flow on the event loop would find, mid-reload.
                seen.append(len(registry.modules))
                yield module

        monkeypatch.setattr(registry_mod, "load_all", observing)
        registry.reload()

        assert len(seen) == 3
        assert all(count == 3 for count in seen), seen

    def test_the_module_set_and_the_contexts_move_together(self, tmp_path: Path) -> None:
        """A context without its module, or the reverse, is what a two-step
        rebuild in place produced."""
        from pporlock.engine.modules.registry import ModuleRegistry

        for name in ("alpha", "beta"):
            self.write(tmp_path, name, python="def on_load(ctx):\n    pass\n")
        registry = ModuleRegistry(tmp_path, state_path=tmp_path / "state.json")
        registry.reload()
        for module in registry.modules:
            assert registry.context(module.name) is not None

    def test_a_reload_flushes_queued_module_store_writes(self, tmp_path: Path) -> None:
        """Persistence is queued (F-13); a reload is a point at which it must
        have happened."""
        from pporlock.engine.modules.context import ModuleStore
        from pporlock.engine.modules.registry import ModuleRegistry

        self.write(tmp_path, "alpha", python="def on_load(ctx):\n    ctx.store_set('k', 7)\n")
        registry = ModuleRegistry(
            tmp_path, state_path=tmp_path / "state.json", store_path=tmp_path / "store.db"
        )
        registry.reload()
        registry.reload()
        assert ModuleStore(tmp_path / "store.db", "alpha").get("k") == 7


# --------------------------------------------------------------------- F-05 ---


class TestF05WebSocketGrowthIsAccounted:
    """REQ PXY-050, CAP-001/003, PRF-005. Frames were appended straight onto a
    record already in the ring, so the byte bound never saw them."""

    @staticmethod
    def sink(**kwargs: Any) -> Any:
        from pporlock.capture.ring import RingBuffer
        from pporlock.capture.sink import RingSink

        ring = RingBuffer(max_flows=10, max_bytes=kwargs.pop("max_bytes", 1_000_000))
        return RingSink(ring, **kwargs), ring

    @staticmethod
    def frame(index: int, size: int = 100, flow_id: str = "ws1") -> Any:
        from pporlock.engine.models import WebSocketMessage

        return WebSocketMessage(
            flow_id=flow_id,
            index=index,
            timestamp="t",
            direction="inbound",
            opcode="text",
            payload=b"x" * size,
        )

    def test_appending_frames_moves_the_ring_byte_count(self) -> None:
        sink, ring = self.sink()
        sink.record_websocket_message(self.frame(0))
        before = ring.stats.bytes
        for i in range(1, 10):
            sink.record_websocket_message(self.frame(i))
        assert ring.stats.bytes > before
        assert ring.stats.bytes == ring.get("ws1").size_bytes

    def test_statistics_match_what_is_actually_retained(self) -> None:
        sink, ring = self.sink()
        for i in range(20):
            sink.record_websocket_message(self.frame(i, size=250))
        assert ring.stats.bytes == sum(r.size_bytes for r in [ring.get("ws1")])

    def test_one_socket_cannot_grow_past_its_retention_bound(self) -> None:
        """The explicit policy the review asked for: newest frames win, and the
        record says how many it dropped."""
        sink, ring = self.sink(max_ws_bytes=1000)
        for i in range(100):
            sink.record_websocket_message(self.frame(i, size=100))
        record = ring.get("ws1")
        assert sum(len(m.payload) for m in record.ws_messages) <= 1000
        assert record.ws_dropped > 0
        # Newest kept, not oldest.
        assert record.ws_messages[-1].index == 99

    def test_growth_evicts_older_flows_when_it_crosses_the_cap(self) -> None:
        """The whole point of accounting: the bound has to act."""
        from pporlock.capture.records import FlowRecord

        sink, ring = self.sink(max_bytes=3000, max_ws_bytes=100_000)
        ring.add(FlowRecord(flow_id="old", kind="http", started_at="t", completed_at="t"))
        for i in range(20):
            sink.record_websocket_message(self.frame(i, size=200))
        assert "old" not in ring
        assert ring.stats.evicted >= 1

    def test_a_quiet_socket_is_not_trimmed(self) -> None:
        sink, ring = self.sink(max_ws_bytes=100_000)
        for i in range(5):
            sink.record_websocket_message(self.frame(i))
        assert ring.get("ws1").ws_dropped == 0
        assert len(ring.get("ws1").ws_messages) == 5


# --------------------------------------------------------------------- F-10 ---


class TestF10NotesBelongToOneInvocation:
    """REQ MOD-024, CAP-010/012/013. One `ModuleContext` per loaded module meant
    two concurrent hook calls shared one notes list, and whichever drained first
    carried away both flows' notes."""

    def test_a_child_context_has_its_own_buffers(self) -> None:
        from pporlock.engine.modules.context import ModuleContext

        base = ModuleContext(name="m", version="1", config={"k": "v"})
        first = base.for_invocation()
        second = base.for_invocation()
        first.note("module_error", "mine")
        assert first.notes and not second.notes and not base.notes

    def test_a_child_context_shares_configuration_and_storage(self, tmp_path: Path) -> None:
        from pporlock.engine.modules.context import ModuleContext, ModuleStore

        store = ModuleStore(tmp_path / "s.db", "m")
        base = ModuleContext(name="m", version="1", config={"k": "v"}, store=store)
        child = base.for_invocation()
        child.store_set("a", 1)
        assert base.store_get("a") == 1
        assert child.config == {"k": "v"}
        assert child.name == "m"

    def test_concurrent_hooks_keep_their_own_notes(self, tmp_path: Path) -> None:
        """Two response hooks in the same module, held at a barrier so they
        genuinely overlap, must not exchange notes."""
        import threading

        registry = hook_only_module(
            tmp_path,
            name="noter",
            python=(
                "import threading, time\n"
                "BARRIER = threading.Barrier(2, timeout=5)\n"
                "def on_response(req, resp, ctx):\n"
                "    ctx.note('module_error', 'flow=' + req.flow_id, severity='info')\n"
                "    BARRIER.wait()\n"
                "    return None\n"
            ),
        )
        ev = Evaluator(RuleSet(modules=("noter",)), registry=registry)

        results: dict[str, list[str]] = {}

        def run(flow_id: str) -> None:
            b = ProvenanceBuilder("default")
            ev.evaluate_response_body(req(flow_id=flow_id), resp(), b, None)
            results[flow_id] = [n.message for n in b.build().notes]

        threads = [threading.Thread(target=run, args=(f"flow{i}",)) for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results["flow1"] == ["flow=flow1"]
        assert results["flow2"] == ["flow=flow2"]


# --------------------------------------------------------------------- F-13 ---


class TestF13StoreWritesLeaveTheCallingThread:
    """REQ MOD-022, DD-3. A request hook runs on the proxy event loop, so a
    synchronous SQLite write there stalls every other connection."""

    def test_a_write_returns_before_it_reaches_disk(self, tmp_path: Path) -> None:
        from pporlock.engine.modules.context import ModuleStore

        store = ModuleStore(tmp_path / "s.db", "m")
        store.set("k", 1)
        # Read-after-write holds immediately; durability is the writer's job.
        assert store.get("k") == 1
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "m").get("k") == 1

    def test_repeated_writes_to_one_key_coalesce(self, tmp_path: Path) -> None:
        """A module counting on every request must not cost a statement each."""
        from pporlock.engine.modules.context import ModuleStore

        store = ModuleStore(tmp_path / "s.db", "m")
        for i in range(100):
            store.set("count", i)
        assert len(store._pending) <= 1
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "m").get("count") == 99

    def test_a_write_does_not_block_on_slow_storage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property that matters: the caller returns whatever the disk does.

        Before this, that 300ms sat in front of every other connection the
        browser had open, because the caller is the proxy event loop.
        """
        import time

        from pporlock.engine.modules.context import ModuleStore

        store = ModuleStore(tmp_path / "s.db", "m")
        original = ModuleStore._connect

        def slow(self: ModuleStore) -> Any:
            time.sleep(0.3)
            return original(self)

        monkeypatch.setattr(ModuleStore, "_connect", slow)
        started = time.perf_counter()
        store.set("k", 1)
        assert (time.perf_counter() - started) < 0.1
        store.flush()

    def test_close_persists_what_is_outstanding(self, tmp_path: Path) -> None:
        from pporlock.engine.modules.context import ModuleStore

        store = ModuleStore(tmp_path / "s.db", "m")
        store.set("k", "v")
        store.close()
        assert ModuleStore(tmp_path / "s.db", "m").get("k") == "v"
