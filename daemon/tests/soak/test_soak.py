"""Bounded memory over sustained traffic — REQ PRF-005.

    "Memory SHALL be bounded by the ring buffer caps (CAP-001) and SHALL not
    grow without limit over a multi-day daemon uptime. A long-running soak test
    SHALL verify this."

A multi-day run is not a test anyone will wait for, so this compresses the shape
of the failure instead of its duration: sustained traffic through the real
``Interceptor``, the real ``Evaluator`` and the real ``RingBuffer``, for long
enough that a per-flow leak — the only kind that produces the multi-day failure —
is unmistakable. A leak of a hundred bytes a flow is invisible over ten flows and
obvious over a hundred thousand.

Two decisions here are load-bearing and are the reason this test is not the
usual "allocate and hope":

* **The tail is compared against the middle, not the start.** The first slice
  pays for interpreter warm-up, regex compilation, the module import graph, and
  filling the ring for the first time. A run compared against its own start has
  a large one-time allocation in the denominator, and passes for that reason
  rather than because nothing leaked. By the middle slice the ring is full and
  evicting; from there on, flat is the only correct shape.

* **``ru_maxrss`` is reported, never asserted on.** It is a high-water mark: it
  never goes down, so it cannot distinguish "still holding it" from "held it once
  and freed it". The load-bearing assertions are on ``tracemalloc``'s *current*
  traced size, which does go down, and on the ring's own reported caps.

Marked ``slow`` and skipped unless selected with ``-m slow`` (``pyproject.toml``
declares the marker but does not deselect it, and a soak has no place in the
default run). ``PPORLOCK_SOAK_SECONDS`` lengthens it.
"""

from __future__ import annotations

import os
import resource
import statistics
import sys
import time
import tracemalloc
from typing import Any

import pytest

from pporlock.addon.interceptor import Interceptor
from pporlock.capture.ring import RingBuffer
from pporlock.capture.sink import RingSink
from pporlock.config import Config
from pporlock.engine.evaluator import Evaluator
from pporlock.engine.exclusions import ExclusionList
from pporlock.engine.ruleset import RuleSet
from tests.stubs import StubFlow, StubHeaders, StubRequest, StubResponse

pytestmark = pytest.mark.slow

#: Wall-clock budget for the main soak. A minute is enough for six figures of
#: flows on this path, which is several orders of magnitude past the point where
#: a per-flow leak stops being deniable. Raise it for a genuinely long run:
#: ``PPORLOCK_SOAK_SECONDS=1800 uv run pytest tests/soak -m slow``.
SOAK_SECONDS = float(os.environ.get("PPORLOCK_SOAK_SECONDS", "60"))

#: The run is cut into slices so growth has a shape rather than two endpoints.
SLICES = 10

#: Ring caps for the main soak. Deliberately small: the point is to reach and
#: hold the cap quickly, not to model production sizing.
SOAK_MAX_FLOWS = 100_000
SOAK_MAX_BYTES = 8 * 1024 * 1024

#: Allowed drift between the middle of the run and its tail. Generous, because
#: the ring's contents legitimately vary by a few hundred KiB as bodies of
#: different sizes cycle through it; a leak large enough to matter over days is
#: orders of magnitude past this, and a threshold tight enough to flag noise
#: would be a test nobody trusts.
TAIL_GROWTH_FACTOR = 1.25
TAIL_GROWTH_SLACK_BYTES = 2 * 1024 * 1024


@pytest.fixture(autouse=True)
def _only_when_slow_is_selected(request: pytest.FixtureRequest) -> None:
    """Keep the soak out of the default run.

    ``pyproject.toml`` declares the ``slow`` marker but its ``addopts`` do not
    deselect it, so the marker alone would not be enough. Editing ``addopts``
    would change what every other suite runs; this changes only this directory.
    """
    selected = request.config.getoption("-m") or ""
    if "slow" not in selected:
        pytest.skip("soak test: select it explicitly with `-m slow`")


# ------------------------------------------------------------ traffic model --

RULES: list[dict[str, Any]] = [
    {
        "name": "label every response",
        "action": "headers",
        "match": {"host": "*.soak.test"},
        "response": {"set": {"x-pporlock-soak": "1"}},
    },
    {
        "name": "rewrite the marker",
        "action": "body",
        "match": {"host": "app.soak.test", "content_type": "text/html"},
        "transform": {"kind": "replace_literal", "find": "MARKER", "replace": "REWRITTEN"},
    },
    {
        "name": "block the tracker",
        "action": "block",
        "match": {"host": "tracker.soak.test"},
    },
]

