"""Starting and stopping the proxy listener over the control API — OI-3.

``POST /state`` declared ``proxy_running`` and silently discarded it, answering
200 with a payload saying the proxy was still running. This is the automated
proof that the listener now actually stops and starts, run against a genuine
DumpMaster because it is the only thing that can tell.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from .test_interception import ProxyHarness

pytestmark = pytest.mark.integration


def _accepting(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _call(harness: ProxyHarness, running: bool) -> bool:
    """Drive set_proxy_running on the proxy's own loop, as the route does."""
    interceptor = harness.interceptor
    loop = harness._loop
    assert interceptor is not None and loop is not None
    future = asyncio.run_coroutine_threadsafe(interceptor.set_proxy_running(running), loop)
    return future.result(timeout=20)


class TestProxyListenerControl:
    def test_stop_actually_closes_the_listener(self) -> None:  # OI-3
        harness = ProxyHarness().start()
        try:
            assert _accepting(harness.port)
            assert harness.interceptor is not None
            assert harness.interceptor.proxy_listening is True

            assert _call(harness, False) is True
            assert not _accepting(harness.port)
            assert harness.interceptor.proxy_listening is False
        finally:
            harness.stop()

    def test_start_brings_it_back(self) -> None:  # OI-3
        harness = ProxyHarness().start()
        try:
            _call(harness, False)
            assert not _accepting(harness.port)

            assert _call(harness, True) is True
            assert _accepting(harness.port)
            assert harness.interceptor is not None
            assert harness.interceptor.proxy_listening is True
        finally:
            harness.stop()
