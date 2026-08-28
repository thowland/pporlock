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
    raw: dict[str, Any], *, module: str, index: int, priority: int = DEFAULT_PRIORITY
) -> CompiledRule:
    """Validate and compile one rule. Raises rather than skipping.

    A rule that fails to compile is a load-time error: silently dropping it
    would leave the user believing a block is in force when it is not.
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        raise RuleValidationError("rule has no name", module=module, rule_index=index, field="name")

    action_raw = str(raw.get("action") or "").strip()
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

    matcher = compile_matcher(
        raw.get("match"),
        module=module,
        index=index,
        request_phase=phase_for(action, params) in REQUEST_PHASES,
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
        "short_circuit",
    )

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

    def __len__(self) -> int:
        return (
            len(self.passthrough)
            + len(self.short_circuit)
            + len(self.request_headers)
            + len(self.response_headers)
            + len(self.response_body)
        )

    @property
    def all_rules(self) -> tuple[CompiledRule, ...]:
        return (
            *self.passthrough,
            *self.short_circuit,
            *self.request_headers,
            *self.response_headers,
            *self.response_body,
        )

    def wants_body(self, request: NormalizedRequest) -> bool:
        """Could any enabled rule produce a body transform for this flow?

        Feeds the buffering guard (SPEC-1 §3.4). When nothing could, the
        response is streamed regardless of size or type — the cheapest
        optimisation available, and it applies to the overwhelming majority of
        flows on any real page.
        """
        return any(r.matcher.matches_request(request) for r in self.response_body)

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

    @classmethod
    def from_rules(
        cls,
        raw_rules: Sequence[dict[str, Any]],
        *,
        module: str = "inline",
        priority: int = DEFAULT_PRIORITY,
    ) -> RuleSet:
        compiled = [
            compile_rule(raw, module=module, index=index, priority=priority)
            for index, raw in enumerate(raw_rules)
        ]
        return cls(compiled, modules=(module,))
