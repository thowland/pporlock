"""PRF-001 / PRF-002 benchmark — REQ PRF-003. `make bench`.

Two measurements, deliberately of different things.

**PRF-002 — per-flow overhead for a flow matching no rules, p95 < 2 ms.**
Measured in-process against the real ``Evaluator``, over the whole decision path
a flow takes: request short-circuit, request headers, buffering decision,
response headers, response body. No socket is involved, because PRF-002 is a
budget on *our* work, and putting a loopback TCP round trip inside the
measurement would report the kernel's scheduling jitter as pporlock's cost.

**PRF-001 — added page-load latency, <15% p50 and <30% p95.**
Measured end to end through a real ``DumpMaster`` against the in-repo fixture
origin: the reference page and its subresources fetched direct, then the same
set fetched through the proxy, alternating, and the two distributions compared.
Alternating rather than "all direct then all proxied" matters — the machine is
not the same machine for two minutes running, and back-to-back blocks would
attribute a thermal ramp to the proxy.

**What this does not measure**, stated because a benchmark's caveats are part of
its result: the origin is plain HTTP on loopback, so no TLS handshake or
certificate generation is in the PRF-001 figure, and the client is ``urllib``
rather than Chrome, so there is no connection-pool or prioritisation behaviour.
Both make the *ratio* flatter than a real page load would: the direct baseline
here is unrealistically fast, which makes any fixed proxy overhead look like a
larger percentage. That is the conservative direction — the number reported is
if anything worse than what a browser sees against a real origin.

The harness does not tune itself to pass. If a number misses its budget it is
printed with the budget beside it and the process exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SRC = REPO_ROOT / "daemon" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pporlock.addon.interceptor import Interceptor, NullSink
from pporlock.config import Config
from pporlock.engine.evaluator import Evaluator, TimeBudget
from pporlock.engine.exclusions import ExclusionList
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.provenance import ProvenanceBuilder
from pporlock.engine.ruleset import RuleSet

from bench.workload import (
    NON_MATCHING_FLOW,
    NON_MATCHING_RULES,
    PAGE_PATHS,
    REFERENCE_RULES,
)

#: PRF-002's budget, in milliseconds at p95.
PRF_002_BUDGET_MS = 2.0
#: PRF-001's budgets, as a fraction of direct page-load time.
PRF_001_P50_BUDGET = 0.15
PRF_001_P95_BUDGET = 0.30

DEFAULT_FLOW_ITERATIONS = 20_000
DEFAULT_PAGE_ITERATIONS = 30
WARMUP_FRACTION = 0.1


# ------------------------------------------------------------- statistics ---


@dataclass(frozen=True, slots=True)
class Distribution:
    """A measured sample. Percentiles, not just a mean.

    A mean hides the case this system fails in: most flows are fast and the
    occasional one parses a document. PRF-001 and PRF-002 are both stated at
    percentiles for that reason, so a mean-only report would not answer them.
    """

    name: str
    unit: str
    samples: tuple[float, ...]

    @property
    def n(self) -> int:
        return len(self.samples)

    def percentile(self, q: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        return ordered[index]

    @property
    def p50(self) -> float:
        return self.percentile(0.50)

    @property
    def p95(self) -> float:
        return self.percentile(0.95)

    @property
    def p99(self) -> float:
        return self.percentile(0.99)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "unit": self.unit,
            "n": self.n,
            "mean": round(self.mean, 4),
            "p50": round(self.p50, 4),
            "p95": round(self.p95, 4),
            "p99": round(self.p99, 4),
            "max": round(max(self.samples), 4) if self.samples else 0.0,
        }


@dataclass(frozen=True, slots=True)
class Result:
    requirement: str
    label: str
    measured: float
    budget: float
    unit: str
    detail: dict[str, Any]

    @property
    def passed(self) -> bool:
        return self.measured <= self.budget

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "label": self.label,
            "measured": round(self.measured, 4),
            "budget": self.budget,
            "unit": self.unit,
            "pass": self.passed,
            "detail": self.detail,
        }


# ------------------------------------------------------------------ PRF-002 --


def _build_flow() -> tuple[NormalizedRequest, NormalizedResponse]:
    spec = NON_MATCHING_FLOW
    url = f"{spec['scheme']}://{spec['host']}:{spec['port']}{spec['path']}"
    request = NormalizedRequest(
        flow_id="bench",
        timestamp="2026-08-27T14:00:00.000Z",
        scheme=spec["scheme"],
        method=spec["method"],
        host=spec["host"],
        port=spec["port"],
        path=spec["path"],
        url=url,
        headers=spec["request_headers"],
        dest=spec["dest"],
    )
    response = NormalizedResponse(
        flow_id="bench",
        timestamp="2026-08-27T14:00:00.100Z",
        status=spec["status"],
        headers=spec["response_headers"],
        body=spec["body"],
    )
    return request, response


def measure_non_matching_flow(iterations: int = DEFAULT_FLOW_ITERATIONS) -> Result:
    """PRF-002: the whole engine path for a flow that matches nothing."""
    config = Config()
    evaluator = Evaluator(
        RuleSet.from_rules(NON_MATCHING_RULES, module="bench"),
        exclusions=ExclusionList(),
        buffer_types=tuple(config.buffering.content_types),
        max_buffer_bytes=config.buffering.max_body_bytes,
    )
    request, response = _build_flow()
    content_type = response.content_type
    length = response.body_size

    samples: list[float] = []
    warmup = max(1, int(iterations * WARMUP_FRACTION))
    for index in range(iterations + warmup):
        started = time.perf_counter()
        builder = ProvenanceBuilder("bench")
        budget = TimeBudget(config.budget.per_flow_ms)
        decision = evaluator.evaluate_request(request, builder, budget)
        evaluator.evaluate_response_headers(request, response, builder)
        evaluator.decide_buffering(request, content_type, length, decision.wants_body, builder)
        evaluator.evaluate_response_body(request, response, builder, budget)
        builder.build((time.perf_counter() - started) * 1000)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if index >= warmup:
            samples.append(elapsed_ms)

    distribution = Distribution("non-matching flow", "ms", tuple(samples))
    return Result(
        requirement="PRF-002",
        label="per-flow engine overhead, no rule matches",
        measured=distribution.p95,
        budget=PRF_002_BUDGET_MS,
        unit="ms (p95)",
        detail=distribution.to_dict(),
    )


# ------------------------------------------------------------------ PRF-001 --


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BenchProxy:
    """A real DumpMaster with the real addon, on its own thread and loop.

    The same shape as the integration suite's harness. Deliberately the real
    thing: a benchmark against a stubbed proxy would measure the stub.
    """

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        import asyncio

        self._asyncio = asyncio
        self.port = _free_port()
        self.rules = rules
        self.sink = NullSink()
        self._ready = threading.Event()
        self._master: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def __enter__(self) -> BenchProxy:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("proxy did not start")
        if self.error is not None:
            raise RuntimeError(f"proxy failed to start: {self.error!r}")
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.05)
        raise RuntimeError(f"proxy never listened on {self.port}")

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

            config = Config()
            config.proxy.listen_port = self.port
            options = Options(listen_host="127.0.0.1", listen_port=self.port, mode=["regular"])
            self._master = DumpMaster(options, with_termlog=False, with_dumper=False)
            evaluator = Evaluator(
                RuleSet.from_rules(self.rules, module="bench"),
                exclusions=ExclusionList(),
                buffer_types=tuple(config.buffering.content_types),
                max_buffer_bytes=config.buffering.max_body_bytes,
            )
            self._master.addons.add(
                Interceptor(config, sink=self.sink, exclusions=ExclusionList(), evaluator=evaluator)
            )
            self._ready.set()
            await self._master.run()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(boot())
        except BaseException as exc:  # reported through .error, not swallowed
            self.error = exc
            self._ready.set()
        finally:
            self._loop.close()


def _load_page(opener: Any, base: str, paths: tuple[str, ...]) -> float:
    """Fetch a whole page's worth of requests. Returns wall time in ms."""
    started = time.perf_counter()
    for path in paths:
        with opener.open(base + path, timeout=30) as response:
            response.read()
    return (time.perf_counter() - started) * 1000


