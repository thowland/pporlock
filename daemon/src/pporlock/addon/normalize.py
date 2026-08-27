"""mitmproxy -> pporlock adapter — SPEC-1 §3.2.

This module and ``apply.py`` are the only places that know mitmproxy's shapes.
Everything above this line is mitmproxy-shaped and expected to change between
releases; everything below sees our own dataclasses (REQ DD-2).

When a mitmproxy upgrade breaks something, it breaks here. That is the point:
the API has shifted across major versions in exactly the areas we lean on
hardest — streaming control, TLS hooks, option names — and confining that to one
file is what keeps an upgrade from becoming a rewrite (SPEC-1 §2.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from ..engine.models import (
    HeaderPairs,
    NormalizedRequest,
    NormalizedResponse,
    QueryPairs,
    Scheme,
    WebSocketMessage,
)


def _iso(moment: datetime) -> str:
    """ISO 8601, milliseconds, UTC — the single timestamp format (SPEC-0 §2)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def now_iso() -> str:
    """Current time in wire format.

    One clock read, not two: sampling the second and the millisecond separately
    can straddle a boundary and emit a timestamp that never existed.
    """
    return _iso(datetime.now(UTC))


def ts_to_iso(timestamp: float | None) -> str:
    """Convert a mitmproxy epoch timestamp to our wire format."""
    if timestamp is None:
        return now_iso()
    return _iso(datetime.fromtimestamp(timestamp, tz=UTC))


def normalize_headers(headers: Any) -> HeaderPairs:
    """mitmproxy Headers -> ordered lowercase pairs.

    A list rather than a map, because headers repeat and collapsing them loses
    information we need — Set-Cookie above all (SPEC-0 §2).
    """
    out: list[tuple[str, str]] = []
    for key, value in headers.fields:
        name = key.decode("latin-1", errors="replace").lower()
        out.append((name, value.decode("latin-1", errors="replace")))
    return tuple(out)


def normalize_query(request: Any) -> QueryPairs:
    """Query pairs in wire order, repeats preserved."""
    try:
        return tuple((str(k), str(v)) for k, v in request.query.items(multi=True))
    except (AttributeError, TypeError):
        query = urlsplit(request.url).query
        if not query:
            return ()
        pairs: list[tuple[str, str]] = []
        for part in query.split("&"):
            if not part:
                continue
            key, _, value = part.partition("=")
            pairs.append((key, value))
        return tuple(pairs)


def _scheme_of(request: Any) -> Scheme:
    return "https" if str(request.scheme).lower() == "https" else "http"


def normalize_request(
    flow: Any,
    *,
    flow_id: str,
    tab_id: int | None = None,
    body: bytes | None = None,
) -> NormalizedRequest:
    """Build a NormalizedRequest from a mitmproxy flow.

    ``body`` is passed in rather than read here, because whether a body was
    buffered at all is a pipeline decision made elsewhere.
    """
    request = flow.request
    headers = normalize_headers(request.headers)
    dest = None
    for name, value in headers:
        if name == "sec-fetch-dest":
            dest = value
            break

    return NormalizedRequest(
        flow_id=flow_id,
        timestamp=ts_to_iso(getattr(request, "timestamp_start", None)),
        scheme=_scheme_of(request),
        method=str(request.method).upper(),
        host=str(request.pretty_host),
        port=int(request.port),
        path=urlsplit(request.path).path or "/",
        url=str(request.pretty_url),
        http_version=str(request.http_version),
        query=normalize_query(request),
        headers=headers,
        dest=dest,
        body=body,
        body_truncated=False,
        tab_id=tab_id,
    )


def normalize_response(
    flow: Any,
    *,
    flow_id: str,
    body: bytes | None = None,
    streamed: bool = False,
) -> NormalizedResponse:
    """Build a NormalizedResponse from a mitmproxy flow.

    When ``streamed`` is True the body was never buffered, so transforms are
    unavailable and the engine records that rather than silently doing nothing
    (REQ PXY-022).
    """
    response = flow.response
    headers = normalize_headers(response.headers)
    encoding = None
    for name, value in headers:
        if name == "content-encoding":
            encoding = value
            break

    return NormalizedResponse(
        flow_id=flow_id,
        timestamp=ts_to_iso(getattr(response, "timestamp_start", None)),
        status=int(response.status_code),
        reason=str(response.reason or ""),
        http_version=str(response.http_version),
        headers=headers,
        body=None if streamed else body,
        body_truncated=False,
        streamed=streamed,
        encoding=encoding,
    )


def normalize_ws_message(flow: Any, message: Any, *, flow_id: str, index: int) -> WebSocketMessage:
    """Build a WebSocketMessage. Inspection-only in v1 (REQ PXY-051)."""
    payload = message.content
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")

    return WebSocketMessage(
        flow_id=flow_id,
        index=index,
        timestamp=ts_to_iso(getattr(message, "timestamp", None)),
        direction="outbound" if message.from_client else "inbound",
        opcode="text" if getattr(message, "is_text", False) else "binary",
        payload=payload,
    )


def sni_of(data: Any) -> str | None:
    """SNI from a ClientHelloData, or None when the client sent none."""
    try:
        sni = data.client_hello.sni
    except AttributeError:
        return None
    return str(sni) if sni else None


def peer_ip_of(data: Any) -> str | None:
    """Destination IP for a ClientHello, for the no-SNI fallback path.

    Returns only genuine addresses. Before resolution the connection's address
    is the hostname from the CONNECT line, and reporting that as an IP would put
    a hostname in a field the UI and the exclusion list both read as an address.
    """
    import ipaddress

    try:
        address = data.context.server.address
    except AttributeError:
        return None
    if not address:
        return None
    candidate = str(address[0]).strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate
