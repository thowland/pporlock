"""Text transforms — SPEC-0 §5.5."""

from __future__ import annotations

import re
from typing import Any

from ...errors import TransformError

#: Regex flags a rule may ask for, by name. An allowlist rather than eval of a
#: flag expression: rules are trusted, but a typo should fail loudly at load
#: rather than silently changing what a pattern matches.
_FLAGS = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}


def _compile_flags(spec: str | None) -> int:
    flags = 0
    for char in spec or "":
        if char not in _FLAGS:
            raise TransformError(f"unknown regex flag {char!r}; valid flags are ims x")
        flags |= _FLAGS[char]
    return flags


def regex_sub(text: str, params: Any) -> str:
    """Regular-expression substitution over the body."""
    pattern = str(params.get("pattern"))
    repl = str(params.get("repl"))
    count = int(params.get("count") or 0)
    try:
        compiled = re.compile(pattern, _compile_flags(params.get("flags")))
    except re.error as exc:
        raise TransformError(f"invalid pattern: {exc}") from exc
    return compiled.sub(repl, text, count=count)


def replace_literal(text: str, params: Any) -> str:
    """Literal substring replacement.

    Distinct from regex_sub rather than a special case of it: escaping a literal
    into a pattern is exactly the step people get wrong, and getting it wrong
    silently changes what is matched.
    """
    find = str(params.get("find"))
    replace = str(params.get("replace"))
    count = int(params.get("count") or 0)
    return text.replace(find, replace, count if count > 0 else -1)
