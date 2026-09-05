"""The transform registry — SPEC-1 §4.6, SPEC-0 §5.5, REQ MOD-013/014.

Transforms are named registry entries with validated parameters, never
expressions embedded in YAML. That keeps the configuration declarative and keeps
anything requiring real logic in Python where it can be tested — and it means a
malformed transform is a load-time error rather than a runtime surprise.

Every transform is a pure function of (text, context) -> text. It returns the
input unchanged when it has nothing to do, so "no change" is expressible without
raising, and the evaluator can record ``no_change`` honestly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...errors import RuleValidationError, TransformError
from ..cost import Cost

#: A transform receives the decoded body and a context, and returns a new body.
TransformFn = Callable[[str, "TransformContext"], str]


@dataclass(slots=True)
class TransformContext:
    """What a transform may know about the flow it is changing.

    Deliberately small. A transform that needed more than this would be doing
    something the rule model should express instead.
    """

    url: str = ""
    content_type: str | None = None
    #: Response headers, so a transform can read the page's own CSP.
    headers: tuple[tuple[str, str], ...] = ()
    #: Filled in by a transform to report something the flow should carry —
    #: an SRI strip, a CSP relaxation, a script injection (SPEC-0 §4.4).
    notes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def note(self, code: str, message: str, **detail: Any) -> None:
        self.notes.append((code, message, detail))

    def drain(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Take the accumulated notes and clear them.

        Draining per transform rather than once at the end is what lets the
        evaluator attribute each note to the rule that produced it. One context
        is shared across every rule in the body phase, so a single drain at the
        end knows only that *something* stripped SRI — which is the question the
        UI's "which module weakened this page" panel exists to answer.
        """
        taken = self.notes
        self.notes = []
        return taken

    def header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers:
            if key.lower() == target:
                return value
        return None


@dataclass(frozen=True, slots=True)
class TransformSpec:
    """A registered transform: what it is called, what it does, what it costs."""

    name: str
    fn: TransformFn
    cost: Cost
    #: Required parameter names, checked at load time (REQ MOD-014).
    required: tuple[str, ...] = ()
    #: At least one of these must be present, when the transform needs a choice.
    one_of: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    #: An extra load-time check for parameters whose validity is more than
    #: presence — a regex that must compile, say. Raising `TransformError` here
    #: is what turns a per-flow runtime failure into a named load error.
    check: Callable[[dict[str, Any]], None] | None = None

    def validate(self, params: dict[str, Any], *, module: str = "", index: int = 0) -> None:
        missing = [name for name in self.required if params.get(name) in (None, "")]
        if missing:
            raise RuleValidationError(
                f"{self.name} requires {', '.join(missing)}",
                module=module,
                rule_index=index,
                field=missing[0],
            )

        if self.one_of and not any(params.get(name) not in (None, "") for name in self.one_of):
            raise RuleValidationError(
                f"{self.name} requires one of {', '.join(self.one_of)}",
                module=module,
                rule_index=index,
                field=self.one_of[0],
            )

        known = {"kind", *self.required, *self.one_of, *self.optional}
        unknown = set(params) - known
        if unknown:
            raise RuleValidationError(
                f"{self.name} does not take {', '.join(sorted(unknown))}",
                module=module,
                rule_index=index,
                field=next(iter(sorted(unknown))),
            )

        if self.check is not None:
            try:
                self.check(params)
            except TransformError as exc:
                raise RuleValidationError(
                    f"{self.name}: {exc.message}",
                    module=module,
                    rule_index=index,
                    field="kind",
                ) from exc


class TransformRegistry:
    """Named transforms. One per process, extended by modules in Sprint 11."""

    __slots__ = ("_specs",)

    def __init__(self) -> None:
        self._specs: dict[str, TransformSpec] = {}

    def register(self, spec: TransformSpec) -> None:
        self._specs[spec.name] = spec

    def copy(self) -> TransformRegistry:
        """An independent registry holding the same transforms.

        The dry runner loads candidate modules for real, and ``on_load`` may
        register a transform. Handing it the live registry would let a module
        that is only being *considered* extend the set the running proxy
        evaluates against (REQ CAP-031 asks for the same code path, not the same
        mutable state).
        """
        clone = TransformRegistry()
        clone._specs = dict(self._specs)
        return clone

    def get(self, name: str) -> TransformSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise RuleValidationError(
                f"unknown transform {name!r}; available: {', '.join(self.names) or 'none'}",
                field="kind",
            )
        return spec

    def has(self, name: str) -> bool:
        return name in self._specs

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def validate(self, params: dict[str, Any], *, module: str = "", index: int = 0) -> None:
        """Validate one transform block at load time."""
        kind = str(params.get("kind") or "")
        if not kind:
            raise RuleValidationError(
                "transform requires a 'kind'", module=module, rule_index=index, field="kind"
            )
        self.get(kind).validate(params, module=module, index=index)

    def apply(self, params: dict[str, Any], text: str, context: TransformContext) -> str:
        """Run one transform. Raises TransformError on anything unexpected."""
        spec = self.get(str(params.get("kind") or ""))
        try:
            return spec.fn(text, _Params(params, context))  # type: ignore[arg-type]
        except TransformError:
            raise
        except Exception as exc:
            raise TransformError(f"{spec.name} failed: {exc}", transform=spec.name) from exc


class _Params:
    """Binds a parameter block to a context, so transform functions take two args."""

    __slots__ = ("context", "params")

    def __init__(self, params: dict[str, Any], context: TransformContext) -> None:
        self.params = params
        self.context = context

    def get(self, name: str, default: Any = None) -> Any:
        return self.params.get(name, default)

    def note(self, code: str, message: str, **detail: Any) -> None:
        self.context.note(code, message, **detail)

    def header(self, name: str) -> str | None:
        return self.context.header(name)

    @property
    def url(self) -> str:
        return self.context.url


def build_registry() -> TransformRegistry:
    """The built-in registry (SPEC-0 §5.5)."""
    from .html import (
        inject_script,
        inject_style,
        strip_integrity_attributes,
    )
    from .json_ops import json_patch
    from .text import check_regex_sub, regex_sub, replace_literal

    registry = TransformRegistry()
    registry.register(
        TransformSpec("strip_integrity_attributes", strip_integrity_attributes, Cost.EXPENSIVE)
    )
    registry.register(
        TransformSpec(
            "inject_script",
            inject_script,
            Cost.EXPENSIVE,
            one_of=("src", "inline"),
            optional=("position", "reuse_nonce"),
        )
    )
    registry.register(
        TransformSpec(
            "inject_style",
            inject_style,
            Cost.EXPENSIVE,
            one_of=("href", "inline"),
            optional=("position",),
        )
    )
    registry.register(
        TransformSpec(
            "regex_sub",
            regex_sub,
            Cost.SIZED,
            required=("pattern", "repl"),
            optional=("count", "flags"),
            check=check_regex_sub,
        )
    )
    registry.register(
        TransformSpec(
            "replace_literal",
            replace_literal,
            Cost.SIZED,
            required=("find", "replace"),
            optional=("count",),
        )
    )
    registry.register(TransformSpec("json_patch", json_patch, Cost.SIZED, required=("ops",)))
    return registry
