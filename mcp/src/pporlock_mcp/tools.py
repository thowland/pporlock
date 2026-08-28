"""The four tool families of SPEC-1 §11.2.

Every tool is a thin, documented shape over one or two control API calls. The
shaping that happens here is not convenience — it is REQ MCP-005. An agent pays
for every byte of a tool result, so:

* listing tools default to ``detail=summary`` and a bounded page size;
* bodies are never returned unless the caller asks for ``detail="bodies"``;
* long text (module sources, dry-run diffs) is truncated with the cut marked;
* every tool description states its default and its cost.

Read-only mode (REQ MCP-032) is enforced by family: only ``introspection`` tools
are registered when it is on, so the authoring, validation, and control tools do
not merely fail — they are not advertised.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .client import ControlClient
from .errors import GuardrailError
from .guardrails import (
    clamp,
    coerce_detail,
    require_provenance,
    summarize_notes,
    truncate_files,
    truncate_text,
)

# ------------------------------------------------------------------ caps ----
# Every number here is a token-budget decision (REQ MCP-005) and is repeated in
# the tool description the agent reads.
FLOW_LIST_DEFAULT = 50
FLOW_LIST_MAX = 200
WS_DEFAULT = 50
WS_MAX = 200
STATS_SAMPLE_DEFAULT = 200
STATS_SAMPLE_MAX = 1000
MODULE_FILE_CAP = 8_000
DRYRUN_LIMIT_DEFAULT = 200
DRYRUN_LIMIT_MAX = 500
DRYRUN_RESULTS_SHOWN = 20
DIFF_TEXT_CAP = 2_000

INTROSPECTION = "introspection"
AUTHORING = "authoring"
VALIDATION = "validation"
CONTROL = "control"

READ_ONLY_FAMILIES = frozenset({INTROSPECTION})

Handler = Callable[[ControlClient, dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    family: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    mutating: bool = False


# ------------------------------------------------------------- helpers ------
def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


_FILTER_PROPERTIES: dict[str, Any] = {
    "host": {"type": "string", "description": "Exact host match."},
    "path": {"type": "string"},
    "method": {"type": "string"},
    "status": {"type": "integer"},
    "content_type": {"type": "string"},
    "dest": {"type": "string", "description": "Sec-Fetch-Dest, e.g. script, document."},
    "tab_id": {"type": "integer", "description": "May be null for unattributed flows."},
    "modified": {"type": "boolean"},
    "blocked": {"type": "boolean"},
    "module": {"type": "string", "description": "Only flows this module fired on."},
    "note_code": {"type": "string", "description": "Provenance note code, e.g. csp_modified."},
    "since": {"type": "string"},
    "until": {"type": "string"},
    "q": {"type": "string", "description": "Substring over the URL."},
}

_DETAIL_PROPERTY = {
    "type": "string",
    "enum": ["summary", "full", "bodies"],
    "description": (
        "summary: no bodies, provenance collapsed to counts — cheapest. "
        "full: everything but bodies over the cap. "
        "bodies: full plus bodies — expensive, ask for it per flow, not per page."
    ),
}


def _filter_args(args: dict[str, Any]) -> dict[str, Any]:
    return {k: args[k] for k in _FILTER_PROPERTIES if k in args and args[k] is not None}


def _files_arg(args: dict[str, Any]) -> dict[str, str]:
    files = args.get("files")
    if not isinstance(files, dict) or not files:
        raise GuardrailError(
            "files must be a non-empty object mapping filename to contents, "
            "e.g. {'module.yaml': '...', 'module.py': '...'}",
            requirement="MOD-001",
        )
    out: dict[str, str] = {}
    for name, content in files.items():
        if not isinstance(content, str):
            raise GuardrailError(
                f"file {name!r} must be a string of file contents", requirement="MOD-001"
            )
        out[str(name)] = content
    return out


# ------------------------------------------------- introspection handlers ---
async def _list_flows(client: ControlClient, args: dict[str, Any]) -> Any:
    params = _filter_args(args)
    params["limit"] = clamp(args.get("limit"), FLOW_LIST_DEFAULT, FLOW_LIST_MAX)
    params["detail"] = coerce_detail(args.get("detail"), "summary")
    if args.get("cursor"):
        params["cursor"] = args["cursor"]
    page = await client.request("GET", "/flows", params=params)
    return require_provenance(page, where="list_flows")


async def _get_flow(client: ControlClient, args: dict[str, Any]) -> Any:
    flow_id = str(args["flow_id"])
    detail = coerce_detail(args.get("detail"), "full")
    flow = await client.request("GET", f"/flows/{flow_id}", params={"detail": detail})
    return require_provenance(flow, where="get_flow")


async def _get_provenance(client: ControlClient, args: dict[str, Any]) -> Any:
    flow_id = str(args["flow_id"])
    flow = await client.request("GET", f"/flows/{flow_id}", params={"detail": "summary"})
    require_provenance(flow, where="get_provenance")
    return {
        "flow_id": flow.get("flow_id", flow_id),
        "url": flow.get("request", {}).get("url")
        if isinstance(flow.get("request"), dict)
        else None,
        "provenance": flow.get("provenance"),
    }


async def _flow_stats(client: ControlClient, args: dict[str, Any]) -> Any:
    params = _filter_args(args)
    params["limit"] = clamp(args.get("sample"), STATS_SAMPLE_DEFAULT, STATS_SAMPLE_MAX)
    params["detail"] = "summary"
    page = await client.request("GET", "/flows", params=params)
    require_provenance(page, where="flow_stats")
    flows = [f for f in page.get("flows", []) if isinstance(f, dict)]

    by_host: dict[str, int] = {}
    statuses: dict[str, int] = {}
    modified = blocked = 0
    for flow in flows:
        raw_request = flow.get("request")
        request: dict[str, Any] = raw_request if isinstance(raw_request, dict) else {}
        host = str(request.get("host", "?"))
        by_host[host] = by_host.get(host, 0) + 1
        raw_response = flow.get("response")
        response: dict[str, Any] = raw_response if isinstance(raw_response, dict) else {}
        status = str(response.get("status", "-"))
        statuses[status] = statuses.get(status, 0) + 1
        if flow.get("modified"):
            modified += 1
        if flow.get("blocked"):
            blocked += 1

    return {
        "sampled": len(flows),
        "total_estimate": page.get("total_estimate"),
        "modified": modified,
        "blocked": blocked,
        "by_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])[:20]),
        "by_status": statuses,
        "notes": summarize_notes(flows),
        "note": "Aggregated from a bounded sample of summary-level flows (REQ MCP-005).",
    }


async def _list_websocket_messages(client: ControlClient, args: dict[str, Any]) -> Any:
    flow_id = str(args["flow_id"])
    limit = clamp(args.get("limit"), WS_DEFAULT, WS_MAX)
    flow = await client.request("GET", f"/flows/{flow_id}", params={"detail": "full"})
    require_provenance(flow, where="list_websocket_messages")
    raw_socket = flow.get("websocket")
    websocket: dict[str, Any] = raw_socket if isinstance(raw_socket, dict) else {}
    raw_messages = websocket.get("messages")
    messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
    return {
        "flow_id": flow_id,
        "total": len(messages),
        "limit": limit,
        "messages": messages[:limit],
        "provenance": flow.get("provenance"),
    }


async def _list_sessions(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("GET", "/sessions")


async def _list_session_flows(client: ControlClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    params = _filter_args(args)
    params["limit"] = clamp(args.get("limit"), FLOW_LIST_DEFAULT, FLOW_LIST_MAX)
    params["detail"] = coerce_detail(args.get("detail"), "summary")
    if args.get("cursor"):
        params["cursor"] = args["cursor"]
    page = await client.request("GET", f"/sessions/{session_id}/flows", params=params)
    return require_provenance(page, where="list_session_flows")


# ----------------------------------------------------- authoring handlers ---
async def _list_modules(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("GET", "/modules")


async def _read_module(client: ControlClient, args: dict[str, Any]) -> Any:
    name = str(args["name"])
    module = await client.request("GET", f"/modules/{name}")
    if args.get("full"):
        return module
    if isinstance(module, dict) and isinstance(module.get("files"), dict):
        module = dict(module)
        module["files"] = truncate_files(module["files"], MODULE_FILE_CAP)
    return module


async def _create_module(client: ControlClient, args: dict[str, Any]) -> Any:
    body = {"name": str(args["name"]), "files": _files_arg(args)}
    created = await client.request("POST", "/modules", json=body)
    return _disabled_on_create(created)


async def _update_module(client: ControlClient, args: dict[str, Any]) -> Any:
    name = str(args["name"])
    body = {"name": name, "files": _files_arg(args)}
    updated = await client.request("PUT", f"/modules/{name}", json=body)
    return _disabled_on_create(updated)


def _disabled_on_create(module: Any) -> Any:
    """Annotate the REQ MCP-030 guarantee in the response the agent reads.

    The daemon is what enforces it (SPEC-0 §6.6: create and update never
    enable). Restating it here is how the agent learns it needs a second,
    explicit call rather than assuming the module is live.
    """
    if not isinstance(module, dict):
        return module
    out = dict(module)
    out["enabled"] = bool(module.get("enabled", False))
    out["next_step"] = (
        "Not enabled. Creating or updating a module never enables it (REQ MCP-030). "
        "Dry-run it against a session first, then call set_module_enabled explicitly."
    )
    return out


async def _delete_module(client: ControlClient, args: dict[str, Any]) -> Any:
    name = str(args["name"])
    await client.request("DELETE", f"/modules/{name}")
    return {"deleted": name}


async def _suggest_rule_from_flow(client: ControlClient, args: dict[str, Any]) -> Any:
    flow_id = str(args["flow_id"])
    intent = str(args["intent"])
    return await client.request("POST", f"/flows/{flow_id}/suggest-rule", json={"intent": intent})


# ---------------------------------------------------- validation handlers ---
async def _validate_module(client: ControlClient, args: dict[str, Any]) -> Any:
    body = {"name": str(args.get("name", "candidate")), "files": _files_arg(args)}
    return await client.request("POST", "/validate", json=body)


async def _dry_run(client: ControlClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    include_diffs = bool(args.get("include_diffs", False))
    body: dict[str, Any] = {
        "limit": clamp(args.get("limit"), DRYRUN_LIMIT_DEFAULT, DRYRUN_LIMIT_MAX),
        "include_diffs": include_diffs,
        "profile": args.get("profile"),
    }
    if args.get("files"):
        body["modules"] = [{"name": str(args.get("name", "candidate")), "files": _files_arg(args)}]
    if args.get("module_name"):
        body["use_installed"] = [str(args["module_name"])]
    if "modules" not in body and "use_installed" not in body:
        raise GuardrailError(
            "dry_run needs either files (a candidate module) or module_name (an installed one)",
            requirement="MCP-012",
        )

    result = await client.request("POST", f"/sessions/{session_id}/dryrun", json=body)
    return _shape_dryrun(result, include_diffs)


def _shape_dryrun(result: Any, include_diffs: bool) -> Any:
    """Cap the per-flow results and the diff text (REQ MCP-005).

    The aggregate summary is what answers "is it clean yet"; the per-flow diffs
    are what answers "why not", and only a handful of them are needed to know.
    """
    if not isinstance(result, dict):
        return result
    out = dict(result)
    results = result.get("results")
    if not isinstance(results, list):
        return out

    shown = results[:DRYRUN_RESULTS_SHOWN]
    capped: list[Any] = []
    for item in shown:
        if not isinstance(item, dict):
            capped.append(item)
            continue
        entry = dict(item)
        diff = entry.get("diff")
        if not include_diffs:
            entry.pop("diff", None)
        elif isinstance(diff, dict) and isinstance(diff.get("body"), dict):
            body = dict(diff["body"])
            text = body.get("text")
            if isinstance(text, str):
                body["text"], cut = truncate_text(text, DIFF_TEXT_CAP)
                body["truncated"] = bool(body.get("truncated")) or cut
            entry["diff"] = {**diff, "body": body}
        capped.append(entry)

    out["results"] = capped
    out["results_shown"] = len(capped)
    out["results_total"] = len(results)
    if len(results) > len(capped):
        out["results_note"] = (
            f"Showing the first {DRYRUN_RESULTS_SHOWN} of {len(results)} affected flows "
            "(REQ MCP-005). Narrow the dry run with a smaller limit or inspect flows "
            "individually with get_flow."
        )
    return out


# ------------------------------------------------------- control handlers ---
async def _get_status(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("GET", "/state")


async def _set_module_enabled(client: ControlClient, args: dict[str, Any]) -> Any:
    name = str(args["name"])
    enabled = bool(args["enabled"])
    return await client.request("PATCH", f"/modules/{name}", json={"enabled": enabled})


async def _activate_profile(client: ControlClient, args: dict[str, Any]) -> Any:
    name = str(args["name"])
    return await client.request("POST", f"/profiles/{name}/activate", json={})


async def _list_profiles(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("GET", "/profiles")


async def _set_dev_toggle(client: ControlClient, args: dict[str, Any]) -> Any:
    toggles = {k: bool(args[k]) for k in ("anticache", "anticomp") if k in args}
    if not toggles:
        raise GuardrailError(
            "set_dev_toggle needs anticache and/or anticomp", requirement="MCP-013"
        )
    return await client.request("POST", "/state", json={"dev_toggles": toggles})


async def _start_recording(client: ControlClient, args: dict[str, Any]) -> Any:
    return await client.request("POST", "/sessions", json={"name": str(args["name"])})


async def _stop_recording(client: ControlClient, args: dict[str, Any]) -> Any:
    session_id = str(args["session_id"])
    return await client.request("POST", f"/sessions/{session_id}/stop", json={})


async def _reload_modules(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("POST", "/modules/reload", json={})


async def _edit_exclusions(client: ControlClient, args: dict[str, Any]) -> Any:
    """Read-modify-write, because the API replaces the whole list (SPEC-0 §6.9).

    Add and remove are expressed as patterns; the comment is preserved for
    entries that survive, because an unexplained exclusion is indistinguishable
    from a bug.
    """
    add = [str(p) for p in args.get("add", []) or []]
    remove = {str(p) for p in args.get("remove", []) or []}
    if not add and not remove:
        raise GuardrailError("edit_exclusions needs add and/or remove", requirement="MCP-013")

    current = await client.request("GET", "/exclusions")
    entries = list(current.get("entries", [])) if isinstance(current, dict) else []
    kept = [e for e in entries if not (isinstance(e, dict) and str(e.get("pattern")) in remove)]
    known = {str(e.get("pattern")) for e in kept if isinstance(e, dict)}
    for pattern in add:
        if pattern not in known:
            kept.append(
                {
                    "pattern": pattern,
                    "comment": str(args.get("comment", "added via MCP")),
                    "source": "user",
                }
            )
    return await client.request("PUT", "/exclusions", json={"entries": kept})


async def _proxy_start(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("POST", "/state", json={"proxy_running": True})


async def _proxy_stop(client: ControlClient, _: dict[str, Any]) -> Any:
    return await client.request("POST", "/state", json={"proxy_running": False})


# --------------------------------------------------------- the tool table ---
_FLOW_FILTER_SCHEMA: dict[str, Any] = dict(_FILTER_PROPERTIES)


def build_tools() -> list[ToolSpec]:
    """The full tool table. Order is the order the client sees."""
    tools: list[ToolSpec] = [
        ToolSpec(
            name="list_flows",
            family=INTROSPECTION,
            description=(
                "List captured flows from the live ring buffer, filtered. "
                f"COST: defaults to detail='summary' and limit={FLOW_LIST_DEFAULT} "
                f"(max {FLOW_LIST_MAX}) — no request or response bodies, provenance "
                "collapsed to counts. Ask for bodies one flow at a time with get_flow. "
                "All values are redacted (REQ CAP-040); there is no way to unmask them here."
            ),
            input_schema=_obj(
                {
                    **_FLOW_FILTER_SCHEMA,
                    "limit": {
                        "type": "integer",
                        "description": f"1..{FLOW_LIST_MAX}, default {FLOW_LIST_DEFAULT}.",
                    },
                    "cursor": {"type": "string"},
                    "detail": _DETAIL_PROPERTY,
                }
            ),
            handler=_list_flows,
        ),
        ToolSpec(
            name="get_flow",
            family=INTROSPECTION,
            description=(
                "One flow with its full provenance. COST: defaults to detail='full' — "
                "headers and provenance entries but no large bodies. Pass detail='bodies' "
                "only when you need the payload; it can be tens of kilobytes."
            ),
            input_schema=_obj(
                {"flow_id": {"type": "string"}, "detail": _DETAIL_PROPERTY}, ["flow_id"]
            ),
            handler=_get_flow,
        ),
        ToolSpec(
            name="get_provenance",
            family=INTROSPECTION,
            description=(
                "Provenance only for one flow: which module and rule did what, in which "
                "phase, with what outcome, plus the silent-breakage notes. COST: the "
                "cheapest way to answer 'why did this page break' — no headers, no bodies."
            ),
            input_schema=_obj({"flow_id": {"type": "string"}}, ["flow_id"]),
            handler=_get_provenance,
        ),
        ToolSpec(
            name="flow_stats",
            family=INTROSPECTION,
            description=(
                "Aggregate counts over a filtered slice: per host, per status, modified, "
                "blocked, and a histogram of provenance note codes. "
                f"COST: samples up to {STATS_SAMPLE_DEFAULT} summary flows by default "
                f"(max {STATS_SAMPLE_MAX}) and returns counts only."
            ),
            input_schema=_obj(
                {
                    **_FLOW_FILTER_SCHEMA,
                    "sample": {
                        "type": "integer",
                        "description": (
                            f"Flows to aggregate, default {STATS_SAMPLE_DEFAULT}, "
                            f"max {STATS_SAMPLE_MAX}."
                        ),
                    },
                }
            ),
            handler=_flow_stats,
        ),
        ToolSpec(
            name="list_websocket_messages",
            family=INTROSPECTION,
            description=(
                "WebSocket frames captured for one flow. "
                f"COST: default {WS_DEFAULT} frames, max {WS_MAX}. Frame payloads are "
                "redacted like any other captured data."
            ),
            input_schema=_obj(
                {
                    "flow_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "description": f"1..{WS_MAX}, default {WS_DEFAULT}.",
                    },
                },
                ["flow_id"],
            ),
            handler=_list_websocket_messages,
        ),
        ToolSpec(
            name="list_sessions",
            family=INTROSPECTION,
            description=(
                "Recorded sessions with their state, flow count, and size. "
                "COST: metadata only, one small object per session."
            ),
            input_schema=_obj({}),
            handler=_list_sessions,
        ),
        ToolSpec(
            name="list_session_flows",
            family=INTROSPECTION,
            description=(
                "Flows from a recorded session, same filter vocabulary as list_flows. "
                f"COST: detail='summary' and limit={FLOW_LIST_DEFAULT} (max {FLOW_LIST_MAX}) "
                "by default. Session data is redacted at write time and can never be "
                "unmasked, by any client (REQ CAP-043)."
            ),
            input_schema=_obj(
                {
                    "session_id": {"type": "string"},
                    **_FLOW_FILTER_SCHEMA,
                    "limit": {"type": "integer"},
                    "cursor": {"type": "string"},
                    "detail": _DETAIL_PROPERTY,
                },
                ["session_id"],
            ),
            handler=_list_session_flows,
        ),
        # ------------------------------------------------------- authoring ---
        ToolSpec(
            name="list_modules",
            family=AUTHORING,
            description=(
                "Every module the daemon knows about, including ones that failed to load "
                "and ones quarantined after repeated failures. COST: one status object "
                "per module, no file contents."
            ),
            input_schema=_obj({}),
            handler=_list_modules,
        ),
        ToolSpec(
            name="read_module",
            family=AUTHORING,
            description=(
                "A module's manifest, rules, and Python source. "
                f"COST: each file is truncated to {MODULE_FILE_CAP} characters with the "
                "cut marked; pass full=true for the whole thing."
            ),
            input_schema=_obj(
                {
                    "name": {"type": "string"},
                    "full": {
                        "type": "boolean",
                        "description": "Return untruncated file contents. Can be large.",
                    },
                },
                ["name"],
            ),
            handler=_read_module,
        ),
        ToolSpec(
            name="create_module",
            family=AUTHORING,
            description=(
                "Create a module from its files. DOES NOT ENABLE IT (REQ MCP-030): the "
                "module is written to disk and loaded, but affects no traffic until you "
                "call set_module_enabled explicitly. Module Python runs unsandboxed with "
                "full access to intercepted traffic — write it as you would trusted code. "
                "COST: returns a status object, not the files back."
            ),
            input_schema=_obj(
                {
                    "name": {"type": "string"},
                    "files": {
                        "type": "object",
                        "description": (
                            "Filename to contents. Recognized: module.yaml, module.py."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                ["name", "files"],
            ),
            handler=_create_module,
            mutating=True,
        ),
        ToolSpec(
            name="update_module",
            family=AUTHORING,
            description=(
                "Replace a module's files. DOES NOT ENABLE IT and does not change its "
                "enabled state (REQ MCP-030). COST: returns a status object only."
            ),
            input_schema=_obj(
                {
                    "name": {"type": "string"},
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                ["name", "files"],
            ),
            handler=_update_module,
            mutating=True,
        ),
        ToolSpec(
            name="delete_module",
            family=AUTHORING,
            description=("Remove a module and its files. COST: negligible."),
            input_schema=_obj({"name": {"type": "string"}}, ["name"]),
            handler=_delete_module,
            mutating=True,
        ),
        ToolSpec(
            name="suggest_rule_from_flow",
            family=AUTHORING,
            description=(
                "A candidate rule matching one observed flow, for a stated intent "
                "(REQ MCP-014). Starting point, not a finished module. COST: one rule."
            ),
            input_schema=_obj(
                {
                    "flow_id": {"type": "string"},
                    "intent": {
                        "type": "string",
                        "enum": ["block", "map_local", "redirect", "headers"],
                    },
                },
                ["flow_id", "intent"],
            ),
            handler=_suggest_rule_from_flow,
        ),
        # ------------------------------------------------------ validation ---
        ToolSpec(
            name="validate_module",
            family=VALIDATION,
            description=(
                "Check a candidate module's schema and Python syntax WITHOUT installing "
                "it. Nothing is written and nothing is enabled. COST: errors with line "
                "numbers only."
            ),
            input_schema=_obj(
                {
                    "name": {"type": "string"},
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                ["files"],
            ),
            handler=_validate_module,
        ),
        ToolSpec(
            name="dry_run",
            family=VALIDATION,
            description=(
                "Evaluate a candidate module (files) or an installed one (module_name) "
                "against a recorded session, touching no live traffic. Executes the "
                "module's Python hooks. "
                f"COST: evaluates {DRYRUN_LIMIT_DEFAULT} flows by default "
                f"(max {DRYRUN_LIMIT_MAX}); returns the aggregate summary plus at most "
                f"{DRYRUN_RESULTS_SHOWN} per-flow results. include_diffs defaults to "
                f"false; when true, diff text is capped at {DIFF_TEXT_CAP} characters."
            ),
            input_schema=_obj(
                {
                    "session_id": {"type": "string"},
                    "name": {"type": "string", "description": "Name for the candidate."},
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "module_name": {"type": "string", "description": "An installed module."},
                    "profile": {"type": ["string", "null"]},
                    "limit": {"type": "integer"},
                    "include_diffs": {"type": "boolean"},
                },
                ["session_id"],
            ),
            handler=_dry_run,
            mutating=True,
        ),
        # --------------------------------------------------------- control ---
        ToolSpec(
            name="get_status",
            family=CONTROL,
            description=(
                "Daemon status: proxy state, active profile, dev toggles, module counts "
                "and load errors, capture counters. COST: one small object."
            ),
            input_schema=_obj({}),
            handler=_get_status,
            mutating=False,
        ),
        ToolSpec(
            name="set_module_enabled",
            family=CONTROL,
            description=(
                "Enable or disable a module. THIS IS THE STEP THAT TOUCHES LIVE BROWSING. "
                "It is deliberately separate from create_module and update_module "
                "(REQ MCP-030) and is recorded in the audit log as originating from MCP "
                "(REQ MCP-031). Dry-run first."
            ),
            input_schema=_obj(
                {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
                ["name", "enabled"],
            ),
            handler=_set_module_enabled,
            mutating=True,
        ),
        ToolSpec(
            name="activate_profile",
            family=CONTROL,
            description=(
                "Activate a profile, changing which modules are live. Recorded in the "
                "audit log as originating from MCP (REQ MCP-031)."
            ),
            input_schema=_obj({"name": {"type": "string"}}, ["name"]),
            handler=_activate_profile,
            mutating=True,
        ),
        ToolSpec(
            name="list_profiles",
            family=CONTROL,
            description="Available profiles. COST: one small object per profile.",
            input_schema=_obj({}),
            handler=_list_profiles,
        ),
        ToolSpec(
            name="set_dev_toggle",
            family=CONTROL,
            description=(
                "Set the anticache and/or anticomp development toggles. These change "
                "every request while on, and are recorded in the audit log (REQ MCP-031)."
            ),
            input_schema=_obj({"anticache": {"type": "boolean"}, "anticomp": {"type": "boolean"}}),
            handler=_set_dev_toggle,
            mutating=True,
        ),
        ToolSpec(
            name="start_recording",
            family=CONTROL,
            description=(
                "Start recording a session. Recording is opt-in and writes redacted flows "
                "to disk. COST: returns session metadata."
            ),
            input_schema=_obj({"name": {"type": "string"}}, ["name"]),
            handler=_start_recording,
            mutating=True,
        ),
        ToolSpec(
            name="stop_recording",
            family=CONTROL,
            description="Stop a recording session. COST: returns session metadata.",
            input_schema=_obj({"session_id": {"type": "string"}}, ["session_id"]),
            handler=_stop_recording,
            mutating=True,
        ),
        ToolSpec(
            name="reload_modules",
            family=CONTROL,
            description=(
                "Force a reload of all modules from disk. Does not change any module's "
                "enabled state. COST: returns the reload result."
            ),
            input_schema=_obj({}),
            handler=_reload_modules,
            mutating=True,
        ),
        ToolSpec(
            name="edit_exclusions",
            family=CONTROL,
            description=(
                "Add or remove ClientHello exclusion patterns (hosts tunneled without "
                "decryption). Removing a certificate-pinning or financial host may break "
                "that site and will draw its traffic into the capture buffer. "
                "COST: reads the current list and writes it back; returns the new list."
            ),
            input_schema=_obj(
                {
                    "add": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                    "comment": {
                        "type": "string",
                        "description": "Why. An unexplained exclusion looks like a bug.",
                    },
                }
            ),
            handler=_edit_exclusions,
            mutating=True,
        ),
        ToolSpec(
            name="proxy_start",
            family=CONTROL,
            description=(
                "Start the proxy listener. Affects all browsing immediately. "
                "COST: negligible; returns daemon state."
            ),
            input_schema=_obj({}),
            handler=_proxy_start,
            mutating=True,
        ),
        ToolSpec(
            name="proxy_stop",
            family=CONTROL,
            description=(
                "Stop the proxy listener. Interception ceases. "
                "COST: negligible; returns daemon state."
            ),
            input_schema=_obj({}),
            handler=_proxy_stop,
            mutating=True,
        ),
    ]
    return tools


@dataclass(frozen=True, slots=True)
class ToolRegistry:
    """The tools this server exposes, after the read-only filter (REQ MCP-032)."""

    read_only: bool = False
    _by_name: dict[str, ToolSpec] = field(default_factory=dict)

    @classmethod
    def build(cls, *, read_only: bool = False) -> ToolRegistry:
        specs = [
            spec for spec in build_tools() if not read_only or spec.family in READ_ONLY_FAMILIES
        ]
        return cls(read_only=read_only, _by_name={spec.name: spec for spec in specs})

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def get(self, name: str) -> ToolSpec:
        spec = self._by_name.get(name)
        if spec is None:
            raise GuardrailError(
                f"unknown tool {name!r}"
                + (
                    " — the authoring, validation, and control families are unavailable "
                    "because this server was started with --read-only"
                    if self.read_only
                    else ""
                ),
                requirement="MCP-032",
            )
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._by_name
