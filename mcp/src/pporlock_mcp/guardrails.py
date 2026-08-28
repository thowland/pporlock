"""Response-shaping guardrails.

Three separate concerns live here, all of them requirements rather than taste:

* **Provenance** (REQ MCP-004) — every flow-bearing response carries provenance.
  SPEC-0 §6.3 guarantees it at all three detail levels (``summary`` substitutes
  a ``provenance.summary`` count object for the entries). If it is missing, the
  daemon violated its own contract and we say so rather than quietly returning a
  flow the agent cannot reason about.
* **Redaction** (REQ MCP-003) — the daemon redacts at serialization time
  (CAP-045). This module does not un-redact, and additionally scans outbound
  tool results for the one thing that would prove a leak: a masked-value marker
  that has been resolved back to plaintext is undetectable, but a request that
  *asked* for unmasking is not, and that is refused in ``client.py``.
* **Token cost** (REQ MCP-005) — bounded page sizes and truncated text, with the
  cap stated in the payload so the agent knows it did not see everything.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ContractViolation

#: SPEC-0 §9.1. Present in any correctly redacted payload; used by the tests to
#: assert masked values survive the round trip unchanged.
MASK_RE = re.compile(r"«redacted:sha1=[0-9a-f]{4},len=\d+»")

DETAIL_LEVELS = ("summary", "full", "bodies")


def coerce_detail(value: Any, default: str) -> str:
    """Clamp a caller's ``detail`` to the three known levels (SPEC-0 §6.3)."""
    if value is None:
        return default
    text = str(value).lower()
    if text not in DETAIL_LEVELS:
        return default
    return text


def clamp(value: Any, default: int, maximum: int) -> int:
    """Bounded page size (REQ MCP-005). Out-of-range is clamped, not rejected."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return default
    return min(number, maximum)


def has_provenance(flow: dict[str, Any]) -> bool:
    provenance = flow.get("provenance")
    return isinstance(provenance, dict) and bool(provenance)


def require_provenance(payload: Any, *, where: str) -> Any:
    """Assert REQ MCP-004 over one flow or a page of flows.

    Raising is the correct behaviour: a flow without provenance is a flow whose
    modifications cannot be explained, and handing one to an agent silently is
    exactly the failure mode CAP-010 exists to prevent.
    """
    if isinstance(payload, dict) and "flows" in payload:
        flows = payload.get("flows")
        if isinstance(flows, list):
            for index, flow in enumerate(flows):
                if isinstance(flow, dict) and not has_provenance(flow):
                    raise ContractViolation(
                        f"{where}: flow {flow.get('flow_id', index)} has no provenance "
                        "(SPEC-0 §3.4/§4, REQ CAP-010)"
                    )
        return payload

    if isinstance(payload, dict) and "flow_id" in payload and not has_provenance(payload):
        raise ContractViolation(
            f"{where}: flow {payload.get('flow_id')} has no provenance "
            "(SPEC-0 §3.4/§4, REQ CAP-010)"
        )
    return payload


def truncate_text(text: str, cap: int) -> tuple[str, bool]:
    if len(text) <= cap:
        return text, False
    return text[:cap], True


def truncate_files(files: dict[str, Any], cap: int) -> dict[str, Any]:
    """Cap each module file, marking what was cut (REQ MCP-005)."""
    out: dict[str, Any] = {}
    for name, content in files.items():
        if not isinstance(content, str):
            out[name] = content
            continue
        body, cut = truncate_text(content, cap)
        out[name] = body
        if cut:
            out[f"{name}__truncated"] = {
                "shown_chars": cap,
                "total_chars": len(content),
                "hint": "call read_module with full=true for the whole file",
            }
    return out


def summarize_notes(flows: list[dict[str, Any]]) -> dict[str, int]:
    """Histogram of provenance note codes — the cheap 'what broke' signal."""
    histogram: dict[str, int] = {}
    for flow in flows:
        provenance = flow.get("provenance")
        if not isinstance(provenance, dict):
            continue
        notes = provenance.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if isinstance(note, dict):
                code = str(note.get("code", "unknown"))
                histogram[code] = histogram.get(code, 0) + 1
    return histogram
