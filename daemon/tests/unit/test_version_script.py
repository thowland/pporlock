"""The version and copyright propagation — OI-25, OI-38.

`scripts/version.py` had no tests. It is not shipped code, which is the usual
reason a script escapes them, but it is the thing that decides what version the
running system reports — the first question of every diagnosis — and now also
what the product claims about its own copyright. Both are statements the build
makes on the project's behalf, in files nobody re-reads.

What is worth pinning is the *policy*, not the substitution:

  * the year advances on its own, and is a range once the project outlives its
    first year
  * `check` does not fail merely because a new year began on an untouched tree,
    because a check that cries wolf every January is a check people route around
  * `check` does fail when the three notices disagree — which is the state the
    repository was actually in, with two files saying 2025 and the README 2026
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    """Import `scripts/version.py`, which is not on any package path."""
    spec = importlib.util.spec_from_file_location(
        "pporlock_version", REPO / "scripts" / "version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


version_script = _load()


class TestCopyrightYears:
    def test_is_a_single_year_during_the_first_one(self) -> None:
        years = version_script.copyright_years(datetime.date(2026, 6, 1))
        assert years == "2026"

    def test_becomes_a_range_in_a_later_year(self) -> None:
        # First publication through last modification, which is what the notice
        # is actually claiming.
        assert version_script.copyright_years(datetime.date(2031, 1, 2)) == "2026-2031"

    def test_never_predates_the_first_commit(self) -> None:
        # A clock set wrong, or a build in a container with no NTP, must not be
        # able to make the project claim a year before it existed.
        assert version_script.copyright_years(datetime.date(2019, 5, 5)) == "2026"

    def test_uses_the_current_year_rather_than_the_last_commit(self) -> None:
        # Deliberate: sync always runs immediately before a commit (`make
        # bump-*` calls it, and a bump is required on every branch), so "now" is
        # the year of the modification being made. The last commit's year is the
        # *previous* modification and would be stale every January. It also
        # keeps this script independent of git, which the gate needs.
        assert version_script.copyright_years() == version_script.copyright_years(
            datetime.date.today()
        )


class TestCopyrightCheck:
    def test_the_repository_agrees_with_itself(self) -> None:
        assert version_script.check_copyright() == []

    def test_an_untouched_tree_does_not_fail_when_a_new_year_begins(self) -> None:
        # The repository says 2026. Asked in 2099, this must still pass: a
        # project unmodified since 2026 correctly says 2026 for ever, and a
        # check that failed every January would teach people to skip it.
        assert version_script.check_copyright(datetime.date(2099, 1, 1)) == []

    def test_a_year_that_has_not_happened_is_refused(self) -> None:
        # Asked in 2020, the repository's 2026 is a claim about the future.
        problems = version_script.check_copyright(datetime.date(2020, 1, 1))
        assert any("future" in problem for problem in problems)

    def test_disagreement_between_files_is_reported_with_both_sides(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The state the repository was in: two constants at 2025 and a README at
        # 2026, with nothing looking at any of them.
        one = tmp_path / "one.ts"
        two = tmp_path / "two.md"
        one.write_text("export const COPYRIGHT = '© 2025 Tim Howland';\n")
        two.write_text("Copyright © 2026 Tim Howland.\n")
        monkeypatch.setattr(version_script, "REPO", tmp_path)
        monkeypatch.setattr(version_script, "COPYRIGHT_FILES", (one, two))

        problems = version_script.check_copyright(datetime.date(2026, 6, 1))
        assert any("2025" in problem and "one.ts" in problem for problem in problems)
        assert any("2026" in problem and "two.md" in problem for problem in problems)

    def test_a_missing_notice_is_reported_rather_than_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "gone.md"
        empty.write_text("no notice here\n")
        monkeypatch.setattr(version_script, "REPO", tmp_path)
        monkeypatch.setattr(version_script, "COPYRIGHT_FILES", (empty,))
        assert version_script.check_copyright() == ["gone.md: no copyright notice found"]


class TestCopyrightSync:
    def test_rewrites_every_notice(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        one = tmp_path / "one.ts"
        two = tmp_path / "two.md"
        one.write_text("export const COPYRIGHT = '© 2025 Tim Howland';\n")
        two.write_text("Copyright © 2026 Tim Howland.\n")
        monkeypatch.setattr(version_script, "REPO", tmp_path)
        monkeypatch.setattr(version_script, "COPYRIGHT_FILES", (one, two))

        changed = version_script.sync_copyright("2026-2027")
        assert set(changed) == {one, two}
        assert "© 2026-2027 Tim Howland" in one.read_text()
        assert "© 2026-2027 Tim Howland" in two.read_text()
        # Everything around the notice is left alone.
        assert one.read_text().startswith("export const COPYRIGHT = ")

    def test_reports_no_change_when_already_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        only = tmp_path / "one.ts"
        only.write_text("© 2026 Tim Howland\n")
        monkeypatch.setattr(version_script, "REPO", tmp_path)
        monkeypatch.setattr(version_script, "COPYRIGHT_FILES", (only,))
        assert version_script.sync_copyright("2026") == []

    def test_refuses_a_file_whose_notice_has_moved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A sync that silently skips a file it can no longer find is how the
        # drift this exists to prevent comes back — the same rule `_sub_once`
        # already applies to the version itself.
        moved = tmp_path / "one.ts"
        moved.write_text("export const COPYRIGHT = '(c) 2026 Tim Howland';\n")
        monkeypatch.setattr(version_script, "REPO", tmp_path)
        monkeypatch.setattr(version_script, "COPYRIGHT_FILES", (moved,))
        with pytest.raises(SystemExit, match="expected one copyright notice"):
            version_script.sync_copyright("2026")


def test_the_gate_actually_runs_version_check() -> None:
    """OI-38.

    The Makefile comment said `version-check` failed the gate on drift, and
    CLAUDE.md repeated it. `gate` depended on `lint test coverage security` and
    nothing else, so the one mechanism keeping nine version declarations honest
    was never invoked by the thing that was supposed to invoke it. A guard
    nothing runs is a comment.
    """
    makefile = (REPO / "Makefile").read_text()
    gate = next(line for line in makefile.splitlines() if line.startswith("gate:"))
    assert "version-check" in gate, gate
