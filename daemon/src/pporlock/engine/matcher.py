"""Rule matching — SPEC-1 §4.2, SPEC-0 §5.3.

Every pattern is compiled once at rule-load time and never at request time
(REQ PXY-025). Criteria are evaluated cheapest-first — set membership before
glob, glob before regex — because REQ PRF-002 budgets under 2 ms at the 95th
percentile for a flow that matches nothing, and a non-matching flow is the
common case on every page.

Semantics, restated because this is the most error-prone part of the model:
all present criteria must match; absent criteria do not constrain; ``path`` is
``re.search`` rather than ``fullmatch``, so anchor explicitly when you mean it.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from re import Pattern
from typing import Any

from ..errors import RuleValidationError
from .models import NormalizedRequest, NormalizedResponse

#: Response-side criteria. Using one on a request-phase action is a load-time
#: error rather than a rule that silently never matches.
RESPONSE_ONLY = frozenset({"status", "content_type"})

KNOWN_CRITERIA = frozenset(
    {"host", "path", "method", "dest", "query", "request_headers", "status", "content_type"}
)


def _compile_regex(pattern: str, *, field_name: str, module: str, index: int) -> Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleValidationError(
            f"invalid regular expression for {field_name}: {exc}",
            module=module,
            rule_index=index,
            field=field_name,
        ) from exc


def _parse_status(spec: Any, *, module: str, index: int) -> tuple[tuple[int, int], ...]:
    """``200``, ``"300-399"``, or a list of either -> inclusive ranges."""
    items = spec if isinstance(spec, list) else [spec]
    ranges: list[tuple[int, int]] = []
    for item in items:
        if isinstance(item, int):
            ranges.append((item, item))
            continue
        text = str(item).strip()
        if "-" in text:
            low, _, high = text.partition("-")
            try:
                ranges.append((int(low), int(high)))
            except ValueError as exc:
                raise RuleValidationError(
                    f"invalid status range {text!r}",
                    module=module,
                    rule_index=index,
                    field="status",
                ) from exc
        else:
            try:
                value = int(text)
            except ValueError as exc:
                raise RuleValidationError(
                    f"invalid status {text!r}", module=module, rule_index=index, field="status"
                ) from exc
            ranges.append((value, value))
    return tuple(ranges)


def _as_set(spec: Any) -> frozenset[str] | None:
    if spec is None:
        return None
    items = spec if isinstance(spec, list) else [spec]
    return frozenset(str(i).upper() for i in items) or None


@dataclass(frozen=True, slots=True)
class CompiledMatcher:
    """A rule's match criteria, pre-compiled."""

    host_glob: str | None = None
    path_re: Pattern[str] | None = None
    methods: frozenset[str] | None = None
    dests: frozenset[str] | None = None
    query_res: tuple[tuple[str, Pattern[str]], ...] = ()
    request_header_res: tuple[tuple[str, Pattern[str] | None], ...] = ()
    statuses: tuple[tuple[int, int], ...] | None = None
    content_type_re: Pattern[str] | None = None
    #: True when nothing constrains, which matches every flow. Worth knowing
    #: explicitly: an accidentally empty match block is a rule that fires on
    #: everything, and the UI should be able to say so.
    matches_everything: bool = field(default=False)

    # -- request side ----------------------------------------------------

    def matches_request(self, request: NormalizedRequest) -> bool:
        """Cheapest criteria first — see PRF-002 in the module docstring."""
        if self.methods is not None and request.method not in self.methods:
            return False

        if self.dests is not None:
            dest = request.dest
            if dest is None or dest.upper() not in self.dests:
                return False

        if self.host_glob is not None and not fnmatch.fnmatchcase(
            request.host.lower(), self.host_glob
        ):
            return False

        if self.path_re is not None and not self.path_re.search(request.path):
            return False

        for name, pattern in self.query_res:
            value = request.query_param(name)
            if value is None or not pattern.search(value):
                return False

        for name, header_pattern in self.request_header_res:
            value = request.header(name)
            if value is None:
                return False
            if header_pattern is not None and not header_pattern.search(value):
                return False

        return True

    # -- response side ---------------------------------------------------

    def matches_response(self, request: NormalizedRequest, response: NormalizedResponse) -> bool:
        if not self.matches_request(request):
            return False

        if self.statuses is not None and not any(
            low <= response.status <= high for low, high in self.statuses
        ):
            return False

        if self.content_type_re is not None:
            content_type = response.content_type
            if content_type is None or not self.content_type_re.search(content_type):
                return False

        return True

    @property
    def is_response_side(self) -> bool:
        return self.statuses is not None or self.content_type_re is not None


def compile_matcher(
    match: dict[str, Any] | None,
    *,
    module: str = "",
    index: int = 0,
    request_phase: bool = False,
) -> CompiledMatcher:
    """Compile a rule's ``match`` block.

    ``request_phase`` marks actions that run before a response exists. Using a
    response-only criterion there is a load-time error: the rule could never
    fire, and a rule that silently never fires is worse than one that fails
    loudly (REQ MOD-011).
    """
    spec = match or {}

    unknown = set(spec) - KNOWN_CRITERIA
    if unknown:
        raise RuleValidationError(
            f"unknown match criteria: {', '.join(sorted(unknown))}",
            module=module,
            rule_index=index,
            field=next(iter(sorted(unknown))),
        )

    if request_phase:
        misplaced = set(spec) & RESPONSE_ONLY
        if misplaced:
            raise RuleValidationError(
                f"{', '.join(sorted(misplaced))} can only match on the response side; "
                "this action runs before the response exists",
                module=module,
                rule_index=index,
                field=next(iter(sorted(misplaced))),
            )

    query_res: list[tuple[str, Pattern[str]]] = []
    for key, pattern in (spec.get("query") or {}).items():
        query_res.append(
            (
                str(key),
                _compile_regex(str(pattern), field_name=f"query.{key}", module=module, index=index),
            )
        )

    header_res: list[tuple[str, Pattern[str] | None]] = []
    for key, pattern in (spec.get("request_headers") or {}).items():
        compiled = (
            None
            if pattern is None
            else _compile_regex(
                str(pattern), field_name=f"request_headers.{key}", module=module, index=index
            )
        )
        header_res.append((str(key).lower(), compiled))

    host = spec.get("host")
    matcher = CompiledMatcher(
        host_glob=str(host).lower() if host is not None else None,
        path_re=(
            _compile_regex(str(spec["path"]), field_name="path", module=module, index=index)
            if spec.get("path") is not None
            else None
        ),
        methods=_as_set(spec.get("method")),
        dests=_as_set(spec.get("dest")),
        query_res=tuple(query_res),
        request_header_res=tuple(header_res),
        statuses=(
            _parse_status(spec["status"], module=module, index=index)
            if spec.get("status") is not None
            else None
        ),
        content_type_re=(
            _compile_regex(
                str(spec["content_type"]), field_name="content_type", module=module, index=index
            )
            if spec.get("content_type") is not None
            else None
        ),
        matches_everything=not spec,
    )
    return matcher
