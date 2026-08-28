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
from pathlib import Path
from typing import Any

from ..addon.interceptor import Interceptor, NullSink
from ..capture.ring import RingBuffer
from ..capture.sink import RingSink
from ..config import Config
from ..control.app import ControlApp
from ..control.events import EventHub
from ..control.serialize import serialize_flow
from ..engine.evaluator import Evaluator
from ..engine.exclusions import load_exclusions
from ..engine.modules.registry import ModuleRegistry
from ..engine.profiles import ProfileManager
from ..engine.rules_file import load_rules_file
from ..engine.ruleset import RuleSet


def web_assets_dir() -> Path | None:
    """The built web UI, if it has been built.

    Looked up rather than required: the daemon is useful without the UI, and a
    missing build should not stop the proxy from starting. `make web` produces
    it; a packaged install ships it inside the wheel.
    """
    packaged = Path(__file__).resolve().parents[1] / "web"
    if packaged.is_dir():
        return packaged
    repo = Path(__file__).resolve().parents[4] / "web" / "dist"
    return repo if repo.is_dir() else None


def emit(line: str) -> None:
    """Write a line of live output.

    Flushed explicitly: stdout is block-buffered when redirected to a file or a
    pipe, which turns a live traffic feed into nothing at all until the process
    exits. For this command the output *is* the product.
    """
    print(line, flush=True)


class TeeSink(NullSink):
    """Writes to the ring buffer and echoes a line to the console.

    The ring buffer is what the API and UI read; the console line is what makes
    baseline interception observable while you are watching it happen. Both are
    wanted, so neither replaces the other.
    """

    def __init__(self, ring_sink: RingSink, console: ConsoleSink) -> None:
        super().__init__()
        self.ring_sink = ring_sink
        self.console = console

    def record_http(self, request: Any, response: Any, provenance: Any, timing: Any) -> None:
        super().record_http(request, response, provenance, timing)
        self.ring_sink.record_http(request, response, provenance, timing)
        self.console.record_http(request, response, provenance, timing)

    def record_passthrough(self, host: Any, ip: Any, provenance: Any, timing: Any) -> None:
        super().record_passthrough(host, ip, provenance, timing)
        self.ring_sink.record_passthrough(host, ip, provenance, timing)
        self.console.record_passthrough(host, ip, provenance, timing)

    def record_websocket_message(self, message: Any) -> None:
        super().record_websocket_message(message)
        self.ring_sink.record_websocket_message(message)

    def record_websocket_close(self, flow_id: str, close_code: Any) -> None:
        self.ring_sink.record_websocket_close(flow_id, close_code)


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


def build_evaluator(
    config: Config,
) -> tuple[Evaluator, ModuleRegistry, ProfileManager, RuleSet, Path, str | None]:
    """Load exclusions, rules, profiles and modules from disk.

    Deliberately synchronous, and called *before* the event loop exists. The
    control server shares the proxy's loop, so filesystem work must not land on
    it (REQ DD-3, API-002) — and startup is the one moment when doing it
    off-loop costs nothing. Module loading is the heaviest part of it: it reads
    every manifest and executes every module's top level.

    A broken rules file does not stop the daemon: it is still useful for
    inspection, and the alternative is a user who cannot browse because of a
    typo. The failure is returned so the caller can report it loudly. Modules
    fail the same way individually — ``load_module`` never raises — so one bad
    module cannot stop the rest from loading.
    """
    exclusions = load_exclusions()
    state_dir = Path(config.state_dir).expanduser()
    rules_path = state_dir / "rules.yaml"
    ruleset = RuleSet()
    error: str | None = None

    if rules_path.exists():
        try:
            ruleset = load_rules_file(rules_path)
        except Exception as exc:
            error = str(exc)

    profiles = ProfileManager(state_dir / "profiles")
    registry = ModuleRegistry(
        Path(config.modules.root).expanduser(),
        quarantine_after=config.modules.quarantine_after_failures,
    )

    evaluator = Evaluator(
        ruleset,
        exclusions=exclusions,
        asset_root=rules_path.parent,
        buffer_types=tuple(config.buffering.content_types),
        max_buffer_bytes=config.buffering.max_body_bytes,
        registry=registry,
    )

    # Modules are loaded here, against this evaluator's transform registry, so a
    # module registering a transform in on_load has somewhere to register it.
    registry.reload(evaluator.transforms, profiles.active_name)

    # File rules and module rules are one rule set to the engine; a module's
    # priority orders its rules against everything else (REQ MOD-023).
    evaluator.ruleset = RuleSet.combine(ruleset, registry.build_ruleset(profiles.module_filter()))

    return evaluator, registry, profiles, ruleset, rules_path, error