#: Supplementary sweep for PRF-001. The loopback baseline is roughly 0.4 ms per
#: request, which is one to two orders of magnitude faster than any real origin.
#: A fixed per-request proxy cost therefore shows up as an enormous *percentage*
#: against it, and PRF-001 is stated as a percentage. These are the same
#: measurement taken with the origin artificially delayed, so the reader can see
#: what the ratio does as the baseline becomes realistic.
#:
#: This is reported alongside the headline number, never instead of it. The
#: PRF-001 verdict is the loopback measurement, pass or fail.
LATENCY_SWEEP_MS: tuple[int, ...] = (0, 10, 30)
SWEEP_PATHS: tuple[str, ...] = ("/dest/script", "/dest/style", "/dest/image", "/dest/json")
SWEEP_ITERATIONS = 6


def measure_latency_sensitivity(
    base: str, proxied_opener: Any, direct_opener: Any
) -> list[dict[str, Any]]:
    """How the added-latency *ratio* moves as origin latency becomes realistic."""
    rows: list[dict[str, Any]] = []
    for delay in LATENCY_SWEEP_MS:
        suffix = f"?ms={delay}" if delay else ""
        paths = tuple(f"/slow{suffix}" if delay else path for path in SWEEP_PATHS)
        direct: list[float] = []
        proxied: list[float] = []
        _load_page(direct_opener, base, paths)
        _load_page(proxied_opener, base, paths)
        for _ in range(SWEEP_ITERATIONS):
            direct.append(_load_page(direct_opener, base, paths))
            proxied.append(_load_page(proxied_opener, base, paths))
        d = Distribution("direct", "ms", tuple(direct))
        p = Distribution("proxied", "ms", tuple(proxied))
        rows.append(
            {
                "origin_delay_ms": delay,
                "requests": len(paths),
                "direct_p50_ms": round(d.p50, 2),
                "proxied_p50_ms": round(p.p50, 2),
                "added_ms_per_request": round((p.p50 - d.p50) / len(paths), 3),
                "added_p50": round((p.p50 - d.p50) / d.p50, 4) if d.p50 else 0.0,
            }
        )
    return rows


