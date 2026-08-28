"""Wire serialization — SPEC-0 §3.4, §6.3.

Every flow that leaves the daemon goes through here. No route serializes a flow
itself, so detail levels and (from Sprint 13) redaction are applied uniformly
rather than per-route.

Detail levels exist because bodies dominate response size. A list of 500 flows
at ``bodies`` detail is tens of megabytes; the same list at ``summary`` is a few
hundred kilobytes. The MCP tools default to summary for exactly this reason
(REQ MCP-005).
"""

from __future__ import annotations

from typing import Any, Literal

from ..capture.records import FlowRecord
from ..capture.redact import Redactor
from ..capture.ring import encode_body
from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage

DetailLevel = Literal["summary", "full", "bodies"]
VALID_DETAILS: frozenset[str] = frozenset({"summary", "full", "bodies"})

DEFAULT_LIST_DETAIL: DetailLevel = "summary"
DEFAULT_ITEM_DETAIL: DetailLevel = "full"


def parse_detail(raw: str | None, default: DetailLevel) -> DetailLevel:
    """Parse a ``?detail=`` value, falling back rather than erroring.

    A bad detail level is a client bug that should not cost the user their
    answer; the fallback is always the cheaper option.
    """
    if raw is None:
        return default
    value = raw.strip().lower()
    return value if value in VALID_DETAILS else default  # type: ignore[return-value]


def _headers(pairs: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return [[name, value] for name, value in pairs]


def serialize_request(request: NormalizedRequest, detail: DetailLevel) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": request.method,
        "scheme": request.scheme,
        "host": request.host,
        "port": request.port,
        "path": request.path,
        "query": [[k, v] for k, v in request.query],
        "url": request.url,
        "http_version": request.http_version,
        "dest": request.dest,
        "headers": _headers(request.headers),
        "body_size": request.body_size,
        "body_truncated": request.body_truncated,
    }
    if detail == "bodies":
        body, encoding = encode_body(request.body)
        payload["body"] = body
        payload["body_encoding"] = encoding
    elif detail == "full":
        payload["body"] = None
        payload["body_encoding"] = None
    return payload


def serialize_response(response: NormalizedResponse, detail: DetailLevel) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": response.status,
        "reason": response.reason,
        "http_version": response.http_version,
        "headers": _headers(response.headers),
        "content_type": response.content_type,
        "body_size": response.body_size,
        "body_truncated": response.body_truncated,
        "streamed": response.streamed,
    }
    if detail == "bodies":
        body, encoding = encode_body(response.body)
        payload["body"] = body
        payload["body_encoding"] = encoding
    elif detail == "full":
        payload["body"] = None
        payload["body_encoding"] = None
    return payload


def serialize_ws_message(message: WebSocketMessage, detail: DetailLevel) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "index": message.index,
        "timestamp": message.timestamp,
        "direction": message.direction,
        "opcode": message.opcode,
        "size": message.size,
        "truncated": message.truncated,
    }
    if detail == "bodies":
        body, encoding = encode_body(message.payload)
        payload["payload"] = body
        payload["payload_encoding"] = encoding
    else:
        payload["payload"] = None
        payload["payload_encoding"] = None
    return payload


def serialize_flow(
    record: FlowRecord,
    detail: DetailLevel = "full",
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    """One flow, at the requested detail level.

    Provenance travels at every level (REQ CAP-013). At ``summary`` the entries
    collapse to counts, because a hundred-rule provenance chain per row would
    dominate a list response — but the note codes survive, since those are what
    the flag icons in the flow table are drawn from.
    """
    # Redaction at serialization time (SPEC-0 §9, REQ CAP-040). Applied to a
    # copy, never in place: the ring buffer must keep the unredacted values or
    # unmasking has nothing to reveal (REQ CAP-043).
    redacted = redactor is not None and redactor.enabled
    if redacted:
        record = redactor.redact_record(record)  # type: ignore[union-attr]

    payload: dict[str, Any] = {
        "flow_id": record.flow_id,
        "kind": record.kind,
        "started_at": record.started_at,
        "completed_at": record.completed_at,
        "tab_id": record.tab_id,
        "modified": record.modified,
        "blocked": record.blocked,
        "redacted": redacted,
    }

    if record.request is not None:
        payload["request"] = serialize_request(record.request, detail)
    if record.response is not None:
        payload["response"] = serialize_response(record.response, detail)

    if record.kind == "passthrough":
        # An excluded connection has no request or response, but must still be
        # visible with the entry that matched and why (REQ PXY-015).
        payload["passthrough"] = {
            "host": record.passthrough_host,
            "ip": record.passthrough_ip,
            "pattern": record.passthrough_pattern,
            "reason": record.passthrough_reason,
        }

    if record.kind == "websocket":
        websocket: dict[str, Any] = {
            "closed": record.ws_closed,
            "close_code": record.ws_close_code,
            "message_count": len(record.ws_messages),
        }
        if detail != "summary":
            websocket["messages"] = [serialize_ws_message(m, detail) for m in record.ws_messages]
        payload["websocket"] = websocket

    payload["timing"] = record.timing.to_dict()

    if record.provenance is not None:
        if detail == "summary":
            provenance = record.provenance
            payload["provenance"] = {
                "profile": provenance.profile,
                "evaluated_modules": list(provenance.evaluated_modules),
                "entries": [],
                "notes": [n.to_dict() for n in provenance.notes],
                "total_ms": provenance.total_ms,
                "short_circuited_by": provenance.short_circuited_by,
            }
        else:
            payload["provenance"] = record.provenance.to_dict()
    else:
        payload["provenance"] = {
            "profile": "default",
            "evaluated_modules": [],
            "entries": [],
            "notes": [],
            "total_ms": 0.0,
            "short_circuited_by": None,
        }

    return payload


def serialize_flow_page(
    records: list[FlowRecord],
    *,
    next_cursor: str | None,
    total_estimate: int,
    detail: DetailLevel = "summary",
    redactor: Redactor | None = None,
) -> dict[str, Any]:
    return {
        "flows": [serialize_flow(r, detail, redactor) for r in records],
        "next_cursor": next_cursor,
        "total_estimate": total_estimate,
    }
