"""Rule sets and evaluation semantics. SPEC-1 §4.1, SPEC-0 §5.4.

The two semantics are the most error-prone part of the model, so they get the
most attention here.
"""

from __future__ import annotations

from typing import Any

import pytest

from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.provenance import Action, Phase
from pporlock.engine.ruleset import RuleSet, compile_rule
from pporlock.errors import RuleValidationError


def req(**kwargs: object) -> NormalizedRequest:
    base: dict[str, object] = {
        "flow_id": "f",
        "timestamp": "t",
        "scheme": "https",
        "method": "GET",
        "host": "cdn.example.com",
        "port": 443,
        "path": "/a.js",
        "url": "https://cdn.example.com/a.js",
        "dest": "script",
    }
    base.update(kwargs)
    return NormalizedRequest(**base)  # type: ignore[arg-type]


def resp(status: int = 200) -> NormalizedResponse:
    return NormalizedResponse(
        flow_id="f",
        timestamp="t",
        status=status,
        headers=(("content-type", "text/html"),),
    )


def block(name: str, host: str = "*") -> dict[str, Any]:
    return {"name": name, "action": "block", "match": {"host": host}, "stub": "auto"}


class TestCompilation:
    def test_a_rule_needs_a_name(self) -> None:
        with pytest.raises(RuleValidationError, match="no name"):
            compile_rule({"action": "block"}, module="m", index=0)

    def test_an_unknown_action_is_rejected_with_the_valid_list(self) -> None:
        with pytest.raises(RuleValidationError, match="unknown action"):
            compile_rule({"name": "r", "action": "obliterate"}, module="m", index=0)

    def test_rule_id_is_module_and_index(self) -> None:
        rule = compile_rule(block("r"), module="mymod", index=4)
        assert rule.rule_id == "mymod:4"

    def test_phase_follows_the_action(self) -> None:
        assert compile_rule(block("r"), module="m", index=0).phase is Phase.REQUEST_SHORT_CIRCUIT
        headers = compile_rule(
            {"name": "h", "action": "headers", "response": {"remove": ["x"]}},
            module="m",
            index=0,
        )
        assert headers.phase is Phase.RESPONSE_HEADERS

    def test_extra_keys_become_params(self) -> None:
        rule = compile_rule(
            {"name": "r", "action": "block", "stub": "gtm", "mode": "stub"},
            module="m",
            index=0,
        )
        assert rule.params == {"stub": "gtm", "mode": "stub"}

    @pytest.mark.parametrize(
        "raw,message",
        [
            ({"name": "r", "action": "block", "mode": "obliterate"}, "block mode"),
            ({"name": "r", "action": "map_local"}, "requires a 'file'"),
            ({"name": "r", "action": "redirect"}, "requires a 'to'"),
            ({"name": "r", "action": "redirect", "to": {}}, "requires a 'to'"),
            ({"name": "r", "action": "headers"}, "requires a 'request' or 'response'"),
            ({"name": "r", "action": "body"}, "requires a 'transform'"),
        ],
    )
    def test_action_parameters_are_validated_at_load(self, raw: dict, message: str) -> None:
        """REQ MOD-014 — never a runtime surprise."""
        with pytest.raises(RuleValidationError, match=message):
            compile_rule(raw, module="m", index=0)


class TestPartitioning:
    def test_rules_land_in_their_phase(self) -> None:
        rules = RuleSet.from_rules(
            [
                block("b"),
                {"name": "req-h", "action": "headers", "request": {"add": {"x": "1"}}},
                {"name": "res-h", "action": "headers", "response": {"remove": ["y"]}},
                {"name": "body", "action": "body", "transform": {"kind": "regex_sub"}},
            ]
        )
        assert [r.name for r in rules.short_circuit] == ["b"]
        assert [r.name for r in rules.request_headers] == ["req-h"]
        assert [r.name for r in rules.response_headers] == ["res-h"]
        assert [r.name for r in rules.response_body] == ["body"]

    def test_a_headers_rule_can_be_in_both_phases(self) -> None:
        rules = RuleSet.from_rules(
            [
                {
                    "name": "both",
                    "action": "headers",
                    "request": {"add": {"x": "1"}},
                    "response": {"remove": ["y"]},
                }
            ]
        )
        assert len(rules.request_headers) == 1
        assert len(rules.response_headers) == 1

    def test_disabled_rules_are_excluded_entirely(self) -> None:
        rules = RuleSet.from_rules([{**block("off"), "enabled": False}, block("on")])
        assert [r.name for r in rules.short_circuit] == ["on"]

    def test_len_counts_across_phases(self) -> None:
        assert len(RuleSet.from_rules([block("a"), block("b")])) == 2


