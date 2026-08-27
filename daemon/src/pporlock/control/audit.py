"""Audit log — REQ MCP-031.

Every state change is recorded with its actor origin. This is what makes "what
did the MCP client do" answerable, and it is why the client header is required
on mutating requests: without it the origin field would be a guess.

In-memory and bounded for Sprint 3; Sprint 13 moves it to SQLite alongside the
session store.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from ..addon.normalize import now_iso

MAX_ENTRIES = 5000


@dataclass(frozen=True, slots=True)
class AuditEntry:
    ts: str
    origin: str
    action: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "origin": self.origin, "action": self.action, "detail": self.detail}


class AuditLog:
    __slots__ = ("_entries",)

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)

    def record(self, origin: str, action: str, **detail: Any) -> AuditEntry:
        entry = AuditEntry(ts=now_iso(), origin=origin, action=action, detail=detail)
        self._entries.append(entry)
        return entry

    def entries(
        self, limit: int = 100, cursor: str | None = None
    ) -> tuple[list[AuditEntry], str | None]:
        """Newest first."""
        items = list(reversed(self._entries))
        start = 0
        if cursor is not None:
            try:
                start = int(cursor)
            except ValueError:
                start = 0
        page = items[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(items) else None
        return page, next_cursor

    def __len__(self) -> int:
        return len(self._entries)
