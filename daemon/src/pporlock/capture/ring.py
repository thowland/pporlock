"""Bounded in-memory ring buffer — SPEC-1 §6.1, REQ CAP-001/003/004.

Bounded on two axes, because either alone fails: a flow count says nothing about
a page that pulls six 4 MiB videos, and a byte count says nothing about ten
thousand 200-byte beacons. Whichever bound is hit first evicts oldest-first.

Memory boundedness over multi-day uptime is a hard requirement (REQ PRF-005),
and this is where it is enforced.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .filters import FlowFilter
from .records import FlowRecord


@dataclass(frozen=True, slots=True)
class RingStats:
    flows: int
    bytes: int
    max_flows: int
    max_bytes: int
    evicted: int

    def to_dict(self) -> dict[str, int]:
        return {
            "ring_flows": self.flows,
            "ring_bytes": self.bytes,
            "ring_max_flows": self.max_flows,
            "ring_max_bytes": self.max_bytes,
            "evicted": self.evicted,
        }


@dataclass(frozen=True, slots=True)
class QueryResult:
    flows: list[FlowRecord]
    next_cursor: str | None
    total_estimate: int


class RingBuffer:
    """Insertion-ordered, bounded by both flow count and total bytes."""

    __slots__ = ("_bytes", "_evicted", "_records", "max_body_bytes", "max_bytes", "max_flows")

    def __init__(
        self,
        max_flows: int = 2000,
        max_bytes: int = 256 * 1024 * 1024,
        max_body_bytes: int = 512 * 1024,
    ) -> None:
        self._records: OrderedDict[str, FlowRecord] = OrderedDict()
        self._bytes = 0
        self.max_flows = max_flows
        self.max_bytes = max_bytes
        self.max_body_bytes = max_body_bytes
        self._evicted = 0

    # -- writes ----------------------------------------------------------

    def add(self, record: FlowRecord) -> None:
        """Insert, evicting oldest-first until both bounds hold."""
        if record.flow_id in self._records:
            self._bytes -= self._records[record.flow_id].size_bytes
            del self._records[record.flow_id]

        self._records[record.flow_id] = record
        self._bytes += record.size_bytes
        self._evict()

    def update(self, flow_id: str, **changes: Any) -> FlowRecord | None:
        """Patch a record in place. Returns it, or None if it has been evicted.

        Used for attribution backfill (SPEC-0 §3.6): a flow is delivered before
        its tab is known, and clients are told about the change through the
        ``flow.updated`` event.
        """
        record = self._records.get(flow_id)
        if record is None:
            return None
        self._bytes -= record.size_bytes
        for key, value in changes.items():
            if hasattr(record, key):
                setattr(record, key, value)
        self._bytes += record.size_bytes
        self._evict()
        return record

    def _evict(self) -> None:
        while self._records and (
            len(self._records) > self.max_flows or self._bytes > self.max_bytes
        ):
            _, oldest = self._records.popitem(last=False)
            self._bytes -= oldest.size_bytes
            self._evicted += 1

    def clear(self) -> None:
        self._records.clear()
        self._bytes = 0

    # -- reads -----------------------------------------------------------

    def get(self, flow_id: str) -> FlowRecord | None:
        return self._records.get(flow_id)

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, flow_id: str) -> bool:
        return flow_id in self._records

    def query(
        self,
        flow_filter: FlowFilter | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> QueryResult:
        """Newest-first, filtered, cursor-paginated.

        Newest-first because the question being asked is almost always "what
        just happened", and a filter that matches nothing recent should return
        quickly rather than after walking the whole buffer.
        """
        limit = max(1, min(limit, 1000))
        flow_filter = flow_filter or FlowFilter()

        newest_first = list(reversed(self._records.values()))

        start = 0
        if cursor is not None:
            for index, record in enumerate(newest_first):
                if record.flow_id == cursor:
                    start = index + 1
                    break
            else:
                # The cursor's flow has been evicted. Start from the top rather
                # than returning nothing: the client would otherwise silently
                # lose the rest of the page.
                start = 0

        matched: list[FlowRecord] = []
        index = start
        while index < len(newest_first) and len(matched) < limit:
            record = newest_first[index]
            if flow_filter.matches(record):
                matched.append(record)
            index += 1

        more = index < len(newest_first)
        next_cursor = matched[-1].flow_id if more and matched else None

        return QueryResult(
            flows=matched,
            next_cursor=next_cursor,
            total_estimate=len(self._records),
        )

    @property
    def stats(self) -> RingStats:
        return RingStats(
            flows=len(self._records),
            bytes=self._bytes,
            max_flows=self.max_flows,
            max_bytes=self.max_bytes,
            evicted=self._evicted,
        )


def encode_body(body: bytes | None) -> tuple[str | None, str | None]:
    """Encode a body for the wire (SPEC-0 §3.4).

    Text travels as a UTF-8 string so it is readable in a browser devtools
    panel; anything else as base64. The encoding is always reported so the
    client never has to guess.
    """
    if body is None:
        return None, None
    try:
        return body.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        return base64.b64encode(body).decode("ascii"), "base64"
