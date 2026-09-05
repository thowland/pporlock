"""Rules and rule sets — SPEC-1 §4.1, SPEC-0 §5.3/§5.4.

The two evaluation semantics are the thing to get right, and conflating them
causes trouble later:

* ``block``, ``map_local``, ``redirect`` — **first match wins** across all
  enabled modules. Evaluation of this class stops at the first match.
* ``headers``, ``body`` — **all matches apply**, ordered by module priority
  ascending, then declaration order within a module.

The set is built once and swapped atomically on reload, never mutated in place:
an in-flight flow continues against the snapshot it started with, which is what
removes any need for locking (REQ MOD-004).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import RuleValidationError
from .matcher import CompiledMatcher, compile_matcher
from .models import NormalizedRequest, NormalizedResponse
from .provenance import Action, Phase

#: Actions that end evaluation of their class at the first match.
SHORT_CIRCUIT_ACTIONS = frozenset({Action.BLOCK, Action.MAP_LOCAL, Action.REDIRECT})

#: Reserved action namespace for future WebSocket actions (REQ PXY-052). No
#: action uses it yet; holding it is what makes adding one later additive
#: rather than a change that could collide with a name a module already chose.
WS_ACTION_PREFIX = "ws_"

#: The phase each action runs in (REQ PXY-020).
#:
#: `headers` is the exception and is resolved per rule rather than per action: a
#: headers rule declaring only `request` runs before the response exists, while
#: one declaring `response` runs after — and that difference decides whether
#: response-side match criteria are legal on it.
ACTION_PHASE: dict[Action, Phase] = {
    Action.PASSTHROUGH: Phase.CLIENTHELLO,
    Action.BLOCK: Phase.REQUEST_SHORT_CIRCUIT,
    Action.MAP_LOCAL: Phase.REQUEST_SHORT_CIRCUIT,
    Action.REDIRECT: Phase.REQUEST_SHORT_CIRCUIT,
    Action.HEADERS: Phase.REQUEST_HEADERS,
    Action.BODY: Phase.RESPONSE_BODY,
}

#: Phases that run before any response exists. A response-side match criterion
#: on one of these is a rule that could never fire (REQ MOD-011).
REQUEST_PHASES = frozenset({Phase.CLIENTHELLO, Phase.REQUEST_SHORT_CIRCUIT, Phase.REQUEST_HEADERS})


def phase_for(action: Action, params: dict[str, Any]) -> Phase:
    """The phase a specific rule runs in.

    Only `headers` needs the params: a rule touching the response side runs
    after the response arrives, whatever else it also does.
    """
    if action is Action.HEADERS and "response" in params:
        return Phase.RESPONSE_HEADERS
    return ACTION_PHASE[action]


DEFAULT_PRIORITY = 100


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """One rule, ready to evaluate."""

    rule_id: str
    module: str
    name: str
    action: Action
    matcher: CompiledMatcher
    priority: int = DEFAULT_PRIORITY
    index: int = 0
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def phase(self) -> Phase:
        """The phase this rule runs in.

        A headers rule declaring both sides reports the response phase, because
        that is the later of the two and the one that bounds what it may match
        on. The rule still applies in both.
        """
        return phase_for(self.action, self.params)

    @property
    def is_short_circuit(self) -> bool:
        return self.action in SHORT_CIRCUIT_ACTIONS

    @property
    def sort_key(self) -> tuple[int, int]:
        """Module priority, then declaration order (SPEC-0 §5.4)."""
        return (self.priority, self.index)


def compile_rule(
    raw: dict[str, Any],
    *,
    module: str,
    index: int,
    priority: int = DEFAULT_PRIORITY,
    transforms: Any = None,
) -> CompiledRule:
    """Validate and compile one rule. Raises rather than skipping.

    A rule that fails to compile is a load-time error: silently dropping it
    would leave the user believing a block is in force when it is not.
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuleValidationError("rule has no name", module=module, rule_index=index, field="name")

    action_raw = str(raw.get("action") or "").strip()
    if action_raw.startswith(WS_ACTION_PREFIX):
        # REQ PXY-052. WebSocket frames are inspection-only in v1 (REQ PXY-051),
        # but the action namespace for them is reserved now so that adding
        # ws_send, ws_drop, or ws_rewrite later is additive rather than a
        # breaking change to modules that meanwhile invented their own
        # ws_-prefixed action and got a generic "unknown action" for it.
        raise RuleValidationError(
            f"{action_raw!r} is reserved: the {WS_ACTION_PREFIX!r} action namespace is "
            "held for future WebSocket actions. WebSocket frames are "
            "inspection-only in v1 (REQ PXY-051).",
            module=module,
            rule_index=index,
            field="action",
        )
    try:
        action = Action(action_raw)
    except ValueError as exc:
        valid = ", ".join(a.value for a in Action)
        raise RuleValidationError(
            f"unknown action {action_raw!r}; valid actions are {valid}",
            module=module,
            rule_index=index,
            field="action",
        ) from exc

    params = {
        key: value
        for key, value in raw.items()
        if key not in {"name", "action", "match", "enabled"}
    }
    _validate_params(action, params, module=module, index=index)
    if transforms is not None and action is Action.BODY:
        validate_transforms(params, transforms, module=module, index=index)

    # A two-sided headers rule mutates the request *before* any response
    # exists, so it is bound by request-phase criteria even though its later
    # half runs at response time. Classifying it purely by its latest phase let
    # a rule declare `status: 500` and still add its request header to every
    # matching request, response or no (SEP_5_REVIEW F-09, REQ MOD-011).
    request_phase = phase_for(action, params) in REQUEST_PHASES or (
        action is Action.HEADERS and "request" in params
    )
    matcher = compile_matcher(
        raw.get("match"),
        module=module,
        index=index,
        request_phase=request_phase,
    )

    return CompiledRule(
        rule_id=f"{module}:{index}",
        module=module,
        name=name,
        action=action,
        matcher=matcher,
        priority=priority,
        index=index,
        enabled=bool(raw.get("enabled", True)),
        params=params,
    )


