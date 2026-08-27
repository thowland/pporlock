"""The control server running on the proxy's own event loop.

SPEC-1 §7.1 and REQ DD-3: the server shares the proxy's loop, which is what
removes any need for IPC or locking. Unit tests drive the ASGI app directly;
this asserts the thing they cannot — that it actually binds, serves, and stops
on a live loop.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest

from pporlock.capture.records import FlowRecord
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import ControlApp
from pporlock.control.server import ControlServer
from pporlock.errors import NonLoopbackBindError

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers or {}, timeout=10)
        return response.status_code, response.text


@pytest.fixture
async def server(tmp_path: Path) -> Any:
    config = Config()
    config.state_dir = str(tmp_path)
    config.control.listen_port = _free_port()

    ring = RingBuffer()
    ring.add(
        FlowRecord(
            flow_id="pt",
            kind="passthrough",
            started_at="2026-08-27T14:00:00.000Z",
            passthrough_host="www.apple.com",
            passthrough_pattern="*.apple.com",
        )
    )
    app = ControlApp(config, ring=ring)
    control = ControlServer(app, config)
    await control.start()
    try:
        yield control, app
    finally:
        await control.stop()


class TestLifecycle:
    async def test_binds_and_serves(self, server: Any) -> None:
        control, _app = server
        status, body = await _get(f"{control.base_url}/state/health")
        assert status == 200
        assert '"ok":true' in body.replace(" ", "")

    async def test_reports_running(self, server: Any) -> None:
        control, _app = server
        assert control.running

    async def test_stops_cleanly(self, tmp_path: Path) -> None:
        """A daemon that will not stop leaves the browser pointed at a proxy
        that is only half dead."""
        config = Config()
        config.state_dir = str(tmp_path)
        config.control.listen_port = _free_port()
        control = ControlServer(ControlApp(config, ring=RingBuffer()), config)
        await control.start()
        await control.stop()
        assert not control.running

    def test_refuses_a_non_loopback_bind(self, tmp_path: Path) -> None:
        """REQ API-010 — asserted in code, not merely defaulted."""
        config = Config()
        config.state_dir = str(tmp_path)
        config.control.listen_host = "0.0.0.0"
        with pytest.raises(NonLoopbackBindError):
            ControlServer(ControlApp(config, ring=RingBuffer()), config)


class TestOverTheWire:
    async def test_authenticated_read(self, server: Any) -> None:
        control, app = server
        status, _ = await _get(
            f"{control.base_url}/state",
            {"Authorization": f"Bearer {app.tokens.ensure()}"},
        )
        assert status == 200

    async def test_unauthenticated_read_is_refused(self, server: Any) -> None:
        control, _app = server
        status, _ = await _get(f"{control.base_url}/state")
        assert status == 401

    async def test_flows_are_served(self, server: Any) -> None:
        import json

        control, app = server
        _status, body = await _get(
            f"{control.base_url}/flows",
            {"Authorization": f"Bearer {app.tokens.ensure()}"},
        )
        payload = json.loads(body)
        assert payload["flows"][0]["passthrough"]["host"] == "www.apple.com"

    async def test_a_page_origin_is_refused_over_the_wire(self, server: Any) -> None:
        """The end-to-end form of the threat: a page issuing a request at
        127.0.0.1 with a stolen or guessed token still gets nothing."""
        control, app = server
        status, _ = await _get(
            f"{control.base_url}/state",
            {
                "Authorization": f"Bearer {app.tokens.ensure()}",
                "Origin": "https://evil.example",
            },
        )
        assert status == 403


class TestLoopSharing:
    async def test_serves_while_the_loop_does_other_work(self, server: Any) -> None:
        """REQ DD-3. The server and the proxy hooks share one loop, so the
        server must not monopolise it — and must stay responsive while other
        tasks run."""
        control, app = server

        async def busy() -> int:
            total = 0
            for _ in range(50):
                await asyncio.sleep(0)
                total += 1
            return total

        results = await asyncio.gather(
            busy(),
            _get(
                f"{control.base_url}/state",
                {"Authorization": f"Bearer {app.tokens.ensure()}"},
            ),
        )
        assert results[0] == 50
        assert results[1][0] == 200
