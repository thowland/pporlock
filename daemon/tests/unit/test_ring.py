"""Ring buffer and filter vocabulary. SPEC-1 §6.1, §6.2, REQ CAP-001/003/004."""

from __future__ import annotations

import pytest

from pporlock.capture.filters import FlowFilter
from pporlock.capture.records import FlowRecord, Timing, truncate
from pporlock.capture.ring import RingBuffer, encode_body
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.provenance import (
    Action,
    NoteCode,
    Outcome,
    Phase,
    ProvenanceBuilder,
)


def make_record(
    flow_id: str = "f0",
    *,
    host: str = "cdn.example.com",
    path: str = "/a.js",
    method: str = "GET",
    status: int | None = 200,
    content_type: str = "application/javascript",
    dest: str | None = "script",
    tab_id: int | None = None,
    modified: bool = False,
    blocked: bool = False,
    started_at: str = "2026-08-27T14:00:00.000Z",
    body: bytes | None = None,
    provenance: object | None = None,
) -> FlowRecord:
    request = NormalizedRequest(
        flow_id=flow_id,
        timestamp=started_at,
        scheme="https",
        method=method,
        host=host,
        port=443,
        path=path,
        url=f"https://{host}{path}",
        dest=dest,
        tab_id=tab_id,
    )
    response = (
        NormalizedResponse(
            flow_id=flow_id,
            timestamp=started_at,
            status=status,
            headers=(("content-type", content_type),),
            body=body,
        )
        if status is not None
        else None
    )
    return FlowRecord(
        flow_id=flow_id,
        kind="http",
        started_at=started_at,
        request=request,
        response=response,
        tab_id=tab_id,
        modified=modified,
        blocked=blocked,
        provenance=provenance,  # type: ignore[arg-type]
        timing=Timing(pporlock_ms=1.0),
    )


class TestTruncation:
    def test_short_body_is_untouched(self) -> None:
        assert truncate(b"abc", 10) == (b"abc", False)

    def test_long_body_is_cut_and_flagged(self) -> None:
        """REQ CAP-003. A silently shortened body makes a diff look wrong for
        reasons invisible to the user."""
        body, cut = truncate(b"x" * 100, 10)
        assert body is not None
        assert len(body) == 10
        assert cut

    def test_none_passes_through(self) -> None:
        assert truncate(None, 10) == (None, False)

    def test_exact_cap_is_not_truncated(self) -> None:
        assert truncate(b"x" * 10, 10) == (b"x" * 10, False)


class TestEncoding:
    def test_text_travels_as_utf8(self) -> None:
        assert encode_body(b"hello") == ("hello", "utf8")

    def test_binary_travels_as_base64(self) -> None:
        payload, encoding = encode_body(b"\xff\xfe\x00")
        assert encoding == "base64"
        assert payload is not None

    def test_none_stays_none(self) -> None:
        assert encode_body(None) == (None, None)


class TestBounds:
    def test_evicts_on_the_flow_bound(self) -> None:
        ring = RingBuffer(max_flows=3, max_bytes=10**9)
        for i in range(5):
            ring.add(make_record(f"f{i}"))
        assert len(ring) == 3
        assert [f.flow_id for f in ring.query().flows] == ["f4", "f3", "f2"]

    def test_evicts_on_the_byte_bound(self) -> None:
        """A flow count says nothing about a page pulling six 4 MiB videos."""
        ring = RingBuffer(max_flows=10_000, max_bytes=4096)
        for i in range(6):
            ring.add(make_record(f"f{i}", body=b"x" * 1024))
        assert len(ring) < 6
        assert ring.stats.bytes <= 4096

    def test_eviction_is_counted(self) -> None:
        ring = RingBuffer(max_flows=2, max_bytes=10**9)
        for i in range(5):
            ring.add(make_record(f"f{i}"))
        assert ring.stats.evicted == 3

    def test_replacing_an_id_does_not_double_count_bytes(self) -> None:
        ring = RingBuffer()
        ring.add(make_record("f0", body=b"x" * 1000))
        first = ring.stats.bytes
        ring.add(make_record("f0", body=b"x" * 1000))
        assert ring.stats.bytes == first
        assert len(ring) == 1

    def test_clear(self) -> None:
        ring = RingBuffer()
        ring.add(make_record())
        ring.clear()
        assert len(ring) == 0
        assert ring.stats.bytes == 0

    def test_stats_shape(self) -> None:
        stats = RingBuffer(max_flows=7, max_bytes=99).stats.to_dict()
        assert stats["ring_max_flows"] == 7
        assert stats["ring_max_bytes"] == 99