def build_control_app(
    config: Config,
    ring: RingBuffer,
    events: EventHub,
    registry: ModuleRegistry,
    profiles: ProfileManager,
    base_ruleset: RuleSet,
) -> ControlApp:
    """Assemble the control app the daemon actually serves.

    Extracted from ``_run`` so a test can build the same object the daemon does
    rather than one that merely resembles it. Two sprints closed with the module
    system fully unit-tested and not wired in here (OI-11); a unit test that
    constructs its own ControlApp cannot notice that, and this is the seam that
    lets one notice.
    """
    return ControlApp(
        config,
        ring=ring,
        interceptor=None,
        events=events,
        registry=registry,
        profiles=profiles,
        base_ruleset=base_ruleset,
        static_dir=web_assets_dir(),
    )


async def _run(
    config: Config,
    sink: Any,
    evaluator: Evaluator,
    registry: ModuleRegistry,
    profiles: ProfileManager,
    base_ruleset: RuleSet,
    rules_path: Path,
    rules_error: str | None,
) -> int:
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

    ring = RingBuffer(
        max_flows=config.capture.ring_max_flows,
        max_bytes=config.capture.ring_max_bytes,
        max_body_bytes=config.capture.max_body_bytes,
    )
    events = EventHub()
    control = build_control_app(config, ring, events, registry, profiles, base_ruleset)

    def publish_flow(record: Any) -> None:
        """Fan a completed flow out to SSE subscribers.

        Called from the sink on the proxy's own loop. The hub never blocks, so
        a stalled subscriber cannot slow traffic (SPEC-1 §7.3).
        """
        events.publish_flow(
            "flow.completed",
            record,
            # Redacted like every other representation that leaves the daemon
            # (REQ CAP-040). The SSE stream feeds the flow table, and a masked
            # value there is what the unmask affordance acts on.
            serialize_flow(record, "summary", control.redactor),
        )

    ring_sink = RingSink(
        ring,
        max_body_bytes=config.capture.max_body_bytes,
        on_flow=publish_flow,
        # The attribution join. Both orderings occur: the extension usually
        # observes before the flow completes (this hook), and when the flow wins
        # the race the POST /attribution handler backfills instead.
        resolve_tab=control.attribution.resolve,
        session=control.sessions,
    )
    console = sink if isinstance(sink, ConsoleSink) else ConsoleSink(quiet=True)
    tee = TeeSink(ring_sink, console)

    interceptor = Interceptor(
        config, sink=tee, exclusions=evaluator.exclusions, evaluator=evaluator
    )
    control.interceptor = interceptor
    interceptor.control = control
    master.addons.add(interceptor)  # type: ignore[no-untyped-call]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, master.shutdown)

    emit(f"pporlock proxy listening on {config.proxy.listen_host}:{config.proxy.listen_port}")
    emit(f"  exclusions: {len(interceptor.exclusions)} entries")
    active = registry.active(profiles.module_filter())
    broken = [m for m in registry.modules if m.error is not None]
    emit(
        f"  modules:    {len(active)} active of {len(registry.modules)}"
        f" in {config.modules.root}" + (f", {len(broken)} failed to load" if broken else "")
    )
    for module in broken:
        # Loudly, and by name. A module that silently is not there is the
        # failure the loader's never-raise contract exists to prevent.
        emit(f"    ! {module.name}: {module.error.message if module.error else 'unknown'}")
    emit(f"  profile:    {profiles.active_name}")
    if rules_error is not None:
        emit(f"  rules:      FAILED to load {rules_path}: {rules_error}")
    else:
        emit(
            f"  rules:      {len(evaluator.ruleset)} active"
            + (f" from {rules_path}" if len(evaluator.ruleset) else " (no rules.yaml)")
        )
    assets = web_assets_dir()
    emit(
        f"  control API on http://{config.control.listen_host}:"
        f"{config.control.listen_port}  (token: {control.tokens.path})"
    )
    if assets is not None:
        emit(f"  web UI      on http://{config.control.listen_host}:{config.control.listen_port}/")
    else:
        emit("  web UI      not built — run `make web`")
    emit("  ctrl-c to stop\n")

    await master.run()

    if interceptor.control_server is not None:
        await interceptor.control_server.stop()

    emit("\nstopped.")
    emit(
        f"  {tee.http} http flows, {tee.passthrough} tunneled, {interceptor.counters.errors} errors"
    )
    return 0


def run_foreground(config: Config, *, quiet: bool = False) -> int:
    sink: Any = ConsoleSink(quiet=quiet)
    # Loaded before the loop exists, so no filesystem work lands on it.
    evaluator, registry, profiles, base_ruleset, rules_path, rules_error = build_evaluator(config)
    try:
        return asyncio.run(
            _run(
                config,
                sink,
                evaluator,
                registry,
                profiles,
                base_ruleset,
                rules_path,
                rules_error,
            )
        )
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"could not start proxy: {exc}", file=sys.stderr)
        return 1