def _validate_params(action: Action, params: dict[str, Any], *, module: str, index: int) -> None:
    """Action-specific parameter checks, at load time (REQ MOD-014)."""
    if action is Action.BLOCK:
        mode = params.get("mode", "stub")
        if mode not in {"stub", "kill"}:
            raise RuleValidationError(
                f"block mode must be 'stub' or 'kill', got {mode!r}",
                module=module,
                rule_index=index,
                field="mode",
            )
    elif action is Action.MAP_LOCAL:
        if not params.get("file"):
            raise RuleValidationError(
                "map_local requires a 'file'", module=module, rule_index=index, field="file"
            )
    elif action is Action.REDIRECT:
        target = params.get("to")
        if not isinstance(target, dict) or not target:
            raise RuleValidationError(
                "redirect requires a 'to' with at least one component",
                module=module,
                rule_index=index,
                field="to",
            )
    elif action is Action.HEADERS:
        if not (params.get("request") or params.get("response")):
            raise RuleValidationError(
                "headers requires a 'request' or 'response' block",
                module=module,
                rule_index=index,
                field="request",
            )
    elif action is Action.BODY:
        if not (params.get("transform") or params.get("transforms")):
            raise RuleValidationError(
                "body requires a 'transform' or 'transforms'",
                module=module,
                rule_index=index,
                field="transform",
            )


#: Transform kinds that are declared as body transforms but operate on headers.
#:
#: `strip_csp` is applied during response-*header* evaluation, where a mutation
#: can still reach the wire on a streamed response. Treating a rule whose only
#: transform is one of these as body demand made every matching HTML response
#: eligible for buffering for a header edit that needs no body at all
#: (SEP_5_REVIEW F-14, REQ PXY-021).
HEADER_ONLY_TRANSFORMS = frozenset({"strip_csp"})


def transforms_of(rule_params: dict[str, Any]) -> list[dict[str, Any]]:
    """A rule's transform blocks, whether declared singly or as a list."""
    single = rule_params.get("transform")
    many = rule_params.get("transforms")
    out: list[dict[str, Any]] = []
    if isinstance(single, dict):
        out.append(single)
    if isinstance(many, list):
        out.extend(item for item in many if isinstance(item, dict))
    return out


def needs_body(rule: CompiledRule) -> bool:
    """Whether evaluating this rule requires the response body in memory."""
    if rule.action is not Action.BODY:
        return False
    return any(
        str(t.get("kind", "")) not in HEADER_ONLY_TRANSFORMS for t in transforms_of(rule.params)
    )


def validate_transforms(
    params: dict[str, Any], registry: Any, *, module: str = "", index: int = 0
) -> None:
    """Check every transform block of a body rule against the live registry.

    Separate from `_validate_params` because it needs the registry, and the
    registry is not always complete at compile time: a module's own `on_load`
    may register a transform its rules use, and rules compile during load. So
    this runs wherever the registry *is* complete — the loader's post-load pass,
    the validation endpoint, the dry runner — and the shallow structural check
    runs everywhere (SEP_5_REVIEW F-07, REQ MOD-014/015).

    `strip_csp` is not in the registry: it is applied by the evaluator during
    header evaluation rather than as a registered body transform. It is a
    declared kind all the same, so it is accepted here.
    """
    for transform in transforms_of(params):
        kind = str(transform.get("kind") or "")
        if kind in HEADER_ONLY_TRANSFORMS:
            continue
        registry.validate(transform, module=module, index=index)