class TestFirstMatchWins:
    """REQ MOD-012 — short-circuit actions stop at the first match."""

    def test_the_first_matching_rule_wins(self) -> None:
        rules = RuleSet.from_rules([block("first"), block("second")])
        assert rules.first_short_circuit(req()).name == "first"  # type: ignore[union-attr]

    def test_a_non_matching_rule_is_skipped(self) -> None:
        rules = RuleSet.from_rules([block("no", host="other.test"), block("yes")])
        assert rules.first_short_circuit(req()).name == "yes"  # type: ignore[union-attr]

    def test_none_when_nothing_matches(self) -> None:
        rules = RuleSet.from_rules([block("no", host="other.test")])
        assert rules.first_short_circuit(req()) is None

    def test_ordering_is_by_priority_then_declaration(self) -> None:
        from pporlock.engine.ruleset import compile_rule as compile

        low = compile(block("low"), module="low", index=0, priority=10)
        high = compile(block("high"), module="high", index=0, priority=200)
        rules = RuleSet([high, low])
        assert rules.first_short_circuit(req()).name == "low"  # type: ignore[union-attr]

    def test_declaration_order_breaks_a_priority_tie(self) -> None:
        rules = RuleSet.from_rules([block("first"), block("second")])
        assert [r.name for r in rules.short_circuit] == ["first", "second"]


class TestAllMatchesApply:
    """REQ MOD-012 — header and body actions all apply, in order."""

    def test_every_matching_header_rule_is_returned(self) -> None:
        rules = RuleSet.from_rules(
            [
                {"name": "one", "action": "headers", "response": {"remove": ["a"]}},
                {"name": "two", "action": "headers", "response": {"remove": ["b"]}},
            ]
        )
        matched = rules.matching_response_headers(req(), resp())
        assert [r.name for r in matched] == ["one", "two"]

    def test_non_matching_rules_are_excluded(self) -> None:
        rules = RuleSet.from_rules(
            [
                {
                    "name": "only-404",
                    "action": "headers",
                    "match": {"status": 404},
                    "response": {"remove": ["a"]},
                },
                {"name": "always", "action": "headers", "response": {"remove": ["b"]}},
            ]
        )
        assert [r.name for r in rules.matching_response_headers(req(), resp(200))] == ["always"]

    def test_request_header_rules(self) -> None:
        rules = RuleSet.from_rules(
            [
                {"name": "one", "action": "headers", "request": {"add": {"x": "1"}}},
            ]
        )
        assert len(rules.matching_request_headers(req())) == 1

    def test_body_rules(self) -> None:
        rules = RuleSet.from_rules(
            [
                {"name": "b", "action": "body", "transform": {"kind": "strip_csp"}},
            ]
        )
        assert len(rules.matching_response_body(req(), resp())) == 1


class TestWantsBody:
    """Feeds the buffering guard — the cheapest optimisation available."""

    def test_false_with_no_body_rules(self) -> None:
        assert not RuleSet.from_rules([block("b")]).wants_body(req())

    def test_true_when_a_body_rule_could_match(self) -> None:
        rules = RuleSet.from_rules(
            [
                {"name": "b", "action": "body", "transform": {"kind": "strip_csp"}},
            ]
        )
        assert rules.wants_body(req())

    def test_false_when_no_body_rule_matches_this_request(self) -> None:
        rules = RuleSet.from_rules(
            [
                {
                    "name": "b",
                    "action": "body",
                    "match": {"host": "other.test"},
                    "transform": {"kind": "strip_csp"},
                },
            ]
        )
        assert not rules.wants_body(req())

    def test_an_empty_set_wants_nothing(self) -> None:
        assert not RuleSet().wants_body(req())


class TestAllRules:
    def test_returns_every_phase(self) -> None:
        rules = RuleSet.from_rules(
            [
                block("b"),
                {"name": "h", "action": "headers", "response": {"remove": ["x"]}},
            ]
        )
        assert {r.name for r in rules.all_rules} == {"b", "h"}

    def test_short_circuit_actions_are_identified(self) -> None:
        for action, expected in [
            (Action.BLOCK, True),
            (Action.MAP_LOCAL, True),
            (Action.REDIRECT, True),
            (Action.HEADERS, False),
            (Action.BODY, False),
        ]:
            raw: dict[str, Any] = {"name": "r", "action": str(action)}
            if action is Action.MAP_LOCAL:
                raw["file"] = "x.js"
            elif action is Action.REDIRECT:
                raw["to"] = {"host": "h"}
            elif action is Action.HEADERS:
                raw["response"] = {"remove": ["x"]}
            elif action is Action.BODY:
                raw["transform"] = {"kind": "strip_csp"}
            assert compile_rule(raw, module="m", index=0).is_short_circuit is expected
