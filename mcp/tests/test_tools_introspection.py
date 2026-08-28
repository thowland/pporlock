"""Introspection family — REQ MCP-010, MCP-004, MCP-005."""

from __future__ import annotations

import pytest

from conftest import MASKED_COOKIE, FakeDaemon, flow
from pporlock_mcp.client import ControlClient
from pporlock_mcp.errors import ContractViolation
from pporlock_mcp.tools import (
    FLOW_LIST_DEFAULT,
    FLOW_LIST_MAX,
    STATS_SAMPLE_DEFAULT,
    WS_DEFAULT,
    ToolRegistry,
)

registry = ToolRegistry.build()


async def call(client: ControlClient, tool: str, /, **args: object) -> object:
    """Invoke one tool handler directly; the MCP envelope is tested separately."""
    return await registry.get(tool).handler(client, dict(args))


async def test_list_flows_defaults_to_summary_and_a_bounded_page(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-005 — the cheap representation is the default, not an option."""
    daemon.route("GET", "/flows", {"flows": [flow("f1")], "total_estimate": 1})
    await call(client, "list_flows")
    assert daemon.last.params["detail"] == "summary"
    assert daemon.last.params["limit"] == str(FLOW_LIST_DEFAULT)


async def test_list_flows_clamps_an_oversized_limit(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows", {"flows": []})
    await call(client, "list_flows", limit=100_000)
    assert daemon.last.params["limit"] == str(FLOW_LIST_MAX)


async def test_list_flows_passes_the_filter_vocabulary_through(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ CAP-004 — the same filter vocabulary as the UI and the DevTools panel."""
    daemon.route("GET", "/flows", {"flows": []})
    await call(client, "list_flows", host="example.com", blocked=True, note_code="csp_modified")
    params = daemon.last.params
    assert params["host"] == "example.com"
    assert params["note_code"] == "csp_modified"
    assert params["blocked"] == "true"


async def test_list_flows_forwards_a_cursor(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("GET", "/flows", {"flows": []})
    await call(client, "list_flows", cursor="c1")
    assert daemon.last.params["cursor"] == "c1"


async def test_list_flows_rejects_a_page_without_provenance(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-004."""
    daemon.route("GET", "/flows", {"flows": [flow("f1", provenance={})]})
    with pytest.raises(ContractViolation):
        await call(client, "list_flows")


async def test_redacted_values_survive_the_round_trip_unchanged(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-003 — the MCP server never resolves a mask back to plaintext."""
    daemon.route("GET", "/flows", {"flows": [flow("f1")]})
    page = await call(client, "list_flows")
    assert page["flows"][0]["request"]["headers"]["cookie"] == MASKED_COOKIE  # type: ignore[index]


async def test_get_flow_defaults_to_full_not_bodies(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-005 — bodies cost the most and are opt-in per flow."""
    daemon.route("GET", "/flows/f1", flow("f1"))
    await call(client, "get_flow", flow_id="f1")
    assert daemon.last.params["detail"] == "full"


async def test_get_flow_honours_an_explicit_bodies_request(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows/f1", flow("f1"))
    await call(client, "get_flow", flow_id="f1", detail="bodies")
    assert daemon.last.params["detail"] == "bodies"


async def test_get_provenance_returns_only_provenance(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """The cheapest answer to 'why did this break' — no headers, no bodies."""
    daemon.route("GET", "/flows/f1", flow("f1"))
    result = await call(client, "get_provenance", flow_id="f1")
    assert set(result) == {"flow_id", "url", "provenance"}  # type: ignore[arg-type]
    assert daemon.last.params["detail"] == "summary"


async def test_get_provenance_tolerates_a_flow_with_no_request_object(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows/f1", {"flow_id": "f1", "provenance": {"notes": []}})
    result = await call(client, "get_provenance", flow_id="f1")
    assert result["url"] is None  # type: ignore[index]


async def test_flow_stats_aggregates_a_bounded_sample(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-010, MCP-005 — counts, not flows."""
    daemon.route(
        "GET",
        "/flows",
        {
            "flows": [
                flow("f1", host="a.com", provenance={"notes": [{"code": "csp_modified"}]}),
                flow("f2", host="a.com", modified=True),
                flow("f3", host="b.com", status=404, blocked=True),
            ],
            "total_estimate": 3,
        },
    )
    stats = await call(client, "flow_stats")
    assert daemon.last.params["limit"] == str(STATS_SAMPLE_DEFAULT)
    assert stats["by_host"] == {"a.com": 2, "b.com": 1}  # type: ignore[index]
    assert stats["by_status"] == {"200": 2, "404": 1}  # type: ignore[index]
    assert stats["modified"] == 1 and stats["blocked"] == 1  # type: ignore[index]
    assert stats["notes"] == {"csp_modified": 1}  # type: ignore[index]


async def test_flow_stats_tolerates_flows_with_no_response(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows", {"flows": [{"flow_id": "f1", "provenance": {"notes": []}}]})
    stats = await call(client, "flow_stats")
    assert stats["by_status"] == {"-": 1}  # type: ignore[index]


async def test_websocket_frames_are_capped(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MCP-005 — a chatty socket must not become an unbounded tool result."""
    frames = [{"seq": i, "data": "x"} for i in range(300)]
    daemon.route("GET", "/flows/f1", flow("f1", websocket={"messages": frames}))
    result = await call(client, "list_websocket_messages", flow_id="f1")
    assert len(result["messages"]) == WS_DEFAULT  # type: ignore[index]
    assert result["total"] == 300  # type: ignore[index]


async def test_websocket_tool_on_a_non_websocket_flow_is_empty(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows/f1", flow("f1"))
    result = await call(client, "list_websocket_messages", flow_id="f1")
    assert result["messages"] == []  # type: ignore[index]


async def test_list_sessions(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("GET", "/sessions", [{"session_id": "s1", "name": "n", "state": "stopped"}])
    sessions = await call(client, "list_sessions")
    assert sessions[0]["session_id"] == "s1"  # type: ignore[index]


async def test_list_session_flows_uses_the_session_route_and_summary_detail(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/sessions/s1/flows", {"flows": [flow("f1")]})
    await call(client, "list_session_flows", session_id="s1", host="a.com", cursor="c")
    assert daemon.last.path == "/sessions/s1/flows"
    assert daemon.last.params["detail"] == "summary"
    assert daemon.last.params["cursor"] == "c"