class TestAccess:
    def test_get_and_contains(self) -> None:
        ring = RingBuffer()
        ring.add(make_record("f0"))
        assert ring.get("f0") is not None
        assert "f0" in ring
        assert ring.get("nope") is None

    def test_update_patches_in_place(self) -> None:
        """Attribution backfill: a flow is delivered before its tab is known."""
        ring = RingBuffer()
        ring.add(make_record("f0"))
        updated = ring.update("f0", tab_id=481)
        assert updated is not None
        assert updated.tab_id == 481

    def test_update_of_an_evicted_flow_returns_none(self) -> None:
        assert RingBuffer().update("gone", tab_id=1) is None

    def test_update_ignores_unknown_fields(self) -> None:
        ring = RingBuffer()
        ring.add(make_record("f0"))
        assert ring.update("f0", not_a_field=1) is not None


class TestQuery:
    @pytest.fixture
    def ring(self) -> RingBuffer:
        ring = RingBuffer()
        ring.add(make_record("f0", host="a.example", path="/one.js", status=200))
        ring.add(
            make_record(
                "f1",
                host="b.example",
                path="/two.css",
                content_type="text/css",
                dest="style",
                status=404,
            )
        )
        ring.add(
            make_record(
                "f2",
                host="a.example",
                path="/three.js",
                method="POST",
                status=500,
                tab_id=7,
                modified=True,
            )
        )
        return ring

    def test_newest_first(self, ring: RingBuffer) -> None:
        assert [f.flow_id for f in ring.query().flows] == ["f2", "f1", "f0"]

    def test_limit(self, ring: RingBuffer) -> None:
        assert len(ring.query(limit=2).flows) == 2

    def test_limit_is_clamped(self, ring: RingBuffer) -> None:
        assert len(ring.query(limit=99999).flows) == 3
        assert len(ring.query(limit=0).flows) >= 1

    def test_cursor_pagination(self, ring: RingBuffer) -> None:
        first = ring.query(limit=2)
        assert first.next_cursor == "f1"
        second = ring.query(limit=2, cursor=first.next_cursor)
        assert [f.flow_id for f in second.flows] == ["f0"]

    def test_evicted_cursor_restarts_rather_than_returning_nothing(self, ring: RingBuffer) -> None:
        """The client would otherwise silently lose the rest of the page."""
        result = ring.query(cursor="long-gone")
        assert len(result.flows) == 3

    def test_no_cursor_when_exhausted(self, ring: RingBuffer) -> None:
        assert ring.query(limit=100).next_cursor is None

    def test_total_estimate_is_buffer_size_not_match_count(self, ring: RingBuffer) -> None:
        assert ring.query(FlowFilter(host="a.example")).total_estimate == 3


