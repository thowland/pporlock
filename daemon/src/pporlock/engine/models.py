"""Normalized flow model — SPEC-0 §3.

These are the types the rules engine and module code see. They contain nothing
from mitmproxy (REQ DD-2, MOD-021): the adapter in ``pporlock.addon.normalize``
is the only place that crosses that boundary, which is what lets this package be
unit-tested with no proxy process and no network (REQ TST-001).

The request and response objects are frozen. Rule and module code does not
mutate them; it returns a mutation proposal (§3.3) that the adapter applies.
That separation is what makes evaluation pure and therefore replayable, which
the dry runner depends on (REQ CAP-031).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Scheme = Literal["http", "https"]
BodyEncoding = Literal["utf8", "base64"]
WsDirection = Literal["outbound", "inbound"]
WsOpcode = Literal["text", "binary"]

HeaderPairs = tuple[tuple[str, str], ...]
QueryPairs = tuple[tuple[str, str], ...]


def _media_type(value: str | None) -> str | None:
    """Strip parameters from a Content-Type: ``text/html; charset=utf-8`` -> ``text/html``."""
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower() or None


class _HeaderAccess:
    """Header and content-type accessors shared by requests and responses.

    These are the only supported way module code reads headers (SPEC-0 §3.1).
    Header names are lowercased on the wire, but lookups normalise anyway so a
    module written with ``Content-Type`` does not silently miss.
    """

    headers: HeaderPairs

    def header(self, name: str) -> str | None:
        """First value for ``name``, or None."""
        target = name.lower()
        for key, value in self.headers:
            if key.lower() == target:
                return value
        return None

    def headers_all(self, name: str) -> list[str]:
        """Every value for ``name``. Headers repeat; this is why we keep pairs."""
        target = name.lower()
        return [value for key, value in self.headers if key.lower() == target]

    def has_header(self, name: str) -> bool:
        target = name.lower()
        return any(key.lower() == target for key, _ in self.headers)

    @property
    def content_type(self) -> str | None:
        """Media type only, no parameters."""
        return _media_type(self.header("content-type"))


@dataclass(frozen=True, slots=True)
class NormalizedRequest(_HeaderAccess):
    """A request as the engine sees it (SPEC-0 §3.1)."""

    flow_id: str
    timestamp: str
    scheme: Scheme
    method: str
    host: str
    port: int
    path: str
    url: str
    http_version: str = "HTTP/1.1"
    query: QueryPairs = ()
    headers: HeaderPairs = ()
    dest: str | None = None
    body: bytes | None = None
    body_truncated: bool = False
    tab_id: int | None = None

    def query_param(self, name: str) -> str | None:
        for key, value in self.query:
            if key == name:
                return value
        return None

    def query_params_all(self, name: str) -> list[str]:
        return [value for key, value in self.query if key == name]

    @property
    def body_size(self) -> int:
        return len(self.body) if self.body is not None else 0


@dataclass(frozen=True, slots=True)
class NormalizedResponse(_HeaderAccess):
    """A response as the engine sees it (SPEC-0 §3.2).

    ``body`` is decoded — gzip, deflate, and brotli are handled by the adapter
    (REQ PXY-023). It is None when the buffering guard chose to stream, in which
    case ``streamed`` is True and body transforms are unavailable (REQ PXY-022).
    """

    flow_id: str
    timestamp: str
    status: int
    reason: str = ""
    http_version: str = "HTTP/1.1"
    headers: HeaderPairs = ()
    body: bytes | None = None
    body_truncated: bool = False
    streamed: bool = False
    encoding: str | None = None

    @property
    def body_size(self) -> int:
        return len(self.body) if self.body is not None else 0

    @property
    def text(self) -> str | None:
        """The body decoded per charset, or None if absent or not decodable.

        Returns None rather than raising: a transform that cannot read a body
        should record no_change, not break the flow.
        """
        if self.body is None:
            return None
        charset = "utf-8"
        raw = self.header("content-type")
        if raw and "charset=" in raw.lower():
            charset = raw.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="strict")
        except (UnicodeDecodeError, LookupError):
            return None


@dataclass(frozen=True, slots=True)
class WebSocketMessage:
    """One WebSocket frame. Inspection-only in v1 (REQ PXY-051)."""

    flow_id: str
    index: int
    timestamp: str
    direction: WsDirection
    opcode: WsOpcode
    payload: bytes
    truncated: bool = False

    @property
    def size(self) -> int:
        return len(self.payload)


# --------------------------------------------------------------- mutations ---
# SPEC-0 §3.3. Mutable by design: the evaluator accumulates into these across
# rules and modules, then hands one object to the adapter.


@dataclass(frozen=True, slots=True)
class RedirectSpec:
    """Independent rewrite of any URL component (REQ PXY-035)."""

    scheme: Scheme | None = None
    host: str | None = None
    port: int | None = None
    path: str | None = None
    query: str | None = None

    def is_empty(self) -> bool:
        return all(v is None for v in (self.scheme, self.host, self.port, self.path, self.query))


@dataclass(frozen=True, slots=True)
class SyntheticResponse:
    """A response manufactured by pporlock rather than fetched.

    ``origin`` names the rule or module responsible, so a synthesized response is
    always attributable in provenance and in the UI.
    """

    status: int
    body: bytes = b""
    headers: tuple[tuple[str, str], ...] = ()
    origin: str = ""

    @property
    def content_type(self) -> str | None:
        for key, value in self.headers:
            if key.lower() == "content-type":
                return _media_type(value)
        return None


@dataclass(slots=True)
class HeaderMutation:
    """Accumulated header edits. Names are compared case-insensitively (REQ PXY-036)."""

    set_headers: dict[str, str] = field(default_factory=dict)
    add_headers: list[tuple[str, str]] = field(default_factory=list)
    remove_headers: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.set_headers or self.add_headers or self.remove_headers)

    def remove(self, name: str) -> None:
        lowered = name.lower()
        if lowered not in [r.lower() for r in self.remove_headers]:
            self.remove_headers.append(lowered)

    def set(self, name: str, value: str) -> None:
        self.set_headers[name.lower()] = value

    def add(self, name: str, value: str) -> None:
        self.add_headers.append((name.lower(), value))


@dataclass(slots=True)
class RequestMutation(HeaderMutation):
    """Everything a rule or module proposes to do to a request (SPEC-0 §3.3)."""

    redirect: RedirectSpec | None = None
    short_circuit: SyntheticResponse | None = None
    body: bytes | None = None

    def is_empty(self) -> bool:
        # Explicit base call rather than super(): @dataclass(slots=True) rebuilds
        # the class object, which breaks zero-argument super() in subclasses.
        return (
            HeaderMutation.is_empty(self)
            and self.redirect is None
            and self.short_circuit is None
            and self.body is None
        )


@dataclass(slots=True)
class ResponseMutation(HeaderMutation):
    """Everything a rule or module proposes to do to a response (SPEC-0 §3.3)."""

    status: int | None = None
    body: bytes | None = None

    def is_empty(self) -> bool:
        # See RequestMutation.is_empty for why this is not super().
        return HeaderMutation.is_empty(self) and self.status is None and self.body is None
