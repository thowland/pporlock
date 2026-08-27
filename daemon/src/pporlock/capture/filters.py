"""The filter vocabulary — SPEC-0 §6.5, REQ CAP-004.

One implementation serves ``GET /flows``, ``GET /sessions/{id}/flows``, the SSE
subscription filter, the DevTools panel, and the MCP listing tools. That is the
point: a filter set is transferable between them, and there is no second
definition to drift.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

from .records import FlowRecord


def _parse_status(spec: str) -> list[tuple[int, int]]:
    """``"200"``, ``"300-399"``, or ``"200,404,500-599"`` -> inclusive ranges."""
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            ranges.append((int(low), int(high)))
        else:
            value = int(part)
            ranges.append((value, value))
    return ranges


@dataclass(frozen=True, slots=True)
class FlowFilter:
    """A parsed filter. Absent criteria do not constrain."""

    host: str | None = None
    path: str | None = None
    method: str | None = None
    status: str | None = None
    content_type: str | None = None
    dest: str | None = None
    tab_id: int | None = None
    modified: bool | None = None
    blocked: bool | None = None
    module: str | None = None
    note_code: str | None = None
    since: str | None = None
    until: str | None = None
    q: str | None = None

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in self.__slots__)

    def matches(self, record: FlowRecord) -> bool:
        """All present criteria must match."""
        if self.host is not None:
            host = record.host
            if host is None:
                return False
            pattern = self.host.lower()
            candidate = host.lower()
            matched = (
                fnmatch.fnmatchcase(candidate, pattern)
                if any(c in pattern for c in "*?[")
                else pattern in candidate
            )
            if not matched:
                return False

        if self.path is not None:
            if record.request is None:
                return False
            try:
                if not re.search(self.path, record.request.path):
                    return False
            except re.error:
                return False

        if self.method is not None:
            if record.request is None or record.request.method != self.method.upper():
                return False

        if self.status is not None:
            if record.status is None:
                return False
            try:
                ranges = _parse_status(self.status)
            except ValueError:
                return False
            if not any(low <= record.status <= high for low, high in ranges):
                return False

        if self.content_type is not None:
            actual = record.content_type
            if actual is None or self.content_type.lower() not in actual.lower():
                return False

        if self.dest is not None:
            if record.request is None or record.request.dest != self.dest:
                return False

        if self.tab_id is not None and record.tab_id != self.tab_id:
            return False

        if self.modified is not None and record.modified is not self.modified:
            return False

        if self.blocked is not None and record.blocked is not self.blocked:
            return False

        if self.module is not None and self.module not in record.modules_fired():
            return False

        if self.note_code is not None and self.note_code not in record.note_codes():
            return False

        # Timestamps are ISO 8601 with a fixed width, so lexical comparison is
        # chronological — no parsing on the hot path.
        if self.since is not None and record.started_at < self.since:
            return False
        if self.until is not None and record.started_at > self.until:
            return False

        if self.q is not None:
            haystack = record.request.url if record.request is not None else (record.host or "")
            if self.q.lower() not in haystack.lower():
                return False

        return True

    @classmethod
    def from_query(cls, params: dict[str, Any]) -> FlowFilter:
        """Build from query-string parameters, ignoring anything unrecognised.

        Unknown parameters are ignored rather than rejected: pagination and
        detail-level parameters travel on the same query string, and a filter
        that rejected them would make every real request fail.
        """

        def boolean(name: str) -> bool | None:
            raw = params.get(name)
            if raw is None or raw == "":
                return None
            return str(raw).strip().lower() in {"1", "true", "yes", "on"}

        def integer(name: str) -> int | None:
            raw = params.get(name)
            if raw is None or raw == "":
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def text(name: str) -> str | None:
            raw = params.get(name)
            if raw is None or raw == "":
                return None
            return str(raw)

        return cls(
            host=text("host"),
            path=text("path"),
            method=text("method"),
            status=text("status"),
            content_type=text("content_type"),
            dest=text("dest"),
            tab_id=integer("tab_id"),
            modified=boolean("modified"),
            blocked=boolean("blocked"),
            module=text("module"),
            note_code=text("note_code"),
            since=text("since"),
            until=text("until"),
            q=text("q"),
        )
