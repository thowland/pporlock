"""Sessions, redaction, unmasking, and export over the control API.

SPEC-0 §6.8, §9; SPEC-1 §6.3, §6.4. REQ CAP-020, CAP-021, CAP-023, CAP-024, CAP-040 through CAP-045.

Driven through the real app and the real middleware, so the access-control
tests exercise the same path a browser or an MCP client would.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pporlock.capture.records import FlowRecord
from pporlock.capture.redact import is_masked
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import INLINE_ROUTES, OFFLOAD_ROUTES, ControlApp
from pporlock.engine.models import NormalizedRequest, NormalizedResponse

COOKIE_SECRET = "session=9f3ac1de4b7711efbc1f0242ac120002"
BEARER_SECRET = "Bearer eyJzdXBlci1zZWNyZXQtdmFsdWV9"
BODY_SECRET = "correct-horse-battery-staple"


def secret_record(flow_id: str = "f0") -> FlowRecord:
    request = NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="POST",
        host="api.example.com",
        port=443,
        path="/v1/login",
        url="https://api.example.com/v1/login",
        headers=(
            ("accept", "*/*"),
            ("cookie", COOKIE_SECRET),
            ("authorization", BEARER_SECRET),
        ),
        body=json.dumps({"user": "tim", "password": BODY_SECRET}).encode(),
    )
    response = NormalizedResponse(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:01.000Z",
        status=200,
        headers=(("content-type", "application/json"), ("set-cookie", COOKIE_SECRET)),
        body=json.dumps({"access_token": BODY_SECRET}).encode(),
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
    config = Config()
    config.state_dir = str(tmp_path)
    ring = RingBuffer()
    ring.add(secret_record("f0"))
    return ControlApp(config, ring=ring)


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def token(app: ControlApp) -> str:
    return app.tokens.ensure()


def auth(token: str, client_name: str = "cli") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Pporlock-Client": client_name}


def record_session(client: TestClient, token: str, app: ControlApp, name: str = "s") -> str:
    created = client.post("/sessions", json={"name": name}, headers=auth(token))
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    app.sessions.enqueue(secret_record("f0"))
    stopped = client.post(f"/sessions/{session_id}/stop", headers=auth(token))
    assert stopped.status_code == 200
    return str(session_id)


class TestSerializeTimeRedaction:
    """REQ CAP-040. Every representation that leaves the daemon is redacted."""

    def test_the_flow_list_is_masked(self, client: TestClient, token: str) -> None:
        payload = client.get("/flows", headers=auth(token)).json()
        headers = dict(payload["flows"][0]["request"]["headers"])
        assert is_masked(headers["cookie"])
        assert is_masked(headers["authorization"])
        assert headers["accept"] == "*/*"

    def test_the_flow_detail_is_masked_including_bodies(
        self, client: TestClient, token: str
    ) -> None:
        payload = client.get("/flows/f0?detail=bodies", headers=auth(token)).json()
        assert BODY_SECRET not in json.dumps(payload)
        assert is_masked(json.loads(payload["request"]["body"])["password"])

    def test_the_redacted_flag_reports_what_happened(self, client: TestClient, token: str) -> None:
        """SPEC-0 §3.4 — clients render masked values differently, so they must
        be told whether redaction was applied."""
        assert client.get("/flows/f0", headers=auth(token)).json()["redacted"] is True

    def test_no_secret_appears_anywhere_in_the_response_text(
        self, client: TestClient, token: str
    ) -> None:
        body = client.get("/flows?detail=bodies", headers=auth(token)).text
        assert COOKIE_SECRET not in body
        assert BEARER_SECRET not in body
        assert BODY_SECRET not in body

    def test_the_ring_buffer_still_holds_the_raw_value(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """Serialization redacts a copy. If it redacted in place, the second
        read would differ from the first and unmasking would be impossible."""
        client.get("/flows/f0?detail=bodies", headers=auth(token))
        record = app.ring.get("f0")
        assert record is not None and record.request is not None
        assert record.request.header("cookie") == COOKIE_SECRET


class TestUnmask:
    """REQ CAP-043, MCP-003."""

    def test_reveals_one_value_from_a_live_flow(self, client: TestClient, token: str) -> None:
        response = client.get("/flows/f0?unmask=request.headers.cookie", headers=auth(token, "ui"))
        assert response.status_code == 200
        assert response.json()["value"] == COOKIE_SECRET
        assert response.json()["field_path"] == "request.headers.cookie"

    def test_reveals_only_the_value_asked_for(self, client: TestClient, token: str) -> None:
        payload = client.get(
            "/flows/f0?unmask=request.headers.cookie", headers=auth(token, "ui")
        ).json()
        assert BEARER_SECRET not in json.dumps(payload)
        assert BODY_SECRET not in json.dumps(payload)

    def test_a_body_field_can_be_unmasked(self, client: TestClient, token: str) -> None:
        payload = client.get(
            "/flows/f0?unmask=request.body.password", headers=auth(token, "ui")
        ).json()
        assert payload["value"] == BODY_SECRET

    def test_requires_the_bearer_token(self, client: TestClient) -> None:
        response = client.get(
            "/flows/f0?unmask=request.headers.cookie", headers={"X-Pporlock-Client": "ui"}
        )
        assert response.status_code == 401

    def test_is_refused_for_the_mcp_client(self, client: TestClient, token: str) -> None:
        """REQ MCP-003. The MCP interface has no unmask capability, and the
        server half of that must hold even if a build tried to call the URL."""
        response = client.get("/flows/f0?unmask=request.headers.cookie", headers=auth(token, "mcp"))
        assert response.status_code == 403
        assert COOKIE_SECRET not in response.text

    @pytest.mark.parametrize("client_name", ["mcp", "cli", "extension"])
    def test_only_the_ui_may_unmask(self, client: TestClient, token: str, client_name: str) -> None:
        response = client.get(
            "/flows/f0?unmask=request.headers.cookie", headers=auth(token, client_name)
        )
        assert response.status_code == 403

    def test_a_missing_client_header_is_refused(self, client: TestClient, token: str) -> None:
        """Without the header the origin field of the audit entry would be a
        guess, and unmasking is precisely the action that must be attributable."""
        response = client.get(
            "/flows/f0?unmask=request.headers.cookie",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403

    def test_an_unknown_field_path_is_a_404(self, client: TestClient, token: str) -> None:
        response = client.get("/flows/f0?unmask=request.headers.x-nope", headers=auth(token, "ui"))
        assert response.status_code == 404

    def test_an_evicted_flow_is_a_404(self, client: TestClient, token: str) -> None:
        response = client.get(
            "/flows/gone?unmask=request.headers.cookie", headers=auth(token, "ui")
        )
        assert response.status_code == 404

    def test_it_is_audited_without_recording_the_value(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """REQ MCP-031. An audit log that quoted what it protected would be the
        leak it exists to make visible."""
        client.get("/flows/f0?unmask=request.headers.cookie", headers=auth(token, "ui"))
        entries, _ = app.audit.entries()
        entry = entries[0]
        assert entry.action == "unmask"
        assert entry.origin == "ui"
        assert entry.detail["field_path"] == "request.headers.cookie"
        assert COOKIE_SECRET not in json.dumps(entry.to_dict())

    def test_the_response_is_never_cached(self, client: TestClient, token: str) -> None:
        response = client.get("/flows/f0?unmask=request.headers.cookie", headers=auth(token, "ui"))
        assert response.headers["cache-control"] == "no-store"


class TestUnmaskIsUnavailableForSessions:
    """REQ CAP-043. Not refused — unavailable, because the value is not there."""

    def test_no_session_route_offers_unmask(self, app: ControlApp) -> None:
        session_routes = {
            r.path  # type: ignore[attr-defined]
            for r in app.asgi.routes
            if str(getattr(r, "path", "")).startswith("/sessions")
        }
        assert session_routes
        assert not any("unmask" in path for path in session_routes)

    def test_an_unmask_parameter_on_a_session_listing_is_simply_ignored(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        response = client.get(
            f"/sessions/{session_id}/flows?detail=bodies&unmask=request.headers.cookie",
            headers=auth(token, "ui"),
        )
        assert response.status_code == 200
        assert COOKIE_SECRET not in response.text
        assert BEARER_SECRET not in response.text

    def test_the_session_flow_id_cannot_reach_the_live_unmask_path(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """The unmask handler reads the ring buffer only. A flow that exists
        solely in a session is a 404 there, and its stored value is masked."""
        session_id = record_session(client, token, app)
        app.ring.clear()
        response = client.get("/flows/f0?unmask=request.headers.cookie", headers=auth(token, "ui"))
        assert response.status_code == 404

        stored = app.sessions.reader(session_id).get("f0")
        assert stored is not None and stored.request is not None
        assert is_masked(stored.request.header("cookie") or "")


class TestSessionRoutes:
    def test_start_stop_list_get_delete(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        assert client.get("/sessions", headers=auth(token)).json() == []

        created = client.post("/sessions", json={"name": "morning"}, headers=auth(token))
        assert created.status_code == 201
        session_id = created.json()["session_id"]
        assert created.json()["state"] == "recording"

        app.sessions.enqueue(secret_record("f0"))
        stopped = client.post(f"/sessions/{session_id}/stop", headers=auth(token))
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "stopped"
        assert stopped.json()["flow_count"] == 1

        listing = client.get("/sessions", headers=auth(token)).json()
        assert [s["session_id"] for s in listing] == [session_id]

        assert client.get(f"/sessions/{session_id}", headers=auth(token)).status_code == 200
        assert client.delete(f"/sessions/{session_id}", headers=auth(token)).status_code == 204
        assert client.get("/sessions", headers=auth(token)).json() == []

    def test_recording_is_off_by_default(self, client: TestClient, token: str) -> None:
        """REQ CAP-020 — opt-in."""
        state = client.get("/state", headers=auth(token)).json()
        assert state["capture"]["recording_session"] is None

    def test_state_names_the_recording_session(self, client: TestClient, token: str) -> None:
        created = client.post("/sessions", json={"name": "live"}, headers=auth(token))
        state = client.get("/state", headers=auth(token)).json()
        assert state["capture"]["recording_session"] == created.json()["session_id"]

    def test_a_second_start_is_a_conflict(self, client: TestClient, token: str) -> None:
        client.post("/sessions", json={"name": "a"}, headers=auth(token))
        second = client.post("/sessions", json={"name": "b"}, headers=auth(token))
        assert second.status_code == 409

    def test_stopping_something_not_recording_is_a_conflict(
        self, client: TestClient, token: str
    ) -> None:
        assert client.post("/sessions/sabc/stop", headers=auth(token)).status_code == 409

    def test_rename(self, client: TestClient, token: str, app: ControlApp) -> None:
        """REQ CAP-021."""
        session_id = record_session(client, token, app)
        renamed = client.patch(
            f"/sessions/{session_id}", json={"name": "the CSP bug"}, headers=auth(token)
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "the CSP bug"

    def test_rename_refuses_anything_but_the_name(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        response = client.patch(
            f"/sessions/{session_id}", json={"flow_count": 0}, headers=auth(token)
        )
        assert response.status_code == 400

    def test_unknown_sessions_are_404(self, client: TestClient, token: str) -> None:
        assert client.get("/sessions/sabsent", headers=auth(token)).status_code == 404
        assert client.delete("/sessions/sabsent", headers=auth(token)).status_code == 404
        assert (
            client.patch("/sessions/sabsent", json={"name": "x"}, headers=auth(token)).status_code
            == 404
        )

    def test_a_traversal_id_is_refused_rather_than_resolved(
        self, client: TestClient, token: str
    ) -> None:
        response = client.get("/sessions/..%2F..%2Fetc%2Fpasswd", headers=auth(token))
        assert response.status_code in (400, 404)

    def test_session_flows_are_paged_and_filtered(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        created = client.post("/sessions", json={"name": "p"}, headers=auth(token))
        session_id = created.json()["session_id"]
        for i in range(10):
            app.sessions.enqueue(secret_record(f"f{i}"))
        client.post(f"/sessions/{session_id}/stop", headers=auth(token))

        page = client.get(f"/sessions/{session_id}/flows?limit=4", headers=auth(token)).json()
        assert len(page["flows"]) == 4
        assert page["next_cursor"] is not None
        assert page["total_estimate"] == 10

        filtered = client.get(
            f"/sessions/{session_id}/flows?host=nowhere.example", headers=auth(token)
        ).json()
        assert filtered["flows"] == []

    def test_session_flows_for_an_unknown_session_is_404(
        self, client: TestClient, token: str
    ) -> None:
        assert client.get("/sessions/sabsent/flows", headers=auth(token)).status_code == 404

    def test_sessions_require_a_token(self, client: TestClient) -> None:
        assert client.get("/sessions").status_code == 401

    def test_starting_a_session_is_audited(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        client.post("/sessions", json={"name": "audited"}, headers=auth(token))
        entries, _ = app.audit.entries()
        assert entries[0].action == "start_session"


class TestExport:
    """REQ CAP-024."""

    def test_har_export_carries_masked_values_only(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        response = client.get(f"/sessions/{session_id}/export?format=har", headers=auth(token))
        assert response.status_code == 200
        assert COOKIE_SECRET not in response.text
        assert BEARER_SECRET not in response.text
        assert BODY_SECRET not in response.text

        log = response.json()["log"]
        assert log["version"] == "1.2"
        assert log["creator"]["name"] == "pporlock"
        entry = log["entries"][0]
        assert entry["request"]["method"] == "POST"
        assert entry["response"]["status"] == 200
        names = {h["name"] for h in entry["request"]["headers"]}
        assert "cookie" in names

    def test_har_carries_provenance_in_the_tool_namespace(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """HAR cannot represent provenance, which is the whole diagnostic
        value; it travels in the underscore namespace HAR reserves for tools."""
        session_id = record_session(client, token, app)
        entry = client.get(f"/sessions/{session_id}/export?format=har", headers=auth(token)).json()[
            "log"
        ]["entries"][0]
        assert entry["_pporlock"]["flow_id"] == "f0"
        assert "provenance" in entry["_pporlock"]

    def test_native_export_preserves_provenance(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        payload = client.get(
            f"/sessions/{session_id}/export?format=pporlock", headers=auth(token)
        ).json()
        assert payload["format"] == "pporlock-session"
        assert payload["session"]["session_id"] == session_id
        assert payload["flows"][0]["provenance"]["profile"] == "default"

    def test_native_export_carries_masked_values_only(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        response = client.get(f"/sessions/{session_id}/export?format=pporlock", headers=auth(token))
        assert COOKIE_SECRET not in response.text
        assert BODY_SECRET not in response.text
        assert "«redacted:sha1=" in response.text

    def test_the_default_format_is_native(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        payload = client.get(f"/sessions/{session_id}/export", headers=auth(token)).json()
        assert payload["format"] == "pporlock-session"

    def test_an_unknown_format_is_refused(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        session_id = record_session(client, token, app)
        response = client.get(f"/sessions/{session_id}/export?format=pcap", headers=auth(token))
        assert response.status_code == 400

    def test_exporting_an_unknown_session_is_404(self, client: TestClient, token: str) -> None:
        assert client.get("/sessions/sabsent/export", headers=auth(token)).status_code == 404

    def test_export_is_never_cached(self, client: TestClient, token: str, app: ControlApp) -> None:
        session_id = record_session(client, token, app)
        response = client.get(f"/sessions/{session_id}/export", headers=auth(token))
        assert response.headers["cache-control"] == "no-store"


class TestConfigurableRedaction:
    """REQ CAP-044 — configurable, and the effective configuration is visible."""

    def test_the_effective_configuration_is_readable(self, client: TestClient, token: str) -> None:
        payload = client.get("/config", headers=auth(token)).json()
        assert payload["redaction"]["enabled"] is True
        assert "cookie" in payload["redaction"]["header_patterns"]
        assert "password" in payload["redaction"]["json_key_patterns"]

    def test_patterns_can_be_changed_and_read_back(self, client: TestClient, token: str) -> None:
        response = client.put(
            "/config",
            json={"redaction": {"header_patterns": ["x-tenant-id"]}},
            headers=auth(token),
        )
        assert response.status_code == 200
        assert response.json()["redaction"]["header_patterns"] == ["x-tenant-id"]
        assert client.get("/config", headers=auth(token)).json()["redaction"][
            "header_patterns"
        ] == ["x-tenant-id"]

    def test_a_new_pattern_takes_effect_immediately(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        """The Redactor is shared, so a change lands in the serializer and the
        session writer at once rather than in whichever was rebuilt."""
        client.put(
            "/config", json={"redaction": {"header_patterns": ["accept"]}}, headers=auth(token)
        )
        headers = dict(client.get("/flows/f0", headers=auth(token)).json()["request"]["headers"])
        assert is_masked(headers["accept"])
        # And the old pattern no longer applies.
        assert headers["cookie"] == COOKIE_SECRET

    def test_redaction_can_be_turned_off_and_the_flag_says_so(
        self, client: TestClient, token: str
    ) -> None:
        client.put("/config", json={"redaction": {"enabled": False}}, headers=auth(token))
        payload = client.get("/flows/f0", headers=auth(token)).json()
        assert payload["redacted"] is False

    def test_the_change_is_persisted(self, client: TestClient, token: str, app: ControlApp) -> None:
        client.put(
            "/config",
            json={"redaction": {"json_key_patterns": ["pin"]}},
            headers=auth(token),
        )
        saved = Path(app.config.state_dir) / "config.yaml"
        assert saved.is_file()
        assert "pin" in saved.read_text()

    def test_the_bind_address_cannot_be_changed_at_runtime(
        self, client: TestClient, token: str
    ) -> None:
        """A listener is already bound by the time this route is reachable, so
        accepting a new one would either be a lie or a way to move a
        loopback-only listener (REQ API-010)."""
        response = client.put(
            "/config", json={"control": {"listen_host": "0.0.0.0"}}, headers=auth(token)
        )
        assert response.status_code == 400

    def test_an_invalid_value_leaves_the_running_config_untouched(
        self, client: TestClient, token: str, app: ControlApp
    ) -> None:
        before = app.config.logging.level
        response = client.put("/config", json={"logging": {"level": "chatty"}}, headers=auth(token))
        assert response.status_code == 400
        assert app.config.logging.level == before

    def test_a_non_mapping_body_is_refused(self, client: TestClient, token: str) -> None:
        assert client.put("/config", json=[1, 2], headers=auth(token)).status_code == 400

    def test_the_change_is_audited(self, client: TestClient, token: str, app: ControlApp) -> None:
        client.put("/config", json={"redaction": {"enabled": True}}, headers=auth(token))
        entries, _ = app.audit.entries()
        assert entries[0].action == "put_config"


class TestLoopDiscipline:
    """DD-3. Everything added this sprint touches SQLite or the filesystem."""

    @pytest.mark.parametrize(
        "path",
        [
            "/sessions",
            "/sessions/{session_id}",
            "/sessions/{session_id}/stop",
            "/sessions/{session_id}/flows",
            "/sessions/{session_id}/export",
        ],
    )
    def test_session_routes_offload(self, path: str) -> None:
        assert path in OFFLOAD_ROUTES
        assert path not in INLINE_ROUTES

    def test_every_route_is_still_classified(self, app: ControlApp) -> None:
        from pporlock.control.app import PUBLIC_ROUTES

        registered = {r.path for r in app.asgi.routes}  # type: ignore[attr-defined]
        assert not registered - INLINE_ROUTES - OFFLOAD_ROUTES - PUBLIC_ROUTES
