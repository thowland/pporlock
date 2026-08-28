"""Rule suggestion from a flow — REQ WUI-008, MCP-014."""

from __future__ import annotations

import pytest
import yaml

from pporlock.capture.records import FlowRecord
from pporlock.capture.suggest import INTENTS, suggest_rule
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.ruleset import RuleSet
from pporlock.errors import ConfigError


def record(
    path: str = "/static/analytics.js", method: str = "GET", dest: str | None = "script"
) -> FlowRecord:
    return FlowRecord(
        flow_id="f0",
        kind="http",
        started_at="2026-08-27T14:00:00.000Z",
        request=NormalizedRequest(
            flow_id="f0",
            timestamp="2026-08-27T14:00:00.000Z",
            scheme="https",
            method=method,
            host="cdn.example.com",
            port=443,
            path=path,
            url=f"https://cdn.example.com{path}",
            dest=dest,
        ),
        response=NormalizedResponse(flow_id="f0", timestamp="2026-08-27T14:00:01.000Z", status=200),
    )


class TestSuggestRule:
    @pytest.mark.parametrize("intent", INTENTS)
    def test_every_suggestion_compiles(self, intent: str) -> None:  # REQ WUI-008
        """A suggestion that would not load is worse than none: the author
        would spend their time debugging our text rather than their intent."""
        payload = suggest_rule(record(), intent)
        compiled = RuleSet.from_rules([payload["rule"]], module="suggested")
        assert len(compiled) == 1

    def test_the_yaml_round_trips_to_the_same_rule(self) -> None:
        payload = suggest_rule(record(), "block")
        assert yaml.safe_load(payload["yaml"]) == [payload["rule"]]

    def test_the_match_is_anchored_to_the_exact_path(self) -> None:
        """A rule suggested from /a.js must not also catch /a.js.map."""
        payload = suggest_rule(record("/a.js"), "block")
        pattern = payload["rule"]["match"]["path"]
        assert pattern.startswith("^") and pattern.endswith("$")
        import re

        assert re.match(pattern, "/a.js")
        assert not re.match(pattern, "/a.js.map")

    def test_a_non_get_carries_the_method(self) -> None:
        payload = suggest_rule(record(method="POST"), "block")
        assert payload["rule"]["match"]["method"] == "POST"

    def test_a_get_does_not_carry_a_redundant_method(self) -> None:
        assert "method" not in suggest_rule(record(), "block")["rule"]["match"]

    def test_a_flow_with_no_dest_omits_it(self) -> None:
        assert "dest" not in suggest_rule(record(dest=None), "block")["rule"]["match"]

    def test_block_defaults_to_a_synthesised_stub(self) -> None:  # REQ MOD-016
        rule = suggest_rule(record(), "block")["rule"]
        assert rule["mode"] == "stub"
        assert rule["stub"] == "auto"

    def test_map_local_proposes_an_asset_filename(self) -> None:
        assert suggest_rule(record("/static/app.js"), "map_local")["rule"]["file"] == "app.js"

    def test_map_local_handles_a_pathless_request(self) -> None:
        assert suggest_rule(record("/"), "map_local")["rule"]["file"]

    def test_an_unknown_intent_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            suggest_rule(record(), "teleport")

    def test_a_flow_with_no_request_is_refused(self) -> None:
        empty = FlowRecord(flow_id="p0", kind="passthrough", started_at="2026-08-27T14:00:00.000Z")
        with pytest.raises(ConfigError):
            suggest_rule(empty, "block")
