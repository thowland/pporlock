"""JSON transforms — SPEC-0 §5.5.

A minimal RFC 6902 subset: add, remove, replace. Enough to strip a tracking
field or neutralise a flag, which is what the rule model needs; move, copy, and
test are omitted rather than half-implemented.
"""

from __future__ import annotations

import json
from typing import Any

from ...errors import TransformError

SUPPORTED_OPS = frozenset({"add", "remove", "replace"})


def _split(pointer: str) -> list[str]:
    """RFC 6901 JSON pointer to path segments."""
    if pointer in ("", "/"):
        return []
    if not pointer.startswith("/"):
        raise TransformError(f"JSON pointer must start with '/': {pointer!r}")
    return [seg.replace("~1", "/").replace("~0", "~") for seg in pointer[1:].split("/")]


def _navigate(document: Any, segments: list[str]) -> tuple[Any, str | int]:
    """Resolve to the container holding the final segment, and that key."""
    node = document
    for segment in segments[:-1]:
        if isinstance(node, list):
            node = node[int(segment)]
        elif isinstance(node, dict):
            node = node[segment]
        else:
            raise TransformError(f"cannot descend into {type(node).__name__} at {segment!r}")

    last = segments[-1]
    if isinstance(node, list):
        return node, len(node) if last == "-" else int(last)
    return node, last


def json_patch(text: str, params: Any) -> str:
    """Apply RFC 6902 operations to a JSON body.

    A body that is not valid JSON is left untouched and reported, rather than
    raising: a rule matching on path may legitimately encounter a non-JSON
    response, and failing the flow for that would be worse than doing nothing.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        params.note("module_error", "json_patch: body is not valid JSON; left unchanged")
        return text

    for operation in params.get("ops") or []:
        if not isinstance(operation, dict):
            raise TransformError(f"each op must be a mapping, got {type(operation).__name__}")
        op = str(operation.get("op", ""))
        if op not in SUPPORTED_OPS:
            raise TransformError(
                f"unsupported op {op!r}; pporlock supports {', '.join(sorted(SUPPORTED_OPS))}"
            )

        segments = _split(str(operation.get("path", "")))
        if not segments:
            raise TransformError("op path must address a member, not the whole document")

        try:
            container, key = _navigate(document, segments)
        except (KeyError, IndexError, ValueError):
            # A path that does not exist is not an error for remove/replace: the
            # rule is describing a shape the body may or may not have.
            continue

        if op == "remove":
            try:
                del container[key]
            except (KeyError, IndexError, TypeError):
                continue
        elif op == "add" and isinstance(container, list):
            container.insert(int(key), operation.get("value"))
        else:
            try:
                container[key] = operation.get("value")
            except (TypeError, IndexError):
                continue

    return json.dumps(document, separators=(",", ":"))
