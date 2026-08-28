"""The MCP request/response plumbing, and the remaining client helpers.

These exercise the handlers the SDK calls, without a stdio transport: the
handlers are ordinary coroutines, so a hand-built params object is a complete
test of the envelope.
"""

from __future__ import annotations

from typing import Any

import mcp.types as types
import pytest

from conftest import FakeDaemon, flow, result_payload
from pporlock_mcp.client import ControlClient
from pporlock_mcp.errors import ConfigurationError, GuardrailError
from pporlock_mcp.server import PporlockMCP
from pporlock_mcp.tools import ToolRegistry, _disabled_on_create, _shape_dryrun


async def test_list_tools_handler_returns_the_registry(server: PporlockMCP) -> None:
    result = await server._on_list_tools(None, None)  # type: ignore[arg-type]
    assert isinstance(result, types.ListToolsResult)
    assert len(result.tools) == len(server.registry.specs)


async def test_call_tool_handler_unpacks_the_request_params(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows", {"flows": [flow("f1")]})
    params = types.CallToolRequestParams(name="list_flows", arguments={"limit": 5})
    result = await server._on_call_tool(None, params)  # type: ignore[arg-type]
    assert result_payload(result)["flows"][0]["flow_id"] == "f1"
    assert daemon.last.params["limit"] == "5"


async def test_call_tool_handler_tolerates_absent_arguments(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/sessions", [])
    params = types.CallToolRequestParams(name="list_sessions", arguments=None)
    result = await server._on_call_tool(None, params)  # type: ignore[arg-type]
    assert result_payload(result) == []


async def test_server_close_is_safe(server: PporlockMCP) -> None:
    await server.aclose()


async def test_client_put_and_patch_helpers(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("PUT", "/exclusions", {"entries": []})
    daemon.route("PATCH", "/modules/m", {"name": "m"})
    assert await client.put("/exclusions", {"entries": []}) == {"entries": []}
    assert await client.patch("/modules/m", {"enabled": False}) == {"name": "m"}


def test_registry_membership_and_read_only_message() -> None:
    """REQ MCP-032 — the refusal names the mode, not just the missing tool."""
    full = ToolRegistry.build()
    assert "create_module" in full
    assert "nonsense" not in full

    limited = ToolRegistry.build(read_only=True)
    with pytest.raises(GuardrailError) as excinfo:
        limited.get("create_module")
    assert "--read-only" in str(excinfo.value)

    with pytest.raises(GuardrailError) as excinfo:
        full.get("create_modul")
    assert "--read-only" not in str(excinfo.value)


@pytest.mark.parametrize("payload", ["not a dict", 42, None])
def test_shapers_pass_non_dict_payloads_through(payload: Any) -> None:
    """A daemon that answers with something unexpected must not crash the tool."""
    assert _shape_dryrun(payload, include_diffs=True) == payload
    assert _disabled_on_create(payload) == payload


def test_dry_run_shaper_leaves_non_dict_results_alone() -> None:
    out = _shape_dryrun({"summary": {}, "results": ["odd", {"flow_id": "f1"}]}, True)
    assert out["results"][0] == "odd"


def test_dry_run_shaper_leaves_a_diff_without_a_body_alone() -> None:
    out = _shape_dryrun(
        {"summary": {}, "results": [{"flow_id": "f1", "diff": {"headers": []}}]}, True
    )
    assert out["results"][0]["diff"] == {"headers": []}


def test_a_server_without_a_token_refuses_to_start() -> None:
    """A tokenless client would 401 on every call; failing at construction says why."""
    with pytest.raises(ConfigurationError):
        PporlockMCP()
