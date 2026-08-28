"""Tab attribution. SPEC-0 §3.6, SPEC-1 §6.6, REQ OI-2."""

from __future__ import annotations

import time

import pytest

from pporlock.capture.attribution import (
    AttributionEntry,
    AttributionIndex,
    coverage_of,
    entry_from_dict,
)
from pporlock.capture.records import FlowRecord


def entry(
    url: str = "https://a.example/x", tab: int = 7, ts: float | None = None
) -> AttributionEntry:
    return AttributionEntry(method="GET", url=url, ts=time.time() if ts is None else ts, tab_id=tab)


class TestParsing:
    def test_parses_a_wire_entry(self) -> None:
        parsed = entry_from_dict(
            {"method": "GET", "url": "https://a.example/x", "tabId": 7, "ts": 1756300000000}
        )
        assert parsed is not None
        assert parsed.tab_id == 7
        assert parsed.method == "GET"

    def test_normalises_epoch_milliseconds_to_seconds(self) -> None:
        """The extension sends chrome.webRequest timeStamp, which is in ms."""
        parsed = entry_from_dict({"method": "GET", "url": "u", "tabId": 1, "ts": 1756300000000})
        assert parsed is not None
        assert 1e9 < parsed.ts < 1e10

    def test_accepts_seconds_unchanged(self) -> None:
        parsed = entry_from_dict({"method": "GET", "url": "u", "tabId": 1, "ts": 1756300000})
        assert parsed is not None
        assert parsed.ts == 1756300000

    def test_defaults_the_timestamp_when_absent(self) -> None:
        parsed = entry_from_dict({"method": "GET", "url": "u", "tabId": 1})
        assert parsed is not None
        assert parsed.ts > 0

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"method": "GET"},
            {"method": "GET", "url": "u"},
            {"method": "GET", "url": "u", "tabId": "not a number"},
        ],
    )
    def test_returns_none_rather_than_raising(self, raw: dict) -> None:
        """A malformed entry must not fail the whole batch: dropping one
        association is far better than dropping a hundred."""
        assert entry_from_dict(raw) is None

    def test_tolerates_a_bad_frame_id(self) -> None:
        parsed = entry_from_dict({"method": "GET", "url": "u", "tabId": 1, "frameId": "x"})
        assert parsed is not None
        assert parsed.frame_id == 0


class TestResolution:
    def test_resolves_a_submitted_observation(self) -> None:
        index = AttributionIndex()
        index.submit([entry()])
        assert index.resolve("GET", "https://a.example/x") == 7

    def test_method_matching_is_case_insensitive(self) -> None:
        index = AttributionIndex()
        index.submit([entry()])
        assert index.resolve("get", "https://a.example/x") == 7

    def test_unknown_request_is_unattributed(self) -> None:
        assert AttributionIndex().resolve("GET", "https://nothing.example/") is None

    def test_resolution_consumes_the_association(self) -> None:
        """A repeated URL in a different tab must not inherit the first tab."""
        index = AttributionIndex()
        index.submit([entry()])
        assert index.resolve("GET", "https://a.example/x") == 7
        assert index.resolve("GET", "https://a.example/x") is None

    def test_an_observation_outside_the_window_does_not_match(self) -> None:
        # Old enough to fail the join window, young enough to survive eviction.
        index = AttributionIndex(window_seconds=1.0)
        index.submit([entry(ts=time.time() - 1.5)])
        assert index.resolve("GET", "https://a.example/x") is None

    def test_a_near_miss_is_left_for_a_later_flow(self) -> None:
        """It failed the window against *this* flow, not against every flow.
        Consuming it on a near miss would lose an association that a slightly
        later flow could legitimately claim."""
        index = AttributionIndex(window_seconds=1.0)
        observed_at = time.time() - 1.5
        index.submit([entry(ts=observed_at)])
        assert index.resolve("GET", "https://a.example/x") is None
        assert index.resolve("GET", "https://a.example/x", when=observed_at) == 7

    def test_an_observation_far_older_than_the_window_is_discarded(self) -> None:
        """It will never match anything, so holding it only costs memory."""
        index = AttributionIndex(window_seconds=1.0)
        index.submit([entry(ts=time.time() - 30)])
        assert index.pending == 0

    def test_different_tabs_for_different_urls(self) -> None:
        index = AttributionIndex()
        index.submit([entry("https://a.example/1", 1), entry("https://a.example/2", 2)])
        assert index.resolve("GET", "https://a.example/1") == 1
        assert index.resolve("GET", "https://a.example/2") == 2


class TestBounds:
    def test_pending_is_capped(self) -> None:
        """A browser submitting faster than flows arrive must not grow this
        index without limit."""
        index = AttributionIndex(max_pending=10)
        index.submit([entry(f"https://a.example/{i}") for i in range(100)])
        assert index.pending <= 10

    def test_overflow_is_counted_not_silent(self) -> None:
        index = AttributionIndex(max_pending=10)
        index.submit([entry(f"https://a.example/{i}") for i in range(100)])
        assert index.stats.dropped > 0

    def test_the_newest_observations_survive(self) -> None:
        index = AttributionIndex(max_pending=5)
        index.submit([entry(f"https://a.example/{i}", tab=i) for i in range(10)])
        assert index.resolve("GET", "https://a.example/9") == 9
        assert index.resolve("GET", "https://a.example/0") is None

    def test_entries_older_than_the_window_are_aged_out(self) -> None:
        index = AttributionIndex(window_seconds=0.01)
        index.submit([entry(ts=time.time() - 10)])
        index.submit([entry("https://b.example/", tab=2)])
        assert index.pending <= 1

    def test_clear(self) -> None:
        index = AttributionIndex()
        index.submit([entry()])
        index.clear()
        assert index.pending == 0


def flow(flow_id: str, kind: str = "http", tab_id: int | None = None) -> FlowRecord:
    return FlowRecord(flow_id=flow_id, kind=kind, started_at="t", tab_id=tab_id)  # type: ignore[arg-type]


class TestCoverage:
    """SPEC-0 §3.6 states the criterion over *flows*, so that is what is measured.

    The index's own counters count join attempts: backfill re-tries every
    unattributed flow on each submission, so one stubbornly unattributable flow
    inflates them without bound. Useful for spotting a broken join, useless as
    the coverage figure.
    """

    def test_is_none_before_any_flows(self) -> None:
        assert coverage_of([]).fraction is None

    def test_full_coverage(self) -> None:
        records = [flow(f"f{i}", tab_id=i) for i in range(10)]
        assert coverage_of(records).fraction == 1.0

    def test_partial_coverage(self) -> None:
        records = [flow("a", tab_id=1), flow("b")]
        assert coverage_of(records).fraction == 0.5

    def test_ignores_passthrough_flows(self) -> None:
        """A tunneled connection was never decrypted; attribution cannot apply."""
        records = [flow("a", tab_id=1), flow("pt", kind="passthrough")]
        measured = coverage_of(records)
        assert measured.total == 1
        assert measured.fraction == 1.0

    def test_serializes(self) -> None:
        payload = coverage_of([flow("a", tab_id=1), flow("b")]).to_dict()
        assert payload == {"attributed": 1, "total": 2, "coverage": 0.5}

    def test_index_stats_are_diagnostics_not_coverage(self) -> None:
        index = AttributionIndex()
        index.submit([entry()])
        payload = index.stats.to_dict()
        assert payload["submitted"] == 1
        assert "resolve_attempts_missed" in payload
        # Coverage does not live here — it is measured over flows.
        assert "coverage" not in payload
