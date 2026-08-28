"""Daemon control family — REQ MCP-013, MCP-031."""

from __future__ import annotations

import pytest

from conftest import FakeDaemon
from pporlock_mcp.client import CLIENT_HEADER, ControlClient
from pporlock_mcp.errors import GuardrailError
from pporlock_mcp.tools import CONTROL, ToolRegistry

registry = ToolRegistry.build()


async def call(client: ControlClient, tool: str, /, **args: object) -> object:
    return await registry.get(tool).handler(client, dict(args))


async def test_get_status(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("GET", "/state", {"version": "0.1.0", "clients": {"mcp_connected": 1}})
    state = await call(client, "get_status")
    assert state["version"] == "0.1.0"  # type: ignore[index]


async def test_set_module_enabled_is_the_only_path_to_enablement(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-030 — a separate, explicit call, and a PATCH, not a create."""
    daemon.route("PATCH", "/modules/m", {"name": "m", "enabled": True})
    await call(client, "set_module_enabled", name="m", enabled=True)
    assert daemon.last.method == "PATCH"
    assert daemon.last.json_body == {"enabled": True}


async def test_enabling_a_module_is_tagged_for_the_audit_log(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-031 — enable, activate, and toggle are each audited with origin 'mcp'."""
    daemon.route("PATCH", "/modules/m", {"name": "m", "enabled": True})
    daemon.route("POST", "/profiles/p/activate", {"active": "p"})
    daemon.route("POST", "/state", {"dev_toggles": {"anticache": True}})

    await call(client, "set_module_enabled", name="m", enabled=True)
    await call(client, "activate_profile", name="p")
    await call(client, "set_dev_toggle", anticache=True)

    assert [r.headers[CLIENT_HEADER.lower()] for r in daemon.requests] == ["mcp", "mcp", "mcp"]


async def test_every_mutating_control_tool_sends_the_client_tag(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-031 — the tag is on the transport, so no tool can forget it."""
    daemon.route("PATCH", "/modules/m", {})
    daemon.route("POST", "/profiles/p/activate", {})
    daemon.route("POST", "/state", {})
    daemon.route("POST", "/sessions", {"session_id": "s1"}, status=201)
    daemon.route("POST", "/sessions/s1/stop", {"session_id": "s1"})
    daemon.route("POST", "/modules/reload", {"loaded": 1})
    daemon.route("GET", "/exclusions", {"entries": []})
    daemon.route("PUT", "/exclusions", {"entries": [{"pattern": "*.bank.com"}]})

    await call(client, "set_module_enabled", name="m", enabled=False)
    await call(client, "activate_profile", name="p")
    await call(client, "set_dev_toggle", anticomp=True)
    await call(client, "start_recording", name="repro")
    await call(client, "stop_recording", session_id="s1")
    await call(client, "reload_modules")
    await call(client, "edit_exclusions", add=["*.bank.com"])
    await call(client, "proxy_start")
    await call(client, "proxy_stop")

    mutating = [r for r in daemon.requests if r.method in {"POST", "PUT", "PATCH", "DELETE"}]
    assert mutating
    assert all(r.headers[CLIENT_HEADER.lower()] == "mcp" for r in mutating)


async def test_activate_profile_uses_the_activate_route(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/profiles/ad-blocking/activate", {"active": "ad-blocking"})
    await call(client, "activate_profile", name="ad-blocking")
    assert daemon.last.path == "/profiles/ad-blocking/activate"


async def test_list_profiles(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("GET", "/profiles", [{"name": "default"}])
    assert await call(client, "list_profiles") == [{"name": "default"}]


async def test_set_dev_toggle_sends_only_the_named_toggles(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/state", {"dev_toggles": {}})
    await call(client, "set_dev_toggle", anticache=True)
    assert daemon.last.json_body == {"dev_toggles": {"anticache": True}}


async def test_set_dev_toggle_with_no_toggle_is_refused(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    with pytest.raises(GuardrailError):
        await call(client, "set_dev_toggle")
    assert daemon.requests == []


async def test_start_and_stop_recording(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("POST", "/sessions", {"session_id": "s1", "state": "recording"}, status=201)
    daemon.route("POST", "/sessions/s1/stop", {"session_id": "s1", "state": "stopped"})
    started = await call(client, "start_recording", name="repro")
    assert daemon.last.json_body == {"name": "repro"}
    stopped = await call(client, "stop_recording", session_id=started["session_id"])  # type: ignore[index]
    assert stopped["state"] == "stopped"  # type: ignore[index]


async def test_edit_exclusions_preserves_untouched_entries(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """SPEC-0 §6.9 — PUT replaces the whole list, so the tool reads before writing."""
    daemon.route(
        "GET",
        "/exclusions",
        {"entries": [{"pattern": "*.bank.com", "comment": "pinned", "source": "default"}]},
    )
    daemon.route("PUT", "/exclusions", {"entries": []})
    await call(client, "edit_exclusions", add=["ads.example.com"], comment="why")
    written = daemon.last.json_body["entries"]
    assert {e["pattern"] for e in written} == {"*.bank.com", "ads.example.com"}
    assert written[0]["comment"] == "pinned"
    assert written[1]["comment"] == "why"


async def test_edit_exclusions_removes_by_pattern(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/exclusions", {"entries": [{"pattern": "a"}, {"pattern": "b"}]})
    daemon.route("PUT", "/exclusions", {"entries": []})
    await call(client, "edit_exclusions", remove=["a"])
    assert daemon.last.json_body["entries"] == [{"pattern": "b"}]


async def test_edit_exclusions_does_not_duplicate_an_existing_pattern(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/exclusions", {"entries": [{"pattern": "a", "comment": "keep"}]})
    daemon.route("PUT", "/exclusions", {"entries": []})
    await call(client, "edit_exclusions", add=["a"])
    assert daemon.last.json_body["entries"] == [{"pattern": "a", "comment": "keep"}]


async def test_edit_exclusions_with_nothing_to_do_is_refused(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    with pytest.raises(GuardrailError):
        await call(client, "edit_exclusions")
    assert daemon.requests == []


async def test_reload_modules(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("POST", "/modules/reload", {"loaded": 3})
    assert await call(client, "reload_modules") == {"loaded": 3}


async def test_proxy_start_and_stop_use_the_state_patch_shape(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """SPEC-0 §6.4 / openapi StatePatch — proxy_running is the documented field."""
    daemon.route("POST", "/state", {"proxy": {"running": True}})
    await call(client, "proxy_start")
    assert daemon.last.json_body == {"proxy_running": True}
    await call(client, "proxy_stop")
    assert daemon.last.json_body == {"proxy_running": False}


def test_control_family_tools_are_annotated_as_state_changing() -> None:
    """The one read-only member of the family is get_status; the rest change things."""
    control = [s for s in registry.specs if s.family == CONTROL]
    assert {s.name for s in control if not s.mutating} == {"get_status", "list_profiles"}