#: Body sizes cycled through, in bytes. A single size would let an allocator
#: reuse one block forever and hide a leak that only shows under churn.
BODY_SIZES = (256, 1_024, 4_096, 16_384, 65_536, 2_048, 512, 32_768)

HOSTS = ("app.soak.test", "cdn.soak.test", "tracker.soak.test", "api.soak.test")


def _make_interceptor(ring: RingBuffer) -> Interceptor:
    """The real addon, over the real evaluator and the real ring sink."""
    return Interceptor(
        Config(),
        sink=RingSink(ring),
        exclusions=ExclusionList(),
        evaluator=Evaluator(RuleSet.from_rules(RULES, module="soak"), exclusions=ExclusionList()),
    )


def _body(index: int) -> bytes:
    size = BODY_SIZES[index % len(BODY_SIZES)]
    filler = b"x" * max(0, size - 40)
    return b"<html><body>MARKER" + filler + b"</body></html>"


def _flow(index: int) -> StubFlow:
    """One synthetic flow, distinct from every other so nothing dedupes."""
    host = HOSTS[index % len(HOSTS)]
    path = f"/asset-{index}.html"
    request = StubRequest(
        host=host,
        pretty_host=host,
        path=path,
        url=f"https://{host}{path}",
        pretty_url=f"https://{host}{path}",
        method="GET",
        headers=StubHeaders(
            [
                (b"accept", b"text/html,*/*"),
                (b"Sec-Fetch-Dest", b"document"),
                (b"user-agent", b"pporlock-soak"),
            ]
        ),
        content=b"",
    )
    flow = StubFlow(request)
    # mitmproxy assigns a distinct id per flow; a fixed one would make the ring
    # overwrite a single record and measure nothing.
    flow.id = f"soak-{index:09d}"
    return flow


def _response(index: int) -> StubResponse:
    body = _body(index)
    return StubResponse(
        status_code=200,
        headers=StubHeaders(
            [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"content-security-policy", b"default-src 'self'"),
            ]
        ),
        content=body,
    )


async def _drive_one(interceptor: Interceptor, index: int) -> None:
    """One complete request/response cycle through the real hooks."""
    flow = _flow(index)
    interceptor.request(flow)
    if flow.response is None:
        # Not short-circuited, so an origin response arrives.
        flow.response = _response(index)
    interceptor.responseheaders(flow)
    await interceptor.response(flow)


# ------------------------------------------------------------- measurement --


