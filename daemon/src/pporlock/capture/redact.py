"""Redaction — SPEC-0 §9, SPEC-1 §6.4, REQ CAP-040/041/042/045.

Two application points, and the difference between them is the whole design:

* **Write time**, for sessions. A record is redacted before it reaches SQLite,
  so the secret never exists on disk at all (REQ CAP-045). Redacting on read
  would leave the plaintext in the file, in a WAL frame, and in freelist pages
  long after the session was "safely" browsed.
* **Serialize time**, for API, SSE, and MCP responses. The live ring buffer
  keeps unredacted values in memory, which is the only reason UI-side unmasking
  can exist at all (REQ CAP-043) — and the reason unmasking a session flow is
  not merely refused but impossible.

The masked format is fixed by SPEC-0 §9.1 and three clients parse it. Changing
it is a breaking change to all of them.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import re
from typing import Any

from ..config import RedactionConfig
from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from .records import FlowRecord

#: SPEC-0 §9.1. Length and a stable hash prefix, so two requests carrying the
#: same token are visibly the same without either being readable (REQ CAP-042).
MASK_TEMPLATE = "«redacted:sha1={digest},len={length}»"

#: Matches anything MASK_TEMPLATE produces. Used to keep an already-masked value
#: from being masked twice, and by tests asserting nothing raw escaped.
MASK_PATTERN = re.compile(r"^«redacted:sha1=[0-9a-f]{4},len=\d+»$")

#: How many hex characters of the digest survive. Four is SPEC-0's number: long
#: enough to distinguish values in practice, short enough to be useless for a
#: dictionary attack against a short secret.
DIGEST_CHARS = 4

_GLOB_CHARS = frozenset("*?[")


def mask(value: str | bytes) -> str:
    """The SPEC-0 §9.1 masked form of one value.

    SHA-1 is a shape-preserving fingerprint here, not a security primitive — the
    point is that equal values mask equally — so it is flagged as such rather
    than upgraded to something that would imply a guarantee it is not making.
    """
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:DIGEST_CHARS]
    return MASK_TEMPLATE.format(digest=digest, length=len(raw))


def is_masked(value: str) -> bool:
    """True if ``value`` is already a masked value."""
    return bool(MASK_PATTERN.match(value))


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    """Header-name match: case-insensitive, exact or glob (SPEC-0 §9.2)."""
    candidate = name.lower()
    for pattern in patterns:
        lowered = pattern.lower()
        if _GLOB_CHARS & set(lowered):
            if fnmatch.fnmatchcase(candidate, lowered):
                return True
        elif candidate == lowered:
            return True
    return False


def _key_matches(key: str, patterns: tuple[str, ...]) -> bool:
    """JSON-key match: case-insensitive substring (SPEC-0 §9.2).

    Substring rather than exact because the keys that carry secrets are named
    every way a hundred APIs could name them — ``authToken``, ``x-secret``,
    ``user_password`` — and an exact list would miss all three.
    """
    candidate = key.lower()
    return any(pattern.lower() in candidate for pattern in patterns)


class Redactor:
    """Applies a RedactionConfig to headers, JSON bodies, and whole records.

    Stateless apart from its configuration, so one instance is shared by the
    session writer thread and the serializer without locking.
    """

    __slots__ = ("cfg",)

    def __init__(self, cfg: RedactionConfig | None = None) -> None:
        self.cfg = cfg if cfg is not None else RedactionConfig()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # -- headers ---------------------------------------------------------

    def redact_headers(
        self, headers: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[tuple[str, str], ...], bool]:
        """Mask every header whose name matches. Returns the pairs and whether
        anything changed."""
        if not self.cfg.enabled:
            return headers, False
        out: list[tuple[str, str]] = []
        changed = False
        for name, value in headers:
            if value and not is_masked(value) and _matches(name, self.cfg.header_patterns):
                out.append((name, mask(value)))
                changed = True
            else:
                out.append((name, value))
        return tuple(out), changed

    # -- bodies ----------------------------------------------------------

    def redact_json_body(self, body: bytes | None) -> tuple[bytes | None, bool]:
        """Mask JSON values whose keys match. Non-JSON is returned untouched.

        A body that does not parse as JSON is left alone rather than blanked:
        blanking every unrecognised body would make sessions useless for the
        debugging they exist for, and the header rules already cover the
        credentials that travel outside bodies.
        """
        if not self.cfg.enabled or body is None or not body.strip():
            return body, False
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return body, False

        walked, changed = self._walk(parsed, masked_key=False)
        if not changed:
            return body, False
        return json.dumps(walked, ensure_ascii=False).encode("utf-8"), True

    def _walk(self, node: Any, *, masked_key: bool) -> tuple[Any, bool]:
        """Recursively mask matching values.

        ``masked_key`` propagates through lists so ``{"tokens": ["a", "b"]}``
        masks both elements: the key named the secret, and the list is just how
        many of them there are.
        """
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            changed = False
            for key, value in node.items():
                hit = _key_matches(str(key), self.cfg.json_key_patterns)
                walked, sub_changed = self._walk(value, masked_key=hit)
                out[str(key)] = walked
                changed = changed or sub_changed
            return out, changed
        if isinstance(node, list):
            results = [self._walk(item, masked_key=masked_key) for item in node]
            return [r[0] for r in results], any(r[1] for r in results)
        if masked_key and node is not None:
            text = node if isinstance(node, str) else json.dumps(node)
            if isinstance(node, str) and is_masked(node):
                return node, False
            return mask(text), True
        return node, False

    # -- whole records ---------------------------------------------------

    def redact_request(self, request: NormalizedRequest) -> tuple[NormalizedRequest, bool]:
        headers, header_changed = self.redact_headers(request.headers)
        body, body_changed = self.redact_json_body(request.body)
        if not (header_changed or body_changed):
            return request, False
        return dataclasses.replace(request, headers=headers, body=body), True

    def redact_response(self, response: NormalizedResponse) -> tuple[NormalizedResponse, bool]:
        headers, header_changed = self.redact_headers(response.headers)
        body, body_changed = self.redact_json_body(response.body)
        if not (header_changed or body_changed):
            return response, False
        return dataclasses.replace(response, headers=headers, body=body), True

    def redact_ws_message(self, message: WebSocketMessage) -> tuple[WebSocketMessage, bool]:
        """WebSocket payloads are redacted like bodies (REQ PXY-050)."""
        payload, changed = self.redact_json_body(message.payload)
        if not changed:
            return message, False
        return dataclasses.replace(message, payload=payload or b""), True

    def redact_record(self, record: FlowRecord) -> FlowRecord:
        """A redacted copy of ``record``. The original is never mutated.

        Copying matters: the caller is usually the session writer holding the
        same object the ring buffer holds, and mutating it in place would strip
        the live buffer of the values that make unmasking possible.
        """
        if not self.cfg.enabled:
            return record

        request = record.request
        response = record.response
        if request is not None:
            request, _ = self.redact_request(request)
        if response is not None:
            response, _ = self.redact_response(response)
        messages = [self.redact_ws_message(m)[0] for m in record.ws_messages]

        return dataclasses.replace(record, request=request, response=response, ws_messages=messages)

    def masks_header(self, name: str) -> bool:
        """Whether ``name`` would be masked. Used by the unmask path to reject a
        field path that never named a secret in the first place."""
        return self.cfg.enabled and _matches(name, self.cfg.header_patterns)


# ------------------------------------------------------------- unmasking ---
#
# REQ CAP-043. One value per call, by explicit field path, from the live ring
# buffer only. There is no bulk form and no wildcard on purpose: "reveal this
# one cookie because I am debugging this one request" is the supported action,
# and anything that could reveal a page of secrets in one call would be a
# different feature wearing the same name.


class FieldPathError(ValueError):
    """A field path that names nothing in this flow."""


def _index_of(segment: str) -> int | None:
    return int(segment) if segment.isdigit() else None


def _from_json(body: bytes | None, segments: list[str]) -> str:
    if body is None:
        raise FieldPathError("this flow has no body")
    try:
        node: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise FieldPathError("body is not JSON") from exc
    for segment in segments:
        index = _index_of(segment)
        if isinstance(node, list) and index is not None and index < len(node):
            node = node[index]
        elif isinstance(node, dict) and segment in node:
            node = node[segment]
        else:
            raise FieldPathError(f"no such field: {segment}")
    return node if isinstance(node, str) else json.dumps(node)


def _from_headers(headers: tuple[tuple[str, str], ...], segments: list[str]) -> str:
    if not segments:
        raise FieldPathError("a header name is required")
    name = segments[0].lower()
    values = [value for key, value in headers if key.lower() == name]
    if not values:
        raise FieldPathError(f"no header {segments[0]!r} on this flow")
    occurrence = _index_of(segments[1]) if len(segments) > 1 else 0
    if occurrence is None or occurrence >= len(values):
        raise FieldPathError(f"no occurrence {segments[1:]!r} of header {segments[0]!r}")
    return values[occurrence]


def resolve_field(record: FlowRecord, field_path: str) -> str:
    """The raw value at ``field_path`` in a live record.

    Accepted paths::

        request.headers.cookie            first Cookie header
        response.headers.set-cookie.2     third Set-Cookie header
        request.body.auth.token           a JSON body field, dotted
        response.body.items.0.secret      numeric segments index lists
        websocket.messages.3.payload      one frame's payload

    Raises FieldPathError for anything else. The caller has already established
    that this is a live flow and a UI request; this function's only job is to
    make "one value" literally true.
    """
    segments = [s for s in field_path.split(".") if s]
    if len(segments) < 2:
        raise FieldPathError(f"{field_path!r} is not a field path")
    root, kind, rest = segments[0], segments[1], segments[2:]

    if root == "request" and record.request is not None:
        if kind == "headers":
            return _from_headers(record.request.headers, rest)
        if kind == "body":
            return _from_json(record.request.body, rest)
    elif root == "response" and record.response is not None:
        if kind == "headers":
            return _from_headers(record.response.headers, rest)
        if kind == "body":
            return _from_json(record.response.body, rest)
    elif root == "websocket" and kind == "messages":
        index = _index_of(rest[0]) if rest else None
        if index is None or index >= len(record.ws_messages):
            raise FieldPathError(f"no websocket message {rest[:1]}")
        return record.ws_messages[index].payload.decode("utf-8", errors="replace")

    raise FieldPathError(f"{field_path!r} names nothing on this flow")
