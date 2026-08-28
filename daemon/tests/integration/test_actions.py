"""The short-circuit and header actions, end to end through a real proxy.

Unit tests establish that the evaluator produces the right mutation; these
establish that the mutation reaches the browser — the part unit tests
structurally cannot cover.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from pporlock.engine.provenance import Action, NoteCode, Outcome

from .test_interception import ProxyHarness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def assets(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("assets")
    (directory / "local.js").write_text("window.__served_locally = true;\n")
    (directory / "data.json").write_text('{"local": true}')
    return directory


@pytest.fixture(scope="module")
def proxy(assets: Path, fixture_origin: Any) -> Any:
    harness = ProxyHarness(
        rules=[
            {
                "name": "map-local-js",
                "action": "map_local",
                "match": {"path": "^/mapped\\.js$"},
                "file": "local.js",
            },
            {
                "name": "map-local-typed",
                "action": "map_local",
                "match": {"path": "^/typed$"},
                "file": "data.json",
                "content_type": "application/json",
            },
            {
                "name": "map-local-missing",
                "action": "map_local",
                "match": {"path": "^/missing$"},
                "file": "not-there.js",
            },
            {
                "name": "redirect-to-fixture",
                "action": "redirect",
                "match": {"host": "redirect.example"},
                "to": {"host": "127.0.0.1", "port": fixture_origin.port, "path": "/health"},
            },
            {
                "name": "add-request-header",
                "action": "headers",
                "match": {"path": "^/dest/json$"},
                "request": {"add": {"x-pporlock-test": "added"}},
            },
            {
                "name": "rewrite-response-headers",
                "action": "headers",
                "match": {"path": "^/csp/nonce$"},
                "response": {
                    "remove": ["content-security-policy"],
                    "set": {"x-pporlock-rewritten": "yes"},
                },
            },
        ],
        asset_root=assets,
    ).start()
    try:
        yield harness
    finally:
        harness.stop()


def last(proxy: ProxyHarness) -> tuple[Any, Any, Any]:
    return proxy.sink.flows[-1]


class TestMapLocal:
    def test_serves_a_local_file_to_the_browser(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/mapped.js") as response:
            body = response.read()
        assert b"__served_locally" in body
        proxy.wait_for_flows(before + 1)

    def test_the_content_type_is_guessed_from_the_extension(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        with proxy.get(f"{fixture_origin.base_url}/mapped.js") as response:
            assert "javascript" in response.headers["content-type"]

    def test_an_explicit_content_type_wins(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        with proxy.get(f"{fixture_origin.base_url}/typed") as response:
            assert response.headers["content-type"] == "application/json"

    def test_a_locally_served_response_is_never_cached(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """The rule can be edited a second later; a cached body would outlive it."""
        with proxy.get(f"{fixture_origin.base_url}/mapped.js") as response:
            assert "no-store" in response.headers["cache-control"]

    def test_it_is_labelled_as_ours(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        with proxy.get(f"{fixture_origin.base_url}/mapped.js") as response:
            assert response.headers["x-pporlock"] == "map_local"

    def test_a_missing_file_fails_loudly_and_lets_the_request_proceed(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """REQ PXY-034. Silence looks exactly like a rule that did not match."""
        before = len(proxy.sink.flows)
        with pytest.raises(urllib.error.HTTPError):
            proxy.get(f"{fixture_origin.base_url}/missing")
        proxy.wait_for_flows(before + 1)

        _request, _response, provenance = last(proxy)
        assert provenance.has_note(NoteCode.MAP_LOCAL_MISSING)
        assert provenance.entries[0].outcome is Outcome.ERROR

    def test_provenance_records_what_was_served(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/mapped.js"):
            pass
        proxy.wait_for_flows(before + 1)

        entry = last(proxy)[2].entries[0]
        assert entry.action is Action.MAP_LOCAL
        assert entry.outcome is Outcome.APPLIED
        assert entry.detail["file"] == "local.js"
        assert entry.detail["bytes"] > 0


class TestRedirect:
    def test_sends_the_request_somewhere_else(self, proxy: ProxyHarness) -> None:
        before = len(proxy.sink.flows)
        with proxy.get("http://redirect.example/anything") as response:
            assert b'"ok":true' in response.read()
        proxy.wait_for_flows(before + 1)

    def test_the_rewrite_is_recorded(self, proxy: ProxyHarness) -> None:
        before = len(proxy.sink.flows)
        with proxy.get("http://redirect.example/anything"):
            pass
        proxy.wait_for_flows(before + 1)

        entry = last(proxy)[2].entries[0]
        assert entry.action is Action.REDIRECT
        assert entry.detail["to"]["host"] == "127.0.0.1"

    def test_the_target_comes_only_from_the_rule(self, proxy: ProxyHarness) -> None:
        """The action is SSRF by design — substituting a remote asset with a
        local one is a stated use case. What makes it safe is that the target is
        read from the rule and nothing else: no response body, header, or URL
        can influence it (implementation-plan.md §2.5)."""
        assert proxy.interceptor is not None
        rule = next(
            r
            for r in proxy.interceptor.evaluator.ruleset.short_circuit
            if r.name == "redirect-to-fixture"
        )
        assert rule.params["to"] == {
            "host": "127.0.0.1",
            "port": rule.params["to"]["port"],
            "path": "/health",
        }


class TestHeaderActions:
    def test_a_request_header_reaches_the_origin(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/dest/json"):
            pass
        proxy.wait_for_flows(before + 1)

        request = last(proxy)[0]
        assert request.header("x-pporlock-test") == "added"

    def test_a_response_header_is_removed_and_another_set(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        with proxy.get(f"{fixture_origin.base_url}/csp/nonce") as response:
            assert response.headers.get("content-security-policy") is None
            assert response.headers["x-pporlock-rewritten"] == "yes"

    def test_the_change_is_recorded_as_applied(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/csp/nonce"):
            pass
        proxy.wait_for_flows(before + 1)

        entries = [e for e in last(proxy)[2].entries if e.action is Action.HEADERS]
        assert entries
        assert entries[0].outcome is Outcome.APPLIED

    def test_a_flow_whose_headers_changed_is_marked_modified_not_blocked(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/csp/nonce"):
            pass
        proxy.wait_for_flows(before + 1)
        provenance = last(proxy)[2]
        assert provenance.short_circuited_by is None


class TestBudgetAndOffload:
    def test_body_rules_record_their_offload_decision(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """REQ PXY-024 — the classification is visible, not implicit."""
        assert proxy.interceptor is not None
        from pporlock.engine.cost import decide_offload

        assert decide_offload("inject_script", 10).offload
        assert not decide_offload("strip_csp", 10).offload
