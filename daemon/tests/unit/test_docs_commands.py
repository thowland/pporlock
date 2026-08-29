"""Every `make` target the user-facing docs tell you to run must exist.

REQ DOC-001. This exists because `docs/install.md` and `README.md` both told
the reader to run `make ext` for the entire life of the project. There is no
such target and there never was — the extension builds with `make extension`.
Nothing caught it: the docs are prose, the Makefile is not imported by any
test, and every developer who built the extension already knew the real name.

It is the same shape as the four bugs in CLAUDE.md's "learned the expensive
way" list. A green suite says nothing about whether the documented path works,
because no test walks it. This walks the cheapest, most mechanical part of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MAKEFILE = REPO_ROOT / "Makefile"

# The docs a new user is told to follow literally. A stale command here is a
# failed install, not a typo.
USER_FACING_DOCS = (
    "README.md",
    "docs/install.md",
    "docs/troubleshooting.md",
    "docs/module-authoring.md",
    "docs/worked-example.md",
    "docs/llm-with-mcp.md",
)

# `make <target>` as a command, i.e. inside a fenced block or inline backticks.
# Matching raw prose instead would flag "make it impossible" and "make sure";
# a command is only a command where the doc formats it as one.
_MAKE_CALL = re.compile(r"\bmake\s+([a-z][a-z0-9-]*)\b")
_FENCED = re.compile(r"```.*?\n(.*?)```", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")


def _code_spans(text: str) -> list[str]:
    """Every fenced block body and inline code span in a Markdown document."""
    return _FENCED.findall(text) + _INLINE.findall(text)


def _declared_targets() -> frozenset[str]:
    """Target names declared at the start of a line in the Makefile."""
    pattern = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:(?!=)", re.MULTILINE)
    return frozenset(pattern.findall(MAKEFILE.read_text()))


@pytest.mark.parametrize("relative_path", USER_FACING_DOCS)
def test_documented_make_targets_exist(relative_path: str) -> None:
    """REQ DOC-001: a documented `make <target>` must be a real target.

    Catches both a target that was renamed out from under the docs and one that
    was never spelled correctly in the first place.
    """
    doc = REPO_ROOT / relative_path
    assert doc.is_file(), f"{relative_path} is referenced by the test suite but missing"

    declared = _declared_targets()
    referenced = {
        target for span in _code_spans(doc.read_text()) for target in _MAKE_CALL.findall(span)
    }

    missing = sorted(referenced - declared)
    assert not missing, (
        f"{relative_path} documents make target(s) that do not exist: "
        f"{', '.join(missing)}. Declared targets: {', '.join(sorted(declared))}"
    )


def test_the_guard_can_see_a_missing_target() -> None:
    """The guard fails on a bad target — not just on an empty set of matches.

    A guard nobody has watched fail is not a guard. `make ext` is the exact
    string that shipped in both install.md and README.md, so it is what this
    asserts against.
    """
    declared = _declared_targets()
    assert "extension" in declared, "the real target name changed; update the docs guard"
    assert "ext" not in declared, (
        "a target named `ext` now exists, so this guard no longer proves anything. "
        "Either the docs were fixed the wrong way, or this test needs a new sentinel."
    )