def _maxrss_bytes() -> int:
    """``ru_maxrss`` in bytes.

    macOS reports bytes, Linux reports kibibytes. Normalised here so the printed
    figure means the same thing on both. NOTE: this is a HIGH-WATER MARK — it
    never decreases, so it cannot show that memory was released and must not be
    asserted on as if it were current usage. It is reported for context only.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def _mib(value: float) -> str:
    return f"{value / (1024 * 1024):.2f} MiB"


# ------------------------------------------------------------------- tests --


async def test_memory_is_bounded_over_sustained_traffic() -> None:
    """REQ PRF-005 — traced memory is flat from the middle of the run onward."""
    ring = RingBuffer(max_flows=SOAK_MAX_FLOWS, max_bytes=SOAK_MAX_BYTES)
    interceptor = _make_interceptor(ring)

    tracemalloc.start()
    try:
        # Warm the path before the first measured slice: import graphs, regex
        # compilation and the first fill of the ring are one-time costs, and
        # charging them to slice zero would put a large constant in the
        # denominator of every comparison that follows.
        for index in range(2_000):
            await _drive_one(interceptor, index)

        slice_seconds = SOAK_SECONDS / SLICES
        index = 2_000
        samples: list[dict[str, Any]] = []
        started = time.perf_counter()

        for slice_number in range(SLICES):
            deadline = time.perf_counter() + slice_seconds
            while time.perf_counter() < deadline:
                for _ in range(200):
                    await _drive_one(interceptor, index)
                    index += 1
            current, peak = tracemalloc.get_traced_memory()
            stats = ring.stats
            samples.append(
                {
                    "slice": slice_number,
                    "flows_driven": index,
                    "traced_current": current,
                    "traced_peak": peak,
                    "ring_flows": stats.flows,
                    "ring_bytes": stats.bytes,
                    "evicted": stats.evicted,
                    "maxrss": _maxrss_bytes(),
                }
            )

        elapsed = time.perf_counter() - started
    finally:
        tracemalloc.stop()

    for sample in samples:
        print(
            f"slice {sample['slice']:2d}  flows={sample['flows_driven']:>9,}  "
            f"traced_current={_mib(sample['traced_current']):>10}  "
            f"ring={sample['ring_flows']:>6} flows / {_mib(sample['ring_bytes']):>10}  "
            f"evicted={sample['evicted']:>9,}  ru_maxrss={_mib(sample['maxrss']):>10}"
        )
    print(
        f"soak: {index:,} flows in {elapsed:.1f}s "
        f"({index / elapsed:,.0f}/s); ru_maxrss (high-water, not current) "
        f"{_mib(samples[-1]['maxrss'])}"
    )

    # -- the ring did its job on both axes -------------------------------
    stats = ring.stats
    assert stats.bytes <= stats.max_bytes, (
        f"REQ CAP-001: ring holds {stats.bytes} bytes, cap is {stats.max_bytes}"
    )
    assert stats.flows <= stats.max_flows
    assert stats.evicted > 0, "the byte cap never bound; the soak proved nothing"

    # -- and traced memory is flat from the middle onward ----------------
    middle = statistics.mean(
        s["traced_current"] for s in samples[SLICES // 2 - 1 : SLICES // 2 + 2]
    )
    tail = statistics.mean(s["traced_current"] for s in samples[-3:])
    ceiling = middle * TAIL_GROWTH_FACTOR + TAIL_GROWTH_SLACK_BYTES
    assert tail <= ceiling, (
        f"REQ PRF-005: traced memory grew across the run. "
        f"middle-of-run mean {_mib(middle)}, tail mean {_mib(tail)}, "
        f"ceiling {_mib(ceiling)}, over {index:,} flows. "
        f"Samples: {[_mib(s['traced_current']) for s in samples]}"
    )

    # An absolute ceiling as well as a relative one: a run that leaked steadily
    # from the very first slice would keep a flat ratio and still be a leak.
    assert tail < SOAK_MAX_BYTES * 4, (
        f"REQ PRF-005: traced memory {_mib(tail)} is far above the "
        f"{_mib(SOAK_MAX_BYTES)} the ring is allowed to hold"
    )


async def test_both_ring_caps_bind_under_sustained_traffic() -> None:
    """REQ CAP-001 — max_flows and max_bytes each bind on their own.

    Two axes, because either alone fails: a flow count says nothing about six
    4 MiB videos, and a byte count says nothing about ten thousand 200-byte
    beacons. The main soak exercises the byte axis; this exercises both, so a
    regression that disabled one of the two eviction conditions cannot hide
    behind the other still working.
    """
    # Flow-count bound: a byte cap far too large to ever bind.
    flow_bound = RingBuffer(max_flows=250, max_bytes=1024 * 1024 * 1024)
    interceptor = _make_interceptor(flow_bound)
    for index in range(4_000):
        await _drive_one(interceptor, index)

    stats = flow_bound.stats
    print(f"flow-bound ring: {stats.flows} flows, {_mib(stats.bytes)}, {stats.evicted:,} evicted")
    assert stats.flows == 250, f"max_flows did not bind: {stats.flows} records held"
    assert stats.bytes < stats.max_bytes, "the byte cap bound instead; this axis proved nothing"
    assert stats.evicted >= 3_000

    # Byte bound: a flow cap far too large to ever bind.
    byte_bound = RingBuffer(max_flows=10_000_000, max_bytes=1024 * 1024)
    interceptor = _make_interceptor(byte_bound)
    for index in range(4_000):
        await _drive_one(interceptor, index)

    stats = byte_bound.stats
    print(f"byte-bound ring: {stats.flows} flows, {_mib(stats.bytes)}, {stats.evicted:,} evicted")
    assert stats.bytes <= stats.max_bytes, f"max_bytes did not bind: {stats.bytes} held"
    assert stats.flows < stats.max_flows, "the flow cap bound instead; this axis proved nothing"
    assert stats.evicted > 0
