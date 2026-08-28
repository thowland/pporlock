"""Work classification and offload decisions — SPEC-1 §4.5, REQ PXY-024.

The control server and the addon share the proxy's single event loop, so any
work that blocks it stalls every connection the browser has open. Most rule
evaluation is trivially fast — a set lookup, a compiled regex against a short
path — and paying thread-pool handoff costs for that would be slower than doing
it inline.

So work is classified rather than uniformly offloaded, and the classification is
data so a test can assert against it:

* ``CHEAP``    runs inline. Bounded by construction.
* ``EXPENSIVE`` always offloads. HTML parsing, anything that walks a document.
* ``SIZED``    offloads once the body it operates on crosses a threshold. A
               regex over 2 KiB is nothing; the same regex over 2 MiB is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: Bodies at or above this go to the executor even for cheap transforms
#: (REQ PXY-024, config.budget.executor_threshold_bytes).
DEFAULT_OFFLOAD_THRESHOLD_BYTES = 256 * 1024


class Cost(StrEnum):
    CHEAP = "cheap"
    SIZED = "sized"
    EXPENSIVE = "expensive"


#: Cost of each built-in transform (SPEC-0 §5.5).
#:
#: The HTML-touching transforms are expensive unconditionally: they parse a
#: document rather than scanning bytes, and the cost is driven by structure
#: rather than length, so a size threshold would not predict it.
TRANSFORM_COST: dict[str, Cost] = {
    "strip_integrity_attributes": Cost.EXPENSIVE,
    "inject_script": Cost.EXPENSIVE,
    "inject_style": Cost.EXPENSIVE,
    "strip_csp": Cost.CHEAP,
    "regex_sub": Cost.SIZED,
    "replace_literal": Cost.SIZED,
    "json_patch": Cost.SIZED,
}


@dataclass(frozen=True, slots=True)
class OffloadDecision:
    """Whether to run something on a worker thread, and why."""

    offload: bool
    cost: Cost
    reason: str

    def to_dict(self) -> dict[str, object]:
        # `offload_reason`, not `reason`: a provenance entry already carries a
        # `reason` for why the rule did what it did, and two different facts
        # under one key is how a detail block becomes misleading.
        return {
            "offload": self.offload,
            "cost": str(self.cost),
            "offload_reason": self.reason,
        }


def cost_of(transform_kind: str) -> Cost:
    """Cost class of a transform. Unknown kinds are treated as expensive.

    Defaulting an unknown to expensive is the safe direction: a module-provided
    transform we know nothing about should not be assumed to be fast on the
    proxy's event loop.
    """
    return TRANSFORM_COST.get(transform_kind, Cost.EXPENSIVE)


def decide_offload(
    transform_kind: str,
    body_bytes: int,
    threshold: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
) -> OffloadDecision:
    """Should this transform run on a worker thread?"""
    cost = cost_of(transform_kind)

    if cost is Cost.EXPENSIVE:
        return OffloadDecision(True, cost, "expensive transform")
    if cost is Cost.CHEAP:
        return OffloadDecision(False, cost, "cheap transform")
    if body_bytes >= threshold:
        return OffloadDecision(True, cost, f"body {body_bytes} >= {threshold}")
    return OffloadDecision(False, cost, f"body {body_bytes} < {threshold}")


@dataclass(slots=True)
class ModuleStat:
    """Accumulated cost and effect of one module (REQ PRF-007).

    Two different questions are kept apart on purpose. ``flows_matched`` counts
    flows where this module's rules were evaluated and produced a provenance
    entry; ``flows_modified`` counts the subset where something actually
    changed. A module that matches four hundred flows and modifies none is a
    module whose rules are wrong, and collapsing the two would hide that.
    """

    module: str
    flows_matched: int = 0
    flows_modified: int = 0
    entries: int = 0
    applied: int = 0
    errors: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        """Mean per *flow*, not per entry — a flow is what the user perceives."""
        return self.total_ms / self.flows_matched if self.flows_matched else 0.0

    @property
    def mean_entry_ms(self) -> float:
        return self.total_ms / self.entries if self.entries else 0.0

    def to_dict(self) -> dict[str, object]:
        """The `/metrics` shape: everything, most detail."""
        return {
            "module": self.module,
            "flows_matched": self.flows_matched,
            "flows_modified": self.flows_modified,
            "entries": self.entries,
            "applied": self.applied,
            "errors": self.errors,
            "total_ms": round(self.total_ms, 3),
            "avg_ms": round(self.avg_ms, 4),
            "mean_entry_ms": round(self.mean_entry_ms, 4),
            "max_ms": round(self.max_ms, 3),
        }

    def to_status_dict(self) -> dict[str, object]:
        """The `ModuleStatus.stats` shape declared in ``contracts/openapi.yaml``.

        Narrower than ``to_dict``. The module library renders four columns and
        the contract declares exactly those four; emitting more here would put
        fields on the wire that no schema describes.
        """
        return {
            "flows_matched": self.flows_matched,
            "flows_modified": self.flows_modified,
            "errors": self.errors,
            "avg_ms": round(self.avg_ms, 4),
        }


class ModuleCostIndex:
    """Per-module timing, accumulated as flows complete (REQ PRF-007).

    Accumulated rather than computed on demand. ``GET /metrics`` is an
    inline-classified route — it runs on the proxy's own event loop and may only
    read in-memory state (SPEC-1 §7.1) — so walking two thousand ring-buffer
    flows and their provenance entries to answer it would be exactly the kind of
    work that route classification exists to keep off the loop.

    The cost of accumulating is one dict update per provenance entry, in the
    same place the flow counters are already incremented.

    "Expensive module" is what this exists to make visible, so ``max_ms`` is
    kept alongside the average: a module that is fast on four hundred flows and
    takes 300 ms on the one page you care about is invisible in a mean.

    Pure: it takes the provenance shape and nothing else, so it lives in
    ``engine/`` under the DD-2 boundary and is testable without a proxy.
    """

    __slots__ = ("_stats",)

    def __init__(self) -> None:
        self._stats: dict[str, ModuleStat] = {}

    def get(self, module: str) -> ModuleStat | None:
        return self._stats.get(module)

    def record(self, provenance: Any) -> None:
        """Fold one flow's provenance in. Never raises on a partial record."""
        entries = getattr(provenance, "entries", ()) or ()
        matched: set[str] = set()
        modified: set[str] = set()
        for entry in entries:
            module = getattr(entry, "module", "") or "(unattributed)"
            duration = float(getattr(entry, "duration_ms", 0.0) or 0.0)
            outcome = str(getattr(entry, "outcome", ""))
            stat = self._stats.get(module)
            if stat is None:
                stat = ModuleStat(module=module)
                self._stats[module] = stat
            stat.entries += 1
            stat.total_ms += duration
            stat.max_ms = max(stat.max_ms, duration)
            if outcome == "applied":
                stat.applied += 1
                modified.add(module)
            elif outcome == "error":
                stat.errors += 1
            matched.add(module)

        # Per-flow counts are folded once per flow, not once per entry: a module
        # with three rules on one page has matched one flow, not three.
        for module in matched:
            self._stats[module].flows_matched += 1
        for module in modified:
            self._stats[module].flows_modified += 1

    def stats(self) -> list[ModuleStat]:
        """Most expensive first. That is the question this answers."""
        return sorted(self._stats.values(), key=lambda s: s.total_ms, reverse=True)

    def to_list(self) -> list[dict[str, object]]:
        return [s.to_dict() for s in self.stats()]

    def reset(self) -> None:
        self._stats.clear()

    def __len__(self) -> int:
        return len(self._stats)
