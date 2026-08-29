"""The control API. SPEC-0 §6.

Driven through Starlette's TestClient against the real app and real middleware,
so the security tests exercise the same code path a browser would.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from pporlock.addon.interceptor import Interceptor
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import (
    INLINE_ROUTES,
    OFFLOAD_ROUTES,
    PUBLIC_ROUTES,
    ControlApp,
)
from pporlock.control.audit import AuditLog
from pporlock.control.events import EventHub
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.profiles import ProfileManager

from .test_ring import make_record


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    config = Config()
    config.state_dir = str(tmp_path)
    ring = RingBuffer()
    ring.add(make_record("f0", host="a.example", path="/one.js"))
    ring.add(make_record("f1", host="b.example", path="/two.css", status=404))
    interceptor = Interceptor(
        config,
        exclusions=ExclusionList([ExclusionEntry("*.apple.com", "update: OS", "default")]),
    )
    return ControlApp(config, ring=ring, interceptor=interceptor)


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def token(app: ControlApp) -> str:
    return app.tokens.ensure()


def auth(token: str, client_name: str = "cli") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Pporlock-Client": client_name}


class TestHealth:
    def test_is_public(self, client: TestClient) -> None:
        """The extension polls this to decide whether to clear Chrome's proxy
        configuration, so it must answer without a token (REQ EXT-010)."""
        response = client.get("/state/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_reveals_nothing_but_liveness(self, client: TestClient) -> None:
        assert set(client.get("/state/health").json()) == {"ok", "version"}


class TestAuthentication:
    def test_state_requires_a_token(self, client: TestClient) -> None:
        assert client.get("/state").status_code == 401

    def test_state_with_a_token(self, client: TestClient, token: str) -> None:
        assert client.get("/state", headers=auth(token)).status_code == 200

    def test_a_wrong_token_is_rejected(self, client: TestClient) -> None:
        assert client.get("/state", headers=auth("wrong")).status_code == 401

    def test_error_body_shape(self, client: TestClient) -> None:
        payload = client.get("/state").json()
        assert payload["error"]["code"] == "unauthorized"
        assert "message" in payload["error"]

    def test_token_never_appears_in_an_error_body(self, client: TestClient, token: str) -> None:
        assert token not in client.get("/state").text


class TestOriginPolicy:
    def test_an_ordinary_web_page_is_refused(self, client: TestClient, token: str) -> None:
        """The threat this whole layer exists for."""
        response = client.get("/state", headers={**auth(token), "Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_refused_even_with_a_valid_token(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/state", json={}, headers={**auth(token), "Origin": "https://evil.example"}
        )
        assert response.status_code == 403

    def test_own_origin_is_allowed(self, client: TestClient, token: str) -> None:
        response = client.get("/state", headers={**auth(token), "Origin": "http://127.0.0.1:8081"})
        assert response.status_code == 200

    def test_cors_header_echoes_the_allowed_origin(self, client: TestClient, token: str) -> None:
        response = client.get("/state", headers={**auth(token), "Origin": "http://127.0.0.1:8081"})
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8081"
        assert response.headers["vary"] == "origin"


class TestCsrfDefence:
    def test_mutating_request_needs_the_client_header(self, client: TestClient, token: str) -> None:
        """REQ API-013. A cross-origin form can POST to loopback but cannot set
        a custom header without triggering a preflight we would reject."""
        response = client.post("/state", json={}, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_unknown_client_is_refused(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/state",
            json={},
            headers={"Authorization": f"Bearer {token}", "X-Pporlock-Client": "attacker"},
        )
        assert response.status_code == 403

    def test_reads_do_not_need_the_client_header(self, client: TestClient, token: str) -> None:
        response = client.get("/state", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200

    def test_a_full_form_post_attack_fails(self, client: TestClient) -> None:
        """The concrete attack: a page you are visiting submits a hidden form."""
        response = client.post(
            "/state",
            data={"dev_toggles": "anticomp"},
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 403


class TestState:
    def test_shape(self, client: TestClient, token: str) -> None:
        payload = client.get("/state", headers=auth(token)).json()
        for key in ("version", "proxy", "active_profile", "dev_toggles", "capture", "counters"):
            assert key in payload

    def test_reports_ring_occupancy(self, client: TestClient, token: str) -> None:
        assert client.get("/state", headers=auth(token)).json()["capture"]["ring_flows"] == 2

    def test_dev_toggle_can_be_set(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/state", json={"dev_toggles": {"anticomp": True}}, headers=auth(token)
        )
        assert response.json()["dev_toggles"]["anticomp"] is True

    def test_dev_toggle_change_is_audited(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """REQ MCP-031 — these alter traffic, so who flipped them matters."""
        client.post("/state", json={"dev_toggles": {"anticache": True}}, headers=auth(token, "mcp"))
        entries, _ = app.audit.entries()
        assert entries[0].origin == "mcp"
        assert entries[0].action == "dev_toggle"


class TestFlows:
    def test_list(self, client: TestClient, token: str) -> None:
        payload = client.get("/flows", headers=auth(token)).json()
        assert len(payload["flows"]) == 2
        assert payload["total_estimate"] == 2

    def test_newest_first(self, client: TestClient, token: str) -> None:
        flows = client.get("/flows", headers=auth(token)).json()["flows"]
        assert flows[0]["flow_id"] == "f1"

    def test_filtering(self, client: TestClient, token: str) -> None:
        payload = client.get("/flows?host=a.example", headers=auth(token)).json()
        assert [f["flow_id"] for f in payload["flows"]] == ["f0"]

    def test_status_filter(self, client: TestClient, token: str) -> None:
        payload = client.get("/flows?status=404", headers=auth(token)).json()
        assert [f["flow_id"] for f in payload["flows"]] == ["f1"]

    def test_limit(self, client: TestClient, token: str) -> None:
        assert len(client.get("/flows?limit=1", headers=auth(token)).json()["flows"]) == 1

    def test_bad_limit_falls_back(self, client: TestClient, token: str) -> None:
        assert client.get("/flows?limit=abc", headers=auth(token)).status_code == 200

    def test_list_omits_bodies_by_default(self, client: TestClient, token: str) -> None:
        """Summary detail: bodies dominate response size (SPEC-0 §6.3)."""
        flow = client.get("/flows", headers=auth(token)).json()["flows"][0]
        assert "body" not in flow["request"]

    def test_every_listed_flow_carries_provenance(self, client: TestClient, token: str) -> None:
        """REQ CAP-013 — at every detail level, on every flow."""
        for flow in client.get("/flows", headers=auth(token)).json()["flows"]:
            assert "provenance" in flow

    def test_detail_full_includes_body_keys(self, client: TestClient, token: str) -> None:
        flow = client.get("/flows/f0?detail=full", headers=auth(token)).json()
        assert "body" in flow["request"]

    def test_single_flow(self, client: TestClient, token: str) -> None:
        assert client.get("/flows/f0", headers=auth(token)).json()["flow_id"] == "f0"

    def test_unknown_flow_is_404_with_a_code(self, client: TestClient, token: str) -> None:
        response = client.get("/flows/nope", headers=auth(token))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_clear(self, client: TestClient, token: str) -> None:
        assert client.delete("/flows", headers=auth(token)).status_code == 204
        assert client.get("/flows", headers=auth(token)).json()["flows"] == []

    def test_clear_is_audited(self, client: TestClient, token: str, app: ControlApp) -> None:
        client.delete("/flows", headers=auth(token, "ui"))
        entries, _ = app.audit.entries()
        assert entries[0].action == "clear_flows"


class TestExclusions:
    def test_get(self, client: TestClient, token: str) -> None:
        payload = client.get("/exclusions", headers=auth(token)).json()
        assert payload["entries"][0]["pattern"] == "*.apple.com"

    def test_entries_carry_their_comment(self, client: TestClient, token: str) -> None:
        """An exclusion nobody can explain is indistinguishable from a bug."""
        payload = client.get("/exclusions", headers=auth(token)).json()
        assert payload["entries"][0]["comment"]

    def test_put_replaces_and_takes_effect_immediately(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """REQ PXY-014 — no daemon restart."""
        client.put(
            "/exclusions",
            json={"entries": [{"pattern": "new.example", "comment": "test"}]},
            headers=auth(token),
        )
        assert app.interceptor is not None
        assert app.interceptor.exclusions.should_exclude("new.example")
        assert not app.interceptor.exclusions.should_exclude("www.apple.com")

    def test_put_is_audited(self, client: TestClient, token: str, app: ControlApp) -> None:
        client.put("/exclusions", json={"entries": []}, headers=auth(token))
        entries, _ = app.audit.entries()
        assert entries[0].action == "put_exclusions"


class TestOtherRoutes:
    def test_config(self, client: TestClient, token: str) -> None:
        payload = client.get("/config", headers=auth(token)).json()
        assert payload["control"]["listen_port"] == 8081

    def test_metrics(self, client: TestClient, token: str) -> None:
        assert "ring" in client.get("/metrics", headers=auth(token)).json()

    def test_audit_is_newest_first(self, client: TestClient, token: str) -> None:
        client.delete("/flows", headers=auth(token))
        client.post("/state", json={"dev_toggles": {"anticomp": True}}, headers=auth(token))
        entries = client.get("/audit", headers=auth(token)).json()["entries"]
        assert entries[0]["action"] == "dev_toggle"

    def test_audit_pagination(self, client: TestClient, token: str) -> None:
        for _ in range(5):
            client.delete("/flows", headers=auth(token))
        payload = client.get("/audit?limit=2", headers=auth(token)).json()
        assert len(payload["entries"]) == 2
        assert payload["next_cursor"] is not None


class TestPairing:
    def test_requires_an_open_window(self, client: TestClient) -> None:
        response = client.post(
            "/pair",
            json={"code": "x"},
            headers={"Origin": "chrome-extension://" + "a" * 32},
        )
        assert response.status_code == 403

    def test_redeems_for_the_token(self, client: TestClient, app: ControlApp) -> None:
        """REQ API-012 — the extension never reads the filesystem."""
        code = app.pairing.open()
        response = client.post(
            "/pair",
            json={"code": code},
            headers={"Origin": "chrome-extension://" + "a" * 32},
        )
        assert response.status_code == 200
        assert response.json()["token"] == app.tokens.ensure()

    def test_pairing_is_audited(self, client: TestClient, app: ControlApp) -> None:
        code = app.pairing.open()
        client.post(
            "/pair", json={"code": code}, headers={"Origin": "chrome-extension://" + "a" * 32}
        )
        entries, _ = app.audit.entries()
        assert entries[0].action == "paired"

    def test_the_paired_extension_is_then_allowed(
        self, client: TestClient, app: ControlApp
    ) -> None:
        origin = "chrome-extension://" + "a" * 32
        code = app.pairing.open()
        client.post("/pair", json={"code": code}, headers={"Origin": origin})
        response = client.get(
            "/state", headers={"Authorization": f"Bearer {app.tokens.ensure()}", "Origin": origin}
        )
        assert response.status_code == 200


class TestLoopDiscipline:
    """SPEC-1 §7.1.

    The control server shares the proxy's event loop, so a slow handler stalls
    every connection the browser has open. Routes that read in-memory state may
    run inline; anything else must offload.
    """

    def test_inline_routes_are_declared(self) -> None:
        assert INLINE_ROUTES

    def test_config_is_not_inline(self) -> None:
        """It reflects over dataclasses and, from Sprint 9, reads the filesystem."""
        assert "/config" not in INLINE_ROUTES
        assert "/config" in OFFLOAD_ROUTES

    def test_the_two_sets_do_not_overlap(self) -> None:
        """A route cannot be both. Overlap would mean the classification says
        nothing."""
        assert not (INLINE_ROUTES & OFFLOAD_ROUTES)

    def test_public_routes_are_minimal(self) -> None:
        """Every unauthenticated route is attack surface; there are two, and
        both are needed before a token exists."""
        assert PUBLIC_ROUTES == {"/state/health", "/pair"}

    def test_every_registered_route_is_classified(self, app: ControlApp) -> None:
        registered = {r.path for r in app.asgi.routes}  # type: ignore[attr-defined]
        unclassified = registered - INLINE_ROUTES - OFFLOAD_ROUTES - PUBLIC_ROUTES
        assert not unclassified, (
            f"routes with no loop classification: {sorted(unclassified)}. "
            "Add to INLINE_ROUTES if they read memory only; otherwise offload "
            "them and add to OFFLOAD_ROUTES."
        )


class TestAuditLog:
    def test_bounded(self) -> None:
        log = AuditLog(max_entries=3)
        for i in range(10):
            log.record("cli", f"action-{i}")
        assert len(log) == 3

    def test_bad_cursor_starts_from_the_top(self) -> None:
        log = AuditLog()
        log.record("cli", "a")
        entries, _ = log.entries(cursor="not-a-number")
        assert len(entries) == 1

    def test_entry_shape(self) -> None:
        log = AuditLog()
        entry = log.record("mcp", "enable_module", module="x")
        payload: dict[str, Any] = entry.to_dict()
        assert payload["origin"] == "mcp"
        assert payload["detail"] == {"module": "x"}
        assert payload["ts"].endswith("Z")


class TestAttribution:
    """SPEC-0 §3.6 — the join between what the extension sees and what we see."""

    def test_accepts_a_batch(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/attribution",
            json={
                "entries": [{"method": "GET", "url": "https://a.example/x", "tabId": 7, "ts": 1}]
            },
            headers=auth(token, "extension"),
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 1

    def test_a_malformed_entry_does_not_fail_the_batch(
        self, client: TestClient, token: str
    ) -> None:
        """Dropping one association beats dropping a hundred."""
        response = client.post(
            "/attribution",
            json={
                "entries": [
                    {"method": "GET", "url": "https://a.example/x", "tabId": 7},
                    {"nonsense": True},
                ]
            },
            headers=auth(token, "extension"),
        )
        payload = response.json()
        assert payload["accepted"] == 1
        assert payload["rejected"] == 1

    def test_empty_batch_is_fine(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/attribution", json={"entries": []}, headers=auth(token, "extension")
        )
        assert response.json()["accepted"] == 0

    def test_backfills_an_already_delivered_flow(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """A flow is delivered before its tab is known, so the association has
        to reach a row the client has already rendered."""
        flow = app.ring.get("f0")
        assert flow is not None and flow.request is not None
        assert flow.tab_id is None

        response = client.post(
            "/attribution",
            json={
                "entries": [{"method": flow.request.method, "url": flow.request.url, "tabId": 42}]
            },
            headers=auth(token, "extension"),
        )
        assert response.json()["backfilled"] == 1
        assert app.ring.get("f0") is not None
        assert app.ring.get("f0").tab_id == 42  # type: ignore[union-attr]

    def test_backfill_emits_flow_updated(self, tmp_path: Path) -> None:
        """Clients key rows on flow_id and must be told when a field changes.

        Uses a recording hub rather than a monkeypatch: EventHub has __slots__,
        so its methods cannot be replaced on an instance — which is the right
        design for something on the proxy's hot path.
        """
        from pporlock.control.events import EventHub

        class RecordingHub(EventHub):
            def __init__(self) -> None:
                super().__init__()
                self.seen: list[str] = []

            def publish(self, event_type, data, record=None):  # type: ignore[no-untyped-def]
                self.seen.append(event_type)
                return super().publish(event_type, data, record)

        config = Config()
        config.state_dir = str(tmp_path)
        ring = RingBuffer()
        ring.add(make_record("f0", host="a.example", path="/one.js"))
        hub = RecordingHub()
        app = ControlApp(config, ring=ring, events=hub)
        client = TestClient(app.asgi)

        flow = app.ring.get("f0")
        assert flow is not None and flow.request is not None
        client.post(
            "/attribution",
            json={
                "entries": [{"method": flow.request.method, "url": flow.request.url, "tabId": 42}]
            },
            headers=auth(app.tokens.ensure(), "extension"),
        )
        assert "flow.updated" in hub.seen

    def test_requires_the_client_header(self, client: TestClient, token: str) -> None:
        response = client.post(
            "/attribution",
            json={"entries": []},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    def test_coverage_is_reported_in_metrics(self, client: TestClient, token: str) -> None:
        """The Sprint 6 decision criterion is measured against this."""
        payload = client.get("/metrics", headers=auth(token)).json()
        assert "attribution" in payload
        assert "coverage" in payload["attribution"]


class TestRules:
    """REQ MOD-004 — rules edited through the API take effect without a restart."""

    def test_get_returns_the_active_set(self, client: TestClient, token: str) -> None:
        payload = client.get("/rules", headers=auth(token)).json()
        assert payload["count"] == 0

    def test_put_installs_rules(self, client: TestClient, token: str, app: ControlApp) -> None:
        response = client.put(
            "/rules",
            json={"rules": [{"name": "b", "action": "block", "match": {"host": "*.x.test"}}]},
            headers=auth(token),
        )
        assert response.status_code == 200
        assert response.json()["count"] == 1
        assert app.interceptor is not None
        assert len(app.interceptor.evaluator.ruleset) == 1

    def test_a_bad_rule_leaves_the_running_set_untouched(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """Compiling first means a typo does not empty the rules in force."""
        client.put(
            "/rules",
            json={"rules": [{"name": "good", "action": "block"}]},
            headers=auth(token),
        )
        response = client.put(
            "/rules",
            json={"rules": [{"name": "bad", "action": "nonsense"}]},
            headers=auth(token),
        )
        assert response.status_code == 400
        assert app.interceptor is not None
        assert len(app.interceptor.evaluator.ruleset) == 1

    def test_the_error_names_the_problem(self, client: TestClient, token: str) -> None:
        response = client.put(
            "/rules",
            json={"rules": [{"name": "bad", "action": "nonsense"}]},
            headers=auth(token),
        )
        assert response.json()["error"]["code"] == "rule_invalid"
        assert "unknown action" in response.json()["error"]["message"]

    def test_rules_must_be_a_list(self, client: TestClient, token: str) -> None:
        response = client.put("/rules", json={"rules": "nope"}, headers=auth(token))
        assert response.status_code == 400

    def test_the_swap_replaces_rather_than_mutates(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """An in-flight flow keeps the snapshot it started with, which is what
        removes any need for locking (REQ MOD-004)."""
        assert app.interceptor is not None
        before = app.interceptor.evaluator
        client.put(
            "/rules",
            json={"rules": [{"name": "b", "action": "block"}]},
            headers=auth(token),
        )
        assert app.interceptor.evaluator is not before

    def test_a_change_is_audited(self, client: TestClient, token: str, app: ControlApp) -> None:
        client.put("/rules", json={"rules": []}, headers=auth(token, "ui"))
        entries, _ = app.audit.entries()
        assert entries[0].action == "put_rules"


# -- modules and profiles (REQ API-023, API-024) ---------------------------

MANIFEST = "name: tidy\npporlock_api: '1'\nversion: '1.0.0'\nenabled: true\n"
SETTABLE_MANIFEST = (
    "name: tidy\npporlock_api: '1'\nversion: '1.0.0'\n"
    "config: {internal: 1}\n"
    "settings:\n"
    "  - key: identity\n"
    "    label: Identify as\n"
    "    type: enum\n"
    "    default: googlebot\n"
    "    options: [googlebot, claudebot]\n"
    "  - key: hosts\n"
    "    type: string_list\n"
    "    default: ['*']\n"
)
BLOCK_MANIFEST = (
    "name: tidy\npporlock_api: '1'\nversion: '1.0.0'\n"
    "rules:\n"
    "  - name: block-ads\n"
    "    action: block\n"
    "    match:\n"
    "      host: ads.example\n"
)


class RecordingHub(EventHub):
    """An EventHub that remembers what it published.

    A subclass rather than a monkeypatch: EventHub has __slots__, which is the
    right design for something on the proxy's hot path.
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def publish(self, event_type, data, record=None):  # type: ignore[no-untyped-def]
        self.seen.append((event_type, data))
        return super().publish(event_type, data, record)


