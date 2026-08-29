"""Throughput under concurrency — the axis `bench.run` does not measure.

REQ PRF-003, OI-21.

`bench.run` measures *serial* added latency: one request at a time, and the
question "how much does pporlock add to a page load". That is PRF-001 and it is
the right question for a quiet browser. It cannot see saturation, which is what
a heavy site actually produces — a hundred subresources in flight at once — and
saturation is a different failure with a different cause.

The distinction matters because the two have opposite conclusions. Serial
latency looked like something pporlock might optimise. Concurrency does not:
the ceiling belongs to mitmproxy, and it is a *single-core* ceiling, because
mitmproxy is a single-threaded asyncio program. Adding cores does nothing and
compiling pporlock's Python moves only the sliver between the two curves this
harness draws.

So the point of this file is to keep that sliver honest. It measures the same
workload three ways:

    direct      the fixture origin with no proxy at all — the ceiling
    baseline    a bare DumpMaster with no pporlock addon — mitmproxy's ceiling
    pporlock    the real addon — ours

`pporlock / baseline` is the only number an optimisation of this codebase can
move, and it is reported explicitly so that a change claiming a speed-up has
somewhere to prove it.

**Read the pporlock row as a floor, not a figure.** ``BenchProxy`` runs the real
addon against a ``NullSink``, so the engine is real and the *capture* path — the
ring, the body caps, the SSE fan-out, the session queue — is not. Measured this
way pporlock reaches ~96% of baseline. The same workload against a real daemon
with the real ``RingSink`` measured ~86%. The difference is capture, and it is
the larger half of what this codebase actually spends.

That gap is deliberate and worth stating rather than quietly closing: this
harness isolates the addon so a change to the engine has a clean signal, and a
number taken from it must not be quoted as the daemon's overhead. When the
question is "what does the daemon cost", measure a daemon.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from bench.run import BenchProxy

#: Concurrency levels. The interesting shape is the plateau: once throughput
#: stops rising with clients while latency keeps rising linearly, the server is
#: saturated and everything after that is queueing.
DEFAULT_CLIENTS = (1, 4, 16, 32)

DEFAULT_REQUESTS_PER_CLIENT = 20


@dataclass(frozen=True, slots=True)
class Sample:
    """One (proxy, concurrency) measurement."""

    label: str
    clients: int
    rps: float
    p50_ms: float
    p95_ms: float
    failed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "clients": self.clients,
            "rps": round(self.rps, 1),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "failed": self.failed,
        }


@dataclass
class _Collector:
    """Latencies from every worker thread, guarded — threads append concurrently."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    latencies: list[float] = field(default_factory=list)
    failures: int = 0

    def add(self, latencies: list[float], failures: int) -> None:
        with self.lock:
            self.latencies.extend(latencies)
            self.failures += failures


def _worker(
    url: str, proxy_port: int | None, count: int, collector: _Collector, barrier: threading.Barrier
) -> None:
    """One client. Opens its own opener so connection reuse is per-client.

    The barrier matters: without it the first threads finish before the last
    have started, and the measurement reports a concurrency that never
    existed.
    """
    handler = urllib.request.ProxyHandler(
        {"http": f"http://127.0.0.1:{proxy_port}"} if proxy_port else {}
    )
    opener = urllib.request.build_opener(handler)

    latencies: list[float] = []
    failures = 0
    barrier.wait()
    for _ in range(count):
        started = time.perf_counter()
        try:
            opener.open(url, timeout=30).read()
        except Exception:  # a failed request is data, not an error
            failures += 1
            continue
        latencies.append((time.perf_counter() - started) * 1000)
    collector.add(latencies, failures)


def measure(label: str, url: str, proxy_port: int | None, clients: int, per_client: int) -> Sample:
    collector = _Collector()
    barrier = threading.Barrier(clients)
    threads = [
        threading.Thread(
            target=_worker, args=(url, proxy_port, per_client, collector, barrier), daemon=True
        )
        for _ in range(clients)
    ]

    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.perf_counter() - started

    latencies = sorted(collector.latencies)
    return Sample(
        label=label,
        clients=clients,
        rps=len(latencies) / wall if wall > 0 else 0.0,
        p50_ms=statistics.median(latencies) if latencies else float("nan"),
        # quantiles needs enough points to be meaningful; below that, say so
        # rather than reporting a percentile computed from four samples.
        p95_ms=(statistics.quantiles(latencies, n=20)[18] if len(latencies) > 20 else float("nan")),
        failed=collector.failures,
    )


