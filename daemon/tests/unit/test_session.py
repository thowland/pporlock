"""Session storage. SPEC-1 §6.3, REQ CAP-020/021/023/045.

The test that matters most in this file is
``TestNothingUnredactedReachesDisk``. It opens the recorded database with a
subprocess that never imports pporlock, and greps the raw bytes of the file and
its WAL. If redaction is ever moved from write time to read time, that test
fails — which is the entire point of it existing.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pporlock.capture.filters import FlowFilter
from pporlock.capture.records import FlowRecord, Timing
from pporlock.capture.redact import Redactor, is_masked
from pporlock.capture.session import (
    SCHEMA_VERSION,
    SessionReader,
    SessionStore,
    SessionWriter,
    validate_session_id,
)
from pporlock.config import RedactionConfig
from pporlock.engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage
from pporlock.engine.provenance import (
    NoteCode,
    Provenance,
    ProvenanceNote,
    Severity,
)
from pporlock.errors import SessionError

COOKIE_SECRET = "session=9f3ac1de4b7711efbc1f0242ac120002; theme=dark"
BEARER_SECRET = "Bearer eyJzdXBlci1zZWNyZXQtdmFsdWUtbm9ib2R5LXNob3VsZC1zZWV9"
BODY_SECRET = "correct-horse-battery-staple"


def secret_record(flow_id: str = "f0", *, host: str = "api.example.com") -> FlowRecord:
    """A flow carrying every kind of secret redaction is supposed to catch."""
    request = NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="POST",
        host=host,
        port=443,
        path="/v1/login",
        url=f"https://{host}/v1/login",
        headers=(
            ("accept", "*/*"),
            ("cookie", COOKIE_SECRET),
            ("authorization", BEARER_SECRET),
        ),
        body=json.dumps({"user": "tim", "password": BODY_SECRET}).encode(),
    )
    response = NormalizedResponse(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:01.000Z",
        status=200,
        reason="OK",
        headers=(("content-type", "application/json"), ("set-cookie", COOKIE_SECRET)),
        body=json.dumps({"access_token": BODY_SECRET}).encode(),
    )
    provenance = Provenance(
        profile="default",
        notes=(
            ProvenanceNote(
                code=NoteCode.CSP_MODIFIED, severity=Severity.WARNING, message="csp relaxed"
            ),
        ),
    )
    return FlowRecord(
        flow_id=flow_id,
        kind="http",
        started_at="2026-08-27T14:00:00.000Z",
        completed_at="2026-08-27T14:00:01.000Z",
        request=request,
        response=response,
        provenance=provenance,
        timing=Timing(pporlock_ms=1.5, total_ms=42.0),
    )


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions", Redactor())


def record_one_session(store: SessionStore, *records: FlowRecord) -> Path:
    meta = store.start("secrets")
    for record in records or (secret_record(),):
        store.enqueue(record)
    store.stop(meta.session_id)
    return store.path_for(meta.session_id)


# ------------------------------------------------------------------------
# The exit criterion for this sprint.
# ------------------------------------------------------------------------


class TestNothingUnredactedReachesDisk:
    """REQ CAP-045. A session file on disk never contains the secret."""

    def test_raw_bytes_contain_no_cookie_or_authorization_value(self, store: SessionStore) -> None:
        """Greps every byte of the database, WAL, and shared-memory file.

        Byte-level rather than row-level deliberately: a value can survive in a
        freelist page or an uncheckpointed WAL frame long after the row that
        held it was replaced, and a SELECT would not see it.
        """
        path = record_one_session(store)

        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if not candidate.exists():
                continue
            raw = candidate.read_bytes()
            for secret in (COOKIE_SECRET, BEARER_SECRET, BODY_SECRET):
                assert secret.encode() not in raw, (
                    f"unredacted secret found in {candidate.name}. Redaction must be "
                    f"applied at write time, before any parameter is bound (REQ CAP-045)."
                )

    def test_an_external_sqlite_reader_sees_only_masked_values(
        self, store: SessionStore, tmp_path: Path
    ) -> None:
        """Opened by a subprocess that never imports pporlock.

        A check written against our own reader could pass because our reader
        redacts. This one cannot: it is plain ``sqlite3`` in a fresh
        interpreter, which is exactly how a user would open the file.
        """
        path = record_one_session(store)

        script = textwrap.dedent(
            """
            import json, sqlite3, sys
            assert "pporlock" not in sys.modules
            conn = sqlite3.connect(sys.argv[1])
            found = []
            cursor = conn.execute(
                "SELECT req_headers, resp_headers, req_body, resp_body FROM flows"
            )
            for row in cursor:
                for cell in row:
                    if cell is None:
                        continue
                    found.append(
                        cell if isinstance(cell, str) else cell.decode("utf-8", "replace")
                    )
            print(json.dumps(found))
            """
        )
        script_path = tmp_path / "reader.py"
        script_path.write_text(script)
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(script_path), str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        cells = json.loads(completed.stdout)
        blob = "\n".join(cells)

        assert COOKIE_SECRET not in blob
        assert BEARER_SECRET not in blob
        assert BODY_SECRET not in blob
        assert "«redacted:sha1=" in blob

    def test_the_ring_record_handed_to_the_writer_is_not_mutated(self, store: SessionStore) -> None:
        """Redaction copies. If it stripped the caller's record in place, the
        live buffer would lose the values unmasking exists to reveal."""
        record = secret_record()
        record_one_session(store, record)
        assert record.request is not None
        assert record.request.header("cookie") == COOKIE_SECRET

    def test_the_reader_cannot_unmask(self) -> None:
        """REQ CAP-043. Not "refuses to" — has no such method, and no live
        buffer to read one from."""
        assert not hasattr(SessionReader, "unmask")
        assert not hasattr(SessionReader, "resolve_field")


# ------------------------------------------------------------------------


class TestSchema:
    def test_the_schema_file_ships_beside_the_module(self) -> None:
        from pporlock.capture.session import SCHEMA_PATH

        assert SCHEMA_PATH.is_file()

    def test_journal_mode_is_wal(self, store: SessionStore) -> None:
        """SPEC-1 §6.3. A reader browsing the current session must not fight the
        writer still recording it."""
        path = record_one_session(store)
        connection = sqlite3.connect(path)
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        connection.close()

    def test_meta_records_the_schema_version(self, store: SessionStore) -> None:
        path = record_one_session(store)
        connection = sqlite3.connect(path)
        values = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        connection.close()
        assert int(values["schema_version"]) == SCHEMA_VERSION

    def test_meta_records_the_redaction_config_in_force(self, store: SessionStore) -> None:
        """So a session opened later says what it was redacted with."""
        path = record_one_session(store)
        connection = sqlite3.connect(path)
        values = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        connection.close()
        cfg = json.loads(values["redaction_config"])
        assert cfg["enabled"] is True
        assert "cookie" in cfg["header_patterns"]

    def test_notes_are_indexed_out_of_provenance(self, store: SessionStore) -> None:
        path = record_one_session(store)
        connection = sqlite3.connect(path)
        codes = [r[0] for r in connection.execute("SELECT code FROM notes")]
        connection.close()
        assert "csp_modified" in codes

    def test_a_newer_schema_is_refused_with_a_clear_message(self, store: SessionStore) -> None:
        path = record_one_session(store)
        connection = sqlite3.connect(path)
        connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", ("99",))
        connection.commit()
        connection.close()
        with pytest.raises(SessionError, match="newer pporlock"):
            SessionReader(path)


class TestWriterDiscipline:
    def test_enqueue_never_blocks_when_the_queue_is_full(self, tmp_path: Path) -> None:
        """REQ CAP-023. Recording must never backpressure the proxy."""
        writer = SessionWriter(
            tmp_path / "s.db",
            _meta("s1"),
            Redactor(),
            queue_max=2,
            batch_interval_s=60.0,  # keep the drain thread parked
        )
        writer.start()
        started = time.monotonic()
        for i in range(200):
            writer.enqueue(secret_record(f"f{i}"))
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, "enqueue blocked; the loop would have stalled"
        assert writer.dropped > 0

    def test_drops_are_counted_and_visible(self, tmp_path: Path) -> None:
        writer = SessionWriter(
            tmp_path / "s.db", _meta("s1"), Redactor(), queue_max=1, batch_interval_s=60.0
        )
        writer.start()
        for i in range(50):
            writer.enqueue(secret_record(f"f{i}"))
        assert writer.dropped >= 40
        meta = writer.stop()
        assert meta.dropped == writer.dropped

    def test_the_size_cap_stops_growth_rather_than_erroring(self, tmp_path: Path) -> None:
        """REQ CAP-023 — a configurable per-session size cap."""
        writer = SessionWriter(
            tmp_path / "s.db", _meta("s1"), Redactor(), max_bytes=2048, batch_interval_s=0.05
        )
        writer.start()
        for i in range(200):
            writer.enqueue(secret_record(f"f{i}"))
        meta = writer.stop()
        assert writer.capped
        assert meta.dropped > 0
        assert meta.flow_count < 200

    def test_writes_land_in_batches_and_all_arrive(self, tmp_path: Path) -> None:
        writer = SessionWriter(
            tmp_path / "s.db", _meta("s1"), Redactor(), batch_size=5, batch_interval_s=0.05
        )
        writer.start()
        for i in range(37):
            writer.enqueue(secret_record(f"f{i}"))
        meta = writer.stop()
        assert meta.flow_count == 37
        assert meta.dropped == 0

    def test_stop_checkpoints_the_wal(self, tmp_path: Path) -> None:
        """A stopped session is one file the user can copy or hand over."""
        writer = SessionWriter(tmp_path / "s.db", _meta("s1"), Redactor())
        writer.start()
        writer.enqueue(secret_record())
        writer.stop()
        wal = tmp_path / "s.db-wal"
        assert not wal.exists() or wal.stat().st_size == 0

    def test_bodies_are_capped(self, tmp_path: Path) -> None:
        """REQ CAP-023 — a configurable body cap."""
        record = secret_record()
        assert record.response is not None
        big = NormalizedResponse(flow_id="f0", timestamp="t", status=200, body=b"x" * 10_000)
        record.response = big
        writer = SessionWriter(tmp_path / "s.db", _meta("s1"), Redactor(), max_body_bytes=100)
        writer.start()
        writer.enqueue(record)
        writer.stop()
        reader = SessionReader(tmp_path / "s.db")
        stored = next(iter(reader.iter_all()))
        assert stored.response is not None and stored.response.body is not None
        assert len(stored.response.body) == 100


class TestRoundTrip:
    def test_a_flow_comes_back_as_a_flow_record(self, store: SessionStore) -> None:
        path = record_one_session(store)
        reader = SessionReader(path)
        record = reader.get("f0")
        assert record is not None
        assert record.request is not None
        assert record.request.method == "POST"
        assert record.request.host == "api.example.com"
        assert record.response is not None and record.response.status == 200
        assert record.timing.pporlock_ms == 1.5

    def test_provenance_survives_the_round_trip(self, store: SessionStore) -> None:
        """REQ CAP-013 — provenance travels with every flow into every
        consumer, and a session flow is a consumer."""
        reader = SessionReader(record_one_session(store))
        record = reader.get("f0")
        assert record is not None and record.provenance is not None
        assert record.provenance.has_note(NoteCode.CSP_MODIFIED)
        assert record.provenance.profile == "default"

    def test_what_comes_back_is_masked(self, store: SessionStore) -> None:
        reader = SessionReader(record_one_session(store))
        record = reader.get("f0")
        assert record is not None and record.request is not None
        assert is_masked(record.request.header("cookie") or "")
        assert is_masked(record.request.header("authorization") or "")
        assert record.request.header("accept") == "*/*"

    def test_websocket_messages_round_trip(self, store: SessionStore) -> None:
        """REQ PXY-050 — direction, opcode, size, timestamp, payload."""
        record = FlowRecord(
            flow_id="ws0",
            kind="websocket",
            started_at="2026-08-27T14:00:00.000Z",
            ws_messages=[
                WebSocketMessage(
                    flow_id="ws0",
                    index=0,
                    timestamp="2026-08-27T14:00:01.000Z",
                    direction="outbound",
                    opcode="text",
                    payload=b"hello",
                ),
                WebSocketMessage(
                    flow_id="ws0",
                    index=1,
                    timestamp="2026-08-27T14:00:02.000Z",
                    direction="inbound",
                    opcode="binary",
                    payload=b"\x00\x01\x02",
                ),
            ],
            ws_closed=True,
            ws_close_code=1000,
        )
        reader = SessionReader(record_one_session(store, record))
        stored = reader.get("ws0")
        assert stored is not None
        assert stored.kind == "websocket"
        assert [m.direction for m in stored.ws_messages] == ["outbound", "inbound"]
        assert [m.opcode for m in stored.ws_messages] == ["text", "binary"]
        assert stored.ws_messages[1].payload == b"\x00\x01\x02"
        assert stored.ws_closed and stored.ws_close_code == 1000

    def test_a_binary_body_survives(self, store: SessionStore) -> None:
        record = secret_record()
        record.response = NormalizedResponse(
            flow_id="f0", timestamp="t", status=200, body=b"\xff\xfe\x00binary"
        )
        reader = SessionReader(record_one_session(store, record))
        stored = reader.get("f0")
        assert stored is not None and stored.response is not None
        assert stored.response.body == b"\xff\xfe\x00binary"


class TestBrowsing:
    def build(self, store: SessionStore, count: int = 25) -> str:
        meta = store.start("browse")
        for i in range(count):
            store.enqueue(secret_record(f"f{i}", host=f"h{i % 3}.example.com"))
        store.stop(meta.session_id)
        return meta.session_id

    def test_paging_walks_the_whole_session_once(self, store: SessionStore) -> None:
        session_id = self.build(store)
        reader = store.reader(session_id)
        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = reader.query(limit=7, cursor=cursor)
            seen.extend(f.flow_id for f in page.flows)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == 25
        assert len(set(seen)) == 25

    def test_pages_are_chronological(self, store: SessionStore) -> None:
        """A session is read as a narrative; the ring buffer's newest-first
        order would make the sequence read backwards."""
        session_id = self.build(store, 5)
        page = store.reader(session_id).query(limit=5)
        assert [f.flow_id for f in page.flows] == ["f0", "f1", "f2", "f3", "f4"]

    def test_the_filter_vocabulary_is_the_same_one(self, store: SessionStore) -> None:
        """REQ CAP-004 — one filter definition, shared with the ring buffer."""
        session_id = self.build(store)
        page = store.reader(session_id).query(FlowFilter(host="h1.example.com"), limit=100)
        assert page.flows
        assert all(f.host == "h1.example.com" for f in page.flows)

    def test_a_filter_matching_nothing_returns_an_empty_page(self, store: SessionStore) -> None:
        session_id = self.build(store)
        page = store.reader(session_id).query(FlowFilter(host="absent.example"), limit=10)
        assert page.flows == [] and page.next_cursor is None

    def test_a_bad_cursor_starts_from_the_beginning(self, store: SessionStore) -> None:
        session_id = self.build(store, 3)
        page = store.reader(session_id).query(limit=10, cursor="not-a-number")
        assert len(page.flows) == 3

    def test_iter_all_yields_every_flow(self, store: SessionStore) -> None:
        session_id = self.build(store, 9)
        assert len(list(store.reader(session_id).iter_all())) == 9


class TestStoreLifecycle:
    def test_recording_is_off_until_started(self, store: SessionStore) -> None:
        """REQ CAP-020 — opt-in, off by default."""
        assert store.recording_session is None
        store.enqueue(secret_record())  # must be a no-op, not an error
        assert store.list() == []

    def test_start_stop_list_get(self, store: SessionStore) -> None:
        meta = store.start("morning debugging")
        assert store.recording_session == meta.session_id
        store.enqueue(secret_record())
        stopped = store.stop(meta.session_id)

        assert stopped.state == "stopped"
        assert stopped.stopped_at is not None
        assert stopped.flow_count == 1
        assert store.recording_session is None

        listed = store.list()
        assert [s.session_id for s in listed] == [meta.session_id]
        assert listed[0].name == "morning debugging"
        assert listed[0].size_bytes > 0

        assert store.get(meta.session_id) is not None
        assert store.get("sdeadbeef") is None

    def test_only_one_session_records_at_a_time(self, store: SessionStore) -> None:
        first = store.start("a")
        with pytest.raises(SessionError, match="already recording"):
            store.start("b")
        store.stop(first.session_id)

    def test_stopping_a_session_that_is_not_recording_is_refused(self, store: SessionStore) -> None:
        with pytest.raises(SessionError):
            store.stop("snothing")

    def test_rename(self, store: SessionStore) -> None:
        """REQ CAP-021 — sessions are renamable."""
        meta = store.start("untitled")
        store.stop(meta.session_id)
        renamed = store.rename(meta.session_id, "the CSP bug")
        assert renamed is not None and renamed.name == "the CSP bug"
        assert store.get(meta.session_id) is not None
        assert store.get(meta.session_id).name == "the CSP bug"  # type: ignore[union-attr]

    def test_renaming_an_absent_session_returns_none(self, store: SessionStore) -> None:
        assert store.rename("sabsent", "x") is None

    def test_delete_removes_the_file_and_its_sidecars(self, store: SessionStore) -> None:
        meta = store.start("doomed")
        store.enqueue(secret_record())
        store.stop(meta.session_id)
        path = store.path_for(meta.session_id)
        assert path.exists()

        assert store.delete(meta.session_id) is True
        assert not path.exists()
        assert not Path(str(path) + "-wal").exists()
        assert store.delete(meta.session_id) is False

    def test_deleting_the_recording_session_stops_it_first(self, store: SessionStore) -> None:
        meta = store.start("live")
        assert store.delete(meta.session_id) is True
        assert store.recording_session is None

    def test_a_foreign_database_in_the_directory_is_skipped_not_fatal(
        self, store: SessionStore
    ) -> None:
        """One unreadable file must not make the whole list unopenable."""
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "junk.db").write_bytes(b"not a database at all")
        meta = store.start("real")
        store.stop(meta.session_id)
        assert [s.session_id for s in store.list()] == [meta.session_id]

    def test_the_recording_session_reports_its_live_state(self, store: SessionStore) -> None:
        meta = store.start("live")
        listed = store.list()
        assert listed[0].state == "recording"
        store.stop(meta.session_id)
        assert store.list()[0].state == "stopped"

    def test_redaction_is_on_by_default_for_sessions(self, tmp_path: Path) -> None:
        """REQ CAP-040 — on by default for data written to sessions."""
        assert RedactionConfig().enabled is True
        assert Redactor().enabled is True


class TestSessionIdValidation:
    @pytest.mark.parametrize(
        "session_id", ["../../etc/passwd", "a/b", "", "x" * 200, "a b", "SELECT"]
    )
    def test_a_path_or_odd_id_is_refused(self, session_id: str) -> None:
        """The id becomes a filename; ``..`` in it would be a traversal."""
        with pytest.raises(SessionError):
            validate_session_id(session_id)

    def test_a_generated_id_is_accepted(self, store: SessionStore) -> None:
        meta = store.start("x")
        assert validate_session_id(meta.session_id) == meta.session_id
        store.stop(meta.session_id)

    def test_the_store_refuses_to_build_a_path_from_a_bad_id(self, store: SessionStore) -> None:
        with pytest.raises(SessionError):
            store.path_for("../escape")


def _meta(session_id: str) -> object:
    from pporlock.capture.session import SessionMeta

    return SessionMeta(
        session_id=session_id,
        name=session_id,
        started_at="2026-08-27T14:00:00.000Z",
    )
