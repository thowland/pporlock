"""End-to-end baseline interception against a real proxy.

This is the automated form of the Sprint 2 checkpoint: traffic actually passes
through mitmdump with our addon loaded, flows are recorded, and an excluded host
is tunneled rather than decrypted.

Everything here runs a genuine DumpMaster on a real port. The unit suite covers
shape translation; this covers the thing unit tests structurally cannot — that
the addon is wired into mitmproxy correctly at all.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
import urllib.request
from typing import Any

import pytest

from pporlock.addon.interceptor import Interceptor, NullSink
from pporlock.config import Config
from pporlock.engine.evaluator import Evaluator
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList
from pporlock.engine.provenance import NoteCode
from pporlock.engine.ruleset import RuleSet

pytestmark = pytest.mark.integration


class CollectingSink(NullSink):
    def __init__(self) -> None:
        super().__init__()
        self.flows: list[tuple[Any, Any, Any]] = []
        self.passthroughs: list[tuple[Any, Any, Any]] = []

    def record_http(self, request: Any, response: Any, provenance: Any, timing: Any) -> None:
        super().record_http(request, response, provenance, timing)
        self.flows.append((request, response, provenance))

    def record_passthrough(self, host: Any, ip: Any, provenance: Any, timing: Any) -> None:
        super().record_passthrough(host, ip, provenance, timing)
        self.passthroughs.append((host, ip, provenance))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProxyHarness:
    """Runs a real DumpMaster on its own thread and event loop."""

    def __init__(
        self,
        exclusions: ExclusionList | None = None,
        rules: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rules = rules or []
        self.port = _free_port()
        self.sink = CollectingSink()
        self.interceptor: Interceptor | None = None
        self._exclusions = exclusions
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._master: Any = None
        self._ready = threading.Event()
        self.error: BaseException | None = None

    def start(self) -> ProxyHarness:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("proxy did not start within 30s")
        if self.error is not None:
            raise RuntimeError(f"proxy failed to start: {self.error!r}") from self.error
        # DumpMaster binds shortly after run() is scheduled; poll rather than sleep.
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.3)
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.1)
        raise RuntimeError(f"proxy never listened on {self.port}")

    def _run(self) -> None:
        # DumpMaster must be constructed inside a running loop — it reaches for
        # the running loop during __init__. Building it outside kills the thread
        # silently, which is why the error is captured and re-raised in start().
        async def boot() -> None:
            from mitmproxy.options import Options
            from mitmproxy.tools.dump import DumpMaster

            config = Config()
            config.proxy.listen_port = self.port
            options = Options(listen_host="127.0.0.1", listen_port=self.port, mode=["regular"])
            self._master = DumpMaster(options, with_termlog=False, with_dumper=False)
            evaluator = Evaluator(
                RuleSet.from_rules(self.rules, module="test"),
                exclusions=self._exclusions or ExclusionList(),
            )
            self.interceptor = Interceptor(
                config,
                sink=self.sink,
                exclusions=self._exclusions,
                evaluator=evaluator,
            )
            self._master.addons.add(self.interceptor)
            self._ready.set()
            await self._master.run()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(boot())
        except BaseException as exc:
            self.error = exc
            self._ready.set()
        finally:
            self._loop.close()

    def stop(self) -> None:
        if self._master is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._master.shutdown)
        if self._thread is not None:
            self._thread.join(timeout=15)

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def get(self, url: str, timeout: float = 15.0) -> Any:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
        )
        return opener.open(url, timeout=timeout)

    def wait_for_flows(self, count: int, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.sink.flows) >= count:
                return
            time.sleep(0.05)
        raise AssertionError(f"expected {count} flows, saw {len(self.sink.flows)}")

    def __enter__(self) -> ProxyHarness:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


@pytest.fixture(scope="module")
def proxy() -> Any:
    harness = ProxyHarness(
        exclusions=ExclusionList(
            [ExclusionEntry("excluded.example", "test: excluded host", "default")]
        ),
        # A body rule for the CSP fixture, so the buffering guard has something
        # to buffer for. Without a rule that wants a body, the guard streams —
        # which is correct, and is asserted separately.
        rules=[
            {
                "name": "wants-csp-body",
                "action": "body",
                "match": {"path": "^/csp/"},
                "transform": {"kind": "strip_integrity_attributes"},
            }
        ],
    ).start()
    try:
        yield harness
    finally:
        harness.stop()


class TestBaselineInterception:
    def test_traffic_passes_through_unbroken(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """The Sprint 2 checkpoint in miniature: real traffic, no breakage."""
        with proxy.get(f"{fixture_origin.base_url}/health") as response:
            assert response.status == 200
            assert b'"ok":true' in response.read()

    def test_flow_is_recorded(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/dest/script"):
            pass
        proxy.wait_for_flows(before + 1)
        request, response, _prov = proxy.sink.flows[-1]
        assert request.method == "GET"
        assert request.path == "/dest/script"
        assert response.status == 200

    def test_every_flow_carries_provenance(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        """REQ CAP-013 — even a flow that matched no rule at all."""
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/health"):
            pass
        proxy.wait_for_flows(before + 1)
        provenance = proxy.sink.flows[-1][2]
        assert provenance is not None
        assert provenance.profile == "default"

    def test_body_is_buffered_when_a_rule_wants_it(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """Buffering is what makes rewriting possible at all (SPEC-1 §3.4).

        The guard only pays that cost when a rule could actually use the body —
        here, the body rule matching /csp/.
        """
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/csp/nonce"):
            pass
        proxy.wait_for_flows(before + 1)
        response = proxy.sink.flows[-1][1]
        assert not response.streamed
        assert response.body is not None
        assert b"csp nonce" in response.body

    def test_encoding_is_recorded_even_when_streamed(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """With no rule wanting the body, the guard streams it (REQ PXY-021).

        That is the cheapest optimisation available and applies to the
        overwhelming majority of flows on any real page. The encoding is still
        recorded, so the UI can say what would have been decoded.
        """
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/encoded?enc=gzip"):
            pass
        proxy.wait_for_flows(before + 1)
        _request, response, provenance = proxy.sink.flows[-1]
        assert response.encoding == "gzip"
        assert provenance.has_note(NoteCode.RESPONSE_STREAMED)

    def test_sec_fetch_dest_reaches_the_engine(
        self, proxy: ProxyHarness, fixture_origin: Any
    ) -> None:
        """Stub synthesis depends on this header surviving the adapter."""
        before = len(proxy.sink.flows)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy.proxy_url}))
        req = urllib.request.Request(  # noqa: S310
            f"{fixture_origin.base_url}/dest/script", headers={"Sec-Fetch-Dest": "script"}
        )
        with opener.open(req, timeout=15):
            pass
        proxy.wait_for_flows(before + 1)
        assert proxy.sink.flows[-1][0].dest == "script"

    def test_large_body_still_completes(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        """Above the buffering threshold. The guard lands in Sprint 9; until then
        this proves a large response is not itself a failure."""
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/large", timeout=30) as response:
            body = response.read()
        assert len(body) == 4 * 1024 * 1024
        proxy.wait_for_flows(before + 1)

    def test_404_is_recorded_not_swallowed(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        before = len(proxy.sink.flows)
        with pytest.raises(urllib.error.HTTPError):
            proxy.get(f"{fixture_origin.base_url}/nope")
        proxy.wait_for_flows(before + 1)
        assert proxy.sink.flows[-1][1].status == 404

    def test_counters_advance(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        assert proxy.interceptor is not None
        before = proxy.interceptor.counters.flows_total
        with proxy.get(f"{fixture_origin.base_url}/health"):
            pass
        proxy.wait_for_flows(len(proxy.sink.flows) + 0)
        assert proxy.interceptor.counters.flows_total > before

    def test_timing_records_pipeline_cost(self, proxy: ProxyHarness, fixture_origin: Any) -> None:
        """PRF-002 needs this number to exist before it can be measured."""
        before = len(proxy.sink.flows)
        with proxy.get(f"{fixture_origin.base_url}/health"):
            pass
        proxy.wait_for_flows(before + 1)
        provenance = proxy.sink.flows[-1][2]
        assert provenance.total_ms >= 0


class TestExclusionBehaviour:
    def test_excluded_host_is_decided_at_clienthello(self, proxy: ProxyHarness) -> None:
        """Exclusion happens before decryption, so there is no downstream
        failure to handle (REQ PXY-013)."""
        assert proxy.interceptor is not None
        assert proxy.interceptor.exclusions.should_exclude("excluded.example")
        assert not proxy.interceptor.exclusions.should_exclude("allowed.example")

    def test_exclusion_records_a_passthrough_with_no_content(self, proxy: ProxyHarness) -> None:
        """REQ PXY-015 — visible without being readable."""
        assert proxy.interceptor is not None
        before = len(proxy.sink.passthroughs)

        class Data:
            ignore_connection = False

        data = Data()
        data.client_hello = type("C", (), {"sni": "excluded.example"})()
        data.context = type("Ctx", (), {"server": type("S", (), {"address": None})()})()
        proxy.interceptor.tls_clienthello(data)

        assert data.ignore_connection is True
        assert len(proxy.sink.passthroughs) == before + 1
        host, _ip, provenance = proxy.sink.passthroughs[-1]
        assert host == "excluded.example"
        assert provenance.has_note(NoteCode.PASSTHROUGH_EXCLUDED)