class _BareProxy:
    """A DumpMaster with no pporlock addon — mitmproxy's own ceiling.

    The control for the whole exercise. Without it a slow number looks like
    pporlock's fault, which is exactly the conclusion OI-12 had to walk back on
    the latency axis.
    """

    def __init__(self) -> None:
        import asyncio
        import socket

        self._asyncio = asyncio
        self._socket = socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        self._ready = threading.Event()
        self._master: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _BareProxy:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("bare proxy did not start")
        deadline = time.time() + 15
        while time.time() < deadline:
            with self._socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.05)
        raise RuntimeError(f"bare proxy never listened on {self.port}")

    def __exit__(self, *exc: object) -> None:
        if self._master is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._master.shutdown)
        if self._thread is not None:
            self._thread.join(timeout=15)

    def _run(self) -> None:
        asyncio = self._asyncio

        async def boot() -> None:
            from mitmproxy.options import Options
            from mitmproxy.tools.dump import DumpMaster

            options = Options(listen_host="127.0.0.1", listen_port=self.port, mode=["regular"])
            self._master = DumpMaster(options, with_termlog=False, with_dumper=False)
            self._ready.set()
            await self._master.run()

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(boot())
        finally:
            loop.close()


def run(
    path: str = "/",
    clients: tuple[int, ...] = DEFAULT_CLIENTS,
    per_client: int = DEFAULT_REQUESTS_PER_CLIENT,
) -> dict[str, Any]:
    from testfixtures.origin.server import FixtureServer

    origin = FixtureServer(port=0).start()
    url = f"http://127.0.0.1:{origin.port}{path}"
    samples: list[Sample] = []
    try:
        for count in clients:
            samples.append(measure("direct", url, None, count, per_client))
        with _BareProxy() as bare:
            for count in clients:
                samples.append(measure("baseline", url, bare.port, count, per_client))
        with BenchProxy(rules=[]) as proxied:
            for count in clients:
                samples.append(measure("pporlock", url, proxied.port, count, per_client))
    finally:
        origin.stop()

    peak = {
        label: max((s.rps for s in samples if s.label == label), default=0.0)
        for label in ("direct", "baseline", "pporlock")
    }
    share = peak["pporlock"] / peak["baseline"] if peak["baseline"] else float("nan")
    return {
        "path": path,
        "samples": [s.as_dict() for s in samples],
        "peak_rps": {k: round(v, 1) for k, v in peak.items()},
        # The headline. Everything an optimisation of this codebase can win
        # lives in (1 - this), and mitmproxy owns the rest.
        "pporlock_share_of_baseline": round(share, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Concurrency/throughput bench (OI-21)")
    parser.add_argument("--path", default="/", help="fixture path, e.g. /large for 4 MiB bodies")
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS_PER_CLIENT)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    result = run(path=args.path, per_client=args.requests)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"\nconcurrency bench — fixture path {result['path']}\n")
    print(f"{'':10} {'clients':>8} {'rps':>10} {'p50 ms':>10} {'p95 ms':>10} {'failed':>8}")
    for sample in result["samples"]:
        print(
            f"{sample['label']:10} {sample['clients']:8d} {sample['rps']:10.1f} "
            f"{sample['p50_ms']:10.2f} {sample['p95_ms']:10.2f} {sample['failed']:8d}"
        )
    peak = result["peak_rps"]
    print(
        f"\npeak rps   direct {peak['direct']}   baseline {peak['baseline']}   "
        f"pporlock {peak['pporlock']}"
    )
    print(
        f"pporlock reaches {result['pporlock_share_of_baseline']:.1%} of mitmproxy's own "
        "ceiling — the remainder is the only thing optimising this codebase can win.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
