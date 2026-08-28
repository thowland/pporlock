"""Authoring and validation families — REQ MCP-011, MCP-012, MCP-014, MCP-030."""

from __future__ import annotations

import pytest

from conftest import FakeDaemon
from pporlock_mcp.client import ControlClient
from pporlock_mcp.errors import GuardrailError
from pporlock_mcp.tools import (
    DIFF_TEXT_CAP,
    DRYRUN_LIMIT_DEFAULT,
    DRYRUN_LIMIT_MAX,
    DRYRUN_RESULTS_SHOWN,
    MODULE_FILE_CAP,
    ToolRegistry,
)

registry = ToolRegistry.build()

FILES = {"module.yaml": "name: candidate\napi_version: 1\n", "module.py": "def x(): pass\n"}


async def call(client: ControlClient, tool: str, /, **args: object) -> object:
    """Invoke one tool handler directly; the MCP envelope is tested separately."""
    return await registry.get(tool).handler(client, dict(args))


async def test_list_modules_includes_failures(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MOD-005 — a module missing because it would not parse is how you learn it failed."""
    daemon.route(
        "GET",
        "/modules",
        [{"name": "broken", "state": "load_error", "error": {"code": "yaml", "line": 3}}],
    )
    modules = await call(client, "list_modules")
    assert modules[0]["state"] == "load_error"  # type: ignore[index]


async def test_read_module_truncates_long_sources(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-005 — a big module is not silently poured into the context window."""
    daemon.route("GET", "/modules/big", {"name": "big", "files": {"module.py": "z" * 20_000}})
    module = await call(client, "read_module", name="big")
    assert len(module["files"]["module.py"]) == MODULE_FILE_CAP  # type: ignore[index]
    assert module["files"]["module.py__truncated"]["total_chars"] == 20_000  # type: ignore[index]


async def test_read_module_full_returns_everything(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/modules/big", {"name": "big", "files": {"module.py": "z" * 20_000}})
    module = await call(client, "read_module", name="big", full=True)
    assert len(module["files"]["module.py"]) == 20_000  # type: ignore[index]


async def test_read_module_without_files_is_passed_through(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("GET", "/modules/m", {"name": "m"})
    assert await call(client, "read_module", name="m") == {"name": "m"}


async def test_create_module_does_not_enable_it(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MCP-030 / MOD-033 — the load-bearing guardrail of this sprint.

    The daemon enforces it (SPEC-0 §6.6: creating never enables). This asserts
    the MCP layer neither asks for it nor reports a module as live when it is
    not, and tells the agent what the separate step is.
    """
    daemon.route("POST", "/modules", {"name": "candidate", "enabled": False}, status=201)
    created = await call(client, "create_module", name="candidate", files=FILES)

    assert daemon.last.json_body == {"name": "candidate", "files": FILES}
    assert "enabled" not in daemon.last.json_body  # nothing asked for enablement
    assert created["enabled"] is False  # type: ignore[index]
    assert "set_module_enabled" in created["next_step"]  # type: ignore[index]


async def test_create_module_reports_disabled_even_if_the_daemon_omits_the_field(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-030 — absence of the field is reported as disabled, never as enabled."""
    daemon.route("POST", "/modules", {"name": "candidate"}, status=201)
    created = await call(client, "create_module", name="candidate", files=FILES)
    assert created["enabled"] is False  # type: ignore[index]


async def test_no_authoring_tool_can_send_an_enabled_flag(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-030 — enablement is not reachable through create or update, at all.

    ``enabled`` is not in either tool's schema, and the handler builds the body
    from name and files only, so a caller passing it cannot smuggle it through.
    """
    daemon.route("POST", "/modules", {"name": "candidate"}, status=201)
    daemon.route("PUT", "/modules/candidate", {"name": "candidate"})

    for tool in ("create_module", "update_module"):
        spec = registry.get(tool)
        assert "enabled" not in spec.input_schema["properties"]
        assert spec.input_schema["additionalProperties"] is False
        await call(client, tool, name="candidate", files=FILES, enabled=True)
        assert set(daemon.last.json_body) == {"name", "files"}


async def test_update_module_targets_the_named_module(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("PUT", "/modules/candidate", {"name": "candidate", "enabled": True})
    await call(client, "update_module", name="candidate", files=FILES)
    assert daemon.last.method == "PUT"
    assert daemon.last.path == "/modules/candidate"


async def test_update_module_reports_the_daemons_enabled_state_honestly(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """Updating an already-enabled module does not disable it — nor claim it did."""
    daemon.route("PUT", "/modules/live", {"name": "live", "enabled": True})
    updated = await call(client, "update_module", name="live", files=FILES)
    assert updated["enabled"] is True  # type: ignore[index]


async def test_empty_files_is_refused_before_the_network(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    with pytest.raises(GuardrailError):
        await call(client, "create_module", name="m", files={})
    assert daemon.requests == []


async def test_non_string_file_contents_are_refused(client: ControlClient) -> None:
    with pytest.raises(GuardrailError):
        await call(client, "create_module", name="m", files={"module.py": 42})


async def test_delete_module(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("DELETE", "/modules/m", None)
    assert await call(client, "delete_module", name="m") == {"deleted": "m"}


async def test_suggest_rule_from_flow(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MCP-014 — mirrors the UI's create-rule-from-flow."""
    daemon.route("POST", "/flows/f1/suggest-rule", {"yaml": "- match: {...}", "rule": {}})
    result = await call(client, "suggest_rule_from_flow", flow_id="f1", intent="block")
    assert daemon.last.json_body == {"intent": "block"}
    assert "yaml" in result  # type: ignore[operator]


async def test_validate_module_installs_nothing(client: ControlClient, daemon: FakeDaemon) -> None:
    """REQ MCP-012 / API-027 — validation hits /validate, never /modules."""
    daemon.route("POST", "/validate", {"valid": False, "errors": [{"line": 3}]})
    await call(client, "validate_module", files=FILES)
    assert daemon.last.path == "/validate"
    assert [r.path for r in daemon.requests] == ["/validate"]


async def test_validate_module_sends_no_name_when_the_caller_gave_none(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """A validator must not report an error that is not in the thing validated.

    This defaulted to "candidate", so validating any manifest with a different
    name came back with module_name_mismatch — an error the tool had
    manufactured itself. Omitted, the daemon uses the manifest's own name,
    which is the only name that can be correct.
    """
    daemon.route("POST", "/validate", {"valid": True})
    await call(client, "validate_module", files=FILES)
    assert daemon.last.json_body == {"files": FILES}


async def test_validate_module_still_sends_an_explicit_name(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/validate", {"valid": True})
    await call(client, "validate_module", name="tidy", files=FILES)
    assert daemon.last.json_body == {"name": "tidy", "files": FILES}


async def test_dry_run_does_not_invent_a_module_name_either(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/sessions/live/dryrun", {"summary": {}, "results": []})
    await call(client, "dry_run", session_id="live", files=FILES)
    assert daemon.last.json_body["modules"] == [{"files": FILES}]


async def test_dry_run_sends_a_candidate_module(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("POST", "/sessions/s1/dryrun", {"summary": {"matched": 1}, "results": []})
    await call(client, "dry_run", session_id="s1", files=FILES, name="cand")
    body = daemon.last.json_body
    assert body["modules"][0]["name"] == "cand"
    assert body["limit"] == DRYRUN_LIMIT_DEFAULT
    assert body["include_diffs"] is False


async def test_dry_run_can_target_an_installed_module(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/sessions/s1/dryrun", {"summary": {}, "results": []})
    await call(client, "dry_run", session_id="s1", module_name="strip-sri")
    assert daemon.last.json_body["use_installed"] == ["strip-sri"]


async def test_dry_run_without_a_subject_is_refused(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    with pytest.raises(GuardrailError):
        await call(client, "dry_run", session_id="s1")
    assert daemon.requests == []


async def test_dry_run_clamps_its_limit(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route("POST", "/sessions/s1/dryrun", {"summary": {}, "results": []})
    await call(client, "dry_run", session_id="s1", module_name="m", limit=10_000)
    assert daemon.last.json_body["limit"] == DRYRUN_LIMIT_MAX


async def test_dry_run_caps_the_number_of_per_flow_results(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    """REQ MCP-005 — the summary answers 'is it clean'; a few diffs answer 'why not'."""
    results = [{"flow_id": f"f{i}", "diff": {}} for i in range(100)]
    daemon.route("POST", "/sessions/s1/dryrun", {"summary": {"matched": 100}, "results": results})
    out = await call(client, "dry_run", session_id="s1", module_name="m")
    assert out["results_shown"] == DRYRUN_RESULTS_SHOWN  # type: ignore[index]
    assert out["results_total"] == 100  # type: ignore[index]
    assert "results_note" in out  # type: ignore[operator]


async def test_dry_run_drops_diffs_unless_asked(client: ControlClient, daemon: FakeDaemon) -> None:
    daemon.route(
        "POST",
        "/sessions/s1/dryrun",
        {"summary": {}, "results": [{"flow_id": "f1", "diff": {"headers": []}}]},
    )
    out = await call(client, "dry_run", session_id="s1", module_name="m")
    assert "diff" not in out["results"][0]  # type: ignore[index]


async def test_dry_run_truncates_diff_text_when_asked_for_diffs(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route(
        "POST",
        "/sessions/s1/dryrun",
        {
            "summary": {},
            "results": [
                {
                    "flow_id": "f1",
                    "diff": {"headers": [], "body": {"kind": "unified", "text": "d" * 9_000}},
                }
            ],
        },
    )
    out = await call(client, "dry_run", session_id="s1", module_name="m", include_diffs=True)
    body = out["results"][0]["diff"]["body"]  # type: ignore[index]
    assert len(body["text"]) == DIFF_TEXT_CAP
    assert body["truncated"] is True


async def test_dry_run_tolerates_a_result_without_a_results_list(
    client: ControlClient, daemon: FakeDaemon
) -> None:
    daemon.route("POST", "/sessions/s1/dryrun", {"summary": {}})
    out = await call(client, "dry_run", session_id="s1", module_name="m")
    assert "results_shown" not in out  # type: ignore[operator]
