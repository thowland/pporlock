"""Control API client behaviour — SPEC-0 §6.1, REQ MCP-001, MCP-003, MCP-031."""

from __future__ import annotations

import httpx
import pytest

from conftest import FakeDaemon
from pporlock_mcp.client import (
    CLIENT_HEADER,
    ControlClient,
    assert_no_forbidden_params,
    read_token,
)
from pporlock_mcp.errors import ConfigurationError, ControlApiError, GuardrailError


async def test_every_request_is_tagged_as_mcp(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MCP-031 — the audit log's origin field must be recorded, not guessed."""
    daemon.route("GET", "/state", {"version": "0.1.0"})
    await client.get("/state")
    assert daemon.last.headers[CLIENT_HEADER.lower()] == "mcp"


async def test_mutating_requests_carry_token_origin_and_content_type(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """SPEC-0 §6.1 — bearer token, allowed origin, and the non-simple client header."""
    daemon.route("POST", "/modules", {"name": "m", "enabled": False}, status=201)
    await client.post("/modules", {"name": "m", "files": {"module.yaml": "x"}})
    headers = daemon.last.headers
    assert headers["authorization"] == "Bearer test-token"
    assert headers["origin"] == "http://127.0.0.1:8081"
    assert headers[CLIENT_HEADER.lower()] == "mcp"
    assert headers["content-type"].startswith("application/json")


async def test_unmask_query_parameter_is_refused(client: ControlClient) -> None:
    """REQ MCP-003 / CAP-043 — MCP has no unmask path, including by passthrough."""
    with pytest.raises(GuardrailError) as excinfo:
        await client.request("GET", "/flows/f1", params={"unmask": "request.headers.cookie"})
    assert "cannot unmask" in str(excinfo.value)
    assert excinfo.value.requirement == "MCP-003/CAP-043"


@pytest.mark.parametrize("name", ["unmask", "UNMASK", "unredact", "reveal", "unmask_field"])
def test_every_unmask_spelling_is_refused(name: str) -> None:
    """REQ MCP-003 — the refusal is by parameter name, not by one exact spelling."""
    with pytest.raises(GuardrailError):
        assert_no_forbidden_params({name: "x"})


async def test_unmask_in_a_json_body_is_refused(client: ControlClient) -> None:
    """REQ MCP-003 — the body is as much a passthrough surface as the query string."""
    with pytest.raises(GuardrailError):
        await client.request("POST", "/flows", json={"unmask": "cookie"})


async def test_no_request_is_made_when_a_guardrail_refuses(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """The refusal happens before the network, so nothing reaches the daemon."""
    with pytest.raises(GuardrailError):
        await client.request("GET", "/flows/f1", params={"unmask": "cookie"})
    assert daemon.requests == []


async def test_none_valued_params_are_dropped(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("GET", "/flows", {"flows": []})
    await client.get("/flows", host=None, limit=10)
    assert daemon.last.params == {"limit": "10"}


async def test_daemon_error_body_is_carried_through(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """SPEC-0 §6.2 — the agent needs the daemon's real reason, not 'HTTP 400'."""
    daemon.route(
        "POST",
        "/modules",
        {"error": {"code": "module_load_failed", "message": "line 3: bad action", "detail": {}}},
        status=400,
    )
    with pytest.raises(ControlApiError) as excinfo:
        await client.post("/modules", {"name": "m"})
    assert excinfo.value.status == 400
    assert "line 3" in str(excinfo.value)
    assert excinfo.value.to_dict()["error"]["daemon"]["code"] == "module_load_failed"


async def test_non_json_error_body_is_tolerated(daemon: FakeDaemon) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlClient("http://127.0.0.1:8081", "t", http=http)
    with pytest.raises(ControlApiError) as excinfo:
        await client.get("/state")
    assert excinfo.value.status == 502
    assert excinfo.value.body == {}


async def test_204_returns_none(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("DELETE", "/modules/m", None)
    assert await client.delete("/modules/m") is None


async def test_unreachable_daemon_says_so(daemon: FakeDaemon) -> None:
    """The most common real failure: the daemon is not running."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ControlClient("http://127.0.0.1:8081", "t", http=http)
    with pytest.raises(ControlApiError) as excinfo:
        await client.get("/state")
    assert "cannot reach the pporlock daemon" in str(excinfo.value)
    assert excinfo.value.status == 0


async def test_context_manager_closes_owned_client() -> None:
    async with ControlClient("http://127.0.0.1:8081", "t") as client:
        assert client.token == "t"


async def test_injected_http_client_is_not_closed(client: ControlClient) -> None:
    """The fixture owns the transport; closing it out from under tests would break them."""
    await client.aclose()
    assert client._owns_http is False


def test_token_comes_from_the_environment_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PPORLOCK_TOKEN", "  env-token  ")
    assert read_token() == "env-token"


def test_token_is_read_from_the_state_directory(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ MCP-002 — no configuration beyond an installed daemon."""
    monkeypatch.delenv("PPORLOCK_TOKEN", raising=False)
    (tmp_path / "token").write_text("file-token\n")
    assert read_token(tmp_path) == "file-token"


def test_state_dir_env_var_is_honoured(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PPORLOCK_TOKEN", raising=False)
    monkeypatch.setenv("PPORLOCK_STATE_DIR", str(tmp_path))
    (tmp_path / "token").write_text("dir-token")
    assert read_token() == "dir-token"


def test_missing_token_is_an_actionable_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PPORLOCK_TOKEN", raising=False)
    with pytest.raises(ConfigurationError) as excinfo:
        read_token(tmp_path)
    assert "Is the daemon installed" in str(excinfo.value)


def test_empty_token_file_is_rejected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PPORLOCK_TOKEN", raising=False)
    (tmp_path / "token").write_text("   ")
    with pytest.raises(ConfigurationError):
        read_token(tmp_path)
