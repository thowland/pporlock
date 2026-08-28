"""Dry run, suggestion, reload and state over the control API.

SPEC-0 §6.4, §6.5, §6.6, §6.8. REQ CAP-030 through CAP-033, WUI-008, MCP-014,
MCP-033, MOD-004, and the closes for OI-3, OI-4 and OI-7.

Driven through the real app and the real middleware, because the shapes asserted
here are the ones ``web/src/api/client.ts`` and ``mcp/src/pporlock_mcp/tools.py``
already send and read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from starlette.testclient import TestClient

from pporlock.capture.records import FlowRecord
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import (
    INLINE_ROUTES,
    OFFLOAD_ROUTES,
    PUBLIC_ROUTES,
    ClientActivity,
    ControlApp,
)
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.modules.registry import ModuleRegistry

CSP_MODULE = """\
name: csp-strip
version: 1.0.0
pporlock_api: "1"
rules:
  - name: strip csp
    action: headers
    match:
      host: app.example.com
    response:
      remove: [content-security-policy]
"""


def record(flow_id: str = "f0") -> FlowRecord:
    request = NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="GET",
        host="app.example.com",
        port=443,
        path="/index.html",
        url="https://app.example.com/index.html",
        dest="document",
        headers=(("accept", "*/*"),),
    )
    response = NormalizedResponse(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:01.000Z",
        status=200,
        headers=(
            ("content-type", "text/html"),
            ("content-security-policy", "default-src 'self'"),
        ),
        body=b"<html><head></head></html>",
    )
    return FlowRecord(
        flow_id=flow_id,
        kind="http",
        started_at="2026-08-27T14:00:00.000Z",
        completed_at="2026-08-27T14:00:01.000Z",
        request=request,
        response=response,
    )


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    config = Config(state_dir=str(tmp_path))
    ring = RingBuffer()
    ring.add(record("f0"))
    registry = ModuleRegistry(Path(config.modules.root))
    registry.reload()
    return ControlApp(config, ring=ring, registry=registry)


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def headers(app: ControlApp) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.tokens.ensure()}", "X-Pporlock-Client": "ui"}


class TestDryRunRoute:
    def test_runs_against_the_live_ring(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ CAP-030
        response = client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={
                "modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}],
                "include_diffs": True,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["flows_evaluated"] == 1
        assert body["summary"]["matched"] == 1
        assert body["results"][0]["flow_id"] == "f0"

    def test_accepts_the_body_the_mcp_server_sends(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ MCP-012
        """``mcp/src/pporlock_mcp/tools.py::_dry_run`` builds exactly this."""
        response = client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={
                "limit": 200,
                "include_diffs": False,
                "profile": None,
                "modules": [{"name": "candidate", "files": {"module.yaml": CSP_MODULE}}],
            },
        )
        assert response.status_code == 200

    def test_returns_the_spec_0_result_shape(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # SPEC-0 §6.8, REQ CAP-033
        body = client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        ).json()
        assert set(body["summary"]) >= {
            "flows_evaluated",
            "matched",
            "modified",
            "blocked",
            "errors",
            "avg_ms",
            "p95_ms",
        }
        result = body["results"][0]
        assert set(result) >= {"flow_id", "url", "provenance", "diff"}
        assert set(result["diff"]) == {"headers", "body"}

    def test_provenance_travels_with_every_result(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ CAP-010, MCP-004
        body = client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        ).json()
        for result in body["results"]:
            assert result["provenance"]["entries"]

    def test_runs_against_a_recorded_session(
        self, app: ControlApp, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ CAP-030
        meta = app.sessions.start("dryrun-fixture")
        app.sessions.enqueue(record("s1"))
        app.sessions.stop(meta.session_id)

        response = client.post(
            f"/sessions/{meta.session_id}/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        )
        assert response.status_code == 200
        assert response.json()["summary"]["flows_evaluated"] == 1

    def test_an_unknown_session_is_404(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.post(
            "/sessions/does-not-exist/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        )
        assert response.status_code == 404

    def test_a_malformed_request_is_400(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.post("/sessions/live/dryrun", headers=headers, json={})
        assert response.status_code == 400

    def test_it_installs_nothing(
        self, app: ControlApp, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ MCP-030
        client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        )
        assert app.registry is not None
        assert app.registry.get("csp-strip") is None
        assert not (Path(app.config.modules.root) / "csp-strip").exists()

    def test_it_is_audited(
        self, app: ControlApp, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ MCP-031
        client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={"modules": [{"name": "csp-strip", "files": {"module.yaml": CSP_MODULE}}]},
        )
        entries, _ = app.audit.entries(10, None)
        assert any(e.action == "dry_run" for e in entries)

    def test_the_route_is_classified_as_offloaded(self) -> None:  # REQ API-002
        assert "/sessions/{session_id}/dryrun" in OFFLOAD_ROUTES

    def test_every_registered_route_is_classified(self, app: ControlApp) -> None:
        registered = {
            r.path  # type: ignore[attr-defined]
            for r in app.asgi.routes
            if hasattr(r, "path") and not hasattr(r, "app")
        }
        assert not registered - INLINE_ROUTES - OFFLOAD_ROUTES - PUBLIC_ROUTES


class TestSuggestRule:
    def test_suggests_a_block_rule(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ WUI-008, MCP-014
        response = client.post("/flows/f0/suggest-rule", headers=headers, json={"intent": "block"})
        assert response.status_code == 200
        body = response.json()
        # web/src/api/types.ts::SuggestedRule reads `rule`; the OpenAPI also
        # declares `yaml`. Both are returned.
        assert body["rule"]["action"] == "block"
        assert body["rule"]["match"]["host"] == "app.example.com"
        assert "block" in body["yaml"]
        assert body["module"] is None

    @pytest.mark.parametrize("intent", ["block", "map_local", "redirect", "headers"])
    def test_every_intent_produces_a_rule_that_compiles(
        self, client: TestClient, headers: dict[str, str], intent: str
    ) -> None:  # REQ WUI-008
        response = client.post("/flows/f0/suggest-rule", headers=headers, json={"intent": intent})
        assert response.status_code == 200
        assert response.json()["rule"]["action"] == intent

    def test_an_unknown_intent_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post(
            "/flows/f0/suggest-rule", headers=headers, json={"intent": "teleport"}
        )
        assert response.status_code == 400

    def test_an_unknown_flow_is_404(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.post(
            "/flows/nope/suggest-rule", headers=headers, json={"intent": "block"}
        )
        assert response.status_code == 404

    def test_a_non_mapping_body_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post("/flows/f0/suggest-rule", headers=headers, json="block")
        assert response.status_code == 400


class TestReloadResultShape:
    def test_reports_the_counts_the_web_ui_reads(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ MOD-004
        """web/src/api/types.ts::ReloadResult reads loaded, enabled,
        quarantined and errors."""
        response = client.post("/modules/reload", headers=headers, json={})
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"loaded", "enabled", "quarantined", "errors"}
        assert body["quarantined"] == 0


class TestStatePatch:
    def test_dev_toggles_still_apply(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ PXY-043
        body = client.post(
            "/state", headers=headers, json={"dev_toggles": {"anticache": True}}
        ).json()
        assert body["dev_toggles"]["anticache"] is True

    def test_an_unknown_key_is_refused_rather_than_discarded(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # OI-3
        """The route used to answer 200 and drop the key, so a caller was told
        an effect had happened when it had not."""
        response = client.post("/state", headers=headers, json={"listen_port": 9999})
        assert response.status_code == 400
        assert "listen_port" in response.json()["error"]["message"]

    def test_an_unknown_dev_toggle_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post(
            "/state", headers=headers, json={"dev_toggles": {"antigravity": True}}
        )
        assert response.status_code == 400

    def test_dev_toggles_must_be_a_mapping(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post("/state", headers=headers, json={"dev_toggles": ["anticache"]})
        assert response.status_code == 400

    def test_a_non_mapping_patch_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post("/state", headers=headers, json=["dev_toggles"])
        assert response.status_code == 400

    def test_proxy_running_without_a_proxy_is_a_conflict_not_a_lie(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # OI-3
        """A control server with no proxy attached cannot start or stop one, and
        says so with a 409 rather than returning a payload claiming success."""
        response = client.post("/state", headers=headers, json={"proxy_running": False})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "proxy_control_failed"

    def test_proxy_running_reaches_the_interceptor(
        self, app: ControlApp, client: TestClient, headers: dict[str, str]
    ) -> None:  # OI-3
        calls: list[bool] = []

        from pporlock.addon.interceptor import Counters

        class FakeInterceptor:
            counters: Any = Counters()
            uptime_s = 1.0
            dev_toggles: ClassVar[dict[str, bool]] = {}
            proxy_listening = False

            async def set_proxy_running(self, running: bool) -> bool:
                calls.append(running)
                return True

        app.interceptor = FakeInterceptor()  # type: ignore[assignment]
        response = client.post("/state", headers=headers, json={"proxy_running": False})
        assert response.status_code == 200
        assert calls == [False]
        # And the payload reports the listener, not "an object exists".
        assert response.json()["proxy"]["running"] is False


class TestClientActivity:
    def test_is_inactive_until_seen(self) -> None:  # REQ MCP-033, OI-4
        activity = ClientActivity(ttl=60.0)
        assert activity.to_dict()["mcp_connected"] == 0
        assert activity.to_dict()["mcp_last_seen"] is None

    def test_becomes_active_and_expires(self) -> None:  # REQ MCP-033, OI-4
        activity = ClientActivity(ttl=60.0)
        activity.touch("mcp", now=1000.0)
        assert activity.is_active("mcp", now=1030.0)
        assert not activity.is_active("mcp", now=1100.0)
        assert activity.to_dict(now=1030.0)["mcp_connected"] == 1
        assert activity.to_dict(now=1100.0)["mcp_connected"] == 0

    def test_last_seen_is_reported(self) -> None:
        activity = ClientActivity()
        activity.touch("mcp", now=1000.0)
        assert activity.last_seen("mcp") == 1000.0
        assert activity.to_dict(now=1000.0)["mcp_last_seen"] is not None

    def test_an_mcp_request_lights_the_indicator(
        self, client: TestClient, app: ControlApp
    ) -> None:  # REQ MCP-033, OI-4
        """No registration endpoint exists and none is needed: every MCP
        request already carries X-Pporlock-Client: mcp."""
        token = app.tokens.ensure()
        before = client.get("/state", headers={"Authorization": f"Bearer {token}"}).json()
        assert before["clients"]["mcp_connected"] == 0

        client.get(
            "/state",
            headers={"Authorization": f"Bearer {token}", "X-Pporlock-Client": "mcp"},
        )
        after = client.get("/state", headers={"Authorization": f"Bearer {token}"}).json()
        assert after["clients"]["mcp_connected"] == 1
        assert after["clients"]["mcp_last_seen"] is not None

    def test_read_only_is_reported_false_because_it_is_unobservable(
        self, client: TestClient, app: ControlApp
    ) -> None:  # OI-4
        """Nothing on the wire carries the MCP server's --read-only flag.
        Guessing it from the absence of mutating calls would read as a fact."""
        token = app.tokens.ensure()
        body = client.get("/state", headers={"Authorization": f"Bearer {token}"}).json()
        assert body["clients"]["mcp_read_only"] is False

    def test_a_bogus_client_header_on_a_read_does_not_reject_the_request(
        self, client: TestClient, app: ControlApp
    ) -> None:
        """Non-mutating requests are not required to identify themselves, so an
        unrecognised tag falls back rather than failing a read."""
        token = app.tokens.ensure()
        response = client.get(
            "/state",
            headers={"Authorization": f"Bearer {token}", "X-Pporlock-Client": "nonsense"},
        )
        assert response.status_code == 200
        assert response.json()["clients"]["mcp_connected"] == 0


class TestGetModuleHasOneNotFoundGuard:
    def test_no_duplicated_unreachable_guard(self) -> None:  # OI-7
        import inspect

        source = inspect.getsource(ControlApp.get_module)
        assert source.count("_not_found") == 1
