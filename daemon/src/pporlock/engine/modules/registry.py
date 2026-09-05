"""The module registry — SPEC-1 §5.3/§5.4, REQ MOD-004/023/024/025.

Reload is a **snapshot swap**: a new immutable set is built, then swapped
atomically. In-flight flows continue against the snapshot they started with,
which is what removes any need for locking under DD-3.

Hook invocation is wrapped. An exception is caught, attributed to the module,
and never affects flow delivery (REQ MOD-024). N consecutive failures quarantine
the module (REQ MOD-025), because a module failing on every flow is producing
noise rather than value, and the noise would bury real findings.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..cost import ModuleStat
from ..provenance import NoteCode, ProvenanceBuilder, Severity
from ..ruleset import CompiledRule, RuleSet
from .context import ModuleContext, ModuleStore
from .loader import LoadedModule, load_all, unload_python
from .settings import coerce_config
from .state import STATE_FILENAME, ModuleStateStore

DEFAULT_QUARANTINE_AFTER = 10


@dataclass(frozen=True, slots=True)
class HookFailure:
    module: str
    hook: str
    message: str
    trace: str


@dataclass(frozen=True, slots=True)
class ReloadResult:
    loaded: int
    enabled: int
    errors: tuple[LoadedModule, ...]
    #: Modules held out of evaluation after repeated hook failures (REQ
    #: MOD-025). Reported because a reload that quietly dropped a quarantined
    #: module would look like a module that had simply vanished.
    quarantined: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "enabled": self.enabled,
            "quarantined": self.quarantined,
            "errors": [m.error.to_dict() | {"module": m.name} for m in self.errors if m.error],
        }


class ModuleRegistry:
    """Holds the loaded module set and builds rule sets from it."""

    __slots__ = (
        "_contexts",
        "_failures",
        "_modules",
        "_stores",
        "quarantine_after",
        "root",
        "state",
        "store_path",
    )

    def __init__(
        self,
        root: Path,
        *,
        store_path: Path | None = None,
        state_path: Path | None = None,
        quarantine_after: int = DEFAULT_QUARANTINE_AFTER,
    ) -> None:
        self.root = root
        self.store_path = store_path or (root.parent / "module-store.db")
        #: User-set enablement and ordering, persisted (OI-8). Defaults beside
        #: the module root; the daemon passes the state directory explicitly.
        self.state = ModuleStateStore(state_path or (root.parent / STATE_FILENAME))
        self.quarantine_after = quarantine_after
        self._modules: dict[str, LoadedModule] = {}
        self._stores: dict[str, ModuleStore] = {}
        self._contexts: dict[str, ModuleContext] = {}
        self._failures: list[HookFailure] = []

    # -- loading ---------------------------------------------------------

    def reload(self, registry: Any = None, profile: str = "default") -> ReloadResult:
        """Rebuild the module set from disk.

        The previous modules' ``on_unload`` hooks run first, so a module holding
        something can release it. A hook that raises there is reported and
        ignored: refusing to reload because the outgoing version misbehaved
        would make a broken module unfixable.

        A module already in the set keeps the enabled state and priority it was
        running with. The manifest seeds those the first time a module is seen
        and never again: reloading to pick up an edit to one module must not
        silently turn every other module off, and enablement is set through the
        API rather than by editing a file.

        "The first time a module is seen" is now measured against the sidecar
        state file rather than against this process's memory (OI-8), so a
        restart is not a first sighting. That is the whole of the change: the
        semantics were always these, they just did not outlive the process.
        """
        # Cost statistics survive a reload for the same reason enablement does:
        # they describe the module, not the particular load of it that happens
        # to be resident, and zeroing them on every edit would make the column
        # useless exactly while someone is iterating on a module (REQ PRF-007).
        live_stats = {name: m.stats for name, m in self._modules.items()}
        outgoing = self._modules

        # The outgoing generation releases what it holds first — a module that
        # opened a file or a connection gets the chance before its successor
        # takes over, which is what `on_unload` is for.
        for module in outgoing.values():
            self._call_lifecycle(module, "on_unload")

        # The replacement is then built entirely into locals and published in
        # one assignment. It used to be built *in place*: `_modules` and
        # `_contexts` were emptied and refilled one module at a time, on a
        # worker thread, while traffic continued on the event loop against this
        # same object. A flow arriving inside that window saw no modules, or
        # half of them, or a context whose module was not yet initialised.
        # Offloading the mutation kept the loop responsive; it did not make the
        # mutation safe (SEP_5_REVIEW F-04, REQ MOD-004, MOD-024, DD-3).
        #
        # What this does not remove is the overlap between `on_unload` above and
        # a hook already running on the old generation. Closing that would mean
        # loading the replacement before releasing the outgoing one, which is
        # the opposite of the guarantee `on_unload` makes. The window is now
        # bounded by hook duration rather than by the whole reload.
        modules: dict[str, LoadedModule] = {}
        contexts: dict[str, ModuleContext] = {}

        for module in load_all(self.root):
            modules[module.name] = module
            module.stats = live_stats.get(module.name) or ModuleStat(module=module.name)
            persisted = self.state.get(module.name)
            if persisted is not None:
                module.enabled = persisted.enabled
                self._recompile_priority(module, persisted.priority)
                # Settings the user set survive a reload for the same reason
                # enablement does: they are the user's answer about the module,
                # not about this particular load of it. Overrides for keys the
                # rewritten manifest no longer declares are ignored by
                # `effective_config` rather than dropped from the file, so
                # renaming a field back restores what was there.
                module.config_overrides = dict(persisted.config)
            else:
                # First sighting: the manifest seeds the sidecar, once.
                self.state.set(module.name, enabled=module.enabled, priority=module.priority)
            if module.state == "loaded":
                contexts[module.name] = self._make_context(module, registry, profile)

        # `on_load` before publication, so a module that registers a transform
        # or warms a cache has done so before any flow can reach it.
        for module in modules.values():
            if module.state == "loaded":
                self._call_lifecycle(module, "on_load", contexts)

        # The swap. A flow reading either name sees a complete generation: the
        # old one, or the new one, never a half-built one.
        self._modules = modules
        self._contexts = contexts

        # sys.modules is cleaned up only after the swap. A hook that is still
        # running holds the function object it is executing, so this cannot pull
        # code out from under it; doing it before the swap would have unloaded
        # code the still-published old generation could be asked to run.
        for name in outgoing:
            unload_python(name)

        # Anything a module wrote through `ctx.store_set` is queued to a
        # background writer (SEP_5_REVIEW F-13); a reload is a point at which
        # it must be on disk.
        for store in self._stores.values():
            store.flush()

        # A module deleted from disk must not leave its row behind forever, and
        # must not come back enabled if it is ever reinstalled.
        self.state.prune(self._modules)

        return ReloadResult(
            loaded=len(self._modules),
            enabled=sum(1 for m in self._modules.values() if m.enabled),
            errors=tuple(m for m in self._modules.values() if m.state == "load_error"),
            quarantined=sum(1 for m in self._modules.values() if m.state == "quarantined"),
        )

    def _make_context(self, module: LoadedModule, registry: Any, profile: str) -> ModuleContext:
        if module.name not in self._stores:
            self._stores[module.name] = ModuleStore(self.store_path, module.name)
        return ModuleContext(
            name=module.name,
            version=module.version,
            config=module.effective_config,
            profile=profile,
            assets=module.assets,
            store=self._stores[module.name],
            registry=registry,
        )

    def _call_lifecycle(
        self, module: LoadedModule, hook: str, contexts: dict[str, ModuleContext] | None = None
    ) -> None:
        fn = module.hooks().get(hook)
        context = (self._contexts if contexts is None else contexts).get(module.name)
        if fn is None or context is None:
            return
        try:
            fn(context)
        except Exception as exc:
            self._failures.append(HookFailure(module.name, hook, str(exc), traceback.format_exc()))

    # -- inspection ------------------------------------------------------

    @property
    def modules(self) -> tuple[LoadedModule, ...]:
        return tuple(sorted(self._modules.values(), key=lambda m: (m.priority, m.name)))

    def get(self, name: str) -> LoadedModule | None:
        return self._modules.get(name)

    def record_provenance(self, provenance: Any) -> None:
        """Fold one completed flow's provenance into per-module stats.

        REQ PRF-007. Called once per flow by the addon. Entries naming something
        that is not a loaded module — a rule from ``rules.yaml``, whose module is
        ``"api"`` or ``"file"`` — are ignored here on purpose: those have no row
        in the module library. ``GET /metrics`` counts them, because there the
        question is "where did the time go", not "which module".
        """
        entries = getattr(provenance, "entries", ()) or ()
        matched: set[str] = set()
        modified: set[str] = set()
        for entry in entries:
            name = getattr(entry, "module", "") or ""
            module = self._modules.get(name)
            if module is None:
                continue
            stat = module.stats
            if not stat.module:
                stat.module = name
            stat.entries += 1
            duration = float(getattr(entry, "duration_ms", 0.0) or 0.0)
            stat.total_ms += duration
            stat.max_ms = max(stat.max_ms, duration)
            outcome = str(getattr(entry, "outcome", ""))
            if outcome == "applied":
                stat.applied += 1
                modified.add(name)
            elif outcome == "error":
                stat.errors += 1
            matched.add(name)

        for name in matched:
            self._modules[name].stats.flows_matched += 1
        for name in modified:
            self._modules[name].stats.flows_modified += 1

    def context(self, name: str) -> ModuleContext | None:
        return self._contexts.get(name)

    def report(self, name: str) -> tuple[str, bytes] | None:
        """Render a module's report: `(content_type, body)`, or None.

        Called from the control API rather than from a flow, so it is not
        subject to the per-flow time budget — a report may legitimately walk
        everything the module has accumulated. It still runs module code, so
        the caller offloads it rather than doing this on the proxy's loop.

        Returns None when the module is absent, failed to load, or declares no
        `on_report`. A module that raises here is reported as an error rather
        than quarantined: a broken report is not a reason to stop a module
        modifying traffic correctly, and the two failures are unrelated.
        """
        module = self._modules.get(name)
        if module is None or module.python is None or module.state != "loaded":
            return None
        hook = getattr(module.python, "on_report", None)
        if not callable(hook):
            return None

        context = self._contexts.get(name)
        result = hook(context)
        if result is None:
            return None

        # A dict rather than a bare string, so a module can choose its content
        # type without the daemon guessing from the bytes.
        if isinstance(result, dict):
            content_type = str(result.get("content_type") or "text/plain; charset=utf-8")
            body = result.get("body", b"")
        else:
            content_type = "text/plain; charset=utf-8"
            body = result
        if isinstance(body, str):
            body = body.encode()
        if not isinstance(body, bytes):
            body = str(body).encode()
        return content_type, body

    @property
    def failures(self) -> tuple[HookFailure, ...]:
        return tuple(self._failures)

    def active(self, profile_modules: list[str] | None = None) -> list[LoadedModule]:
        """Modules that should run: loaded, enabled, not quarantined.

        A profile narrows the set further; without one, every enabled module
        runs.
        """
        out = []
        for module in self.modules:
            if module.state != "loaded" or not module.enabled:
                continue
            if profile_modules is not None and module.name not in profile_modules:
                continue
            out.append(module)
        return out

    def build_ruleset(self, profile_modules: list[str] | None = None) -> RuleSet:
        """Compile the active modules into one rule set.

        Ordering across modules is by priority then declaration (REQ MOD-023),
        which RuleSet applies from each rule's own priority.
        """
        rules: list[CompiledRule] = []
        names: list[str] = []
        for module in self.active(profile_modules):
            rules.extend(module.rules)
            names.append(module.name)
        return RuleSet(rules, modules=tuple(names))

    # -- state changes ---------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> LoadedModule | None:
        """Turn a module on or off, persisting the decision (OI-8).

        Blocking: the sidecar is written through here, so control routes calling
        this offload it like any other filesystem work (REQ API-002).
        """
        module = self._modules.get(name)
        if module is None:
            return None
        module.enabled = enabled
        self.state.set(name, enabled=module.enabled, priority=module.priority)
        if enabled and module.state == "quarantined":
            # Re-enabling is a deliberate act, and it clears the quarantine —
            # otherwise a user who fixed the module could not tell the daemon so.
            module.state = "loaded"
            module.failures = 0
            module.quarantine_reason = None
            module.quarantined_at = None
        return module

    def set_config(
        self, name: str, values: dict[str, Any]
    ) -> tuple[LoadedModule | None, list[str]]:
        """Replace a module's settings overrides, persisting them. Blocking.

        Returns the module and the list of rejected fields; nothing is written
        when anything is rejected, so a form with one bad value does not half
        apply. A module that declares no settings rejects everything, which is
        how `config` stays the author's file rather than a second, hidden one.

        The live `ctx.config` is replaced in place rather than by reloading the
        module. A reload would re-execute `module.py` and take `on_load` with
        it — turning "change a dropdown" into "restart the module", which for a
        module accumulating an audit would silently be "throw the audit away".
        Modules that derive something from config at load time declare
        `on_config` and recompute there.
        """
        module = self._modules.get(name)
        if module is None:
            return None, []
        accepted, errors = coerce_config(module.settings, values)
        if errors:
            return module, errors

        module.config_overrides = accepted
        self.state.set_config(name, accepted)
        context = self._contexts.get(name)
        if context is not None:
            context.config = module.effective_config
            self._call_lifecycle(module, "on_config")
        return module, []

    def set_priority(self, name: str, priority: int) -> LoadedModule | None:
        """Reorder a module against the others, persisting it (OI-8). Blocking."""
        module = self._modules.get(name)
        if module is None:
            return None
        self._recompile_priority(module, priority)
        self.state.set(name, enabled=module.enabled, priority=module.priority)
        return module

    @staticmethod
    def _recompile_priority(module: LoadedModule, priority: int) -> None:
        """Apply a priority in memory. Used by reload, which must not persist.

        Split out because reload applies the priority it just *read* from the
        sidecar; writing it straight back would be a no-op at best and, on a
        read that fell back to manifest defaults, would overwrite the user's
        real state with the fallback.
        """
        module.priority = priority
        # Rules carry the priority they were compiled with, so they have to be
        # rebuilt for the change to affect ordering.
        module.rules = tuple(
            CompiledRule(
                rule_id=r.rule_id,
                module=r.module,
                name=r.name,
                action=r.action,
                matcher=r.matcher,
                priority=priority,
                index=r.index,
                enabled=r.enabled,
                params=r.params,
            )
            for r in module.rules
        )

    def quarantine(self, name: str, reason: str) -> None:
        module = self._modules.get(name)
        if module is None:
            return
        module.state = "quarantined"
        module.quarantine_reason = reason
        module.quarantined_at = datetime.now(UTC).isoformat(timespec="milliseconds")

    def record_failure(self, name: str, builder: ProvenanceBuilder | None = None) -> bool:
        """Count a hook failure, quarantining at the threshold.

        Returns whether this failure caused a quarantine.
        """
        module = self._modules.get(name)
        if module is None:
            return False
        module.failures += 1
        if module.failures < self.quarantine_after:
            return False

        self.quarantine(name, f"{module.failures} consecutive hook failures")
        if builder is not None:
            builder.note(
                NoteCode.MODULE_QUARANTINED,
                f"{name} was disabled after {module.failures} consecutive failures",
                severity=Severity.ERROR,
                module=name,
                failures=module.failures,
            )
        return True

    def record_success(self, name: str) -> None:
        module = self._modules.get(name)
        if module is not None:
            module.failures = 0
