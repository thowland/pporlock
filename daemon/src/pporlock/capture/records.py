"""The FlowRecord — SPEC-0 §3.4.

The persisted and API-exposed representation of one flow. This exact shape
appears in ``GET /flows``, in session storage, in the SSE stream, and in MCP
output, and is validated against ``contracts/schemas/flow.schema.json``.

Bodies are held as bytes here and encoded at serialization time, so the ring
buffer stores one copy rather than one per representation level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from ..engine.provenance import Provenance

FlowKind = Literal["http", "websocket", "passthrough"]


@dataclass(slots=True)
class Timing:
    """Per-phase timing. ``pporlock_ms`` is our own overhead, which the UI shows
    per flow so proxy cost is visible rather than inferred (REQ PRF-007)."""

    connect_ms: float | None = None
    request_ms: float | None = None
    upstream_ms: float | None = None
    response_ms: float | None = None
    pporlock_ms: float | None = None
    total_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dns_ms": None,
            "connect_ms": self.connect_ms,
            "request_ms": self.request_ms,
            "upstream_ms": self.upstream_ms,
            "response_ms": self.response_ms,
            "pporlock_ms": self.pporlock_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class FlowError:
    """Why a flow failed before completing.

    A 502 with no record is the worst outcome a traffic inspector can produce:
    the user sees the browser fail, opens the tool built to explain it, and
    finds nothing. `from_client` separates a browser cancelling from an origin
    refusing, which are indistinguishable in a count and are not the same
    event.
    """

    message: str
    from_client: bool = False


@dataclass(slots=True)
class FlowRecord:
    """One captured flow."""

    flow_id: str
    kind: FlowKind
    started_at: str
    completed_at: str | None = None
    tab_id: int | None = None
    request: NormalizedRequest | None = None
    response: NormalizedResponse | None = None
    provenance: Provenance | None = None
    timing: Timing = field(default_factory=Timing)
    modified: bool = False
    #: The client was denied what it asked for. Not "short-circuited" — see
    #: `short_circuit`, and OI-26 for why the distinction is load-bearing.
    blocked: bool = False
    #: Which of the three short-circuiting actions ended request evaluation.
    short_circuit: str | None = None
    # Set when the exchange never completed — refused, TLS failure, timeout.
    # `response` is None in that case, so without this the record says only
    # that something happened (OI-23).
    error: FlowError | None = None
    # passthrough only
    passthrough_host: str | None = None
    passthrough_ip: str | None = None
    passthrough_pattern: str | None = None
    passthrough_reason: str | None = None
    # websocket only
    ws_messages: list[WebSocketMessage] = field(default_factory=list)
    ws_closed: bool = False
    ws_close_code: int | None = None

    # -- derived, used by the filter vocabulary (SPEC-0 §6.5) ---------------

    @property
    def host(self) -> str | None:
        if self.request is not None:
            return self.request.host
        return self.passthrough_host

    @property
    def status(self) -> int | None:
        return self.response.status if self.response is not None else None

    @property
    def content_type(self) -> str | None:
        return self.response.content_type if self.response is not None else None

    @property
    def streamed(self) -> bool:
        return self.response.streamed if self.response is not None else False

    @property
    def size_bytes(self) -> int:
        """Approximate memory cost, for the ring buffer's byte bound.

        Deliberately approximate: an exact figure would mean walking every
        header on every insert, and the bound exists to stop unbounded growth,
        not to account to the byte.
        """
        total = 512  # record overhead, headers, provenance
        if self.request is not None and self.request.body is not None:
            total += len(self.request.body)
        if self.response is not None and self.response.body is not None:
            total += len(self.response.body)
        total += sum(len(m.payload) for m in self.ws_messages)
        return total

    def modules_fired(self) -> list[str]:
        return self.provenance.modules_fired() if self.provenance is not None else []

    def note_codes(self) -> list[str]:
        if self.provenance is None:
            return []
        return [str(n.code) for n in self.provenance.notes]


def truncate(body: bytes | None, cap: int) -> tuple[bytes | None, bool]:
    """Cap a body, reporting whether it was cut.

    Truncation is always flagged (REQ CAP-003). A silently shortened body would
    make a diff or a transform look wrong for reasons invisible to the user.
    """
    if body is None or len(body) <= cap:
        return body, False
    return body[:cap], True
