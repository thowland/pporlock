"""Provenance — SPEC-0 §4.

Silent breakage is the characteristic failure of this class of tool: the proxy
considers a flow successful, the page is subtly wrong, and the cause is three
rules deep. Provenance is therefore a structural return value of the engine
rather than a logging feature (REQ CAP-010), it is built from the first
implementation rather than retrofitted, and it travels with every flow into
every consumer (REQ CAP-013).

The part that earns its keep is ``notes`` and the non-``applied`` outcomes:
recording what *didn't* happen, and why, is what turns "the page is broken" into
"rule strip-csp:2 was skipped because the response streamed".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    """Pipeline phases, in evaluation order (SPEC-0 §4.2, REQ PXY-020)."""

    CLIENTHELLO = "clienthello"
    REQUEST_SHORT_CIRCUIT = "request_short_circuit"
    REQUEST_HEADERS = "request_headers"
    BUFFERING_DECISION = "buffering_decision"
    RESPONSE_HEADERS = "response_headers"
    RESPONSE_BODY = "response_body"
    WEBSOCKET = "websocket"


class Action(StrEnum):
    """Action taxonomy (REQ PXY-030)."""

    PASSTHROUGH = "passthrough"
    BLOCK = "block"
    MAP_LOCAL = "map_local"
    REDIRECT = "redirect"
    HEADERS = "headers"
    BODY = "body"


class Outcome(StrEnum):
    """What became of an action (SPEC-0 §4.3).

    Everything other than APPLIED and NO_CHANGE is a reason something did not
    happen, and every one of them must be rendered by every client.
    """

    APPLIED = "applied"
    NO_CHANGE = "no_change"
    SKIPPED_STREAMED = "skipped_streamed"
    SKIPPED_BUDGET = "skipped_budget"
    SKIPPED_SHORT_CIRCUIT = "skipped_short_circuit"
    SKIPPED_DISABLED = "skipped_disabled"
    ERROR = "error"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class NoteCode(StrEnum):
    """Behaviour-changing conditions that are not rule actions (SPEC-0 §4.4)."""

    RESPONSE_STREAMED = "response_streamed"
    TRANSFORM_BUDGET_EXCEEDED = "transform_budget_exceeded"
    MODULE_QUARANTINED = "module_quarantined"
    MAP_LOCAL_MISSING = "map_local_missing"
    CSP_MODIFIED = "csp_modified"
    SRI_STRIPPED = "sri_stripped"
    SCRIPT_INJECTED = "script_injected"
    DEV_TOGGLE_ACTIVE = "dev_toggle_active"
    BODY_TRUNCATED = "body_truncated"
    MODULE_ERROR = "module_error"
    PASSTHROUGH_EXCLUDED = "passthrough_excluded"
    ATTRIBUTION_MISSING = "attribution_missing"
    MODULE_DEPRECATION = "module_deprecation"


#: Default severity per note code. A client may style beyond this, but the
#: severity that drives UI treatment and the in-page banner (REQ EXT-020)
#: is decided here, once, so the UI and the DevTools panel cannot disagree.
NOTE_SEVERITY: dict[NoteCode, Severity] = {
    NoteCode.RESPONSE_STREAMED: Severity.INFO,
    NoteCode.TRANSFORM_BUDGET_EXCEEDED: Severity.WARNING,
    NoteCode.MODULE_QUARANTINED: Severity.ERROR,
    NoteCode.MAP_LOCAL_MISSING: Severity.ERROR,
    NoteCode.CSP_MODIFIED: Severity.WARNING,
    NoteCode.SRI_STRIPPED: Severity.WARNING,
    NoteCode.SCRIPT_INJECTED: Severity.WARNING,
    NoteCode.DEV_TOGGLE_ACTIVE: Severity.WARNING,
    NoteCode.BODY_TRUNCATED: Severity.INFO,
    NoteCode.MODULE_ERROR: Severity.ERROR,
    NoteCode.PASSTHROUGH_EXCLUDED: Severity.INFO,
    NoteCode.ATTRIBUTION_MISSING: Severity.INFO,
    NoteCode.MODULE_DEPRECATION: Severity.WARNING,
}


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    """One module/rule that matched, and what became of it."""

    seq: int
    phase: Phase
    module: str
    rule_id: str
    action: Action
    outcome: Outcome
    duration_ms: float = 0.0
    rule_name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "phase": str(self.phase),
            "module": self.module,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "action": str(self.action),
            "outcome": str(self.outcome),
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceNote:
    """A behaviour-changing condition that no rule requested."""

    code: NoteCode
    severity: Severity
    message: str
    module: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "module": self.module,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Provenance:
    """The completed record for one flow."""

    profile: str
    evaluated_modules: tuple[str, ...] = ()
    entries: tuple[ProvenanceEntry, ...] = ()
    notes: tuple[ProvenanceNote, ...] = ()
    total_ms: float = 0.0
    short_circuited_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "evaluated_modules": list(self.evaluated_modules),
            "entries": [e.to_dict() for e in self.entries],
            "notes": [n.to_dict() for n in self.notes],
            "total_ms": self.total_ms,
            "short_circuited_by": self.short_circuited_by,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Provenance:
        """Rebuild from the ``to_dict`` form.

        Needed because a session stores provenance as JSON and a session flow
        must come back out as the same FlowRecord the live pipeline produced —
        provenance travels with every flow into every consumer (REQ CAP-013),
        and a session flow whose provenance was a bare dict would be the one
        place that stopped being true.

        Unknown enum members are tolerated: a session recorded by a newer
        daemon should still open, with the unrecognised value carried through
        as its string rather than taking the whole file down.
        """
        entries = tuple(
            ProvenanceEntry(
                seq=int(e.get("seq", index)),
                phase=Phase(e["phase"]) if e.get("phase") in set(Phase) else Phase.REQUEST_HEADERS,
                module=str(e.get("module", "")),
                rule_id=str(e.get("rule_id", "")),
                action=Action(e["action"]) if e.get("action") in set(Action) else Action.HEADERS,
                outcome=(
                    Outcome(e["outcome"]) if e.get("outcome") in set(Outcome) else Outcome.NO_CHANGE
                ),
                duration_ms=float(e.get("duration_ms", 0.0)),
                rule_name=e.get("rule_name"),
                detail=dict(e.get("detail") or {}),
            )
            for index, e in enumerate(data.get("entries") or [])
        )
        notes = tuple(
            ProvenanceNote(
                code=(
                    NoteCode(n["code"]) if n.get("code") in set(NoteCode) else NoteCode.MODULE_ERROR
                ),
                severity=(
                    Severity(n["severity"])
                    if n.get("severity") in set(Severity)
                    else Severity.WARNING
                ),
                message=str(n.get("message", "")),
                module=n.get("module"),
                detail=dict(n.get("detail") or {}),
            )
            for n in data.get("notes") or []
        )
        return cls(
            profile=str(data.get("profile", "default")),
            evaluated_modules=tuple(data.get("evaluated_modules") or ()),
            entries=entries,
            notes=notes,
            total_ms=float(data.get("total_ms", 0.0)),
            short_circuited_by=data.get("short_circuited_by"),
        )

    # -- queries the UI and the API filter vocabulary rely on ----------------

    def modules_fired(self) -> list[str]:
        """Modules that actually changed something."""
        seen: list[str] = []
        for entry in self.entries:
            if entry.outcome is Outcome.APPLIED and entry.module not in seen:
                seen.append(entry.module)
        return seen

    def has_note(self, code: NoteCode) -> bool:
        return any(n.code is code for n in self.notes)

    def max_severity(self) -> Severity | None:
        """Highest note severity, or None. Drives badge colour and the banner."""
        if not self.notes:
            return None
        order = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}
        return max((n.severity for n in self.notes), key=lambda s: order[s])

    @property
    def errored(self) -> bool:
        return any(e.outcome is Outcome.ERROR for e in self.entries) or any(
            n.severity is Severity.ERROR for n in self.notes
        )


class ProvenanceBuilder:
    """Accumulates provenance during evaluation.

    Mutable and single-flow-scoped; ``build()`` freezes it. The evaluator holds
    one of these per flow and every phase writes into it, which is why the record
    can never be "forgotten" for a code path — there is no path that produces a
    decision without one.
    """

    __slots__ = ("_entries", "_modules", "_notes", "_profile", "_seq", "_short_circuited_by")

    def __init__(self, profile: str, evaluated_modules: tuple[str, ...] = ()) -> None:
        self._profile = profile
        self._modules = evaluated_modules
        self._entries: list[ProvenanceEntry] = []
        self._notes: list[ProvenanceNote] = []
        self._seq = 0
        self._short_circuited_by: str | None = None

    def record(
        self,
        *,
        phase: Phase,
        module: str,
        rule_id: str,
        action: Action,
        outcome: Outcome,
        duration_ms: float = 0.0,
        rule_name: str | None = None,
        **detail: Any,
    ) -> ProvenanceEntry:
        entry = ProvenanceEntry(
            seq=self._seq,
            phase=phase,
            module=module,
            rule_id=rule_id,
            action=action,
            outcome=outcome,
            duration_ms=duration_ms,
            rule_name=rule_name,
            detail=detail,
        )
        self._seq += 1
        self._entries.append(entry)
        return entry

    def note(
        self,
        code: NoteCode,
        message: str,
        *,
        severity: Severity | None = None,
        module: str | None = None,
        **detail: Any,
    ) -> ProvenanceNote:
        """Record a note. Severity defaults to the canonical one for the code."""
        item = ProvenanceNote(
            code=code,
            severity=severity if severity is not None else NOTE_SEVERITY[code],
            message=message,
            module=module,
            detail=detail,
        )
        self._notes.append(item)
        return item

    def short_circuit(self, rule_id: str) -> None:
        """Mark the rule that ended short-circuit evaluation.

        Surfaced prominently in the UI: "an earlier rule ate it" is the single
        most common source of confusion when debugging a rule set.
        """
        self._short_circuited_by = rule_id

    def set_modules(self, modules: tuple[str, ...]) -> None:
        self._modules = modules

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def build(self, total_ms: float = 0.0) -> Provenance:
        return Provenance(
            profile=self._profile,
            evaluated_modules=self._modules,
            entries=tuple(self._entries),
            notes=tuple(self._notes),
            total_ms=total_ms,
            short_circuited_by=self._short_circuited_by,
        )
