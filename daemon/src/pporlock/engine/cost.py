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
