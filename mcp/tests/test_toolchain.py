"""Toolchain invariants for the MCP server. Sprint 0."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_running_python_312() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_mcp_and_httpx_are_importable() -> None:
    """The MCP server is a plain HTTP client of the control API (SPEC-1 §11.1)."""
    import httpx
    import mcp

    assert mcp is not None
    assert httpx is not None


def test_does_not_depend_on_the_daemon_package() -> None:
    """SPEC-1 §11.1: the MCP server imports nothing from the daemon package.

    It reaches the daemon over HTTP like any other client. An import here would
    couple two independently-versioned processes and break the read-only mode
    boundary (REQ MCP-032).
    """
    deps = _pyproject()["project"]["dependencies"]
    assert not any(d.startswith("pporlock") and "mcp" not in d for d in deps), deps


def test_coverage_gate_is_configured() -> None:
    assert _pyproject()["tool"]["coverage"]["report"]["fail_under"] == 80
