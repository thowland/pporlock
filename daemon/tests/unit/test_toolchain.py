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
