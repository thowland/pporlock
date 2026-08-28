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

from ..provenance import NoteCode, ProvenanceBuilder, Severity
from ..ruleset import CompiledRule, RuleSet
from .context import ModuleContext, ModuleStore
from .loader import LoadedModule, load_all, unload_python

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
        "store_path",
    )

    def __init__(
        self,
        root: Path,
        *,
        store_path: Path | None = None,
        quarantine_after: int = DEFAULT_QUARANTINE_AFTER,
    ) -> None:
        self.root = root
        self.store_path = store_path or (root.parent / "module-store.db")
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
        """
        live = {name: (m.enabled, m.priority) for name, m in self._modules.items()}

        for module in self._modules.values():
            self._call_lifecycle(module, "on_unload")
            unload_python(module.name)

        self._modules = {}
        self._contexts = {}

        for module in load_all(self.root):
            self._modules[module.name] = module
            if module.name in live:
                module.enabled = live[module.name][0]
                self.set_priority(module.name, live[module.name][1])
            if module.state == "loaded":
                self._contexts[module.name] = self._make_context(module, registry, profile)
                self._call_lifecycle(module, "on_load")

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
            config=module.config,
            profile=profile,
            assets=module.assets,
            store=self._stores[module.name],
            registry=registry,
        )

    def _call_lifecycle(self, module: LoadedModule, hook: str) -> None:
        fn = module.hooks().get(hook)
        context = self._contexts.get(module.name)
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

    def context(self, name: str) -> ModuleContext | None:
        return self._contexts.get(name)

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
        module = self._modules.get(name)
        if module is None:
            return None
        module.enabled = enabled
        if enabled and module.state == "quarantined":
            # Re-enabling is a deliberate act, and it clears the quarantine —
            # otherwise a user who fixed the module could not tell the daemon so.
            module.state = "loaded"
            module.failures = 0
            module.quarantine_reason = None
            module.quarantined_at = None
        return module

    def set_priority(self, name: str, priority: int) -> LoadedModule | None:
        module = self._modules.get(name)
        if module is None:
            return None
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
        return module

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