def _keepalive_page(
    host: str, port: int, base_host: str, base_port: int, paths: tuple[str, ...]
) -> float:
    """Fetch a page over ONE connection. Returns wall time in ms.

    Chrome reuses connections; ``urllib`` does not. That difference is not a
    detail here — the headline PRF-001 measurement opens a fresh TCP connection
    per request, and through a proxy each of those is *two* connections plus a
    CONNECT-less upstream dial. Measuring the same page over a single kept-alive
    connection separates "what pporlock costs per request" from "what it costs
    per connection", which is the difference between a fixable number and an
    architectural one.
    """
    import http.client

    proxied = (host, port) != (base_host, base_port)
    connection = http.client.HTTPConnection(host, port, timeout=30)
    started = time.perf_counter()
    try:
        for path in paths:
            target = f"http://{base_host}:{base_port}{path}" if proxied else path
            connection.request("GET", target, headers={"Host": f"{base_host}:{base_port}"})
            connection.getresponse().read()
    finally:
        connection.close()
    return (time.perf_counter() - started) * 1000


def measure_keepalive(base_host: str, base_port: int, proxy_port: int) -> dict[str, Any]:
    """The reference page over one reused connection, direct and proxied."""
    direct: list[float] = []
    proxied: list[float] = []
    _keepalive_page(base_host, base_port, base_host, base_port, PAGE_PATHS)
    _keepalive_page("127.0.0.1", proxy_port, base_host, base_port, PAGE_PATHS)
    for _ in range(SWEEP_ITERATIONS):
        direct.append(_keepalive_page(base_host, base_port, base_host, base_port, PAGE_PATHS))
        proxied.append(_keepalive_page("127.0.0.1", proxy_port, base_host, base_port, PAGE_PATHS))
    d = Distribution("direct keep-alive", "ms", tuple(direct))
    p = Distribution("proxied keep-alive", "ms", tuple(proxied))
    return {
        "requests": len(PAGE_PATHS),
        "direct_p50_ms": round(d.p50, 2),
        "proxied_p50_ms": round(p.p50, 2),
        "added_p50": round((p.p50 - d.p50) / d.p50, 4) if d.p50 else 0.0,
        "added_ms_per_request": round((p.p50 - d.p50) / len(PAGE_PATHS), 3),
    }


def measure_page_load(iterations: int = DEFAULT_PAGE_ITERATIONS) -> Result:
    """PRF-001: page-load latency through the proxy against direct."""
    from testfixtures.origin.server import FixtureServer

    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = FixtureServer(port=0).start()
    try:
        base = origin.base_url
        with BenchProxy(REFERENCE_RULES) as proxy:
            proxy_url = f"http://127.0.0.1:{proxy.port}"
            proxied_opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url})
            )
            # Warm both paths: the first proxied page pays for connection setup
            # and for the addon's first pass through cold code.
            _load_page(direct_opener, base, PAGE_PATHS)
            _load_page(proxied_opener, base, PAGE_PATHS)

            direct: list[float] = []
            proxied: list[float] = []
            for _ in range(iterations):
                # Alternating, so a thermal or scheduling drift lands on both.
                direct.append(_load_page(direct_opener, base, PAGE_PATHS))
                proxied.append(_load_page(proxied_opener, base, PAGE_PATHS))

            sweep = measure_latency_sensitivity(base, proxied_opener, direct_opener)
            keepalive = measure_keepalive("127.0.0.1", origin.port, proxy.port)
    finally:
        origin.stop()

    d_dist = Distribution("direct page load", "ms", tuple(direct))
    p_dist = Distribution("proxied page load", "ms", tuple(proxied))
    added_p50 = (p_dist.p50 - d_dist.p50) / d_dist.p50 if d_dist.p50 else 0.0
    added_p95 = (p_dist.p95 - d_dist.p95) / d_dist.p95 if d_dist.p95 else 0.0

    return Result(
        requirement="PRF-001",
        label="added page-load latency",
        measured=added_p50,
        budget=PRF_001_P50_BUDGET,
        unit="fraction of direct (p50)",
        detail={
            "requests_per_page": len(PAGE_PATHS),
            "iterations": iterations,
            "direct": d_dist.to_dict(),
            "proxied": p_dist.to_dict(),
            "added_p50": round(added_p50, 4),
            "added_p95": round(added_p95, 4),
            "p95_budget": PRF_001_P95_BUDGET,
            "p95_pass": added_p95 <= PRF_001_P95_BUDGET,
            "added_ms_per_request": round((p_dist.p50 - d_dist.p50) / len(PAGE_PATHS), 3),
            "latency_sensitivity": sweep,
            "keepalive": keepalive,
        },
    )


