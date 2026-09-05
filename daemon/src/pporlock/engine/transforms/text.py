"""Text transforms — SPEC-0 §5.5."""

from __future__ import annotations

import re
from functools import lru_cache
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


@lru_cache(maxsize=512)
def _compiled(pattern: str, flags: str | None) -> re.Pattern[str]:
    """A rule's compiled pattern, compiled once (REQ PXY-025).

    Match criteria have always been compiled at rule load; transform patterns
    were not, so every matching body re-parsed and re-compiled the same regex —
    on a latency-sensitive path, once per subresource (SEP_5_REVIEW F-08).

    Cached by (pattern, flags) rather than held on the compiled rule because a
    transform block is a plain dict by design: the cache is what gives the
    declarative representation a compiled one without giving it identity. It is
    bounded, so a pathological rule set degrades to the old behaviour rather
    than growing without limit — unlike `re`'s own cache, this is the project's
    promise rather than an interpreter implementation detail.
    """
    try:
        return re.compile(pattern, _compile_flags(flags))
    except re.error as exc:
        raise TransformError(f"invalid pattern: {exc}") from exc


def check_regex_sub(params: dict[str, Any]) -> None:
    """Load-time validation (REQ MOD-014): an unusable pattern is a load error.

    Without this an invalid pattern or an unknown flag became a per-flow runtime
    failure — quarantining a module for a typo the loader could have named.
    """
    _compiled(str(params.get("pattern")), params.get("flags"))


def regex_sub(text: str, params: Any) -> str:
    """Regular-expression substitution over the body."""
    repl = str(params.get("repl"))
    count = int(params.get("count") or 0)
    return _compiled(str(params.get("pattern")), params.get("flags")).sub(repl, text, count=count)


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
