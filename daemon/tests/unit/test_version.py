"""One version, and everything agrees with it — OI-25, REQ DOC-002.

The version was declared independently in nine places and checked in none. It
said 0.1.0 from Sprint 0 through eighteen sprints and a dozen fixes, because
nothing made it move. A version that never changes cannot answer the one
question it exists for: is the thing I am running the thing I just built?

These tests are about the *mechanism*, not the number. A single source only
stays single if something fails when it stops being single.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(REPO / "scripts"))
from version import SEMVER, bump, check, numeric_core, read_version, sync  # noqa: E402


def test_the_declared_version_is_valid_semver() -> None:
    assert SEMVER.match(read_version()), "VERSION must be semver"


def test_every_file_agrees_with_VERSION() -> None:
    """The gate's assertion, as a test.

    `make version-check` runs this in CI; having it here too means a developer
    who edits one pyproject by hand learns immediately rather than at merge.
    """
    assert check(read_version()) == [], "run `make version-sync`"


def test_the_daemon_reports_the_declared_version() -> None:
    """REQ API-002. The number on the wire is the number in the file.

    `/state` and `pporlock version` both read this. If it drifts, every
    diagnosis that starts with "what version are you on" starts with a lie.
    """
    from pporlock.version import VERSION

    assert VERSION == read_version()


def test_a_drifted_file_is_detected() -> None:
    """The guard, watched failing.

    A check that has only ever been run against a correct tree proves nothing.
    """
    problems = check("99.99.99")

    assert problems, "check() must notice when the tree disagrees with VERSION"
    assert any("pyproject.toml" in p for p in problems)


@pytest.mark.parametrize(
    ("part", "before", "after"),
    [
        # The user's stated policy: a significant change bumps the minor, a
        # bundle of small ones bumps the patch.
        ("minor", "0.2.3", "0.3.0"),
        ("patch", "0.2.3", "0.2.4"),
        ("major", "0.2.3", "1.0.0"),
        # A bump resets the parts below it — 0.2.9 -> 0.3.0, not 0.3.9.
        ("minor", "0.2.9", "0.3.0"),
        # And drops a prerelease rather than carrying it forward, which would
        # make every subsequent release a prerelease.
        ("patch", "0.3.0-rc.1", "0.3.1"),
    ],
)
def test_bump_follows_semver(part: str, before: str, after: str) -> None:
    assert bump(part, before) == after


def test_the_chrome_manifest_gets_a_version_it_can_store() -> None:
    """A manifest `version` must be dotted integers.

    `0.3.0-rc.1` is not a legal manifest version and Chrome refuses to load an
    extension carrying one — a failure that appears at install time, far from
    the change that caused it. The prerelease belongs in `version_name`.
    """
    assert numeric_core("0.3.0-rc.1") == "0.3.0"
    assert numeric_core("1.2.3+build.5") == "1.2.3"

    manifest = (REPO / "extension" / "src" / "manifest.config.ts").read_text()
    declared = re.search(r"^  version: '([^']*)',$", manifest, re.MULTILINE)
    assert declared is not None
    assert re.fullmatch(r"\d+(\.\d+){0,3}", declared.group(1)), (
        "the Chrome manifest version must be dotted integers only"
    )


def test_the_manifest_keeps_the_full_semver_in_version_name() -> None:
    manifest = (REPO / "extension" / "src" / "manifest.config.ts").read_text()
    named = re.search(r"^  version_name: '([^']*)',$", manifest, re.MULTILINE)

    assert named is not None
    assert named.group(1) == read_version()


def test_sync_is_idempotent() -> None:
    """Running it twice must not report a second change.

    A sync that always claims to have edited something makes `version-check`
    useless as a signal and turns every build into a dirty tree.
    """
    # Called directly rather than through a subprocess: the tree is already in
    # sync, so this must report no changes.
    assert sync(read_version()) == []


def test_the_javascript_packages_carry_the_same_version() -> None:
    """The extension and web UI are installed separately from the daemon, so
    their versions are the ones a user can most easily get out of step."""
    want = read_version()
    for relative in ("web/package.json", "extension/package.json"):
        data = json.loads((REPO / relative).read_text())
        assert data["version"] == want, f"{relative} disagrees"
