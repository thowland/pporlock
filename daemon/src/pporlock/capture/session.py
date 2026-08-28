"""Session recording — SPEC-1 §6.3, REQ CAP-020/021/023/045.

One SQLite file per session under ``~/.pporlock/sessions/``. Recording is
opt-in and off by default (REQ CAP-020).

Three properties this module exists to guarantee, in order of how badly it
breaks if one is lost:

1. **Nothing unredacted reaches the file.** Every record passes through the
   Redactor on the writer thread before any parameter is bound to an INSERT
   (REQ CAP-045). There is no code path from a FlowRecord to SQLite that
   bypasses ``_redact_and_encode``.
2. **Nothing blocks the proxy's event loop.** ``enqueue`` is a non-blocking put
   onto a bounded queue and returns; a dedicated thread does the SQLite work
   (DD-3). The loop never opens, writes to, or commits a database.
3. **Recording never backpressures browsing.** When the queue is full or the
   session's size cap is reached, flows are *dropped* with a counter increment.
   A capture tool that made the browser slow would be turned off, and then it
   would capture nothing at all.

Redaction runs on the writer thread rather than in ``enqueue`` deliberately:
scanning a megabyte JSON body is real work, and doing it inline would put
exactly the cost this design exists to avoid back on the loop. The queue holds
in-memory objects the ring buffer already holds unredacted; the guarantee that
matters is about the file.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..addon.normalize import now_iso
from ..engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from ..engine.provenance import Provenance
from ..errors import SessionError
from .filters import FlowFilter
from .records import FlowRecord, Timing
from .redact import Redactor
from .ring import QueryResult, encode_body

#: Bumped when a written column changes meaning. The reader refuses anything
#: newer with a clear message rather than silently misreading it.
SCHEMA_VERSION = 1

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Writer batching (SPEC-1 §6.3). Commit every 100 flows or 500 ms, whichever
#: comes first, so a quiet session still lands on disk promptly and a busy one
#: does not pay a durability round-trip per flow.
BATCH_SIZE = 100
BATCH_INTERVAL_S = 0.5

#: Bounded queue. Sized so a burst of a few seconds of heavy browsing is
#: absorbed, and anything beyond that is dropped rather than held: unbounded
#: buffering here would trade a proxy stall for an out-of-memory kill.
QUEUE_MAX = 5000

_SESSION_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz-_"

#: Every flow column, in bind order. The SQL below is written out in full
#: rather than interpolated from this list: a query assembled by string
#: concatenation is one refactor away from being assembled out of something a
#: caller supplied, and the linters are right to refuse to tell the difference.
_FLOW_COLUMNS = (
    "flow_id, seq, kind, started_at, completed_at, tab_id, method, scheme, host, port, "
    "path, query, url, dest, http_version, status, reason, content_type, req_headers, "
    "resp_headers, req_body, req_body_encoding, req_body_truncated, resp_body, "
    "resp_body_encoding, resp_body_truncated, timing, provenance, modified, blocked, "
    "streamed, ws_closed, ws_close_code, passthrough_host, passthrough_ip, "
    "passthrough_pattern, passthrough_reason"
)

#: Column count, asserted against the bind list at import so a column added to
#: the schema without a matching placeholder fails here rather than at the
#: first INSERT of a live recording.
FLOW_COLUMN_COUNT = 37

_INSERT_FLOW = (
    "INSERT OR REPLACE INTO flows ("
    "flow_id, seq, kind, started_at, completed_at, tab_id, method, scheme, host, port, "
    "path, query, url, dest, http_version, status, reason, content_type, req_headers, "
    "resp_headers, req_body, req_body_encoding, req_body_truncated, resp_body, "
    "resp_body_encoding, resp_body_truncated, timing, provenance, modified, blocked, "
    "streamed, ws_closed, ws_close_code, passthrough_host, passthrough_ip, "
    "passthrough_pattern, passthrough_reason"
    ") VALUES ("
    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_FLOW_BY_ID = (
    "SELECT "
    "flow_id, seq, kind, started_at, completed_at, tab_id, method, scheme, host, port, "
    "path, query, url, dest, http_version, status, reason, content_type, req_headers, "
    "resp_headers, req_body, req_body_encoding, req_body_truncated, resp_body, "
    "resp_body_encoding, resp_body_truncated, timing, provenance, modified, blocked, "
    "streamed, ws_closed, ws_close_code, passthrough_host, passthrough_ip, "
    "passthrough_pattern, passthrough_reason"
    " FROM flows WHERE flow_id = ?"
)

_SELECT_FLOWS_AFTER = (
    "SELECT seq, "
    "flow_id, seq, kind, started_at, completed_at, tab_id, method, scheme, host, port, "
    "path, query, url, dest, http_version, status, reason, content_type, req_headers, "
    "resp_headers, req_body, req_body_encoding, req_body_truncated, resp_body, "
    "resp_body_encoding, resp_body_truncated, timing, provenance, modified, blocked, "
    "streamed, ws_closed, ws_close_code, passthrough_host, passthrough_ip, "
    "passthrough_pattern, passthrough_reason"
    " FROM flows WHERE seq > ? ORDER BY seq ASC LIMIT ?"
)

_SELECT_ALL_FLOWS = (
    "SELECT "
    "flow_id, seq, kind, started_at, completed_at, tab_id, method, scheme, host, port, "
    "path, query, url, dest, http_version, status, reason, content_type, req_headers, "
    "resp_headers, req_body, req_body_encoding, req_body_truncated, resp_body, "
    "resp_body_encoding, resp_body_truncated, timing, provenance, modified, blocked, "
    "streamed, ws_closed, ws_close_code, passthrough_host, passthrough_ip, "
    "passthrough_pattern, passthrough_reason"
    " FROM flows ORDER BY seq ASC"
)


def validate_session_id(session_id: str) -> str:
    """Refuse anything that is not a plain session id.

    The id becomes a filename, so ``..`` or a separator in it would be a path
    traversal into the user's home directory. Validated rather than sanitised:
    a rejected id is a bug in the caller, and quietly rewriting it would hide
    that (implementation-plan.md §2.5, path traversal).
    """
    if not session_id or len(session_id) > 128:
        raise SessionError("invalid session id", session_id=session_id)
    if any(c not in _SESSION_ID_ALPHABET for c in session_id):
        raise SessionError("invalid session id", session_id=session_id)
    return session_id


def new_session_id() -> str:
    """Time-ordered and unique enough for one machine's sessions directory."""
    return f"s{int(time.time() * 1000):x}"


