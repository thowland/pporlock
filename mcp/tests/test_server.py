"""MCP protocol surface — REQ MCP-001, MCP-002, MCP-003, MCP-005, MCP-032."""

from __future__ import annotations

import json

import pytest

from conftest import FakeDaemon, flow, result_payload
from pporlock_mcp.server import INSTRUCTIONS, PporlockMCP, build_parser, main
from pporlock_mcp.tools import AUTHORING, CONTROL, INTROSPECTION, VALIDATION, build_tools

ALL_FAMILIES = {INTROSPECTION, AUTHORING, VALIDATION, CONTROL}


def test_all_four_tool_families_are_present() -> None:
    """REQ MCP-010 through MCP-013."""
    assert {spec.family for spec in build_tools()} == ALL_FAMILIES


def test_the_spec_1_tool_table_is_complete() -> None:
    """SPEC-1 §11.2 — every named tool exists, spelled as the spec spells it."""
    expected = {
        # introspection
        "list_flows",
        "get_flow",
        "get_provenance",
        "flow_stats",
        "list_websocket_messages",
        "list_sessions",
        "list_session_flows",
        # authoring
        "list_modules",
        "read_module",
        "create_module",
        "update_module",
        "delete_module",
        "suggest_rule_from_flow",
        # validation
        "validate_module",
        "dry_run",
        # control
        "get_status",
        "set_module_enabled",
        "activate_profile",
        "list_profiles",
        "set_dev_toggle",
        "start_recording",
        "stop_recording",
        "reload_modules",
        "edit_exclusions",
        "proxy_start",
        "proxy_stop",
    }
    assert {spec.name for spec in build_tools()} == expected


def test_every_tool_description_states_its_cost_or_its_guardrail() -> None:
    """REQ MCP-005 — the agent cannot budget for what it is not told."""
    for spec in build_tools():
        text = spec.description
        assert "COST:" in text or "REQ MCP-030" in text or "audit log" in text, spec.name


def test_listing_tools_document_a_bounded_default(server: PporlockMCP) -> None:
    """REQ MCP-005 — every listing tool names its default and its ceiling."""
    for name in ("list_flows", "list_session_flows", "list_websocket_messages", "flow_stats"):
        description = server.registry.get(name).description
        assert "default" in description and "max" in description, name


def test_instructions_state_both_guardrails() -> None:
    """REQ MCP-003, MCP-030 — an agent should not have to discover these by failing."""
    assert "cannot unmask" in INSTRUCTIONS
    assert "NOT enabled" in INSTRUCTIONS
    assert "unsandboxed" in INSTRUCTIONS


def test_read_only_mode_hides_the_write_families(readonly_server: PporlockMCP) -> None:
    """REQ MCP-032 — the tools are not advertised, not merely refused."""
    names = {tool.name for tool in readonly_server.list_tools()}
    assert names == {
        "list_flows",
        "get_flow",
        "get_provenance",
        "flow_stats",
        "list_websocket_messages",
        "list_sessions",
        "list_session_flows",
    }


async def test_read_only_mode_refuses_a_write_tool_by_name(
    readonly_server: PporlockMCP, daemon: FakeDaemon
) -> None:
    """REQ MCP-032 — and a client that calls one anyway gets told why."""
    result = await readonly_server.call_tool("create_module", {"name": "m", "files": {"a": "b"}})
    assert result.is_error
    payload = result_payload(result)
    assert payload["error"]["requirement"] == "MCP-032"
    assert daemon.requests == []


async def test_an_unmask_attempt_through_a_tool_fails(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    """REQ MCP-004 / CAP-043 — the headline guardrail, end to end.

    There is no unmask tool, so the only way to attempt one is to smuggle the
    parameter into a tool that takes a flow id. Both routes fail, and neither
    reaches the daemon.
    """
    daemon.route("GET", "/flows/f1", flow("f1"))

    missing = await server.call_tool("unmask", {"flow_id": "f1", "field": "cookie"})
    assert missing.is_error
    assert "unknown tool" in result_payload(missing)["error"]["message"]

    smuggled = await server.call_tool(
        "get_flow", {"flow_id": "f1", "unmask": "request.headers.cookie"}
    )
    assert smuggled.is_error
    assert result_payload(smuggled)["error"]["requirement"] == "MCP-003/CAP-043"
    assert not any("unmask" in r.params for r in daemon.requests)


def test_no_tool_schema_accepts_an_unmask_argument() -> None:
    """REQ MCP-004 — nothing in the advertised surface even suggests it is possible."""
    for spec in build_tools():
        assert "unmask" not in json.dumps(spec.input_schema).lower(), spec.name


async def test_a_successful_call_returns_json_text_content(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/flows", {"flows": [flow("f1")], "total_estimate": 1})
    result = await server.call_tool("list_flows", {})
    assert result.is_error in (False, None)
    assert result_payload(result)["total_estimate"] == 1


async def test_a_daemon_error_becomes_an_error_result_not_a_crash(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    """A stdio session that dies on a 404 is useless; the agent needs the reason."""
    result = await server.call_tool("get_flow", {"flow_id": "nope"})
    assert result.is_error
    assert result_payload(result)["error"]["status"] == 404


async def test_an_unexpected_exception_is_contained(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    """A missing required argument is a KeyError; it must not kill the session."""
    result = await server.call_tool("get_flow", {})
    assert result.is_error
    assert result_payload(result)["error"]["code"] == "unexpected"


async def test_a_contract_violation_is_reported_as_such(
    server: PporlockMCP, daemon: FakeDaemon
) -> None:
    """REQ MCP-004 — a flow with no provenance is a daemon bug, and says so."""
    daemon.route("GET", "/flows", {"flows": [flow("f1", provenance={})]})
    result = await server.call_tool("list_flows", {})
    assert result.is_error
    assert result_payload(result)["error"]["code"] == "contract_violation"


def test_tool_annotations_mark_reads_as_read_only(server: PporlockMCP) -> None:
    by_name = {tool.name: tool for tool in server.list_tools()}
    assert by_name["list_flows"].annotations is not None
    assert by_name["list_flows"].annotations.read_only_hint is True
    assert by_name["set_module_enabled"].annotations is not None
    assert by_name["set_module_enabled"].annotations.read_only_hint is False


def test_server_defaults_need_no_configuration() -> None:
    """REQ MCP-002 — startable with nothing but an installed daemon."""
    args = build_parser().parse_args([])
    assert args.base_url == "http://127.0.0.1:8081"
    assert args.read_only is False
    assert args.state_dir is None


def test_read_only_flag_is_parsed() -> None:
    assert build_parser().parse_args(["--read-only"]).read_only is True


def test_main_exits_cleanly_when_there_is_no_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PPORLOCK_TOKEN", raising=False)
    code = main(["--state-dir", str(tmp_path)])
    assert code == 2
    assert "configuration_error" in capsys.readouterr().err


def test_main_serves_and_closes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The stdio loop is exercised with the transport stubbed out."""
    monkeypatch.setenv("PPORLOCK_TOKEN", "t")
    served: list[str] = []

    async def fake_serve(self: PporlockMCP) -> None:
        served.append(self.base_url)

    monkeypatch.setattr(PporlockMCP, "serve_stdio", fake_serve)
    assert main(["--base-url", "http://127.0.0.1:9999"]) == 0
    assert served == ["http://127.0.0.1:9999"]
