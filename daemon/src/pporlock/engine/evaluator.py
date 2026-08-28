"""The evaluator — SPEC-1 §4.3.

Pure: given normalised inputs and a rule set, it returns mutations and
provenance and touches nothing else. That is what makes it replayable, which the
dry runner depends on (REQ CAP-031), and testable without a proxy (REQ TST-001).

Phase order is fixed and matches SPEC-0 §4.2 / REQ PXY-020. Every path through
this module writes provenance — there is no path that produces a decision
without one, which is why the builder is threaded through rather than returned
(REQ CAP-010).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AssetPathError
from .cost import DEFAULT_OFFLOAD_THRESHOLD_BYTES, decide_offload
from .exclusions import ExclusionList
from .models import (
    NormalizedRequest,
    NormalizedResponse,
    RedirectSpec,
    RequestMutation,
    ResponseMutation,
    Scheme,
    SyntheticResponse,
)
from .provenance import Action, NoteCode, Outcome, Phase, Provenance, ProvenanceBuilder
from .ruleset import CompiledRule, RuleSet
from .stubs import StubLibrary

#: Content types the buffering guard will hold in memory for transformation.
DEFAULT_BUFFER_TYPES = (
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
    "application/x-javascript",
    "application/json",
)


@dataclass(frozen=True, slots=True)
class ClientHelloDecision:
    passthrough: bool
    pattern: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class RequestDecision:
    mutation: RequestMutation = field(default_factory=RequestMutation)
    short_circuit: SyntheticResponse | None = None
    kill: bool = False
    wants_body: bool = True

    @property
    def blocked(self) -> bool:
        return self.short_circuit is not None or self.kill


@dataclass(slots=True)
class ResponseDecision:
    mutation: ResponseMutation = field(default_factory=ResponseMutation)


@dataclass(frozen=True, slots=True)
class BufferingDecision:
    """Stream or buffer, decided at ``responseheaders`` and nowhere else."""

    buffer: bool
    reason: str | None = None


class TimeBudget:
    """Per-flow ceiling on transform work (REQ PXY-026).

    On exhaustion the remaining transforms are skipped and the flow is delivered
    with what has already been applied — never dropped, never delayed further.
    """

    __slots__ = ("_spent", "total_ms")

    def __init__(self, total_ms: float = 250.0) -> None:
        self.total_ms = total_ms
        self._spent = 0.0

    def consume(self, ms: float) -> None:
        self._spent += ms

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_ms - self._spent)

    @property
    def exhausted(self) -> bool:
        return self._spent >= self.total_ms


def _resolve_asset(base: Path, relative: str) -> Path:
    """Resolve a module asset path, refusing anything outside its directory.

    Containment is checked after symlink resolution, because a symlink pointing
    out of the directory is exactly the case a naive prefix check misses
    (implementation-plan.md §2.5 "Path traversal").
    """
    candidate = Path(relative)
    if candidate.is_absolute():
        raise AssetPathError(f"asset path must be relative: {relative!r}", path=relative)

    root = base.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise AssetPathError(f"asset path escapes its directory: {relative!r}", path=relative)
    return resolved


class Evaluator:
    """Evaluates a rule set against a flow."""

    __slots__ = (
        "asset_root",
        "buffer_types",
        "exclusions",
        "max_buffer_bytes",
        "offload_threshold",
        "ruleset",
        "stubs",
    )

    def __init__(
        self,
        ruleset: RuleSet | None = None,
        *,
        exclusions: ExclusionList | None = None,
        stubs: StubLibrary | None = None,
        asset_root: Path | None = None,
        buffer_types: tuple[str, ...] = DEFAULT_BUFFER_TYPES,
        max_buffer_bytes: int = 2 * 1024 * 1024,
        offload_threshold: int = DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    ) -> None:
        self.ruleset = ruleset if ruleset is not None else RuleSet()
        self.exclusions = exclusions if exclusions is not None else ExclusionList()
        self.stubs = stubs if stubs is not None else StubLibrary()
        self.asset_root = asset_root
        self.buffer_types = buffer_types
        self.max_buffer_bytes = max_buffer_bytes
        self.offload_threshold = offload_threshold

    # -- phase 1: ClientHello -------------------------------------------

    def evaluate_clienthello(
        self, sni: str | None, ip: str | None, builder: ProvenanceBuilder
    ) -> ClientHelloDecision:
        decision = self.exclusions.decide(sni, ip)
        if not decision.excluded:
            return ClientHelloDecision(passthrough=False)

        builder.note(
            NoteCode.PASSTHROUGH_EXCLUDED,
            f"tunneled undecrypted: {decision.pattern}",
            pattern=decision.pattern,
            reason=decision.comment,
            source=decision.source,
        )
        return ClientHelloDecision(
            passthrough=True, pattern=decision.pattern, reason=decision.comment
        )

    # -- phase 2 and 3: request -----------------------------------------

    def evaluate_request(
        self,
        request: NormalizedRequest,
        builder: ProvenanceBuilder,
        budget: TimeBudget | None = None,
    ) -> RequestDecision:
        started = time.perf_counter()
        decision = RequestDecision()
        builder.set_modules(self.ruleset.modules)

        rule = self.ruleset.first_short_circuit(request)
        if rule is not None:
            self._apply_short_circuit(rule, request, decision, builder)
            builder.short_circuit(rule.rule_id)

        # Header rules still run on a short-circuited request: a rule that adds
        # a header the synthesised response should carry is legitimate, and
        # skipping them silently would be surprising.
        for header_rule in self.ruleset.matching_request_headers(request):
            self._apply_header_rule(
                header_rule, "request", decision.mutation, builder, Phase.REQUEST_HEADERS
            )

        decision.wants_body = self.ruleset.wants_body(request)

        # Charge the budget for request-side work too. Matching a large rule set
        # is not free, and a budget that only counted body transforms would let
        # the request phase overrun it unnoticed.
        if budget is not None:
            budget.consume((time.perf_counter() - started) * 1000)
        return decision

    def _apply_short_circuit(
        self,
        rule: CompiledRule,
        request: NormalizedRequest,
        decision: RequestDecision,
        builder: ProvenanceBuilder,
    ) -> None:
        started = time.perf_counter()

        if rule.action is Action.BLOCK:
            self._apply_block(rule, request, decision, builder, started)
        elif rule.action is Action.MAP_LOCAL:
            self._apply_map_local(rule, request, decision, builder, started)
        elif rule.action is Action.REDIRECT:
            self._apply_redirect(rule, decision, builder, started)

    def _apply_block(
        self,
        rule: CompiledRule,
        request: NormalizedRequest,
        decision: RequestDecision,
        builder: ProvenanceBuilder,
        started: float,
    ) -> None:
        mode = rule.params.get("mode", "stub")
        if mode == "kill":
            decision.kill = True
            builder.record(
                phase=Phase.REQUEST_SHORT_CIRCUIT,
                module=rule.module,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                action=Action.BLOCK,
                outcome=Outcome.APPLIED,
                duration_ms=(time.perf_counter() - started) * 1000,
                mode="kill",
            )
            return

        spec = rule.params.get("stub", "auto")
        try:
            synthetic = self.stubs.resolve(spec, request, origin=rule.rule_id, rule=rule.name)
        except Exception as exc:
            builder.record(
                phase=Phase.REQUEST_SHORT_CIRCUIT,
                module=rule.module,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                action=Action.BLOCK,
                outcome=Outcome.ERROR,
                duration_ms=(time.perf_counter() - started) * 1000,
                error=str(exc),
            )
            builder.note(
                NoteCode.MODULE_ERROR, f"stub could not be resolved: {exc}", module=rule.module
            )
            return

        decision.short_circuit = synthetic
        decision.mutation.short_circuit = synthetic
        builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module=rule.module,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            action=Action.BLOCK,
            outcome=Outcome.APPLIED,
            duration_ms=(time.perf_counter() - started) * 1000,
            stub=spec if isinstance(spec, str) else "inline",
            derived_from_dest=request.dest,
            synthesized_status=synthetic.status,
            synthesized_content_type=synthetic.content_type,
        )

    def _apply_map_local(
        self,
        rule: CompiledRule,
        request: NormalizedRequest,
        decision: RequestDecision,
        builder: ProvenanceBuilder,
        started: float,
    ) -> None:
        relative = str(rule.params.get("file", ""))
        root = self.asset_root

        def fail(message: str, code: NoteCode = NoteCode.MAP_LOCAL_MISSING) -> None:
            builder.record(
                phase=Phase.REQUEST_SHORT_CIRCUIT,
                module=rule.module,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                action=Action.MAP_LOCAL,
                outcome=Outcome.ERROR,
                duration_ms=(time.perf_counter() - started) * 1000,
                file=relative,
                error=message,
            )
            # Loudly, not silently: a map_local pointing at a file that is not
            # there looks exactly like a rule that did not match (REQ PXY-034).
            builder.note(code, message, module=rule.module, file=relative)

        if root is None:
            fail("no asset root configured for this rule")
            return

        try:
            path = _resolve_asset(root, relative)
        except AssetPathError as exc:
            fail(exc.message, NoteCode.MAP_LOCAL_MISSING)
            return

        if not path.is_file():
            fail(f"local file not found: {relative}")
            return

        body = path.read_bytes()
        content_type = str(rule.params.get("content_type") or _guess_content_type(path))
        synthetic = SyntheticResponse(
            status=int(rule.params.get("status", 200)),
            body=body,
            headers=(
                ("content-type", content_type),
                ("cache-control", "no-store"),
                ("x-pporlock", "map_local"),
            ),
            origin=rule.rule_id,
        )
        decision.short_circuit = synthetic
        decision.mutation.short_circuit = synthetic
        builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module=rule.module,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            action=Action.MAP_LOCAL,
            outcome=Outcome.APPLIED,
            duration_ms=(time.perf_counter() - started) * 1000,
            file=relative,
            bytes=len(body),
            content_type=content_type,
        )

    def _apply_redirect(
        self,
        rule: CompiledRule,
        decision: RequestDecision,
        builder: ProvenanceBuilder,
        started: float,
    ) -> None:
        """Rewrite where a request goes (REQ PXY-035).

        This action can retarget a request at any host, including one on the
        local network — which is server-side request forgery by design, and is
        the point: substituting a remote asset with a local one is a stated use
        case of the whole system.

        What makes it safe is where the target comes from. It is read only from
        the rule, which is trusted operator input; nothing in a response body, a
        request header, or a URL can influence it. A redirect target derived
        from intercepted content would be a genuine SSRF vector, and the type of
        `params` is what prevents that from being written by accident
        (implementation-plan.md §2.5).
        """
        target = dict(rule.params.get("to") or {})
        scheme_raw = target.get("scheme")
        scheme: Scheme | None = (
            "https" if scheme_raw == "https" else "http" if scheme_raw == "http" else None
        )
        spec = RedirectSpec(
            scheme=scheme,
            host=str(target["host"]) if target.get("host") else None,
            port=int(target["port"]) if target.get("port") else None,
            path=str(target["path"]) if target.get("path") else None,
            query=str(target["query"]) if target.get("query") is not None else None,
        )
        decision.mutation.redirect = spec
        builder.record(
            phase=Phase.REQUEST_SHORT_CIRCUIT,
            module=rule.module,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            action=Action.REDIRECT,
            outcome=Outcome.APPLIED,
            duration_ms=(time.perf_counter() - started) * 1000,
            to=target,
        )

    # -- phase 4: buffering ---------------------------------------------

    def decide_buffering(
        self,
        request: NormalizedRequest,
        content_type: str | None,
        content_length: int | None,
        wants_body: bool,
        builder: ProvenanceBuilder,
    ) -> BufferingDecision:
        """Stream or buffer. Can only be decided here (REQ PXY-021)."""
        if not wants_body:
            # Nothing could transform this body, so holding it in memory buys
            # nothing. The cheapest and most common case on any real page.
            builder.note(
                NoteCode.RESPONSE_STREAMED,
                "no rule wants this body; streamed",
                reason="no_transform",
            )
            return BufferingDecision(buffer=False, reason="no_transform")

        if content_length is not None and content_length > self.max_buffer_bytes:
            builder.note(
                NoteCode.RESPONSE_STREAMED,
                f"body is {content_length} bytes, over the {self.max_buffer_bytes} threshold",
                reason="size",
            )
            return BufferingDecision(buffer=False, reason="size")

        media = (content_type or "").split(";", 1)[0].strip().lower()
        if media and media not in self.buffer_types:
            builder.note(
                NoteCode.RESPONSE_STREAMED,
                f"content type {media} is outside the buffering allowlist",
                reason="content_type",
            )
            return BufferingDecision(buffer=False, reason="content_type")

        builder.record(
            phase=Phase.BUFFERING_DECISION,
            module="",
            rule_id="",
            action=Action.BODY,
            outcome=Outcome.APPLIED,
            buffered=True,
        )
        return BufferingDecision(buffer=True)

    # -- phase 5 and 6: response ----------------------------------------

    def evaluate_response_headers(
        self,
        request: NormalizedRequest,
        response: NormalizedResponse,
        builder: ProvenanceBuilder,
    ) -> ResponseDecision:
        """Response header rules (REQ PXY-020 phase 5).

        Separate from body evaluation, and applied at ``responseheaders``,
        because once a response streams its headers are already on the wire —
        a header mutation computed later is recorded as applied and silently
        changes nothing. Found end to end rather than by reading.
        """
        decision = ResponseDecision()
        for rule in self.ruleset.matching_response_headers(request, response):
            self._apply_header_rule(
                rule, "response", decision.mutation, builder, Phase.RESPONSE_HEADERS
            )
        return decision

    def evaluate_response_body(
        self,
        request: NormalizedRequest,
        response: NormalizedResponse,
        builder: ProvenanceBuilder,
        budget: TimeBudget | None = None,
    ) -> ResponseDecision:
        """Response body rules (REQ PXY-020 phase 6)."""
        decision = ResponseDecision()

        for rule in self.ruleset.matching_response_body(request, response):
            if response.streamed:
                builder.record(
                    phase=Phase.RESPONSE_BODY,
                    module=rule.module,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    action=Action.BODY,
                    outcome=Outcome.SKIPPED_STREAMED,
                    reason="response was streamed; the body was never buffered",
                )
                continue
            if budget is not None and budget.exhausted:
                builder.record(
                    phase=Phase.RESPONSE_BODY,
                    module=rule.module,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    action=Action.BODY,
                    outcome=Outcome.SKIPPED_BUDGET,
                )
                builder.note(
                    NoteCode.TRANSFORM_BUDGET_EXCEEDED,
                    f"per-flow budget exhausted; {rule.name} was not run",
                    module=rule.module,
                )
                continue

            # Transforms themselves land in Sprint 10. The phase, ordering,
            # offload classification, and provenance are established here so
            # nothing has to be retrofitted around them.
            started = time.perf_counter()
            transform = rule.params.get("transform") or {}
            kind = str(transform.get("kind", "")) if isinstance(transform, dict) else ""
            offload = decide_offload(kind, response.body_size, self.offload_threshold)
            elapsed = (time.perf_counter() - started) * 1000
            if budget is not None:
                budget.consume(elapsed)

            builder.record(
                phase=Phase.RESPONSE_BODY,
                module=rule.module,
                rule_id=rule.rule_id,
                rule_name=rule.name,
                action=Action.BODY,
                outcome=Outcome.NO_CHANGE,
                duration_ms=elapsed,
                reason="transform registry lands in Sprint 10",
                **offload.to_dict(),
            )

        return decision

    def evaluate_response(
        self,
        request: NormalizedRequest,
        response: NormalizedResponse,
        builder: ProvenanceBuilder,
        budget: TimeBudget | None = None,
    ) -> ResponseDecision:
        """Both response phases at once.

        Kept for callers that hold a fully-buffered response and have no
        streaming concern — the dry runner above all, which replays complete
        recorded flows.
        """
        decision = self.evaluate_response_headers(request, response, builder)
        body = self.evaluate_response_body(request, response, builder, budget)
        decision.mutation.status = body.mutation.status
        if body.mutation.body is not None:
            decision.mutation.body = body.mutation.body
        return decision

    def _apply_header_rule(
        self,
        rule: CompiledRule,
        side: str,
        mutation: Any,
        builder: ProvenanceBuilder,
        phase: Phase,
    ) -> None:
        started = time.perf_counter()
        ops = rule.params.get(side) or {}
        changed = False

        for name in ops.get("remove", []) or []:
            mutation.remove(str(name))
            changed = True
        for name, value in (ops.get("set") or {}).items():
            mutation.set(str(name), str(value))
            changed = True
        for name, value in (ops.get("add") or {}).items():
            mutation.add(str(name), str(value))
            changed = True

        builder.record(
            phase=phase,
            module=rule.module,
            rule_id=rule.rule_id,
            rule_name=rule.name,
            action=Action.HEADERS,
            outcome=Outcome.APPLIED if changed else Outcome.NO_CHANGE,
            duration_ms=(time.perf_counter() - started) * 1000,
            side=side,
            operations=ops,
        )


def _guess_content_type(path: Path) -> str:
    """Content type from a file extension, for map_local."""
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def empty_provenance(profile: str = "default") -> Provenance:
    """Provenance for a flow that was never evaluated. Never None (REQ CAP-013)."""
    return ProvenanceBuilder(profile).build()
