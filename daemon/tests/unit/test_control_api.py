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
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList

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