# --------------------------------------------------------------- reporting ---


def format_report(results: list[Result]) -> str:
    lines = ["pporlock benchmark — PRF-001 / PRF-002 (REQ PRF-003)", ""]
    for result in results:
        verdict = "PASS" if result.passed else "FAIL"
        lines.append(f"  [{verdict}] {result.requirement}  {result.label}")
        lines.append(
            f"          measured {result.measured:.4g} {result.unit}   budget {result.budget:.4g}"
        )
        detail = result.detail
        if result.requirement == "PRF-002":
            lines.append(
                f"          n={detail['n']}  mean {detail['mean']:.4f} ms  "
                f"p50 {detail['p50']:.4f}  p95 {detail['p95']:.4f}  "
                f"p99 {detail['p99']:.4f}  max {detail['max']:.4f}"
            )
        else:
            direct = detail["direct"]
            proxied = detail["proxied"]
            p95_verdict = "PASS" if detail["p95_pass"] else "FAIL"
            lines.append(
                f"          {detail['requests_per_page']} requests/page, "
                f"{detail['iterations']} iterations, alternating"
            )
            lines.append(
                f"          direct  p50 {direct['p50']:.1f} ms  p95 {direct['p95']:.1f} ms"
            )
            lines.append(
                f"          proxied p50 {proxied['p50']:.1f} ms  p95 {proxied['p95']:.1f} ms"
            )
            lines.append(
                f"          added   p50 {detail['added_p50'] * 100:+.1f}%  "
                f"p95 {detail['added_p95'] * 100:+.1f}%  "
                f"(p95 budget {PRF_001_P95_BUDGET * 100:.0f}%: {p95_verdict})"
            )
            lines.append(f"          that is {detail['added_ms_per_request']:.3f} ms per request")
            lines.append("")
            lines.append("          supplementary — the same measurement with the origin delayed,")
            lines.append("          because a percentage against a 0.4 ms loopback baseline is not")
            lines.append("          the percentage a real page load sees:")
            lines.append("            origin delay   direct p50   proxied p50   added")
            for row in detail.get("latency_sensitivity", []):
                lines.append(
                    f"            {row['origin_delay_ms']:>6} ms     "
                    f"{row['direct_p50_ms']:>7.1f} ms   {row['proxied_p50_ms']:>8.1f} ms   "
                    f"{row['added_p50'] * 100:+.1f}%"
                )
            keep = detail.get("keepalive")
            if keep:
                lines.append("")
                lines.append("          supplementary — the same page over ONE reused connection,")
                lines.append("          which is what a browser does and what urllib does not:")
                lines.append(
                    f"            direct {keep['direct_p50_ms']:.1f} ms   "
                    f"proxied {keep['proxied_p50_ms']:.1f} ms   "
                    f"added {keep['added_p50'] * 100:+.1f}%  "
                    f"({keep['added_ms_per_request']:.3f} ms/request)"
                )
        lines.append("")
    failed = [r for r in results if not r.passed]
    lines.append(f"  {len(results) - len(failed)} of {len(results)} within budget")
    if failed:
        lines.append("  MISSED: " + ", ".join(r.requirement for r in failed))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench", description="PRF-001/002 harness")
    parser.add_argument(
        "--flow-iterations", type=int, default=DEFAULT_FLOW_ITERATIONS, help="PRF-002 samples"
    )
    parser.add_argument(
        "--page-iterations", type=int, default=DEFAULT_PAGE_ITERATIONS, help="PRF-001 page loads"
    )
    parser.add_argument("--only", choices=("prf001", "prf002"), help="run one measurement")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    results: list[Result] = []
    if args.only != "prf001":
        results.append(measure_non_matching_flow(args.flow_iterations))
    if args.only != "prf002":
        results.append(measure_page_load(args.page_iterations))

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print(format_report(results))

    prf001 = next((r for r in results if r.requirement == "PRF-001"), None)
    ok = all(r.passed for r in results) and (prf001 is None or bool(prf001.detail["p95_pass"]))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
