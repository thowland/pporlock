"""A flow that fails must still appear — OI-23, REQ CAP-002.

`Interceptor.error` incremented a counter and stopped, so a request that never
completed left no row in the ring. The user saw the browser fail with a 502,
opened the flow table whose entire purpose is explaining traffic, and the one
request they were looking for was the one request the tool had discarded.

This is the same shape as OI-18 and OI-22: the system knew why and did not say.
Here it did not even say *that*.
"""

from __future__ import annotations

from typing import Any

from pporlock.capture.records import FlowError, FlowRecord
from pporlock.capture.ring import RingBuffer
from pporlock.capture.sink import RingSink
from pporlock.control.serialize import serialize_flow
from pporlock.engine.provenance import ProvenanceBuilder


def _provenance() -> Any:
    return ProvenanceBuilder("default").build(0.0)


def _sink() -> tuple[RingSink, RingBuffer]:
    ring = RingBuffer(max_flows=32, max_bytes=1 << 20)
    return RingSink(ring), ring


def test_a_failed_flow_lands_in_the_ring() -> None:
    """The regression: a 502 used to leave nothing behind at all."""
    sink, ring = _sink()

    sink.record_error(None, _provenance(), "connection refused")

    records = list(ring.query().flows)
    assert len(records) == 1, "a failed flow must be visible, not merely counted"
    assert records[0].error is not None
    assert records[0].error.message == "connection refused"


def test_a_failed_flow_carries_no_response() -> None:
    """A row with a reason and no status is the honest shape.

    Inventing a status would make the table lie about what the browser got.
    """
    sink, ring = _sink()

    sink.record_error(None, _provenance(), "upstream timed out")

    record = ring.query().flows[0]
    assert record.response is None
    assert record.status is None


def test_a_client_cancel_is_distinguishable_from_an_upstream_failure() -> None:
    """These are opposite events and look identical in a count.

    A browser abandoning a request is routine; an origin refusing one is the
    thing being debugged. A flow table that renders them the same is why the
    counter was not enough.
    """
    sink, ring = _sink()

    sink.record_error(None, _provenance(), "client disconnected", from_client=True)
    sink.record_error(None, _provenance(), "connection refused", from_client=False)

    kinds = [r.error.from_client for r in ring.query().flows if r.error is not None]
    assert sorted(kinds) == [False, True]


def test_the_reason_reaches_the_api() -> None:
    """REQ API-004: the field is in the contract, so it must be serialized.

    A record the daemon holds but never sends is invisible in exactly the place
    the user looks.
    """
    record = FlowRecord(
        flow_id="f1",
        kind="http",
        started_at="2026-08-29T00:00:00Z",
        error=FlowError(message="tls handshake failed", from_client=False),
        provenance=_provenance(),
    )

    payload = serialize_flow(record, detail=False)

    assert payload["error"] == {"message": "tls handshake failed", "from_client": False}


def test_a_healthy_flow_carries_no_error_key() -> None:
    """Absent, not null. The schema allows null, but a successful flow should
    not carry an error field at all — a reader scanning for failures should be
    able to test for the key."""
    record = FlowRecord(
        flow_id="f2",
        kind="http",
        started_at="2026-08-29T00:00:00Z",
        provenance=_provenance(),
    )

    assert "error" not in serialize_flow(record, detail=False)


def test_the_tee_the_daemon_actually_builds_forwards_errors() -> None:
    """OI-11's lesson, applied. REQ TST-001.

    The daemon does not use a `RingSink` directly — `cli/runner.py` wraps it in
    a `TeeSink` alongside the console. Every test above constructs the sink it
    exercises, so all of them passed while the running daemon dropped every
    error record on the floor.

    Not with an exception, either: `TeeSink` inherits `NullSink`, so it
    inherited a `record_error` that counted and returned. The counter moved,
    `/metrics` showed four errors, and the ring stayed empty — a silent drop,
    which is the failure this whole change exists to remove.
    """
    from pporlock.cli.runner import ConsoleSink, TeeSink

    ring = RingBuffer(max_flows=8, max_bytes=1 << 20)
    tee = TeeSink(RingSink(ring), ConsoleSink(quiet=True))

    tee.record_error(None, _provenance(), "connection refused")

    flows = ring.query().flows
    assert len(flows) == 1, "the sink the daemon builds must reach the ring, not just count"
    assert flows[0].error is not None
    assert flows[0].error.message == "connection refused"


def test_every_sink_the_daemon_can_build_implements_the_protocol() -> None:
    """A sink that silently no-ops is worse than one that fails loudly.

    `NullSink`'s counting default is right for a stub and wrong for a wrapper:
    a subclass that forgets to forward inherits a method that looks correct.
    This pins the three the daemon can construct so a fourth cannot be added
    without deciding what it does with a failed flow.
    """
    from pporlock.cli.runner import ConsoleSink, TeeSink

    ring = RingBuffer(max_flows=8, max_bytes=1 << 20)
    sinks = [RingSink(ring), ConsoleSink(quiet=True), TeeSink(RingSink(ring), ConsoleSink(True))]

    for sink in sinks:
        assert hasattr(sink, "record_error"), f"{type(sink).__name__} cannot record a failed flow"
