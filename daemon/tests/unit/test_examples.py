"""The shipped example modules, loaded and exercised for real.

Examples that are not tested become examples that do not work, and an example
that does not work is worse than none: it is read as a statement about what the
system does. So every module in `examples/modules/` is loaded through the
ordinary loader here, and the ones with behaviour worth pinning are run.

This is also the closest thing the project has to a public API conformance
suite. If a change to the engine breaks a module written the documented way,
it breaks here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from pporlock.engine.evaluator import Evaluator
from pporlock.engine.models import (
    NormalizedRequest,
    NormalizedResponse,
    WebSocketMessage,
)
from pporlock.engine.modules.loader import discover, load_module
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.provenance import NoteCode, Outcome, ProvenanceBuilder

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "modules"


def example_names() -> list[str]:
    return sorted(p.name for p in discover(EXAMPLES))


def request(**kwargs: Any) -> NormalizedRequest:
    fields: dict[str, Any] = {
        "flow_id": "f1",
        "timestamp": "2026-08-28T00:00:00Z",
        "scheme": "https",
        "method": "GET",
        "host": "example.com",
        "port": 443,
        "path": "/",
        "url": "https://example.com/",
        "dest": "document",
        "headers": (("accept", "text/html"),),
    }
    fields.update(kwargs)
    fields.setdefault("url", f"https://{fields['host']}{fields['path']}")
    return NormalizedRequest(**fields)


def response(
    body: bytes = b"", content_type: str = "text/html", status: int = 200
) -> NormalizedResponse:
    return NormalizedResponse(
        flow_id="f1",
        timestamp="2026-08-28T00:00:00Z",
        status=status,
        headers=(("content-type", content_type),),
        body=body,
    )


@pytest.fixture
def installed(tmp_path: Path) -> Path:
    """A copy of the examples, so a test enabling one cannot edit the shipped tree."""
    root = tmp_path / "modules"
    shutil.copytree(EXAMPLES, root)
    for manifest in root.glob("*/module.yaml"):
        manifest.write_text(manifest.read_text().replace("enabled: false", "enabled: true"))
    return root


def registry_for(root: Path) -> ModuleRegistry:
    registry = ModuleRegistry(root, store_path=root.parent / "store.db")
    registry.reload()
    return registry


# --------------------------------------------------------------------------


class TestEveryExampleIsValid:
    def test_there_are_examples_to_test(self) -> None:
        """A suite that silently tests nothing is the failure this whole
        project keeps rediscovering."""
        assert len(example_names()) >= 8

    @pytest.mark.parametrize("name", example_names())
    def test_it_loads_without_error(self, name: str) -> None:
        module = load_module(EXAMPLES / name)
        assert module.error is None, f"{name}: {module.error}"
        assert module.state != "load_error"

    @pytest.mark.parametrize("name", example_names())
    def test_it_ships_disabled(self, name: str) -> None:
        """REQ MOD-003 in the documentation's own voice. An example that
        installs itself already running teaches the wrong lesson about a tool
        that can rewrite every page you load."""
        module = load_module(EXAMPLES / name)
        assert module.enabled is False, f"{name} ships enabled"

    @pytest.mark.parametrize("name", example_names())
    def test_it_describes_itself(self, name: str) -> None:
        module = load_module(EXAMPLES / name)
        assert module.description, f"{name} has no description"
        assert module.version, f"{name} has no version"

    @pytest.mark.parametrize("name", example_names())
    def test_its_rules_compile(self, name: str) -> None:
        """A rule that fails to compile is reported at load, not at traffic —
        but only if something compiles it."""
        module = load_module(EXAMPLES / name)
        assert module.error is None
        for rule in module.rules:
            assert rule.name
            assert rule.rule_id.startswith(f"{name}:")

    def test_priorities_do_not_collide(self) -> None:
        """The library is meant to be enabled together, and priority is what
        orders one module's rules against another's (REQ MOD-023)."""
        priorities = {}
        for name in example_names():
            module = load_module(EXAMPLES / name)
            assert module.priority not in priorities, (
                f"{name} and {priorities[module.priority]} share priority {module.priority}"
            )
            priorities[module.priority] = name


class TestTheyLoadTogether:
    def test_the_whole_library_loads_as_one_set(self, installed: Path) -> None:
        registry = registry_for(installed)
        assert [m.name for m in registry.modules if m.error] == []
        assert len(registry.active(None)) == len(example_names())

    def test_the_combined_ruleset_compiles(self, installed: Path) -> None:
        registry = registry_for(installed)
        ruleset = registry.build_ruleset(None)
        assert len(ruleset.short_circuit) > 0
        assert len(ruleset.response_body) > 0
        assert len(ruleset.response_headers) > 0


class TestAdblock:
    def evaluator(self, installed: Path) -> Evaluator:
        registry = registry_for(installed)
        return Evaluator(registry.build_ruleset(["adblock"]), registry=registry)

    def test_it_stubs_a_blocked_tracker(self, installed: Path) -> None:
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(installed).evaluate_request(
            request(host="www.google-analytics.com", path="/collect", dest="script"), builder
        )
        assert decision.short_circuit is not None
        assert decision.short_circuit.status == 200

    def test_a_blocked_script_gets_a_script_stub_not_an_error_page(self, installed: Path) -> None:
        """A blocked script answered with HTML breaks the page differently
        than it was already broken (REQ PXY-032)."""
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(installed).evaluate_request(
            request(host="ads.doubleclick.net", path="/x.js", dest="script"), builder
        )
        assert decision.short_circuit is not None
        assert "javascript" in (decision.short_circuit.content_type or "")

    def test_it_leaves_an_ordinary_request_alone(self, installed: Path) -> None:
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(installed).evaluate_request(
            request(host="example.com", path="/about"), builder
        )
        assert decision.short_circuit is None
        assert builder.build().entries == ()

    def test_a_beacon_path_on_a_document_is_not_blocked(self, installed: Path) -> None:
        """The path rules are scoped by dest on purpose: /collect on a
        first-party host may be the application itself."""
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(installed).evaluate_request(
            request(host="example.com", path="/collect", dest="document"), builder
        )
        assert decision.short_circuit is None


class TestJsonTamper:
    def evaluator(self, installed: Path) -> Evaluator:
        registry = registry_for(installed)
        return Evaluator(registry.build_ruleset(["json-tamper"]), registry=registry)

    def test_it_empties_the_ads_array(self, installed: Path) -> None:
        import json

        body = json.dumps({"ads": [1, 2, 3], "content": "keep me", "nested": {"tracker": True}})
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(installed).evaluate_response_body(
            request(path="/json"),
            response(body.encode(), content_type="application/json"),
            builder,
        )
        assert decision.mutation.body is not None
        patched = json.loads(decision.mutation.body)
        assert patched["ads"] == []
        assert patched["content"] == "keep me"
        assert "tracker" not in patched["nested"]

    def test_a_non_json_body_is_reported_not_failed(self, installed: Path) -> None:
        builder = ProvenanceBuilder("default")
        self.evaluator(installed).evaluate_response_body(
            request(path="/json"),
            response(b"<html>not json</html>", content_type="application/json"),
            builder,
        )
        outcomes = {e.outcome for e in builder.build().entries}
        assert Outcome.APPLIED not in outcomes


class TestCookieBanners:
    def test_the_hook_adds_the_unlock_shim_to_a_document(self, installed: Path) -> None:
        registry = registry_for(installed)
        ev = Evaluator(registry.build_ruleset(["cookie-banners"]), registry=registry)
        builder = ProvenanceBuilder("default")
        decision = ev.evaluate_response_body(
            request(),
            response(b"<html><body><p>hi</p></body></html>"),
            builder,
        )
        assert decision.mutation.body is not None
        body = decision.mutation.body
        # Both tiers composed: the manifest's stylesheet AND the hook's script.
        # These used to race — the transform result was written over the hook's
        # body afterwards, so the script vanished while provenance said the
        # hook had applied.
        assert b"<style>" in body and b"onetrust" in body
        assert b"<script>" in body and b"clearInterval" in body
        assert body.index(b"<style>") < body.index(b"<script>")
        assert builder.build().has_note(NoteCode.SCRIPT_INJECTED)

    def test_it_still_runs_on_a_document_with_no_closing_body_tag(self, installed: Path) -> None:
        """</body> is optional in HTML and real pages omit it. Requiring it
        made the module do nothing at all on a valid document, silently —
        found by running it against the fixture origin, which omits it."""
        registry = registry_for(installed)
        ev = Evaluator(registry.build_ruleset(["cookie-banners"]), registry=registry)
        builder = ProvenanceBuilder("default")
        decision = ev.evaluate_response_body(
            request(),
            response(b"<!doctype html>\n<meta charset=utf-8><p>no closing tags</p>\n"),
            builder,
        )
        assert decision.mutation.body is not None
        assert b"clearInterval" in decision.mutation.body
        assert builder.build().has_note(NoteCode.SCRIPT_INJECTED)

    def test_it_leaves_a_json_response_alone(self, installed: Path) -> None:
        registry = registry_for(installed)
        ev = Evaluator(registry.build_ruleset(["cookie-banners"]), registry=registry)
        builder = ProvenanceBuilder("default")
        decision = ev.evaluate_response_body(
            request(dest="empty"),
            response(b'{"a":1}', content_type="application/json"),
            builder,
        )
        assert decision.mutation.body is None


class TestFaultLab:
    def evaluator(self, installed: Path) -> Evaluator:
        registry = registry_for(installed)
        return Evaluator(registry.build_ruleset(["fault-lab"]), registry=registry)

    def test_it_fails_every_nth_request_and_only_those(self, installed: Path) -> None:
        """Deterministic on purpose: a failure you cannot replay is a failure
        you argue about."""
        ev = self.evaluator(installed)
        results = []
        for _ in range(6):
            builder = ProvenanceBuilder("default")
            decision = ev.evaluate_request(
                request(host="api.example.com", path="/v1/orders"), builder
            )
            results.append(decision.short_circuit is not None)
        assert results == [False, False, True, False, False, True]

    def test_it_ignores_hosts_it_was_not_pointed_at(self, installed: Path) -> None:
        ev = self.evaluator(installed)
        for _ in range(6):
            builder = ProvenanceBuilder("default")
            decision = ev.evaluate_request(request(host="example.com", path="/v1/orders"), builder)
            assert decision.short_circuit is None


class TestWsInspect:
    def evaluator(self, installed: Path) -> Evaluator:
        registry = registry_for(installed)
        return Evaluator(registry.build_ruleset(["ws-inspect"]), registry=registry)

    def frame(self, payload: bytes, opcode: str = "text") -> WebSocketMessage:
        return WebSocketMessage(
            flow_id="f1",
            index=0,
            timestamp="2026-08-28T00:00:00Z",
            direction="incoming",
            opcode=opcode,
            payload=payload,
        )

    def test_it_notes_an_interesting_frame(self, installed: Path) -> None:
        builder = ProvenanceBuilder("default")
        self.evaluator(installed).observe_websocket_message(
            self.frame(b'{"type":"error","reason":"unauthorized"}'), request(), builder
        )
        prov = builder.build()
        assert prov.has_note(NoteCode.MODULE_ERROR)
        assert any(n.module == "ws-inspect" for n in prov.notes)

    def test_an_ordinary_frame_says_nothing(self, installed: Path) -> None:
        builder = ProvenanceBuilder("default")
        self.evaluator(installed).observe_websocket_message(
            self.frame(b'{"type":"heartbeat"}'), request(), builder
        )
        assert builder.build().notes == ()

    def test_a_binary_frame_does_not_raise(self, installed: Path) -> None:
        """Decoding a binary frame as UTF-8 to search it for words is how a
        working socket becomes an exception on every frame."""
        builder = ProvenanceBuilder("default")
        self.evaluator(installed).observe_websocket_message(
            self.frame(b"\xff\xfe\x00\x01", opcode="binary"), request(), builder
        )
        assert not builder.build().has_note(NoteCode.MODULE_ERROR)


class TestUserAgentSwitcher:
    """The module the module-settings feature exists for.

    Everything it does is driven from `ctx.config`, so these run it through the
    registry with settings applied the way the API applies them — not by
    hand-building a context, which would not prove the settings reach it.
    """

    def registry(self, installed: Path) -> ModuleRegistry:
        return registry_for(installed)

    def evaluator(self, registry: ModuleRegistry) -> Evaluator:
        return Evaluator(registry.build_ruleset(["user-agent-switcher"]), registry=registry)

    def headers_for(self, ev: Evaluator, **kwargs: Any) -> dict[str, str]:
        builder = ProvenanceBuilder("default")
        decision = ev.evaluate_request(request(**kwargs), builder)
        return dict(decision.mutation.set_headers)

    def test_it_sends_googlebot_out_of_the_box(self, installed: Path) -> None:
        """The default has to be the useful one. A module whose shipped setting
        does nothing is a module people conclude is broken."""
        sent = self.headers_for(self.evaluator(self.registry(installed)))
        assert "Googlebot/2.1" in sent["user-agent"]

    def test_every_identity_the_form_offers_produces_a_user_agent(self, installed: Path) -> None:
        """The manifest's enum and the module's lookup table are two lists that
        have to agree. An identity the form offers but the table lacks presents
        as "enabled and changes nothing", which is the hardest failure to spot.
        """
        registry = self.registry(installed)
        module = registry.get("user-agent-switcher")
        assert module is not None
        (identity,) = [s for s in module.settings if s.key == "identity"]
        offered = [o.value for o in identity.options if o.value != "custom"]
        assert len(offered) >= 8

        for value in offered:
            _, errors = registry.set_config("user-agent-switcher", {"identity": value})
            assert errors == []
            sent = self.headers_for(self.evaluator(registry))
            assert sent.get("user-agent"), f"{value} sends no user agent"

    def test_it_removes_the_client_hints_that_would_contradict_it(self, installed: Path) -> None:
        """Chrome's Sec-CH-UA names the real browser. Left behind, it hands the
        site a *more* interesting signal than an unmodified Chrome would have."""
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(self.registry(installed)).evaluate_request(
            request(headers=(("sec-ch-ua", '"Chromium";v="125"'), ("accept", "text/html"))),
            builder,
        )
        assert "sec-ch-ua" in decision.mutation.remove_headers
        assert "sec-ch-ua-platform" in decision.mutation.remove_headers

    def test_the_hint_removal_can_be_turned_off(self, installed: Path) -> None:
        registry = self.registry(installed)
        registry.set_config("user-agent-switcher", {"strip_client_hints": False})
        builder = ProvenanceBuilder("default")
        decision = self.evaluator(registry).evaluate_request(request(), builder)
        assert decision.mutation.remove_headers == []

    def test_the_host_list_is_what_scopes_it(self, installed: Path) -> None:
        """The difference between auditing one site and announcing yourself as
        a crawler to every site you visit."""
        registry = self.registry(installed)
        registry.set_config("user-agent-switcher", {"hosts": ["*.example.org"]})
        ev = self.evaluator(registry)
        assert "user-agent" not in self.headers_for(ev, host="example.com")
        assert "user-agent" in self.headers_for(ev, host="www.example.org")

    def test_documents_only_leaves_subresources_alone(self, installed: Path) -> None:
        registry = self.registry(installed)
        registry.set_config("user-agent-switcher", {"scope": "documents"})
        ev = self.evaluator(registry)
        assert "user-agent" in self.headers_for(ev, dest="document")
        assert "user-agent" not in self.headers_for(ev, dest="script")

    def test_a_custom_identity_with_an_empty_box_sends_nothing(self, installed: Path) -> None:
        """Not an empty User-Agent. Some servers answer that with a 400, which
        would read as the site blocking crawlers."""
        registry = self.registry(installed)
        registry.set_config(
            "user-agent-switcher", {"identity": "custom", "custom_user_agent": "   "}
        )
        assert self.headers_for(self.evaluator(registry)) == {}

    def test_a_custom_string_is_sent_verbatim(self, installed: Path) -> None:
        registry = self.registry(installed)
        registry.set_config(
            "user-agent-switcher",
            {"identity": "custom", "custom_user_agent": "MyBot/1.0"},
        )
        assert self.headers_for(self.evaluator(registry))["user-agent"] == "MyBot/1.0"

    def test_a_setting_change_takes_effect_without_a_reload(self, installed: Path) -> None:
        """The whole point of `set_config` not reloading: the module keeps its
        store, and the next request already uses the new value."""
        registry = self.registry(installed)
        ev = self.evaluator(registry)
        assert "Googlebot" in self.headers_for(ev)["user-agent"]
        registry.set_config("user-agent-switcher", {"identity": "claudebot"})
        assert "ClaudeBot" in self.headers_for(ev)["user-agent"]

    def test_its_report_counts_what_it_sent(self, installed: Path) -> None:
        """The question that comes up every time is "was this page actually
        fetched as the crawler, or did I leave the host list narrow?"."""
        registry = self.registry(installed)
        ev = self.evaluator(registry)
        self.headers_for(ev)
        self.headers_for(ev)
        rendered = registry.report("user-agent-switcher")
        assert rendered is not None
        content_type, body = rendered
        assert "text/html" in content_type
        assert b"Googlebot/2.1" in body
        assert b"<td>2</td>" in body

    def test_the_report_escapes_a_custom_string_off_the_wire(self, installed: Path) -> None:
        """The identity can be arbitrary user text and the report is rendered in
        the browser of the person auditing. gpc-audit learned this first."""
        registry = self.registry(installed)
        registry.set_config(
            "user-agent-switcher",
            {"identity": "custom", "custom_user_agent": "<script>alert(1)</script>"},
        )
        self.headers_for(self.evaluator(registry))
        rendered = registry.report("user-agent-switcher")
        assert rendered is not None
        assert b"<script>alert" not in rendered[1]


class TestAssetsAreReachable:
    @pytest.mark.parametrize("name,asset", [("css-tamper", "user.css"), ("local-bundle", "app.js")])
    def test_the_map_local_target_exists(self, name: str, asset: str) -> None:
        """A map_local rule pointing at a file that is not there serves the
        real thing and emits map_local_missing — findable, but only once you
        already suspect it."""
        assert (EXAMPLES / name / "assets" / asset).is_file()
