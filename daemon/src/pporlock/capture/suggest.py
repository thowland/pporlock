"""Rule suggestion from a captured flow — REQ WUI-008, MCP-014.

Turns "this request is the problem" into a rule that matches it and nothing
else obviously adjacent. The output is a starting point an author edits, not a
finished rule, and it is deliberately *narrow*: a suggestion that matched more
than the flow it came from would be a rule the author did not read and did not
intend.

The candidate is compiled before it is returned. A suggestion that would not
load is worse than no suggestion, because the author would spend their time
debugging our text rather than their intent.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..engine.ruleset import compile_rule
from ..errors import ConfigError
from .records import FlowRecord

#: The four intents SPEC-2 §7.4 offers from a flow.
INTENTS = ("block", "map_local", "redirect", "headers")

_SAFE_NAME = re.compile(r"[^a-z0-9]+")


def _slug(value: str, fallback: str) -> str:
    slug = _SAFE_NAME.sub("-", value.lower()).strip("-")
    return slug[:48] or fallback


def _match_for(record: FlowRecord) -> dict[str, Any]:
    """The narrowest match that still describes the flow.

    Host and exact path, plus method when it is not a GET. The path is anchored
    so a rule suggested from ``/a.js`` does not also catch ``/a.js.map``.
    """
    request = record.request
    if request is None:
        raise ConfigError("this flow has no request to build a rule from")

    match: dict[str, Any] = {"host": request.host, "path": f"^{re.escape(request.path)}$"}
    if request.method.upper() != "GET":
        match["method"] = request.method.upper()
    if request.dest:
        match["dest"] = request.dest
    return match


def suggest_rule(record: FlowRecord, intent: str) -> dict[str, Any]:
    """A candidate rule for ``record``, as a dict, YAML, and compiled check."""
    if intent not in INTENTS:
        raise ConfigError(
            f"unknown intent {intent!r}; expected one of {', '.join(INTENTS)}",
            setting="intent",
        )
    request = record.request
    if request is None:
        raise ConfigError("this flow has no request to build a rule from")

    match = _match_for(record)
    base = _slug(f"{intent}-{request.host}-{request.path}", intent)
    rule: dict[str, Any] = {"name": base, "action": intent, "match": match}

    if intent == "block":
        # ``stub: auto`` synthesises a reply of the right shape for the
        # destination, which is what stops a blocked script breaking the page
        # more loudly than the thing it was blocking (REQ MOD-016).
        rule["mode"] = "stub"
        rule["stub"] = "auto"
    elif intent == "map_local":
        rule["file"] = _asset_name(request.path)
    elif intent == "redirect":
        rule["to"] = {"host": request.host, "path": request.path}
    elif intent == "headers":
        rule["response"] = {"remove": ["content-security-policy"]}

    # Compiled before it leaves: a suggestion that would not load is worse than
    # no suggestion at all.
    compile_rule(rule, module="suggested", index=0)

    return {
        "rule": rule,
        "yaml": yaml.safe_dump([rule], sort_keys=False, default_flow_style=False),
        "module": None,
        "flow_id": record.flow_id,
        "intent": intent,
    }


def _asset_name(path: str) -> str:
    """A plausible asset filename for a map_local suggestion."""
    tail = path.rsplit("/", 1)[-1] or "index.html"
    cleaned = _SAFE_NAME.sub("-", tail.lower()).strip("-")
    if "." in tail:
        stem, _, suffix = tail.rpartition(".")
        cleaned = f"{_slug(stem, 'asset')}.{_slug(suffix, 'bin')}"
    return cleaned or "asset.bin"


__all__ = ["INTENTS", "suggest_rule"]
