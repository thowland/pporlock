"""Tab attribution — SPEC-0 §3.6, SPEC-1 §6.6, REQ OI-2.

Only the extension knows which tab a request came from; only the daemon sees the
flow. This joins the two.

Three properties matter more than accuracy:

* **It never blocks a flow.** Attribution is best-effort and arrives after the
  fact. A flow is delivered with ``tab_id: null`` and updated later, which is
  why every consumer must tolerate a late field change.
* **It is bounded.** A browser that submits faster than flows arrive must not
  grow this index without limit.
* **It reports its own coverage.** The Sprint 6 decision criterion is a
  measurement, so the measurement has to exist in the product rather than in a
  one-off script.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

#: How far apart the extension's observation and the proxy's flow may be and
#: still be considered the same request. Generous: the extension observes at
#: onBeforeRequest, the proxy at the point the request reaches it, and a slow
#: DNS lookup or a queued connection sits between them.
JOIN_WINDOW_SECONDS = 5.0

#: Bounded so a burst cannot grow memory without limit.
MAX_PENDING = 5000


@dataclass(frozen=True, slots=True)
class AttributionEntry:
    """One (request -> tab) association observed by the extension."""

    method: str
    url: str
    ts: float
    tab_id: int
    frame_id: int = 0
    resource_type: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.method.upper(), self.url)


def entry_from_dict(raw: dict[str, Any]) -> AttributionEntry | None:
    """Parse one wire entry, returning None rather than raising.

    A malformed entry from the extension must not fail the whole batch: the
    batch is best-effort and dropping one association is far better than
    dropping a hundred.
    """
    try:
        method = str(raw["method"])
        url = str(raw["url"])
        tab_id = int(raw["tabId"])
    except (KeyError, TypeError, ValueError):
        return None

    raw_ts = raw.get("ts")
    if isinstance(raw_ts, int | float):
        ts = float(raw_ts)
        # The extension sends epoch milliseconds; normalise to seconds.
        if ts > 1e11:
            ts /= 1000.0
    else:
        ts = time.time()

    try:
        frame_id = int(raw.get("frameId", 0))
    except (TypeError, ValueError):
        frame_id = 0

    return AttributionEntry(
        method=method,
        url=url,
        ts=ts,
        tab_id=tab_id,
        frame_id=frame_id,
        resource_type=str(raw.get("type", "")),
    )


@dataclass(frozen=True, slots=True)
class AttributionStats:
    """Diagnostics about the join itself.

    These count *attempts*, not flows. Backfill re-tries every unattributed flow
    on each submission, so `unresolved` climbs for a single stubbornly
    unattributable flow. That makes it useful for spotting a broken join and
    useless as the coverage figure — coverage is measured over flows, by
    ``coverage_of()`` below.
    """

    submitted: int
    resolved: int
    unresolved: int
    dropped: int
    pending: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "resolved": self.resolved,
            "resolve_attempts_missed": self.unresolved,
            "dropped": self.dropped,
            "pending": self.pending,
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    """Attribution coverage over flows — the figure the criterion names.

    SPEC-0 §3.6 states it in terms of flows: "if fewer than 95% of flows in a
    30-minute reference browsing session are attributed, the primary mechanism
    is rejected." So it is computed over flows, not over join attempts.
    """

    attributed: int
    total: int

    @property
    def fraction(self) -> float | None:
        return None if self.total == 0 else self.attributed / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "attributed": self.attributed,
            "total": self.total,
            "coverage": self.fraction,
        }


def coverage_of(records: Any) -> Coverage:
    """Measure attribution coverage over a set of flow records.

    Only HTTP flows count. A passthrough was never decrypted and a WebSocket
    handshake is counted once as the HTTP flow that opened it, so including them
    would report a coverage figure for traffic attribution cannot apply to.
    """
    attributed = 0
    total = 0
    for record in records:
        if getattr(record, "kind", None) != "http":
            continue
        total += 1
        if getattr(record, "tab_id", None) is not None:
            attributed += 1
    return Coverage(attributed=attributed, total=total)


class AttributionIndex:
    """Joins extension observations to proxy flows."""

    __slots__ = (
        "_dropped",
        "_pending",
        "_resolved",
        "_submitted",
        "_unresolved",
        "max_pending",
        "window",
    )

    def __init__(
        self,
        window_seconds: float = JOIN_WINDOW_SECONDS,
        max_pending: int = MAX_PENDING,
    ) -> None:
        # Keyed on (method, url). A page requesting the same URL twice in quick
        # succession collapses to one entry, which is acceptable: both requests
        # come from the same tab in every case that matters.
        self._pending: OrderedDict[tuple[str, str], AttributionEntry] = OrderedDict()
        self._submitted = 0
        self._resolved = 0
        self._unresolved = 0
        self._dropped = 0
        self.window = window_seconds
        self.max_pending = max_pending

    def submit(self, entries: list[AttributionEntry]) -> int:
        """Record observations. Returns how many were accepted."""
        accepted = 0
        for entry in entries:
            self._pending[entry.key] = entry
            self._pending.move_to_end(entry.key)
            accepted += 1
            self._submitted += 1
        self._evict()
        return accepted

    def _evict(self) -> None:
        now = time.time()
        # Age out first: an entry older than the join window will never match.
        stale = [k for k, e in self._pending.items() if now - e.ts > self.window * 2]
        for key in stale:
            del self._pending[key]
            self._dropped += 1
        while len(self._pending) > self.max_pending:
            self._pending.popitem(last=False)
            self._dropped += 1

    def resolve(self, method: str, url: str, when: float | None = None) -> int | None:
        """Find the tab for a request, consuming the association.

        Consuming rather than keeping it means a repeated URL in a different tab
        is not silently attributed to the first one.
        """
        key = (method.upper(), url)
        entry = self._pending.get(key)
        if entry is None:
            self._unresolved += 1
            return None

        moment = time.time() if when is None else when
        if abs(moment - entry.ts) > self.window:
            # Too far apart to be the same request. Leave it: a later flow may
            # legitimately match it.
            self._unresolved += 1
            return None

        del self._pending[key]
        self._resolved += 1
        return entry.tab_id

    @property
    def pending(self) -> int:
        return len(self._pending)

    @property
    def stats(self) -> AttributionStats:
        return AttributionStats(
            submitted=self._submitted,
            resolved=self._resolved,
            unresolved=self._unresolved,
            dropped=self._dropped,
            pending=len(self._pending),
        )

    def clear(self) -> None:
        self._pending.clear()
