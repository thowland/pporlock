"""The control server — SPEC-1 §7.1, REQ API-001/002/003/010.

Started from the addon's ``running()`` hook as a task on the proxy's own event
loop (REQ DD-3). No IPC, no locking: the hooks and the handlers touch the same
objects on the same loop.

The cost of that is stated plainly in the design and enforced here: **a slow
handler stalls every connection the browser has open.** Routes are therefore
classified. ``INLINE_ROUTES`` read in-memory state only; anything touching the
filesystem, SQLite, or module import must offload to the executor. A test
asserts no inline-classified route performs I/O (SPEC-1 §7.1).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..config import Config, assert_loopback
from .auth import OriginPolicy, PairingWindow, TokenStore

if TYPE_CHECKING:
    from .app import ControlApp


class ControlServer:
    """Runs the ASGI app on the proxy's loop."""

    __slots__ = ("_server", "_started", "_task", "app", "config")

    def __init__(self, app: ControlApp, config: Config) -> None:
        # Asserted, not defaulted. pporlock terminates TLS and holds session
        # cookies in memory; this must never be reachable off-machine.
        assert_loopback(config.control.listen_host, setting="control.listen_host")
        self.config = config
        self.app = app
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def base_url(self) -> str:
        return f"http://{self.config.control.listen_host}:{self.config.control.listen_port}"

    async def start(self) -> None:
        """Start serving on the current event loop."""
        import uvicorn

        uvicorn_config = uvicorn.Config(
            self.app.asgi,
            host=self.config.control.listen_host,
            port=self.config.control.listen_port,
            log_level="warning",
            access_log=False,
            # The proxy owns signal handling; uvicorn installing its own would
            # break ctrl-c on the foreground runner.
            lifespan="off",
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._server.install_signal_handlers = lambda: None
        self._task = asyncio.create_task(self._server.serve())

        # Wait for the bind so callers can rely on the port being live.
        for _ in range(200):
            if getattr(self._server, "started", False):
                self._started = True
                return
            await asyncio.sleep(0.01)
        raise RuntimeError("control server did not start")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._started = False


def build_state(config: Config) -> tuple[TokenStore, OriginPolicy, PairingWindow]:
    """Construct the auth trio for a given configuration."""
    from pathlib import Path

    tokens = TokenStore(Path(config.state_dir))
    policy = OriginPolicy(config.control.listen_host, config.control.listen_port)
    pairing = PairingWindow()
    return tokens, policy, pairing