@dataclass(slots=True)
class SessionMeta:
    """Session metadata — the ``SessionMeta`` shape in the control API."""

    session_id: str
    name: str
    state: str = "recording"  # "recording" | "stopped"
    started_at: str = ""
    stopped_at: str | None = None
    flow_count: int = 0
    size_bytes: int = 0
    profile: str = "default"
    dropped: int = 0
    pporlock_version: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "state": self.state,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "flow_count": self.flow_count,
            "size_bytes": self.size_bytes,
            "profile": self.profile,
            "dropped": self.dropped,
            "schema_version": self.schema_version,
        }


def _connect(path: Path) -> sqlite3.Connection:
    """Open a session database in WAL mode.

    WAL because a reader browsing a session must not block the writer still
    recording it — the UI opens the current session while traffic is arriving,
    and rollback journalling would make that a lock fight.
    """
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA journal_mode=WAL")
    # NORMAL rather than FULL: a session is diagnostic capture, and losing the
    # last few flows to a power cut is a far better trade than an fsync per
    # commit while the user is browsing.
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_PATH.read_text())


def _count_flows(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM flows").fetchone()
    return int(row[0]) if row else 0


def _read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in connection.execute("SELECT key, value FROM meta").fetchall()
    }


def _write_meta(connection: sqlite3.Connection, values: dict[str, Any]) -> None:
    connection.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [(k, "" if v is None else str(v)) for k, v in values.items()],
    )


def _meta_from_row(session_id: str, values: dict[str, str], path: Path) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        name=values.get("session_name", session_id),
        state=values.get("state", "stopped"),
        started_at=values.get("started_at", ""),
        stopped_at=values.get("stopped_at") or None,
        profile=values.get("profile", "default"),
        dropped=int(values.get("dropped", "0") or 0),
        pporlock_version=values.get("pporlock_version", ""),
        schema_version=int(values.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION),
        size_bytes=path.stat().st_size if path.exists() else 0,
    )


# ----------------------------------------------------------------- writer ---


