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
from dataclasses import dataclass, field, replace
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
    WebSocketMessage,
)
from .modules.registry import ModuleRegistry
from .provenance import Action, NoteCode, Outcome, Phase, Provenance, ProvenanceBuilder
from .ruleset import CompiledRule, RuleSet
from .stubs import StubLibrary
from .transforms import TransformContext, TransformRegistry, build_registry
from .transforms.headers import csp_headers_to_remove

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
    #: Which action ended request evaluation, when one did.
    #:
    #: Three actions short-circuit and only one of them is a block (REQ
    #: MOD-012). Collapsing them lost the distinction between a request that
    #: was refused and one that was answered from disk or sent elsewhere —
    #: both succeeded, and calling either "blocked" misreports the flow
    #: (OI-26).
    short_circuit_action: Action | None = None

    @property
    def blocked(self) -> bool:
        """Whether the client was denied the response it asked for.

        `kill` and a `block` stub are refusals. `map_local` and `redirect`
        short-circuit too, and are not: the browser got a response, just not
        from the origin.
        """
        return self.kill or self.short_circuit_action is Action.BLOCK


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
        "registry",
        "ruleset",
        "stubs",
        "transforms",
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
        transforms: TransformRegistry | None = None,
        registry: ModuleRegistry | None = None,
    ) -> None:
        self.ruleset = ruleset if ruleset is not None else RuleSet()
        self.exclusions = exclusions if exclusions is not None else ExclusionList()
        self.stubs = stubs if stubs is not None else StubLibrary()
        self.asset_root = asset_root
        self.buffer_types = buffer_types
        self.max_buffer_bytes = max_buffer_bytes
        self.offload_threshold = offload_threshold
        self.transforms = transforms if transforms is not None else build_registry()
        # The module registry, when there is one. Python hooks are interleaved
        # with declarative rules by module priority (REQ MOD-023) rather than
        # run as a separate stage — a module that both strips CSP declaratively
        # and injects via Python must see one consistent ordering.
        self.registry = registry

    def clone_with(
        self,
        *,
        ruleset: RuleSet,
        registry: ModuleRegistry | None,
        transforms: TransformRegistry | None = None,
    ) -> Evaluator:
        """A second evaluator configured exactly like this one.

        This is how the dry runner gets "the same Evaluator as live" (REQ
        CAP-031) without evaluating against the live rule set: same class, same
        buffering bounds, same stub library, same asset root, same transforms —
        only the rules and the module set differ. Enumerating the constructor
        arguments in one place is deliberate; a new evaluator setting that the
        dry run silently did not inherit would make dry-run output stop
        predicting live behaviour, which is the only thing it is for. A test
        asserts this covers every configured attribute.
        """
        return Evaluator(
            ruleset,
            exclusions=self.exclusions,
            stubs=self.stubs,
            asset_root=self.asset_root,
            buffer_types=self.buffer_types,
            max_buffer_bytes=self.max_buffer_bytes,
            offload_threshold=self.offload_threshold,
            transforms=transforms if transforms is not None else self.transforms,
            registry=registry,
        )

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
            # Only when it took effect. `_apply_short_circuit` already refuses
            # to claim the action for a rule that matched and then failed — a
            # missing map_local file, an unknown stub — and recording the flow
            # as short-circuited anyway contradicted it: the request went
            # upstream and provenance named the rule that did not stop it
            # (SEP_5_REVIEW F-12, REQ PXY-034, CAP-010).
            if decision.short_circuit_action is not None:
                builder.short_circuit(rule.rule_id)

        # Header rules still run on a short-circuited request: a rule that adds
        # a header the synthesised response should carry is legitimate, and
        # skipping them silently would be surprising.
        state = _HeaderState(request.headers)
        for header_rule in self.ruleset.matching_request_headers(request):
            self._apply_header_rule(
                header_rule,
                "request",
                decision.mutation,
                builder,
                Phase.REQUEST_HEADERS,
                state,
            )

        self._run_python_hooks(
            "on_request",
            builder,
            decision.mutation,
            request=request,
            decision=decision,
            budget=budget,
        )

        decision.wants_body = self.wants_body(request)

        # Charge the budget for the matching work too. Matching a large rule set
        # is not free, and a budget that only counted body transforms would let
        # the request phase overrun it unnoticed. Hook time is charged inside
        # `_run_python_hooks`, per hook, so that one slow module is what stops
        # rather than the module that happens to run after it.
        if budget is not None:
            budget.consume((time.perf_counter() - started) * 1000)
        return decision

    # -- body demand and offload, over both tiers ------------------------

    def wants_body(self, request: NormalizedRequest) -> bool:
        """Could anything transform this response body?

        Both tiers, not just the declarative one. `RuleSet.wants_body` knows
        only about body rules, so a module whose entire content is an
        `on_response` hook — a layout the Python tutorial presents as
        supported — reported no body demand, streamed, and was handed
        `response.body=None`. The hook ran and could do nothing, and nothing
        said so (SEP_5_REVIEW F-01, REQ MOD-020, PXY-021).
        """
        return self.ruleset.wants_body(request) or self.has_response_hook()

    def has_response_hook(self) -> bool:
        """Whether any module active in this snapshot reads response bodies."""
        if self.registry is None:
            return False
        profile_modules = None if not self.ruleset.modules else list(self.ruleset.modules)
        return any(
            "on_response" in module.hooks() for module in self.registry.active(profile_modules)
        )

    def should_offload(self, request: NormalizedRequest, response: NormalizedResponse) -> bool:
        """Whether this flow's body work belongs on a worker thread.

        Python response hooks count. They were absent from this decision
        entirely, so a hook-only module ran inline on the proxy event loop
        however large the body, while an unrelated declarative transform on the
        same flow would have moved it to a worker — non-local behaviour, and the
        opposite of what SPEC-1 documents (REQ PXY-024, MOD-023).

        A hook's cost is unknown, so it is treated the way a module-registered
        transform is: expensive above the configured threshold. That is the same
        rule `decide_offload` applies to a SIZED transform, and the threshold is
        the one the operator configured.
        """
        if any(
            decide_offload(
                str(t.get("kind", "")), response.body_size, self.offload_threshold
            ).offload
            for rule in self.ruleset.matching_response_body(request, response)
            for t in _transforms_of(rule)
        ):
            return True
        return self.has_response_hook() and response.body_size >= self.offload_threshold

    def enforce_observed_size(
        self, response: NormalizedResponse, builder: ProvenanceBuilder
    ) -> bool:
        """Whether a buffered body turned out to be too large to transform.

        PXY-021 bounds a body by its declared *or observed* size.
        `decide_buffering` can only see the declared one, and a chunked response
        declares nothing — so an arbitrarily large HTML or JSON body could be
        accumulated for transformation with no later guard. mitmproxy's own
        `stream_large_bodies` is the memory bound (set from the same
        configuration in `cli/runner.py`); this is the engine-side half, and it
        is what makes the transition visible in provenance rather than leaving a
        transform to be silently attempted on a body over the cap
        (SEP_5_REVIEW F-06).
        """
        if response.streamed or response.body is None:
            return False
        if response.body_size <= self.max_buffer_bytes:
            return False
        builder.note(
            NoteCode.RESPONSE_STREAMED,
            f"body arrived at {response.body_size} bytes, over the "
            f"{self.max_buffer_bytes} threshold; body transforms were skipped",
            reason="observed_size",
            observed_bytes=response.body_size,
        )
        return True

    def _asset_root_for(self, rule: CompiledRule) -> Path | None:
        """Where a rule's ``file:`` is resolved from.

        A module's assets live in its own ``assets/`` directory — SPEC-0 §5.4
        and the rule schema both say so, and it is the only place the module
        author can put them.

        This used to resolve every rule against one global asset root, which is
        the state directory. A module's ``map_local`` could therefore never find
        its own file: it reported ``map_local_missing`` and served the real
        response instead, which looks exactly like a rule that did not match.
        The documented primary mechanism did not work from the only place rules
        come from.

        Rules loaded from ``rules.yaml`` have no module and keep the old root,
        which is the directory that file lives in.
        """
        if rule.module and self.registry is not None:
            return Path(self.registry.root) / rule.module / "assets"
        return self.asset_root

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

        # Recorded only when the action actually took effect. A rule that
        # matched and then failed — an unknown stub, an asset outside the
        # module — leaves the flow to proceed normally, and must not be
        # reported as having short-circuited it. Each action signals success
        # differently: a block kills or substitutes, map_local substitutes, a
        # redirect retargets.
        if (
            decision.kill
            or decision.short_circuit is not None
            or decision.mutation.redirect is not None
        ):
            decision.short_circuit_action = rule.action

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
        root = self._asset_root_for(rule)

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
            # A chunked response declares no length, so this decision is taken
            # on trust and re-checked against the delivered bytes in
            # `enforce_observed_size`. Recorded so that a flow which then
            # exceeds the cap has both halves of the story (REQ PXY-021).
            declared_length=content_length,
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
        state = _HeaderState(response.headers)
        for rule in self.ruleset.matching_response_headers(request, response):
            self._apply_header_rule(
                rule, "response", decision.mutation, builder, Phase.RESPONSE_HEADERS, state
            )

        # strip_csp is written as a body transform in the rule schema, but it
        # operates on headers — so it is applied here, where a mutation can
        # still reach the wire on a streamed response (Sprint 9's finding).
        for rule in self.ruleset.matching_response_body(request, response):
            for transform in _transforms_of(rule):
                if str(transform.get("kind")) != "strip_csp":
                    continue
                started = time.perf_counter()
                removed = csp_headers_to_remove(bool(transform.get("report_only", True)))
                present = [h for h in removed if response.header(h) is not None]
                for header in removed:
                    decision.mutation.remove(header)
                if present:
                    builder.note(
                        NoteCode.CSP_MODIFIED,
                        f"removed {', '.join(present)}",
                        module=rule.module,
                        headers=present,
                    )
                builder.record(
                    phase=Phase.RESPONSE_HEADERS,
                    module=rule.module,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    action=Action.BODY,
                    outcome=Outcome.APPLIED if present else Outcome.NO_CHANGE,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    transform="strip_csp",
                    removed=present,
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
        original = response.text or ""
        text = original
        context = TransformContext(
            url="", content_type=response.content_type, headers=response.headers
        )

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

            for transform in _transforms_of(rule):
                kind = str(transform.get("kind", ""))
                if kind == "strip_csp":
                    # Already applied in the header phase, where it can take
                    # effect. Recorded there too, so it is not counted twice.
                    continue

                started = time.perf_counter()
                offload = decide_offload(kind, response.body_size, self.offload_threshold)
                before = text
                outcome = Outcome.NO_CHANGE
                detail: dict[str, Any] = {"transform": kind, **offload.to_dict()}

                try:
                    text = self.transforms.apply(transform, text, context)
                    outcome = Outcome.APPLIED if text != before else Outcome.NO_CHANGE
                except Exception as exc:
                    outcome = Outcome.ERROR
                    detail["error"] = str(exc)
                    builder.note(
                        NoteCode.MODULE_ERROR,
                        f"{kind} failed: {exc}",
                        module=rule.module,
                    )

                elapsed = (time.perf_counter() - started) * 1000
                if budget is not None:
                    budget.consume(elapsed)

                builder.record(
                    phase=Phase.RESPONSE_BODY,
                    module=rule.module,
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    action=Action.BODY,
                    outcome=outcome,
                    duration_ms=elapsed,
                    **detail,
                )

                # Drained here, per transform, so each note carries the module
                # that caused it. One context is shared across every rule in this
                # phase; draining once at the end would leave every SRI_STRIPPED
                # and SCRIPT_INJECTED with a null module, which is the wrong
                # answer to "which module weakened this page" (§2.5, A03).
                self._drain_transform_notes(context, builder, module=rule.module)

        # Any document whose body we rewrote must have its integrity attributes
        # stripped, whether or not a rule asked (REQ PXY-040). The breakage is
        # invisible from the proxy's side — a successful response the browser
        # silently drops — so leaving it to the operator to remember would
        # guarantee it is eventually forgotten.
        if text != original and _is_html(response.content_type):
            started = time.perf_counter()
            text = self.transforms.apply({"kind": "strip_integrity_attributes"}, text, context)
            builder.record(
                phase=Phase.RESPONSE_BODY,
                module="",
                rule_id="",
                rule_name="implicit SRI strip",
                action=Action.BODY,
                outcome=Outcome.APPLIED if text != original else Outcome.NO_CHANGE,
                duration_ms=(time.perf_counter() - started) * 1000,
                reason="a rewritten document must not fail its own integrity checks",
            )

        # The implicit strip is the engine's own act, not a module's, so what is
        # left in the context after it belongs to nobody in particular.
        self._drain_transform_notes(context, builder, module=None)

        # Declarative transforms first, then hooks — and hooks see the body as
        # the transforms have left it.
        #
        # Previously the hooks ran against the original response and the
        # transform result was written over decision.mutation.body afterwards,
        # so a hook that edited the body had its edit silently discarded
        # whenever any declarative body rule also matched. Provenance recorded
        # the hook as `applied`. That is the precise failure this system exists
        # to make impossible, and it broke the ordering promise of REQ MOD-023
        # while appearing to keep it.
        #
        # Handing hooks the transformed body means the two tiers compose rather
        # than race: a hook reads what the rules produced and edits that.
        if text != original:
            decision.mutation.body = text.encode(_charset(response))
            response = replace(response, body=decision.mutation.body)

        self._run_python_hooks(
            "on_response",
            builder,
            decision.mutation,
            request=request,
            response=response,
            budget=budget,
        )

        return decision

    @staticmethod
    def _drain_transform_notes(
        context: TransformContext, builder: ProvenanceBuilder, *, module: str | None
    ) -> None:
        """Move a transform's notes into provenance, attributed.

        An unrecognised code becomes a MODULE_ERROR rather than a ValueError.
        Built-in transforms only emit codes from the taxonomy, but a
        module-registered transform (REQ MOD-021) calling ``ctx.note`` with
        anything else would otherwise take down evaluation of the whole body
        phase from inside a note — a reporting mechanism that can destroy the
        thing it is reporting on.
        """
        for code, message, detail in context.drain():
            try:
                note_code = NoteCode(code)
            except ValueError:
                builder.note(
                    NoteCode.MODULE_ERROR,
                    f"transform emitted an unknown note code {code!r}: {message}",
                    module=module,
                    note_code=code,
                    **detail,
                )
                continue
            builder.note(note_code, message, module=module, **detail)

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
        # The whole mutation, not just status and body. The body phase is where
        # ``on_response`` runs, and a Python hook setting a header writes it
        # here — live applies that mutation in full, so folding only two fields
        # in would have made the dry run under-report the exact case REQ CAP-032
        # exists to cover.
        _merge_mutation(decision.mutation, body.mutation)
        return decision

    def observe_websocket_message(
        self,
        message: WebSocketMessage,
        request: NormalizedRequest,
        builder: ProvenanceBuilder,
    ) -> None:
        """Offer a WebSocket frame to every active module, read-only.

        Frames are inspection-only in v1 (REQ PXY-051), so a returned value is
        deliberately ignored rather than merged: a module that believes it can
        rewrite a frame should find that it did not, rather than find
        provenance claiming a change the wire never saw.

        This exists because ``on_websocket_message`` was a declared hook name
        that nothing ever called. A module defining it loaded cleanly, reported
        healthy, and did nothing — the exact silent failure the provenance
        design is built to prevent.
        """
        if self.registry is None:
            return

        for module in self.registry.active(
            None if not self.ruleset.modules else list(self.ruleset.modules)
        ):
            fn = module.hooks().get("on_websocket_message")
            base = self.registry.context(module.name)
            if fn is None or base is None:
                continue

            # Per invocation, for the same reason the HTTP hooks are: frames
            # from two sockets arrive interleaved (SEP_5_REVIEW F-10).
            context = base.for_invocation()
            started = time.perf_counter()
            try:
                fn(message, request, context)
                self.registry.record_success(module.name)
            except Exception as exc:
                builder.note(
                    NoteCode.MODULE_ERROR,
                    f"{module.name}.on_websocket_message raised: {exc}",
                    module=module.name,
                    hook="on_websocket_message",
                )
                self.registry.record_failure(module.name, builder)
                builder.record(
                    phase=Phase.WEBSOCKET,
                    module=module.name,
                    rule_id=f"{module.name}:python",
                    rule_name="on_websocket_message",
                    action=Action.BODY,
                    outcome=Outcome.ERROR,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error=str(exc),
                )
                continue

            for code, severity, note_message, detail in context.notes:
                builder.note(code, note_message, severity=severity, module=module.name, **detail)

    def _run_python_hooks(
        self,
        hook: str,
        builder: ProvenanceBuilder,
        mutation: Any,
        *,
        request: NormalizedRequest,
        response: NormalizedResponse | None = None,
        decision: Any = None,
        budget: TimeBudget | None = None,
    ) -> None:
        """Run each active module's hook, isolated from every other.

        An exception is caught, attributed to the module, and does not affect
        the flow (REQ MOD-024). N consecutive failures quarantine the module
        (REQ MOD-025): a module failing on every flow produces noise that would
        bury real findings.
        """
        if self.registry is None:
            return

        for module in self.registry.active(
            None if not self.ruleset.modules else list(self.ruleset.modules)
        ):
            fn = module.hooks().get(hook)
            base = self.registry.context(module.name)
            if fn is None or base is None:
                continue

            # The per-flow budget bounds hooks as well as transforms
            # (REQ PXY-026, PRF-006). It cannot interrupt a hook that has
            # already started — nothing in-process can — but charging each hook
            # as it returns, and stopping before the next one, is what keeps one
            # slow module from spending every other module's share of the flow.
            # Previously the whole request phase was charged once, after every
            # hook had run (SEP_5_REVIEW F-01).
            if budget is not None and budget.exhausted:
                builder.record(
                    phase=Phase.REQUEST_HEADERS if response is None else Phase.RESPONSE_BODY,
                    module=module.name,
                    rule_id=f"{module.name}:python",
                    rule_name=hook,
                    action=Action.HEADERS if response is None else Action.BODY,
                    outcome=Outcome.SKIPPED_BUDGET,
                )
                builder.note(
                    NoteCode.TRANSFORM_BUDGET_EXCEEDED,
                    f"per-flow budget exhausted; {module.name}.{hook} was not run",
                    module=module.name,
                )
                continue

            # Notes and logs are per invocation, not per module. One context
            # object per loaded module meant two concurrent flows appended into
            # the same lists and the first to drain took both — a note about one
            # page attached to another, and the correct flow left with none
            # (SEP_5_REVIEW F-10, REQ CAP-010/012).
            context = base.for_invocation()

            started = time.perf_counter()
            try:
                result = (
                    fn(request, context) if response is None else fn(request, response, context)
                )
                outcome = Outcome.NO_CHANGE
                if result is not None:
                    _merge_mutation(mutation, result)
                    outcome = Outcome.APPLIED
                    if decision is not None and getattr(result, "short_circuit", None):
                        decision.short_circuit = result.short_circuit
                self.registry.record_success(module.name)
            except Exception as exc:
                outcome = Outcome.ERROR
                builder.note(
                    NoteCode.MODULE_ERROR,
                    f"{module.name}.{hook} raised: {exc}",
                    module=module.name,
                    hook=hook,
                )
                self.registry.record_failure(module.name, builder)
                builder.record(
                    phase=Phase.REQUEST_HEADERS if response is None else Phase.RESPONSE_BODY,
                    module=module.name,
                    rule_id=f"{module.name}:python",
                    rule_name=hook,
                    action=Action.HEADERS if response is None else Action.BODY,
                    outcome=outcome,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error=str(exc),
                )
                if budget is not None:
                    budget.consume((time.perf_counter() - started) * 1000)
                continue

            for code, severity, message, detail in context.notes:
                builder.note(code, message, severity=severity, module=module.name, **detail)

            elapsed = (time.perf_counter() - started) * 1000
            if budget is not None:
                budget.consume(elapsed)
            builder.record(
                phase=Phase.REQUEST_HEADERS if response is None else Phase.RESPONSE_BODY,
                module=module.name,
                rule_id=f"{module.name}:python",
                rule_name=hook,
                action=Action.HEADERS if response is None else Action.BODY,
                outcome=outcome,
                duration_ms=elapsed,
            )

    def _apply_header_rule(
        self,
        rule: CompiledRule,
        side: str,
        mutation: Any,
        builder: ProvenanceBuilder,
        phase: Phase,
        state: _HeaderState | None = None,
    ) -> None:
        started = time.perf_counter()
        ops = rule.params.get(side) or {}
        changed = False
        # Headers this rule touches that carry a security meaning. A CSP removed
        # or rewritten by a plain headers rule weakens the page exactly as much
        # as one removed by strip_csp, and until this existed only the latter
        # said so — so the in-page banner, which exists precisely to stop a page
        # being weakened invisibly, never appeared for the former. The note is
        # attached to the act, not to the transform that happens to perform it.
        touched_csp: list[str] = []

        # `changed` is what the operation does to the message, not what the
        # rule declares. Removing a header that is not there and setting one to
        # the value it already holds are both no-ops, and recording them as
        # `applied` put untouched flows into the UI's "was modified" filter —
        # capture derives that state from provenance (SEP_5_REVIEW F-12).
        #
        # The state is threaded across every rule in the phase rather than read
        # from the message each time, so a rule removing a header an earlier
        # rule added is correctly a change, and a second rule removing the same
        # header is correctly not.
        for name in ops.get("remove", []) or []:
            mutation.remove(str(name))
            changed |= state is None or state.remove(str(name))
            if _is_csp_header(str(name)):
                touched_csp.append(str(name).lower())
        for name, value in (ops.get("set") or {}).items():
            mutation.set(str(name), str(value))
            changed |= state is None or state.set(str(name), str(value))
            if _is_csp_header(str(name)):
                touched_csp.append(str(name).lower())
        for name, value in (ops.get("add") or {}).items():
            mutation.add(str(name), str(value))
            if state is not None:
                state.add(str(name), str(value))
            changed = True

        if touched_csp and side == "response":
            builder.note(
                NoteCode.CSP_MODIFIED,
                f"a headers rule changed {', '.join(sorted(set(touched_csp)))}",
                module=rule.module,
                headers=sorted(set(touched_csp)),
                rule=rule.name,
            )

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


class _HeaderState:
    """The header collection a phase's rules are collectively editing.

    Pure — it models what the adapter will do to the mitmproxy Headers object,
    so the evaluator can record whether an operation actually changes anything
    without reaching across the boundary it exists to stay behind (REQ DD-2).
    Each method returns whether the state moved.
    """

    __slots__ = ("_values",)

    def __init__(self, headers: tuple[tuple[str, str], ...]) -> None:
        self._values: dict[str, list[str]] = {}
        for name, value in headers:
            self._values.setdefault(name.lower(), []).append(value)

    def remove(self, name: str) -> bool:
        return self._values.pop(name.lower(), None) is not None

    def set(self, name: str, value: str) -> bool:
        key = name.lower()
        if self._values.get(key) == [value]:
            return False
        self._values[key] = [value]
        return True

    def add(self, name: str, value: str) -> None:
        self._values.setdefault(name.lower(), []).append(value)


#: Content-Security-Policy and its report-only sibling, lowercased.
CSP_HEADERS = frozenset({"content-security-policy", "content-security-policy-report-only"})


def _is_csp_header(name: str) -> bool:
    return name.lower() in CSP_HEADERS


def _guess_content_type(path: Path) -> str:
    """Content type from a file extension, for map_local."""
    import mimetypes

    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def empty_provenance(profile: str = "default") -> Provenance:
    """Provenance for a flow that was never evaluated. Never None (REQ CAP-013)."""
    return ProvenanceBuilder(profile).build()


def _transforms_of(rule: CompiledRule) -> list[dict[str, Any]]:
    """A rule's transforms, whether declared singly or as a list."""
    single = rule.params.get("transform")
    many = rule.params.get("transforms")
    out: list[dict[str, Any]] = []
    if isinstance(single, dict):
        out.append(single)
    if isinstance(many, list):
        out.extend(item for item in many if isinstance(item, dict))
    return out


def _is_html(content_type: str | None) -> bool:
    return bool(content_type) and "html" in (content_type or "").lower()


def _charset(response: NormalizedResponse) -> str:
    """Charset to re-encode a rewritten body with.

    Read from the raw Content-Type header, not `response.content_type`, which
    strips parameters by design. Getting this wrong re-encodes a latin-1 page as
    UTF-8 and renders it as mojibake — a page that loads and is subtly wrong,
    which is the exact failure this system exists to avoid.
    """
    raw = (response.header("content-type") or "").lower()
    if "charset=" in raw:
        return raw.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    return "utf-8"


def _merge_mutation(target: Any, source: Any) -> None:
    """Fold a module's returned mutation into the accumulated one.

    Modules contribute to the same mutation declarative rules do, rather than
    getting their own application pass — which is what makes ordering between
    the two meaningful (REQ MOD-023).

    Header operations are folded in the order the source declared them, so a
    hook that adds a header and then removes it means what it says. A source
    without ``ops`` is a hand-built object from trusted module code; it is
    honoured in the only order it expresses.
    """
    ops = getattr(source, "ops", None)
    if ops is not None:
        for op, name, value in ops:
            if op == "set":
                target.set(name, value)
            elif op == "add":
                target.add(name, value)
            else:
                target.remove(name)
    else:
        for name, value in getattr(source, "set_headers", {}).items():
            target.set(name, value)
        for name, value in getattr(source, "add_headers", []):
            target.add(name, value)
        for name in getattr(source, "remove_headers", []):
            target.remove(name)

    body = getattr(source, "body", None)
    if body is not None:
        target.body = body

    status = getattr(source, "status", None)
    if status is not None and hasattr(target, "status"):
        target.status = status

    redirect = getattr(source, "redirect", None)
    if redirect is not None and hasattr(target, "redirect"):
        target.redirect = redirect

    short_circuit = getattr(source, "short_circuit", None)
    if short_circuit is not None and hasattr(target, "short_circuit"):
        target.short_circuit = short_circuit
