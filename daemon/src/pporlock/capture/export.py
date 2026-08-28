"""Session export — REQ CAP-024.

Two formats, because neither one is enough on its own:

* **HAR 1.2**, so a session opens in Chrome DevTools, Charles, or anything else
  that reads HAR. It is a lossy export: HAR has no way to say "this response
  body was rewritten by module ``strip-csp`` rule 2, and this transform was
  skipped because the response streamed". That is the entire diagnostic value
  of this system, and HAR discards it.
* **pporlock native**, which preserves provenance, notes, timing, and the
  redaction configuration the session was recorded under.

Both read from the session database, whose contents were redacted at write time
(REQ CAP-045). An export therefore cannot carry a raw secret, because the raw
secret was never in the file to export — this module does not need a redaction
step of its own, and it must never acquire one that reads from the live ring.
"""

from __future__ import annotations

from typing import Any

from .records import FlowRecord
from .ring import encode_body
from .session import SessionReader

HAR_VERSION = "1.2"

EXPORT_FORMATS: frozenset[str] = frozenset({"har", "pporlock"})


def _har_headers(pairs: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in pairs]


def _har_query(pairs: tuple[tuple[str, str], ...]) -> list[dict[str, str]]:
    return [{"name": name, "value": value} for name, value in pairs]


def _har_entry(record: FlowRecord) -> dict[str, Any] | None:
    """One HAR entry, or None for a flow HAR cannot represent.

    Passthrough and WebSocket flows are skipped rather than faked: HAR has no
    entry shape for a tunnelled connection, and inventing a 200 for one would
    make the export say something that did not happen.
    """
    request = record.request
    if request is None or record.kind != "http":
        return None

    response = record.response
    req_body, req_encoding = encode_body(request.body)
    post_data: dict[str, Any] | None = None
    if req_body is not None:
        post_data = {
            "mimeType": request.content_type or "application/octet-stream",
            "text": req_body,
            "params": [],
        }
        if req_encoding == "base64":
            post_data["encoding"] = "base64"

    content: dict[str, Any] = {"size": 0, "mimeType": ""}
    if response is not None:
        body, encoding = encode_body(response.body)
        content = {
            "size": response.body_size,
            "mimeType": response.content_type or "",
            "text": body,
        }
        if encoding == "base64":
            content["encoding"] = "base64"

    timing = record.timing
    entry: dict[str, Any] = {
        "startedDateTime": record.started_at,
        "time": timing.total_ms or timing.pporlock_ms or 0.0,
        "request": {
            "method": request.method,
            "url": request.url,
            "httpVersion": request.http_version,
            "cookies": [],
            "headers": _har_headers(request.headers),
            "queryString": _har_query(request.query),
            "headersSize": -1,
            "bodySize": request.body_size,
        },
        "response": {
            "status": response.status if response is not None else 0,
            "statusText": response.reason if response is not None else "",
            "httpVersion": response.http_version if response is not None else request.http_version,
            "cookies": [],
            "headers": _har_headers(response.headers) if response is not None else [],
            "content": content,
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": response.body_size if response is not None else 0,
        },
        "cache": {},
        "timings": {
            "send": timing.request_ms if timing.request_ms is not None else -1,
            "wait": timing.upstream_ms if timing.upstream_ms is not None else -1,
            "receive": timing.response_ms if timing.response_ms is not None else -1,
        },
        # HAR has no provenance concept, so ours travels in the underscore
        # namespace HAR reserves for tools. Anything that ignores it still
        # reads the entry; anything of ours can recover why a flow looks the
        # way it does without a second export.
        "_pporlock": {
            "flow_id": record.flow_id,
            "modified": record.modified,
            "blocked": record.blocked,
            "tab_id": record.tab_id,
            "provenance": record.provenance.to_dict() if record.provenance else None,
        },
    }
    if post_data is not None:
        entry["request"]["postData"] = post_data
    return entry


def export_har(reader: SessionReader) -> dict[str, Any]:
    """The session as HAR 1.2. Lossy by the format's nature (REQ CAP-024)."""
    entries = [entry for entry in (_har_entry(r) for r in reader.iter_all()) if entry is not None]
    return {
        "log": {
            "version": HAR_VERSION,
            "creator": {
                "name": "pporlock",
                "version": reader.meta.pporlock_version or "0.1.0",
                "comment": (
                    "Values matching the redaction pattern lists are masked "
                    "(SPEC-0 §9.1). HAR cannot represent pporlock provenance; "
                    "use the pporlock native export to preserve it."
                ),
            },
            "pages": [],
            "entries": entries,
        }
    }


def export_native(reader: SessionReader) -> dict[str, Any]:
    """The session in pporlock's own format, provenance intact (REQ CAP-024).

    Serialized at ``bodies`` detail through the same serializer the API uses,
    so an exported flow and an API flow are the same shape and one schema
    validates both.
    """
    # Imported here rather than at module scope: control.serialize imports
    # capture.records, and a top-level import would close that cycle.
    from ..control.serialize import serialize_flow

    return {
        "format": "pporlock-session",
        "version": 1,
        "session": reader.meta.to_dict(),
        "flows": [serialize_flow(record, "bodies") for record in reader.iter_all()],
    }


def export_session(reader: SessionReader, fmt: str) -> dict[str, Any]:
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    return export_har(reader) if fmt == "har" else export_native(reader)