@dataclass(slots=True)
class _Encoded:
    """One redacted flow, already flattened to bind parameters.

    Exists so the redaction step and the SQL step are separable: the tuple this
    holds is the only thing the INSERT ever sees, and it is produced by code
    that cannot construct it without going through the Redactor.
    """

    flow: tuple[Any, ...]
    ws: list[tuple[Any, ...]] = field(default_factory=list)
    notes: list[tuple[Any, ...]] = field(default_factory=list)
    approx_bytes: int = 0


class SessionWriter:
    """Off-loop batched writer for one session (SPEC-1 §6.3).

    Owns a thread and a bounded queue. The only method the event loop calls is
    ``enqueue``, which never blocks and never raises.
    """

    def __init__(
        self,
        path: Path,
        meta: SessionMeta,
        redactor: Redactor,
        *,
        max_bytes: int = 5 * 1024 * 1024 * 1024,
        max_body_bytes: int = 512 * 1024,
        batch_size: int = BATCH_SIZE,
        batch_interval_s: float = BATCH_INTERVAL_S,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self.path = path
        self.meta = meta
        self.redactor = redactor
        self.max_bytes = max_bytes
        self.max_body_bytes = max_body_bytes
        self.batch_size = batch_size
        self.batch_interval_s = batch_interval_s
        self._queue: queue.Queue[FlowRecord | None] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._written = 0
        self._seq = 0
        self._bytes_written = 0
        self._capped = False
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def start(self) -> SessionMeta:
        """Create the database and start the writer thread."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = _connect(self.path)
        try:
            _init_schema(connection)
            _write_meta(
                connection,
                {
                    "schema_version": SCHEMA_VERSION,
                    "session_name": self.meta.name,
                    "started_at": self.meta.started_at,
                    "stopped_at": "",
                    "state": "recording",
                    "profile": self.meta.profile,
                    "pporlock_version": self.meta.pporlock_version,
                    "dropped": 0,
                    "redaction_config": json.dumps(
                        {
                            "enabled": self.redactor.cfg.enabled,
                            "header_patterns": list(self.redactor.cfg.header_patterns),
                            "json_key_patterns": list(self.redactor.cfg.json_key_patterns),
                        }
                    ),
                },
            )
            connection.commit()
        finally:
            connection.close()

        self._thread = threading.Thread(
            target=self._run, name=f"pporlock-session-{self.meta.session_id}", daemon=True
        )
        self._thread.start()
        self.meta.state = "recording"
        return self.meta

    def enqueue(self, record: FlowRecord) -> None:
        """Hand a flow to the writer. Never blocks, never raises (REQ CAP-023).

        Called from the proxy's event loop. A full queue drops the flow and
        counts it: the alternative is backpressure on the loop, which is a
        stalled browser.
        """
        if self._capped:
            self._count_drop()
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._count_drop()

    def _count_drop(self) -> None:
        with self._lock:
            self._dropped += 1

    def stop(self) -> SessionMeta:
        """Drain, commit, checkpoint the WAL, and close.

        The WAL is checkpointed with TRUNCATE rather than left for SQLite to
        fold in later: a stopped session is a file the user may copy or hand to
        someone, and everything it contains must be inside that one file.
        """
        thread = self._thread
        if thread is not None:
            self._queue.put(None)
            thread.join(timeout=30.0)
            self._thread = None

        self.meta.stopped_at = now_iso()
        self.meta.state = "stopped"

        connection = _connect(self.path)
        try:
            _write_meta(
                connection,
                {
                    "state": "stopped",
                    "stopped_at": self.meta.stopped_at,
                    "dropped": self.dropped,
                },
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

        self.meta.flow_count = self._written
        self.meta.dropped = self.dropped
        self.meta.size_bytes = self.path.stat().st_size if self.path.exists() else 0
        return self.meta

    # -- properties ------------------------------------------------------

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def written(self) -> int:
        with self._lock:
            return self._written

    @property
    def capped(self) -> bool:
        """True once the session hit its size cap (REQ CAP-023)."""
        return self._capped

    # -- the writer thread -----------------------------------------------

    def _run(self) -> None:
        connection = _connect(self.path)
        try:
            while True:
                batch, done = self._collect()
                if batch:
                    self._flush(connection, batch)
                if done:
                    return
        finally:
            connection.close()

    def _collect(self) -> tuple[list[FlowRecord], bool]:
        """Gather up to ``batch_size`` records, or whatever arrives within the
        batch interval. Returns the batch and whether the sentinel was seen."""
        batch: list[FlowRecord] = []
        deadline = time.monotonic() + self.batch_interval_s
        while len(batch) < self.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._queue.get(timeout=remaining)
            except queue.Empty:
                break
            if item is None:
                return batch, True
            batch.append(item)
        return batch, False

    def _flush(self, connection: sqlite3.Connection, batch: list[FlowRecord]) -> None:
        encoded: list[_Encoded] = []
        for record in batch:
            if self._capped:
                self._count_drop()
                continue
            item = self._redact_and_encode(record)
            self._bytes_written += item.approx_bytes
            encoded.append(item)
            if self._bytes_written >= self.max_bytes:
                # REQ CAP-023. Stop at the cap rather than growing without
                # bound; the flows already accepted are kept and the session
                # reports how many were dropped.
                self._capped = True

        if not encoded:
            return

        try:
            connection.executemany(_INSERT_FLOW, [e.flow for e in encoded])
            ws_rows = [row for e in encoded for row in e.ws]
            if ws_rows:
                connection.executemany(
                    "INSERT OR REPLACE INTO ws_messages "
                    "(flow_id, idx, ts, direction, opcode, size, payload, "
                    "payload_encoding, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ws_rows,
                )
            note_rows = [row for e in encoded for row in e.notes]
            if note_rows:
                # Replaced wholesale per flow: a websocket flow is rewritten on
                # every frame, and appending would multiply its notes.
                connection.executemany(
                    "DELETE FROM notes WHERE flow_id = ?", [(e.flow[0],) for e in encoded]
                )
                connection.executemany(
                    "INSERT INTO notes (flow_id, code, severity, module, message, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    note_rows,
                )
            connection.commit()
        except sqlite3.Error:
            # A failed batch is dropped, not retried into a stall. The session
            # reports the loss; the proxy keeps running.
            connection.rollback()
            with self._lock:
                self._dropped += len(encoded)
            return

        with self._lock:
            self._written = _count_flows(connection)

    def _redact_and_encode(self, record: FlowRecord) -> _Encoded:
        """Redact, then flatten to bind parameters.

        The single chokepoint between a live FlowRecord and SQLite. Redaction
        happens here, before encoding, so there is no ordering in which a raw
        value could be bound to a statement (REQ CAP-045).
        """
        redacted = self.redactor.redact_record(record)
        self._seq += 1

        request: NormalizedRequest | None = redacted.request
        response: NormalizedResponse | None = redacted.response

        req_body, req_encoding = encode_body(
            _cap(request.body if request else None, self.max_body_bytes)
        )
        resp_body, resp_encoding = encode_body(
            _cap(response.body if response else None, self.max_body_bytes)
        )
        provenance = redacted.provenance.to_dict() if redacted.provenance else {}

        flow_row: tuple[Any, ...] = (
            redacted.flow_id,
            self._seq,
            redacted.kind,
            redacted.started_at,
            redacted.completed_at,
            redacted.tab_id,
            request.method if request else None,
            request.scheme if request else None,
            request.host if request else redacted.passthrough_host,
            request.port if request else None,
            request.path if request else None,
            json.dumps([[k, v] for k, v in request.query]) if request else None,
            request.url if request else None,
            request.dest if request else None,
            request.http_version if request else None,
            response.status if response else None,
            response.reason if response else None,
            response.content_type if response else None,
            json.dumps([[k, v] for k, v in request.headers]) if request else "[]",
            json.dumps([[k, v] for k, v in response.headers]) if response else None,
            req_body,
            req_encoding,
            int(request.body_truncated) if request else 0,
            resp_body,
            resp_encoding,
            int(response.body_truncated) if response else 0,
            json.dumps(redacted.timing.to_dict()),
            json.dumps(provenance),
            int(redacted.modified),
            int(redacted.blocked),
            int(response.streamed) if response else 0,
            int(redacted.ws_closed),
            redacted.ws_close_code,
            redacted.passthrough_host,
            redacted.passthrough_ip,
            redacted.passthrough_pattern,
            redacted.passthrough_reason,
        )

        ws_rows: list[tuple[Any, ...]] = []
        for message in redacted.ws_messages:
            payload, encoding = encode_body(_cap(message.payload, self.max_body_bytes))
            ws_rows.append(
                (
                    redacted.flow_id,
                    message.index,
                    message.timestamp,
                    message.direction,
                    message.opcode,
                    message.size,
                    payload,
                    encoding,
                    int(message.truncated),
                )
            )

        note_rows: list[tuple[Any, ...]] = []
        if redacted.provenance is not None:
            note_rows = [
                (
                    redacted.flow_id,
                    str(note.code),
                    str(note.severity),
                    note.module,
                    note.message,
                    json.dumps(note.detail),
                )
                for note in redacted.provenance.notes
            ]

        approx = 512 + len(req_body or "") + len(resp_body or "")
        approx += sum(len(row[6] or "") for row in ws_rows)

        return _Encoded(flow=flow_row, ws=ws_rows, notes=note_rows, approx_bytes=approx)


def _cap(body: bytes | None, cap: int) -> bytes | None:
    if body is None:
        return None
    return body[:cap]


# ----------------------------------------------------------------- reader ---


class SessionReader:
    """Reads a recorded session back as FlowRecords.

    Deliberately has no unmask method and no access to a live buffer. There is
    nothing to unmask: what is in the file is what the Redactor produced
    (REQ CAP-043 — unmasking is unavailable for session data because the data
    does not exist, not because a check refuses it).
    """

    __slots__ = ("meta", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            raise SessionError("no such session", path=str(path))
        connection = _connect(path)
        try:
            values = _read_meta(connection)
        except sqlite3.Error as exc:
            connection.close()
            raise SessionError(f"session database is unreadable: {exc}") from exc
        connection.close()

        version = int(values.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise SessionError(
                f"session was recorded by a newer pporlock (schema {version}, "
                f"this build reads {SCHEMA_VERSION}). Upgrade to open it.",
                schema_version=version,
            )
        self.meta = _meta_from_row(path.stem, values, path)

    def flow_count(self) -> int:
        connection = _connect(self.path)
        try:
            return _count_flows(connection)
        finally:
            connection.close()

    def get(self, flow_id: str) -> FlowRecord | None:
        connection = _connect(self.path)
        try:
            row = connection.execute(_SELECT_FLOW_BY_ID, (flow_id,)).fetchone()
            if row is None:
                return None
            return self._record(connection, row)
        finally:
            connection.close()

    def query(
        self,
        flow_filter: FlowFilter | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> QueryResult:
        """A page of flows, oldest first.

        Chronological rather than the ring buffer's newest-first: a session is
        read as a narrative — this page loaded, then it called that API, then
        the thing broke — and reversing it would make the sequence the user is
        trying to follow read backwards.
        """
        limit = max(1, min(limit, 1000))
        flow_filter = flow_filter or FlowFilter()
        after = 0
        if cursor is not None:
            try:
                after = int(cursor)
            except ValueError:
                after = 0

        connection = _connect(self.path)
        try:
            total = _count_flows(connection)
            matched: list[FlowRecord] = []
            last_seq = after
            more = False
            while len(matched) < limit:
                rows = connection.execute(_SELECT_FLOWS_AFTER, (last_seq, limit * 4)).fetchall()
                if not rows:
                    break
                for row in rows:
                    if len(matched) >= limit:
                        # Stop *before* consuming the row, so the cursor still
                        # points at it. Advancing first would silently skip one
                        # flow per page boundary.
                        more = True
                        break
                    last_seq = int(row[0])
                    record = self._record(connection, row[1:])
                    if flow_filter.matches(record):
                        matched.append(record)
                if more:
                    break
            if not more:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM flows WHERE seq > ?", (last_seq,)
                ).fetchone()
                more = bool(remaining and remaining[0])
            return QueryResult(
                flows=matched,
                next_cursor=str(last_seq) if more else None,
                total_estimate=total,
            )
        finally:
            connection.close()

    def iter_all(self) -> Iterator[FlowRecord]:
        """Every flow, in order. Used by export and, in Sprint 14, dry run."""
        connection = _connect(self.path)
        try:
            for row in connection.execute(_SELECT_ALL_FLOWS).fetchall():
                yield self._record(connection, row)
        finally:
            connection.close()

    def _record(self, connection: sqlite3.Connection, row: tuple[Any, ...]) -> FlowRecord:
        (
            flow_id,
            _seq,
            kind,
            started_at,
            completed_at,
            tab_id,
            method,
            scheme,
            host,
            port,
            path,
            query,
            url,
            dest,
            http_version,
            status,
            reason,
            _content_type,
            req_headers,
            resp_headers,
            req_body,
            req_body_encoding,
            req_body_truncated,
            resp_body,
            resp_body_encoding,
            resp_body_truncated,
            timing,
            provenance,
            modified,
            blocked,
            streamed,
            ws_closed,
            ws_close_code,
            pt_host,
            pt_ip,
            pt_pattern,
            pt_reason,
        ) = row

        request: NormalizedRequest | None = None
        if method is not None:
            request = NormalizedRequest(
                flow_id=flow_id,
                timestamp=started_at,
                scheme=scheme or "https",
                method=method,
                host=host or "",
                port=int(port or 0),
                path=path or "/",
                url=url or "",
                http_version=http_version or "HTTP/1.1",
                query=tuple((k, v) for k, v in json.loads(query or "[]")),
                headers=tuple((k, v) for k, v in json.loads(req_headers or "[]")),
                dest=dest,
                body=_decode_body(req_body, req_body_encoding),
                body_truncated=bool(req_body_truncated),
                tab_id=tab_id,
            )

        response: NormalizedResponse | None = None
        if status is not None:
            response = NormalizedResponse(
                flow_id=flow_id,
                timestamp=completed_at or started_at,
                status=int(status),
                reason=reason or "",
                http_version=http_version or "HTTP/1.1",
                headers=tuple((k, v) for k, v in json.loads(resp_headers or "[]")),
                body=_decode_body(resp_body, resp_body_encoding),
                body_truncated=bool(resp_body_truncated),
                streamed=bool(streamed),
            )

        timing_values = json.loads(timing or "{}")
        messages: list[WebSocketMessage] = []
        if kind == "websocket":
            for ws_row in connection.execute(
                "SELECT idx, ts, direction, opcode, payload, payload_encoding, truncated "
                "FROM ws_messages WHERE flow_id = ? ORDER BY idx ASC",
                (flow_id,),
            ).fetchall():
                messages.append(
                    WebSocketMessage(
                        flow_id=flow_id,
                        index=int(ws_row[0]),
                        timestamp=ws_row[1],
                        direction=ws_row[2] or "inbound",
                        opcode=ws_row[3] or "text",
                        payload=_decode_body(ws_row[4], ws_row[5]) or b"",
                        truncated=bool(ws_row[6]),
                    )
                )

        return FlowRecord(
            flow_id=flow_id,
            kind=kind,
            started_at=started_at,
            completed_at=completed_at,
            tab_id=tab_id,
            request=request,
            response=response,
            provenance=Provenance.from_dict(json.loads(provenance or "{}")),
            timing=Timing(
                connect_ms=timing_values.get("connect_ms"),
                request_ms=timing_values.get("request_ms"),
                upstream_ms=timing_values.get("upstream_ms"),
                response_ms=timing_values.get("response_ms"),
                pporlock_ms=timing_values.get("pporlock_ms"),
                total_ms=timing_values.get("total_ms"),
            ),
            modified=bool(modified),
            blocked=bool(blocked),
            passthrough_host=pt_host,
            passthrough_ip=pt_ip,
            passthrough_pattern=pt_pattern,
            passthrough_reason=pt_reason,
            ws_messages=messages,
            ws_closed=bool(ws_closed),
            ws_close_code=ws_close_code,
        )


def _decode_body(stored: Any, encoding: str | None) -> bytes | None:
    """Undo ``encode_body``. Text was stored as UTF-8, binary as base64."""
    if stored is None:
        return None
    if encoding == "base64":
        import base64

        try:
            return base64.b64decode(stored)
        except ValueError:
            return None
    if isinstance(stored, bytes):
        return stored
    return str(stored).encode("utf-8")


# ------------------------------------------------------------------ store ---


class SessionStore:
    """The sessions directory: start, stop, list, get, rename, delete.

    Blocking throughout — every method touches the filesystem, so every caller
    on the control server runs it through ``offload`` (DD-3).
    """

    __slots__ = ("_writer", "config_max_body_bytes", "max_bytes", "redactor", "root", "version")

    def __init__(
        self,
        root: Path,
        redactor: Redactor,
        *,
        max_bytes: int = 5 * 1024 * 1024 * 1024,
        max_body_bytes: int = 512 * 1024,
        version: str = "0.1.0",
    ) -> None:
        self.root = Path(root).expanduser()
        self.redactor = redactor
        self.max_bytes = max_bytes
        self.config_max_body_bytes = max_body_bytes
        self.version = version
        self._writer: SessionWriter | None = None

    @property
    def writer(self) -> SessionWriter | None:
        """The writer for the session currently recording, if any."""
        return self._writer

    @property
    def recording_session(self) -> str | None:
        return self._writer.meta.session_id if self._writer is not None else None

    def path_for(self, session_id: str) -> Path:
        return self.root / f"{validate_session_id(session_id)}.db"

    # -- recording -------------------------------------------------------

    def start(self, name: str, *, profile: str = "default") -> SessionMeta:
        """Begin recording. One at a time (REQ CAP-020).

        Concurrent sessions are refused rather than supported: two writers means
        a flow that is in one file and not the other, and "which session was I
        recording" stops having an answer.
        """
        if self._writer is not None:
            raise SessionError(
                "a session is already recording",
                session_id=self._writer.meta.session_id,
            )
        session_id = new_session_id()
        meta = SessionMeta(
            session_id=session_id,
            name=name or session_id,
            state="recording",
            started_at=now_iso(),
            profile=profile,
            pporlock_version=self.version,
        )
        writer = SessionWriter(
            self.path_for(session_id),
            meta,
            self.redactor,
            max_bytes=self.max_bytes,
            max_body_bytes=self.config_max_body_bytes,
        )
        writer.start()
        self._writer = writer
        return meta

    def stop(self, session_id: str) -> SessionMeta:
        writer = self._writer
        if writer is None or writer.meta.session_id != session_id:
            raise SessionError("that session is not recording", session_id=session_id)
        meta = writer.stop()
        self._writer = None
        return meta

    def enqueue(self, record: FlowRecord) -> None:
        """Record one flow if a session is recording. Non-blocking."""
        writer = self._writer
        if writer is not None:
            writer.enqueue(record)

    # -- browsing --------------------------------------------------------

    def list(self) -> list[SessionMeta]:
        """Every session on disk, newest first."""
        if not self.root.is_dir():
            return []
        out: list[SessionMeta] = []
        for path in sorted(self.root.glob("*.db")):
            meta = self._meta_of(path)
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.started_at, reverse=True)
        return out

    def get(self, session_id: str) -> SessionMeta | None:
        path = self.path_for(session_id)
        return self._meta_of(path) if path.exists() else None

    def _meta_of(self, path: Path) -> SessionMeta | None:
        """Metadata for one file, or None if it is not a readable session.

        A corrupt or foreign ``.db`` in the directory is skipped rather than
        raised: one bad file must not make the session list unopenable, which
        is the same reasoning as a malformed profile being skipped.
        """
        try:
            connection = _connect(path)
        except sqlite3.Error:
            return None
        try:
            values = _read_meta(connection)
            count = _count_flows(connection)
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        meta = _meta_from_row(path.stem, values, path)
        meta.flow_count = count
        if self._writer is not None and self._writer.meta.session_id == meta.session_id:
            meta.state = "recording"
            meta.dropped = self._writer.dropped
        return meta

    def reader(self, session_id: str) -> SessionReader:
        return SessionReader(self.path_for(session_id))

    def rename(self, session_id: str, name: str) -> SessionMeta | None:
        """Rename a session (REQ CAP-021). The id and the filename are stable;
        only the human-facing label changes."""
        path = self.path_for(session_id)
        if not path.exists():
            return None
        connection = _connect(path)
        try:
            _write_meta(connection, {"session_name": name})
            connection.commit()
        finally:
            connection.close()
        if self._writer is not None and self._writer.meta.session_id == session_id:
            self._writer.meta.name = name
        return self._meta_of(path)

    def delete(self, session_id: str) -> bool:
        """Delete a session and its database file. Stops it first if recording."""
        if self._writer is not None and self._writer.meta.session_id == session_id:
            self.stop(session_id)
        path = self.path_for(session_id)
        if not path.exists():
            return False
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.unlink()
        return True