class RuleSet:
    """An immutable, phase-partitioned set of rules.

    Partitioned at build time so a request touches only the lists that can
    possibly apply to it — which is most of how REQ PRF-002's 2 ms budget for a
    non-matching flow is met.
    """

    __slots__ = (
        "modules",
        "passthrough",
        "request_headers",
        "response_body",
        "response_headers",
        "rules",
        "short_circuit",
    )

    @staticmethod
    def combine(*sets: RuleSet) -> RuleSet:
        """One rule set from several sources.

        File rules and module rules are one rule set to the engine — a module's
        priority orders its rules against everything else, including yours
        (REQ MOD-023). Building them separately and only ever installing the
        module half is how enabling a module silently deletes rules.yaml.
        """
        rules: list[CompiledRule] = []
        modules: list[str] = []
        for ruleset in sets:
            rules.extend(ruleset.rules)
            modules.extend(ruleset.modules)
        return RuleSet(rules, modules=tuple(dict.fromkeys(modules)))

    def __init__(self, rules: Sequence[CompiledRule] = (), modules: tuple[str, ...] = ()) -> None:
        enabled = sorted((r for r in rules if r.enabled), key=lambda r: r.sort_key)

        self.passthrough = tuple(r for r in enabled if r.action is Action.PASSTHROUGH)
        self.short_circuit = tuple(r for r in enabled if r.is_short_circuit)
        self.request_headers = tuple(
            r for r in enabled if r.action is Action.HEADERS and "request" in r.params
        )
        self.response_headers = tuple(
            r for r in enabled if r.action is Action.HEADERS and "response" in r.params
        )
        self.response_body = tuple(r for r in enabled if r.action is Action.BODY)
        self.modules = modules
        # The rules this set was built from, enabled and sorted. Kept because
        # the partitions cannot be reassembled into a set: file rules and module
        # rules are compiled separately and have to become one set before the
        # engine sees them, and reconstructing that from six tuples would be a
        # second, divergent definition of the ordering.
        self.rules = tuple(enabled)

    def __len__(self) -> int:
        """How many rules were declared.

        `self.rules`, not the sum of the phase partitions. A two-sided headers
        rule is deliberately placed in both the request and response partitions
        — it applies in both — so summing them counted one declared rule twice
        and any aggregate view built from them returned it twice
        (SEP_5_REVIEW F-09).
        """
        return len(self.rules)

    @property
    def all_rules(self) -> tuple[CompiledRule, ...]:
        """Every declared rule, once, in priority then declaration order."""
        return self.rules

    def wants_body(self, request: NormalizedRequest) -> bool:
        """Could any enabled rule produce a body transform for this flow?

        Feeds the buffering guard (SPEC-1 §3.4). When nothing could, the
        response is streamed regardless of size or type — the cheapest
        optimisation available, and it applies to the overwhelming majority of
        flows on any real page.
        """
        return any(needs_body(r) and r.matcher.matches_request(request) for r in self.response_body)

    def first_short_circuit(self, request: NormalizedRequest) -> CompiledRule | None:
        """First-match-wins across all modules (REQ MOD-012)."""
        for rule in self.short_circuit:
            if rule.matcher.matches_request(request):
                return rule
        return None

    def matching_request_headers(self, request: NormalizedRequest) -> tuple[CompiledRule, ...]:
        """All matches, in priority then declaration order."""
        return tuple(r for r in self.request_headers if r.matcher.matches_request(request))

    def matching_response_headers(
        self, request: NormalizedRequest, response: NormalizedResponse
    ) -> tuple[CompiledRule, ...]:
        return tuple(
            r for r in self.response_headers if r.matcher.matches_response(request, response)
        )

    def matching_response_body(
        self, request: NormalizedRequest, response: NormalizedResponse
    ) -> tuple[CompiledRule, ...]:
        return tuple(r for r in self.response_body if r.matcher.matches_response(request, response))

    def validate_transforms(self, registry: Any) -> None:
        """Check every body rule against a now-complete transform registry.

        The late half of the two-stage validation described in
        `validate_transforms`. Raises `RuleValidationError` for the first
        offender, which is a load error: a rule naming a transform that does not
        exist can never run, and letting it sit in the set until traffic matches
        turns a deterministic typo into a per-flow runtime failure blamed on the
        module author (SEP_5_REVIEW F-07).
        """
        for rule in self.rules:
            if rule.action is Action.BODY:
                validate_transforms(rule.params, registry, module=rule.module, index=rule.index)

    @classmethod
    def from_rules(
        cls,
        raw_rules: Sequence[dict[str, Any]],
        *,
        module: str = "inline",
        priority: int = DEFAULT_PRIORITY,
        transforms: Any = None,
    ) -> RuleSet:
        compiled = [
            compile_rule(raw, module=module, index=index, priority=priority, transforms=transforms)
            for index, raw in enumerate(raw_rules)
        ]
        return cls(compiled, modules=(module,))
