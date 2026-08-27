"""Foreground proxy runner — SPEC-1 §8, REQ PXY-005.

Starts ``mitmdump`` in-process with our addon set, in explicit (regular) proxy
mode on loopback (REQ PXY-001).

Running mitmproxy's DumpMaster in-process rather than shelling out to the
``mitmdump`` binary keeps one Python process, one event loop, and one place for
the control server to attach in Sprint 3 — which is what SPEC-1 §7.1 requires
when it says the control server shares the proxy's loop.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from ..addon.interceptor import Interceptor, NullSink
from ..config import Config


def emit(line: str) -> None:
    """Write a line of live output.

    Flushed explicitly: stdout is block-buffered when redirected to a file or a
    pipe, which turns a live traffic feed into nothing at all until the process
    exits. For this command the output *is* the product.
    """
    print(line, flush=True)


class ConsoleSink(NullSink):
    """Prints a line per flow. Replaced by the ring buffer in Sprint 3.

    Exists so that baseline interception is observable at all before the capture
    subsystem lands — the Sprint 2 checkpoint is a human watching traffic go by
    without certificate warnings.
    """

    def __init__(self, quiet: bool = False) -> None:
        super().__init__()
        self.quiet = quiet

    def record_http(self, request: Any, response: Any, provenance: Any, timing: Any) -> None:
        super().record_http(request, response, provenance, timing)
        if self.quiet:
            return
        status = response.status if response is not None else "---"
        size = response.body_size if response is not None else 0
        emit(
            f"  {request.method:6} {status:>3}  {size:>9,}b  "
            f"{timing.get('pporlock_ms', 0.0):6.2f}ms  {request.url[:96]}"
        )

    def record_passthrough(self, host: Any, ip: Any, provenance: Any, timing: Any) -> None:
        super().record_passthrough(host, ip, provenance, timing)
        if self.quiet:
            return
        reason = ""
        for note in provenance.notes:
            reason = note.detail.get("pattern", "")
            break
        emit(f"  {'TUNNEL':6} ---  {'':>9}   {'':>8}  {host or ip}  [{reason}]")


async def _run(config: Config, sink: ConsoleSink) -> int:
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    # Only options registered on the bare Options object may be passed here.
    # mitmproxy's own addons register many more at load time — flow_detail comes
    # from the dumper, anticache and anticomp from the proxyauth/core set — so
    # anything outside the base 29 must be set after the addon set is built.
    # This is exactly the version-churn surface SPEC-1 §2.1 confines to the
    # adapter: an option name moving between releases breaks here and nowhere
    # else. Sprint 10 sets anticache/anticomp through master.options once the
    # dev toggles exist (REQ PXY-043).
    options = Options(
        listen_host=config.proxy.listen_host,
        listen_port=config.proxy.listen_port,
        mode=["regular"],
        # Certificate handling is mitmproxy's; we supply only the addon set.
        ssl_insecure=False,
    )

    master = DumpMaster(options, with_termlog=False, with_dumper=False)
    interceptor = Interceptor(config, sink=sink)
    master.addons.add(interceptor)  # type: ignore[no-untyped-call]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, master.shutdown)

    emit(f"pporlock proxy listening on {config.proxy.listen_host}:{config.proxy.listen_port}")
    emit(f"  exclusions: {len(interceptor.exclusions)} entries")
    emit("  ctrl-c to stop\n")

    await master.run()

    emit("\nstopped.")
    emit(
        f"  {sink.http} http flows, {sink.passthrough} tunneled, "
        f"{interceptor.counters.errors} errors"
    )
    return 0


def run_foreground(config: Config, *, quiet: bool = False) -> int:
    sink = ConsoleSink(quiet=quiet)
    try:
        return asyncio.run(_run(config, sink))
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"could not start proxy: {exc}", file=sys.stderr)
        return 1
