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


# ---------------------------------------------------------------------------
# `make help` and the job-control recipes.
#
# The help text is the only listing of targets most people will read, and it is
# hand-maintained echo lines — exactly the shape that goes stale silently, which
# is what `make ext` above already proved about prose.


def _help_targets() -> frozenset[str]:
    """Target names named in the `help` recipe's echo lines.

    Only the leading word of each indented description line: those are the
    names, and the prose after them mentions things like `git pull` that are
    not targets.
    """
    text = MAKEFILE.read_text()
    body = text.split("help:", 1)[1].split("\n# ", 1)[0]
    names: set[str] = set()
    for line in body.splitlines():
        match = re.match(r'\s*@echo\s+"  (\S.*?)"$', line)
        if not match:
            # Section headings, blank lines, and the indented continuation of a
            # description all fail this deliberately: only a line whose quote
            # opens with exactly two spaces is a target entry.
            continue
        # Names first, then two-or-more spaces, then prose. A line that is only
        # names ("daemon web extension mcp") has no prose to split off.
        # A "/" separator between alternatives is punctuation, not a target.
        # Anything else target-shaped is one, including a misspelling — which
        # is the point.
        names.update(
            word
            for word in re.split(r"\s{2,}", match.group(1))[0].split()
            if re.fullmatch(r"[a-z][a-z0-9-]*", word)
        )
    return frozenset(names)


def test_every_target_named_in_make_help_exists() -> None:
    """REQ DOC-001, for the listing people actually read.

    `make help` is the front door. A target renamed without touching its echo
    line sends the reader to a command that does not exist, and make's own error
    for that names the target but not the fact that the docs lied.
    """
    missing = sorted(_help_targets() - _declared_targets())
    assert not missing, f"`make help` advertises target(s) that do not exist: {', '.join(missing)}"


def test_the_help_guard_can_see_the_targets_at_all() -> None:
    """The parse above returns something, so the test cannot pass vacuously."""
    found = _help_targets()
    assert {"setup", "gate", "install", "restart"} <= found, (
        f"the help parser found only {sorted(found)} — it has stopped reading the recipe, "
        "so test_every_target_named_in_make_help_exists proves nothing"
    )


def _recipe_lines(target: str) -> list[str]:
    """The recipe lines of one target, with continuations joined."""
    text = MAKEFILE.read_text()
    body = text.split(f"\n{target}:", 1)[1].split("\n", 1)[1]
    lines: list[str] = []
    current = ""
    for line in body.splitlines():
        if not line.startswith("\t"):
            break
        current += line
        if line.endswith("\\"):
            continue
        lines.append(current)
        current = ""
    if current:
        lines.append(current)
    return lines


@pytest.mark.parametrize("target", ["start", "stop", "restart"])
def test_a_delegating_recipe_does_not_fall_through(target: str) -> None:
    """A recipe that `exec`s into launchd must not have lines after it.

    Make runs every recipe line in its own shell, so an `exec` on line one
    replaces *that* shell and make cheerfully runs line two. `restart` shipped
    that way for about ten minutes: on a machine with the launchd agent loaded
    it would delegate the restart and then restart the daemon again itself,
    the second time against launchd's own supervision.

    It is invisible without the agent installed, which is the state of every
    machine this was written on and tested against — the same blind spot as
    lesson 1 in CLAUDE.md, one layer down.
    """
    lines = _recipe_lines(target)
    assert lines, f"no recipe found for `{target}`"
    for index, line in enumerate(lines):
        if "exec pporlock" in line:
            assert index == len(lines) - 1, (
                f"`{target}` execs into launchd on recipe line {index + 1} of {len(lines)}. "
                "Make runs each line in its own shell, so the lines after it still run. "
                "Join them into one recipe line with backslash continuations."
            )
            return
    pytest.fail(f"`{target}` no longer delegates to launchd; this guard needs rewriting")
