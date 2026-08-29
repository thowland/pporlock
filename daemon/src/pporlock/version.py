"""The daemon's version, derived rather than declared — OI-25.

This was a literal in `cli/main.py` and said 0.1.0 through eighteen sprints,
because nothing made it move and nothing checked. It lives here, below both the
CLI and the control server, so neither has to import the other to know it.

The value comes from the installed distribution's metadata, which packaging
generates from `daemon/pyproject.toml`, which `scripts/version.py` generates
from the repository's `VERSION`. One fact, one direction of travel: the number
reported at runtime is the version of the package that is actually installed,
not a string someone remembered to edit.
"""

from __future__ import annotations

from pathlib import Path


def resolve_version() -> str:
    try:
        from importlib.metadata import version as _dist_version

        return _dist_version("pporlock")
    except Exception:
        # A source tree that was never installed — a test runner, or someone
        # reading the module directly. Fall back to the file the packaging
        # metadata is itself generated from.
        try:
            return (Path(__file__).resolve().parents[3] / "VERSION").read_text().strip()
        except OSError:
            # Better an obviously wrong version than a plausible stale one.
            return "0.0.0+unknown"


VERSION = resolve_version()
