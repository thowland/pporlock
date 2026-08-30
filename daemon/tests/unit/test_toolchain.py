"""Toolchain invariants. Sprint 0.

These are not product tests. They assert that the environment the later gates
depend on is actually the environment we specified.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_running_python_312() -> None:
    """The daemon targets Python 3.12 exactly (SPEC-0 §1.2)."""
    assert sys.version_info[:2] == (3, 12)


def test_mitmproxy_is_pinned_exactly() -> None:
    """REQ PXY-006: the mitmproxy version is pinned exactly, never floated.

    An upgrade is deliberate work against the addon/ adapter layer and is gated
    on the integration suite (REQ TST-007). A '>=' or '~=' here would let a
    transitive resolution silently change the hook API underneath us.
    """
    deps = _pyproject()["project"]["dependencies"]
    pins = [d for d in deps if d.replace(" ", "").startswith("mitmproxy")]
    assert len(pins) == 1, f"expected exactly one mitmproxy dependency, got {pins}"
    assert "==" in pins[0], f"mitmproxy must be pinned with '==', got {pins[0]!r}"


def test_installed_mitmproxy_matches_the_pin() -> None:
    """The resolved environment matches the declared pin."""
    from mitmproxy import version

    deps = _pyproject()["project"]["dependencies"]
    pin = next(d for d in deps if d.replace(" ", "").startswith("mitmproxy"))
    expected = pin.split("==", 1)[1].strip()
    assert version.VERSION == expected


def test_coverage_gate_is_configured() -> None:
    """Gate G2: the daemon coverage floor is declared in config, not just in docs."""
    assert _pyproject()["tool"]["coverage"]["report"]["fail_under"] == 80


def test_engine_is_type_checked_strictly() -> None:
    """SPEC-1 §2.2: engine/ is the pure, load-bearing core and is mypy --strict."""
    overrides = _pyproject()["tool"]["mypy"]["overrides"]
    engine = [o for o in overrides if "pporlock.engine.*" in o.get("module", "")]
    assert engine, "engine/ must have a mypy override"
    assert engine[0].get("strict") is True


def _git(*args: str) -> str | None:
    """Run git in the repository, or None when there is no repository to ask.

    An installed wheel has no `.git`, and a test that failed there would be
    asserting something about the developer's checkout from inside the shipped
    artefact.
    """
    import subprocess

    # The daemon directory, so `git ls-files src/pporlock` returns paths
    # relative to it and they compare directly against what is on disk.
    root = Path(__file__).resolve().parents[2]
    try:
        # S603/S607: a literal argv, no shell, and `git` from PATH is the same
        # git that cloned this. An absolute path would break on every machine
        # that installs it somewhere else.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):  # pragma: no cover - git absent
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def test_every_file_the_package_needs_is_actually_in_the_repository() -> None:
    """The shipped tree and the committed tree are the same tree.

    **This is not hypothetical.** `src/pporlock/data/exclusions-default.yaml` —
    the 33 default ClientHello exclusions (REQ PXY-013) — was missing from the
    repository from the first commit until 0.9.0, because a *global* gitignore
    on the author's machine ignored any directory named `data`. Locally
    everything worked and `git status` was clean. Every clone shipped a proxy
    with no exclusions at all, which would decrypt OS update endpoints,
    certificate revocation, and banking hosts — the precise traffic that list
    exists to leave alone.

    A unit test cannot see that, because the file is right there on the machine
    running it. Only something that asks *git* what it has can, which is what
    this does: every file under `src/pporlock` is tracked, or this fails and
    names it.
    """
    tracked = _git("ls-files", "src/pporlock")
    if tracked is None:
        import pytest

        pytest.skip("not a git checkout")

    root = Path(__file__).resolve().parents[2]
    committed = {line.strip() for line in tracked.splitlines() if line.strip()}
    on_disk = {
        path.relative_to(root).as_posix()
        for path in (root / "src" / "pporlock").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.name.endswith((".pyc", ".pyo"))
    }

    missing = sorted(on_disk - committed)
    assert not missing, (
        "these files are on this machine but not in the repository, so a clone "
        f"would not have them: {missing}"
    )