@pytest.fixture
def module_root(tmp_path: Path) -> Path:
    root = tmp_path / "modules"
    root.mkdir()
    return root


@pytest.fixture
def modular(tmp_path: Path, module_root: Path) -> ControlApp:
    """A ControlApp with a real module registry and profile store on disk."""
    config = Config()
    config.state_dir = str(tmp_path)
    config.modules.root = str(module_root)
    registry = ModuleRegistry(module_root, store_path=tmp_path / "module-store.db")
    registry.reload()
    return ControlApp(
        config,
        ring=RingBuffer(),
        interceptor=Interceptor(config),
        registry=registry,
        profiles=ProfileManager(tmp_path / "profiles"),
        events=RecordingHub(),
    )


@pytest.fixture
def mclient(modular: ControlApp) -> TestClient:
    return TestClient(modular.asgi)


@pytest.fixture
def mtoken(modular: ControlApp) -> str:
    return modular.tokens.ensure()


def write_module_dir(root: Path, name: str, manifest: str, python: str | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "module.yaml").write_text(manifest)
    if python is not None:
        (path / "module.py").write_text(python)
    return path


class TestModuleListing:
    """REQ API-023 — what is installed, and what state each module is in."""

    def test_an_empty_root_lists_nothing(self, mclient: TestClient, mtoken: str) -> None:
        assert mclient.get("/modules", headers=auth(mtoken)).json() == []

    def test_a_module_is_listed_with_its_state(
        self, mclient: TestClient, mtoken: str, modular: ControlApp, module_root: Path
    ) -> None:
        write_module_dir(module_root, "tidy", MANIFEST)
        mclient.post("/modules/reload", headers=auth(mtoken))
        listed = mclient.get("/modules", headers=auth(mtoken)).json()
        assert [m["name"] for m in listed] == ["tidy"]
        assert listed[0]["state"] == "loaded"

    def test_a_module_that_failed_to_load_is_still_listed(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """Omitting it is how an author concludes the daemon never saw their
        module at all (REQ MOD-005)."""
        write_module_dir(
            module_root,
            "boom",
            MANIFEST.replace("tidy", "boom"),
            python="raise ValueError('nope')\n",
        )
        mclient.post("/modules/reload", headers=auth(mtoken))
        listed = mclient.get("/modules", headers=auth(mtoken)).json()
        assert listed[0]["state"] == "load_error"
        assert listed[0]["error"]["code"] == "module_import_failed"

    def test_the_detail_view_returns_the_source(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """The editor needs the bytes on disk, not a re-serialisation of what we
        parsed — a round trip would quietly reformat the author's file."""
        write_module_dir(module_root, "tidy", MANIFEST, python="X = 1\n")
        mclient.post("/modules/reload", headers=auth(mtoken))
        payload = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        assert payload["files"]["module.yaml"] == MANIFEST
        assert payload["files"]["module.py"] == "X = 1\n"

    def test_the_detail_view_lists_assets_without_returning_them(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """Assets are arbitrary bytes of arbitrary size; a listing is what an
        editor needs to show a tree."""
        path = write_module_dir(module_root, "tidy", MANIFEST)
        (path / "assets" / "sub").mkdir(parents=True)
        (path / "assets" / "sub" / "logo.png").write_bytes(b"\x89PNG")
        mclient.post("/modules/reload", headers=auth(mtoken))
        payload = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        assert payload["assets"] == ["sub/logo.png"]

    def test_an_unknown_module_is_404(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.get("/modules/nope", headers=auth(mtoken))
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_state_counts_the_modules(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """/state is what the UI polls, so it carries the summary rather than
        making every client list and count."""
        write_module_dir(module_root, "tidy", MANIFEST)
        write_module_dir(
            module_root, "quiet", MANIFEST.replace("tidy", "quiet").replace("true", "false")
        )
        mclient.post("/modules/reload", headers=auth(mtoken))
        summary = mclient.get("/state", headers=auth(mtoken)).json()["modules"]
        assert summary == {"loaded": 2, "enabled": 1, "quarantined": 0, "errors": []}


class TestCreatingAModule:
    """REQ MCP-030 — writing a module is not deploying it."""

    def create(self, client: TestClient, token: str, manifest: str = MANIFEST) -> Any:
        return client.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": manifest}},
            headers=auth(token),
        )

    def test_creating_returns_the_new_module(self, mclient: TestClient, mtoken: str) -> None:
        response = self.create(mclient, mtoken)
        assert response.status_code == 201
        assert response.json()["name"] == "tidy"

    def test_creating_writes_the_files_to_disk(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        self.create(mclient, mtoken)
        assert (module_root / "tidy" / "module.yaml").read_text() == MANIFEST

    def test_creating_never_enables_even_when_the_manifest_asks(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """The one review step an agent-authored module gets is being read
        before it touches traffic. Honouring `enabled: true` here removes it."""
        assert self.create(mclient, mtoken).json()["enabled"] is False

    def test_a_created_module_contributes_no_rules_until_enabled(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        self.create(mclient, mtoken, BLOCK_MANIFEST)
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 0

    def test_creating_over_an_existing_module_is_refused(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """POST creates. Silently replacing would let an agent overwrite a
        module the user wrote."""
        self.create(mclient, mtoken)
        assert self.create(mclient, mtoken).status_code == 400

    def test_a_path_shaped_name_is_refused(self, mclient: TestClient, mtoken: str) -> None:
        """The name becomes a directory under the module root."""
        response = mclient.post(
            "/modules",
            json={"name": "../escape", "files": {"module.yaml": MANIFEST}},
            headers=auth(mtoken),
        )
        assert response.status_code == 400

    def test_a_file_the_loader_never_reads_is_refused(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """A file that does nothing is a file whose author believes it does
        something."""
        response = mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": MANIFEST, "setup.py": "x"}},
            headers=auth(mtoken),
        )
        assert response.status_code == 400
        assert "setup.py" in response.json()["error"]["message"]

    def test_a_manifest_is_required(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.py": "X = 1\n"}},
            headers=auth(mtoken),
        )
        assert response.status_code == 400

    def test_files_must_not_be_empty(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.post(
            "/modules", json={"name": "tidy", "files": {}}, headers=auth(mtoken)
        )
        assert response.status_code == 400

    def test_a_module_that_does_not_compile_is_created_with_its_error(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """Written but not loaded: the author needs the file on disk to fix, and
        the error to know what to fix."""
        response = self.create(mclient, mtoken, "name: tidy\npporlock_api: '99'\n")
        assert response.status_code == 201
        assert response.json()["state"] == "load_error"
        assert response.json()["error"]["code"] == "module_api_unsupported"

    def test_creating_is_audited(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MCP-031 — an agent that can write modules is one whose writes
        have to be reviewable afterwards."""
        self.create(mclient, mtoken)
        entries, _ = modular.audit.entries()
        assert entries[0].action == "write_module"
        assert entries[0].detail["module"] == "tidy"


class TestReplacingAModule:
    def setup_module_files(self, client: TestClient, token: str, python: str | None = None) -> None:
        files = {"module.yaml": MANIFEST}
        if python is not None:
            files["module.py"] = python
        client.post("/modules", json={"name": "tidy", "files": files}, headers=auth(token))

    def test_put_rewrites_the_manifest(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        self.setup_module_files(mclient, mtoken)
        mclient.put(
            "/modules/tidy",
            json={"files": {"module.yaml": BLOCK_MANIFEST}},
            headers=auth(mtoken),
        )
        assert (module_root / "tidy" / "module.yaml").read_text() == BLOCK_MANIFEST

    def test_put_removes_a_file_the_caller_left_out(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """A replace that left the old module.py behind would keep running code
        the author believes they deleted."""
        self.setup_module_files(mclient, mtoken, python="X = 1\n")
        mclient.put(
            "/modules/tidy", json={"files": {"module.yaml": MANIFEST}}, headers=auth(mtoken)
        )
        assert not (module_root / "tidy" / "module.py").exists()

    def test_put_never_enables_a_disabled_module(self, mclient: TestClient, mtoken: str) -> None:
        """REQ MCP-030 — enablement is API state, not manifest state. An update
        that flipped a module on would be a write turning into a deployment."""
        self.setup_module_files(mclient, mtoken)
        response = mclient.put(
            "/modules/tidy", json={"files": {"module.yaml": MANIFEST}}, headers=auth(mtoken)
        )
        assert response.json()["enabled"] is False

    def test_put_leaves_an_enabled_module_enabled(self, mclient: TestClient, mtoken: str) -> None:
        """Editing a running module should not turn it off underneath the user."""
        self.setup_module_files(mclient, mtoken)
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        response = mclient.put(
            "/modules/tidy", json={"files": {"module.yaml": MANIFEST}}, headers=auth(mtoken)
        )
        assert response.json()["enabled"] is True

    def test_put_reloads_so_the_change_takes_effect(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MOD-004 — no daemon restart."""
        self.setup_module_files(mclient, mtoken)
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        mclient.put(
            "/modules/tidy", json={"files": {"module.yaml": BLOCK_MANIFEST}}, headers=auth(mtoken)
        )
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 1


class TestModulePatching:
    """Enable, disable, reprioritise. Nothing that could change behaviour."""

    @pytest.fixture(autouse=True)
    def _installed(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": BLOCK_MANIFEST}},
            headers=auth(mtoken),
        )

    def test_enabling_puts_the_modules_rules_in_force(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        response = mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        assert response.json()["enabled"] is True
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 1

    def test_disabling_withdraws_them_again(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        mclient.patch("/modules/tidy", json={"enabled": False}, headers=auth(mtoken))
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 0

    def test_priority_is_settable(self, mclient: TestClient, mtoken: str) -> None:
        """REQ MOD-023 — priority is how a user resolves a conflict between two
        modules that both want to act on the same flow."""
        response = mclient.patch("/modules/tidy", json={"priority": 5}, headers=auth(mtoken))
        assert response.json()["priority"] == 5

    def test_a_priority_change_reorders_the_rules_in_force(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.post(
            "/modules",
            json={
                "name": "other",
                "files": {"module.yaml": BLOCK_MANIFEST.replace("tidy", "other")},
            },
            headers=auth(mtoken),
        )
        for name in ("tidy", "other"):
            mclient.patch(f"/modules/{name}", json={"enabled": True}, headers=auth(mtoken))
        mclient.patch("/modules/other", json={"priority": 1}, headers=auth(mtoken))
        assert modular.interceptor is not None
        ordered = [r.module for r in modular.interceptor.evaluator.ruleset.short_circuit]
        assert ordered == ["other", "tidy"]

    def test_anything_but_enabled_and_priority_is_refused(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """A PATCH that could rewrite behaviour would bypass the reload that
        makes a change visible in the module's load state."""
        response = mclient.patch("/modules/tidy", json={"rules": []}, headers=auth(mtoken))
        assert response.status_code == 400
        assert "rules" in response.json()["error"]["message"]

    def test_a_non_numeric_priority_is_refused(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.patch("/modules/tidy", json={"priority": "high"}, headers=auth(mtoken))
        assert response.status_code == 400

    def test_patching_an_unknown_module_is_404(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.patch("/modules/nope", json={"enabled": True}, headers=auth(mtoken))
        assert response.status_code == 404

    def test_enabling_is_audited(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MCP-031 — enabling a module changes what happens to traffic."""
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken, "mcp"))
        entries, _ = modular.audit.entries()
        assert entries[0].action == "patch_module"
        assert entries[0].origin == "mcp"
        assert entries[0].detail["enabled"] is True


class TestModuleSettings:
    """`config` on PATCH: the one thing beyond enabled and priority a PATCH may
    write, and only because the module's author declared exactly which fields
    it covers."""

    @pytest.fixture(autouse=True)
    def _installed(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": SETTABLE_MANIFEST}},
            headers=auth(mtoken),
        )

    def test_the_listing_says_which_modules_have_settings(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """So the library can offer a settings control only where there is
        something to set, rather than opening an empty form."""
        (module,) = mclient.get("/modules", headers=auth(mtoken)).json()
        assert module["has_settings"] is True

    def test_the_detail_carries_the_declaration_and_the_current_values(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """Both, in one response: a client rendering a form needs the fields
        and what is in them, and two round trips is two chances to disagree."""
        detail = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        assert [s["key"] for s in detail["settings"]] == ["identity", "hosts"]
        assert detail["settings"][0]["options"][1] == {
            "value": "claudebot",
            "label": "claudebot",
            "description": "",
        }
        assert detail["config"]["identity"] == "googlebot"
        # The author's free-form config block is still in force alongside the
        # declared fields; `settings` adds a surface, it does not replace one.
        assert detail["config"]["internal"] == 1

    def test_a_manifest_config_value_is_served_as_the_fields_default(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """A client compares against `default` to decide what to send. If that
        were the field's declared default while the manifest had already
        overridden it, opening the dialog and saving would write the module's
        own shipped value back as a user override — freezing it against any
        later improvement to it."""
        mclient.put(
            "/modules/tidy",
            json={
                "files": {
                    "module.yaml": SETTABLE_MANIFEST.replace(
                        "config: {internal: 1}", "config: {identity: claudebot}"
                    )
                }
            },
            headers=auth(mtoken),
        )
        detail = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        (identity,) = [s for s in detail["settings"] if s["key"] == "identity"]
        assert identity["default"] == "claudebot"

    def test_setting_a_value_takes_effect_and_is_read_back(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        response = mclient.patch(
            "/modules/tidy", json={"config": {"identity": "claudebot"}}, headers=auth(mtoken)
        )
        assert response.status_code == 200
        detail = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        assert detail["config"]["identity"] == "claudebot"

    def test_an_undeclared_key_is_refused_and_nothing_is_written(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """A settings form is a place where a typo is easy, and a value that
        goes nowhere is indistinguishable from one that does nothing."""
        response = mclient.patch(
            "/modules/tidy",
            json={"config": {"identity": "claudebot", "identtiy": "x"}},
            headers=auth(mtoken),
        )
        assert response.status_code == 400
        assert "identtiy" in response.json()["error"]["message"]
        detail = mclient.get("/modules/tidy", headers=auth(mtoken)).json()
        assert detail["config"]["identity"] == "googlebot", "a refused PATCH must not half apply"

    def test_a_value_outside_an_enum_is_refused(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.patch(
            "/modules/tidy", json={"config": {"identity": "bingbot"}}, headers=auth(mtoken)
        )
        assert response.status_code == 400

    def test_a_module_declaring_no_settings_accepts_no_config(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """This is what keeps `config:` the author's file rather than giving
        the API a second, hidden way to configure any module."""
        mclient.post(
            "/modules",
            json={
                "name": "plain",
                "files": {"module.yaml": BLOCK_MANIFEST.replace("tidy", "plain")},
            },
            headers=auth(mtoken),
        )
        response = mclient.patch(
            "/modules/plain", json={"config": {"anything": 1}}, headers=auth(mtoken)
        )
        assert response.status_code == 400

    def test_a_settings_change_is_audited_by_key_and_not_by_value(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MCP-031. Which settings someone changed is the auditable fact;
        the values are arbitrary user text and the audit log is not the place
        to accumulate it."""
        mclient.patch(
            "/modules/tidy", json={"config": {"identity": "claudebot"}}, headers=auth(mtoken, "mcp")
        )
        entries, _ = modular.audit.entries()
        assert entries[0].action == "patch_module"
        assert entries[0].detail["settings"] == ["identity"]
        assert "claudebot" not in str(entries[0].detail)

    def test_an_enable_is_not_recorded_as_a_settings_change(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        entries, _ = modular.audit.entries()
        assert "settings" not in entries[0].detail
        assert entries[0].detail["enabled"] is True

    def test_enabling_tells_connected_clients(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """A UI showing a module as off after someone else turned it on is
        showing a lie."""
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        hub = modular.events
        assert isinstance(hub, RecordingHub)
        assert any(t == "state.changed" and "modules" in d for t, d in hub.seen)


class TestDeletingAModule:
    def test_deleting_removes_the_directory(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": MANIFEST}},
            headers=auth(mtoken),
        )
        assert mclient.delete("/modules/tidy", headers=auth(mtoken)).status_code == 204
        assert not (module_root / "tidy").exists()

    def test_a_deleted_modules_rules_stop_applying(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": BLOCK_MANIFEST}},
            headers=auth(mtoken),
        )
        mclient.patch("/modules/tidy", json={"enabled": True}, headers=auth(mtoken))
        mclient.delete("/modules/tidy", headers=auth(mtoken))
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 0

    def test_deleting_an_unknown_module_is_404(self, mclient: TestClient, mtoken: str) -> None:
        assert mclient.delete("/modules/nope", headers=auth(mtoken)).status_code == 404

    def test_deleting_is_audited(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": MANIFEST}},
            headers=auth(mtoken),
        )
        mclient.delete("/modules/tidy", headers=auth(mtoken))
        entries, _ = modular.audit.entries()
        assert entries[0].action == "delete_module"


class TestModuleReload:
    def test_reload_picks_up_a_module_written_outside_the_api(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        """Editing a module in an editor is the normal way to write one; the
        API is not the only door."""
        write_module_dir(module_root, "tidy", MANIFEST)
        payload = mclient.post("/modules/reload", headers=auth(mtoken)).json()
        assert payload["loaded"] == 1

    def test_reload_reports_load_errors_with_the_module_named(
        self, mclient: TestClient, mtoken: str, module_root: Path
    ) -> None:
        write_module_dir(
            module_root,
            "boom",
            MANIFEST.replace("tidy", "boom"),
            python="raise ValueError('nope')\n",
        )
        payload = mclient.post("/modules/reload", headers=auth(mtoken)).json()
        assert payload["errors"][0]["module"] == "boom"
        assert payload["errors"][0]["trace"]

    def test_reload_is_not_read_as_a_module_named_reload(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """Route order: /modules/{name} would happily match 'reload'."""
        assert mclient.post("/modules/reload", headers=auth(mtoken)).status_code == 200

    def test_reload_is_audited(self, mclient: TestClient, mtoken: str, modular: ControlApp) -> None:
        mclient.post("/modules/reload", headers=auth(mtoken))
        entries, _ = modular.audit.entries()
        assert entries[0].action == "reload_modules"


class TestModuleRoutesWithoutARegistry:
    """A daemon with no module root is a legitimate state, not an error one."""

    def test_listing_is_empty_rather_than_failing(self, client: TestClient, token: str) -> None:
        assert client.get("/modules", headers=auth(token)).json() == []

    def test_a_named_module_is_404(self, client: TestClient, token: str) -> None:
        assert client.get("/modules/tidy", headers=auth(token)).status_code == 404

    def test_creating_is_404_rather_than_writing_somewhere_arbitrary(
        self, client: TestClient, token: str
    ) -> None:
        response = client.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": MANIFEST}},
            headers=auth(token),
        )
        assert response.status_code == 404

    def test_reload_is_404(self, client: TestClient, token: str) -> None:
        assert client.post("/modules/reload", headers=auth(token)).status_code == 404

    def test_state_still_reports_a_module_summary(self, client: TestClient, token: str) -> None:
        assert client.get("/state", headers=auth(token)).json()["modules"]["loaded"] == 0


class TestProfileRoutes:
    """REQ API-024, MOD-040-044."""

    def test_the_default_profile_is_always_listed(self, mclient: TestClient, mtoken: str) -> None:
        """REQ MOD-041 — there is never a state with no profile at all."""
        assert [p["name"] for p in mclient.get("/profiles", headers=auth(mtoken)).json()] == [
            "default"
        ]

    def test_creating_a_profile(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.post(
            "/profiles",
            json={"name": "debug", "modules": ["tidy"], "description": "for site x"},
            headers=auth(mtoken),
        )
        assert response.status_code == 201
        assert response.json()["modules"] == ["tidy"]

    def test_a_created_profile_persists(
        self, mclient: TestClient, mtoken: str, tmp_path: Path
    ) -> None:
        """Profiles are settings, not session state."""
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        assert (tmp_path / "profiles" / "debug.yaml").is_file()

    def test_an_unknown_profile_key_is_refused(self, mclient: TestClient, mtoken: str) -> None:
        """Strict for the same reason module manifests are: a silently ignored
        key is a setting its author believes is in force."""
        response = mclient.post(
            "/profiles", json={"name": "debug", "modules_add": []}, headers=auth(mtoken)
        )
        assert response.status_code == 400

    def test_writing_the_default_profile_is_refused(self, mclient: TestClient, mtoken: str) -> None:
        response = mclient.post("/profiles", json={"name": "default"}, headers=auth(mtoken))
        assert response.status_code == 400

    def test_reading_one_profile(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        assert mclient.get("/profiles/debug", headers=auth(mtoken)).json()["name"] == "debug"

    def test_an_unknown_profile_is_404(self, mclient: TestClient, mtoken: str) -> None:
        assert mclient.get("/profiles/nope", headers=auth(mtoken)).status_code == 404

    def test_put_replaces_the_profile(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post("/profiles", json={"name": "debug", "modules": ["a"]}, headers=auth(mtoken))
        response = mclient.put(
            "/profiles/debug", json={"name": "debug", "modules": ["b"]}, headers=auth(mtoken)
        )
        assert response.json()["modules"] == ["b"]

    def test_the_path_names_the_profile_a_disagreeing_body_does_not(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """Otherwise a PUT to one profile could quietly create another."""
        mclient.put("/profiles/debug", json={"name": "elsewhere"}, headers=auth(mtoken))
        assert mclient.get("/profiles/elsewhere", headers=auth(mtoken)).status_code == 404
        assert mclient.get("/profiles/debug", headers=auth(mtoken)).status_code == 200

    def test_deleting_a_profile(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        assert mclient.delete("/profiles/debug", headers=auth(mtoken)).status_code == 204
        assert mclient.get("/profiles/debug", headers=auth(mtoken)).status_code == 404

    def test_deleting_the_default_profile_is_a_conflict(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """REQ MOD-041 — it conflicts with an invariant, rather than being a
        malformed request the caller could rephrase."""
        response = mclient.delete("/profiles/default", headers=auth(mtoken))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "config_invalid"

    def test_deleting_an_unknown_profile_is_404(self, mclient: TestClient, mtoken: str) -> None:
        assert mclient.delete("/profiles/nope", headers=auth(mtoken)).status_code == 404

    def test_saving_is_audited(self, mclient: TestClient, mtoken: str, modular: ControlApp) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken, "ui"))
        entries, _ = modular.audit.entries()
        assert entries[0].action == "save_profile"


class TestProfileActivation:
    """REQ MOD-042 — a profile switch is a working context switch."""

    @pytest.fixture(autouse=True)
    def _modules(self, mclient: TestClient, mtoken: str) -> None:
        for name in ("tidy", "other"):
            mclient.post(
                "/modules",
                json={
                    "name": name,
                    "files": {"module.yaml": BLOCK_MANIFEST.replace("tidy", name)},
                },
                headers=auth(mtoken),
            )
            mclient.patch(f"/modules/{name}", json={"enabled": True}, headers=auth(mtoken))

    def test_activating_returns_the_new_state(self, mclient: TestClient, mtoken: str) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        payload = mclient.post("/profiles/debug/activate", headers=auth(mtoken)).json()
        assert payload["active_profile"] == "debug"

    def test_activating_narrows_the_rules_to_the_profiles_modules(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MOD-043 — this is the whole point of a profile: the same
        installed modules, a different subset running."""
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 2
        mclient.post("/profiles", json={"name": "debug", "modules": ["tidy"]}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        assert [r.module for r in modular.interceptor.evaluator.ruleset.short_circuit] == ["tidy"]

    def test_the_profiles_dev_toggles_come_with_it(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """Applying them separately is the step an operator forgets, and then
        spends an hour wondering why the page is still cached."""
        mclient.post(
            "/profiles",
            json={"name": "debug", "dev_toggles": {"anticache": True}},
            headers=auth(mtoken),
        )
        payload = mclient.post("/profiles/debug/activate", headers=auth(mtoken)).json()
        assert payload["dev_toggles"]["anticache"] is True
        assert modular.interceptor is not None
        assert modular.interceptor.dev_toggles["anticache"] is True

    def test_switching_back_to_default_clears_the_toggles_again(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """A context that outlived the profile it came from is a setting nobody
        can account for."""
        mclient.post(
            "/profiles",
            json={"name": "debug", "dev_toggles": {"anticache": True}},
            headers=auth(mtoken),
        )
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        payload = mclient.post("/profiles/default/activate", headers=auth(mtoken)).json()
        assert payload["dev_toggles"]["anticache"] is False

    def test_switching_back_to_default_restores_every_enabled_module(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.post("/profiles", json={"name": "debug", "modules": ["tidy"]}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        mclient.post("/profiles/default/activate", headers=auth(mtoken))
        assert modular.interceptor is not None
        assert len(modular.interceptor.evaluator.ruleset) == 2

    def test_activating_an_unknown_profile_is_404(self, mclient: TestClient, mtoken: str) -> None:
        assert mclient.post("/profiles/nope/activate", headers=auth(mtoken)).status_code == 404

    def test_a_failed_activation_leaves_the_previous_profile_in_force(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        mclient.post("/profiles/nope/activate", headers=auth(mtoken))
        assert mclient.get("/state", headers=auth(mtoken)).json()["active_profile"] == "debug"

    def test_activation_is_audited(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        """REQ MCP-031 — a profile switch changes what happens to traffic."""
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken, "mcp"))
        entries, _ = modular.audit.entries()
        assert entries[0].action == "activate_profile"
        assert entries[0].detail["profile"] == "debug"

    def test_activation_tells_connected_clients(
        self, mclient: TestClient, mtoken: str, modular: ControlApp
    ) -> None:
        mclient.post("/profiles", json={"name": "debug"}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        hub = modular.events
        assert isinstance(hub, RecordingHub)
        assert ("state.changed", {"active_profile": "debug"}) in hub.seen

    def test_deleting_the_active_profile_falls_back_to_default(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """Otherwise the daemon points at a profile that no longer exists."""
        mclient.post("/profiles", json={"name": "debug", "modules": []}, headers=auth(mtoken))
        mclient.post("/profiles/debug/activate", headers=auth(mtoken))
        mclient.delete("/profiles/debug", headers=auth(mtoken))
        assert mclient.get("/state", headers=auth(mtoken)).json()["active_profile"] == "default"


class TestProfileRoutesWithoutAStore:
    def test_listing_is_empty_rather_than_failing(self, client: TestClient, token: str) -> None:
        assert client.get("/profiles", headers=auth(token)).json() == []

    def test_a_named_profile_is_404(self, client: TestClient, token: str) -> None:
        assert client.get("/profiles/default", headers=auth(token)).status_code == 404

    def test_activation_is_404(self, client: TestClient, token: str) -> None:
        assert client.post("/profiles/default/activate", headers=auth(token)).status_code == 404


class TestModuleAndProfileRoutesAreGuarded:
    """The security layer applies to the routes that can change behaviour."""

    def test_listing_modules_needs_a_token(self, mclient: TestClient) -> None:
        assert mclient.get("/modules").status_code == 401

    def test_creating_a_module_needs_the_client_header(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        """REQ API-013 — a page you are visiting must not be able to install a
        module on your proxy."""
        response = mclient.post(
            "/modules",
            json={"name": "tidy", "files": {"module.yaml": MANIFEST}},
            headers={"Authorization": f"Bearer {mtoken}"},
        )
        assert response.status_code == 403

    def test_activating_a_profile_from_a_web_page_is_refused(
        self, mclient: TestClient, mtoken: str
    ) -> None:
        response = mclient.post(
            "/profiles/default/activate",
            headers={**auth(mtoken), "Origin": "https://evil.example"},
        )
        assert response.status_code == 403