class TestFilters:
    @pytest.fixture
    def ring(self) -> RingBuffer:
        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_BODY,
            module="strip-csp",
            rule_id="strip-csp:0",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
        )
        builder.note(NoteCode.CSP_MODIFIED, "removed")
        ring = RingBuffer()
        ring.add(make_record("f0", host="a.example", path="/one.js", status=200))
        ring.add(
            make_record(
                "f1",
                host="b.example",
                path="/two.css",
                content_type="text/css",
                dest="style",
                status=404,
            )
        )
        ring.add(
            make_record(
                "f2",
                host="a.example",
                path="/three.js",
                method="POST",
                status=500,
                tab_id=7,
                modified=True,
                provenance=builder.build(),
            )
        )
        return ring

    def _ids(self, ring: RingBuffer, **kwargs: object) -> list[str]:
        return [f.flow_id for f in ring.query(FlowFilter(**kwargs)).flows]  # type: ignore[arg-type]

    def test_empty_filter_matches_everything(self, ring: RingBuffer) -> None:
        assert FlowFilter().is_empty
        assert len(self._ids(ring)) == 3

    def test_host_substring(self, ring: RingBuffer) -> None:
        assert self._ids(ring, host="a.example") == ["f2", "f0"]

    def test_host_glob(self, ring: RingBuffer) -> None:
        assert self._ids(ring, host="*.example") == ["f2", "f1", "f0"]

    def test_path_regex(self, ring: RingBuffer) -> None:
        assert self._ids(ring, path=r"\.css$") == ["f1"]

    def test_invalid_path_regex_matches_nothing_rather_than_raising(self, ring: RingBuffer) -> None:
        """A malformed filter is a client bug; it must not 500 the API."""
        assert self._ids(ring, path="[unclosed") == []

    def test_method(self, ring: RingBuffer) -> None:
        assert self._ids(ring, method="post") == ["f2"]

    def test_status_exact(self, ring: RingBuffer) -> None:
        assert self._ids(ring, status="404") == ["f1"]

    def test_status_range(self, ring: RingBuffer) -> None:
        assert self._ids(ring, status="400-499") == ["f1"]

    def test_status_list(self, ring: RingBuffer) -> None:
        assert self._ids(ring, status="404,500") == ["f2", "f1"]

    def test_invalid_status_matches_nothing(self, ring: RingBuffer) -> None:
        assert self._ids(ring, status="not-a-status") == []

    def test_content_type(self, ring: RingBuffer) -> None:
        assert self._ids(ring, content_type="text/css") == ["f1"]

    def test_dest(self, ring: RingBuffer) -> None:
        assert self._ids(ring, dest="style") == ["f1"]

    def test_tab_id(self, ring: RingBuffer) -> None:
        assert self._ids(ring, tab_id=7) == ["f2"]

    def test_modified(self, ring: RingBuffer) -> None:
        assert self._ids(ring, modified=True) == ["f2"]

    def test_blocked(self, ring: RingBuffer) -> None:
        assert self._ids(ring, blocked=True) == []

    def test_module_fired(self, ring: RingBuffer) -> None:
        assert self._ids(ring, module="strip-csp") == ["f2"]

    def test_note_code(self, ring: RingBuffer) -> None:
        assert self._ids(ring, note_code="csp_modified") == ["f2"]

    def test_substring_over_url(self, ring: RingBuffer) -> None:
        assert self._ids(ring, q="three") == ["f2"]

    def test_since_and_until_compare_lexically(self, ring: RingBuffer) -> None:
        """ISO 8601 at fixed width sorts chronologically, so no parsing is
        needed on the hot path."""
        assert self._ids(ring, since="2026-08-27T13:59:59.000Z") == ["f2", "f1", "f0"]
        assert self._ids(ring, until="2026-08-27T13:00:00.000Z") == []

    def test_criteria_combine_with_and(self, ring: RingBuffer) -> None:
        assert self._ids(ring, host="a.example", method="POST") == ["f2"]

    def test_passthrough_has_no_request_and_is_excluded_by_request_criteria(self) -> None:
        ring = RingBuffer()
        ring.add(
            FlowRecord(
                flow_id="pt",
                kind="passthrough",
                started_at="2026-08-27T14:00:00.000Z",
                passthrough_host="www.apple.com",
            )
        )
        assert [f.flow_id for f in ring.query(FlowFilter(host="apple")).flows] == ["pt"]
        assert ring.query(FlowFilter(method="GET")).flows == []


class TestFromQuery:
    def test_parses_each_type(self) -> None:
        parsed = FlowFilter.from_query(
            {"host": "a", "tab_id": "7", "modified": "true", "blocked": "0"}
        )
        assert parsed.host == "a"
        assert parsed.tab_id == 7
        assert parsed.modified is True
        assert parsed.blocked is False

    def test_ignores_unknown_parameters(self) -> None:
        """limit, cursor, and detail travel on the same query string."""
        assert FlowFilter.from_query({"limit": "100", "detail": "full"}).is_empty

    def test_empty_strings_are_absent_not_empty(self) -> None:
        assert FlowFilter.from_query({"host": "", "tab_id": ""}).is_empty

    def test_bad_integer_is_ignored(self) -> None:
        assert FlowFilter.from_query({"tab_id": "abc"}).tab_id is None
