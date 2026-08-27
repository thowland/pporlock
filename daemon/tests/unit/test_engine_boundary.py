"""The engine boundary. SPEC-1 §2.2, REQ DD-2, REQ TST-001.

This test is load-bearing. Do not weaken it.

The rules engine must import nothing from mitmproxy, asyncio, or the control
server. Two things depend on that holding:

1. The entire rules and module system is unit-testable with no proxy process and
   no network. That is what makes ~90% coverage of the engine achievable at all.
2. A mitmproxy upgrade is confined to the addon/ adapter layer. mitmproxy's API
   has shifted across major versions in exactly the areas we lean on hardest —
   streaming control, TLS hooks, option names — and this boundary is what keeps
   that from becoming a rewrite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "pporlock" / "engine"

#: Importing any of these from engine/ breaks one of the two guarantees above.
FORBIDDEN_ROOTS = frozenset({"mitmproxy", "asyncio", "aiohttp", "tornado"})

#: engine/ may not reach into these sibling packages. Dependencies point inward:
#: the adapter and the server know about the engine, never the reverse.
FORBIDDEN_SIBLINGS = frozenset({"addon", "control", "capture", "cli"})


def _engine_modules() -> list[Path]:
    return sorted(ENGINE_ROOT.rglob("*.py"))


def _imported_roots(source: str) -> set[str]:
    """Top-level module names imported by ``source``, absolute imports only."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _relative_targets(source: str) -> set[str]:
    """Sibling package names reached by relative import, e.g. ``from ..control import x``."""
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module:
            targets.add(node.module.split(".")[0])
    return targets


def test_engine_directory_exists() -> None:
    assert ENGINE_ROOT.is_dir(), f"engine package missing at {ENGINE_ROOT}"
    assert _engine_modules(), "engine package contains no modules"


@pytest.mark.parametrize("module_path", _engine_modules(), ids=lambda p: p.name)
def test_engine_module_has_no_forbidden_imports(module_path: Path) -> None:
    """REQ DD-2 / TST-001."""
    source = module_path.read_text()
    offending = _imported_roots(source) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{module_path.relative_to(ENGINE_ROOT.parent)} imports {sorted(offending)}. "
        "The engine must stay pure — see SPEC-1 §2.2."
    )


@pytest.mark.parametrize("module_path", _engine_modules(), ids=lambda p: p.name)
def test_engine_module_does_not_reach_into_siblings(module_path: Path) -> None:
    """Dependencies point inward. The engine knows nothing of the proxy or server."""
    source = module_path.read_text()
    offending = _relative_targets(source) & FORBIDDEN_SIBLINGS
    assert not offending, (
        f"{module_path.relative_to(ENGINE_ROOT.parent)} relative-imports {sorted(offending)}. "
        "The engine is imported by those packages, not the other way round."
    )


def test_the_boundary_check_actually_detects_a_violation(tmp_path: Path) -> None:
    """Guards the guard.

    A boundary test that cannot fail is worse than no test, because it reports
    safety it is not providing. Plant a violation and confirm the detector fires.
    """
    planted = "from mitmproxy import http\n\ndef f() -> None:\n    return http\n"
    assert _imported_roots(planted) & FORBIDDEN_ROOTS == {"mitmproxy"}

    planted_relative = "from ..control.server import ControlServer\n"
    assert _relative_targets(planted_relative) & FORBIDDEN_SIBLINGS == {"control"}

    clean = "from __future__ import annotations\nimport re\nfrom .models import NormalizedRequest\n"
    assert not _imported_roots(clean) & FORBIDDEN_ROOTS
    assert not _relative_targets(clean) & FORBIDDEN_SIBLINGS


def test_engine_is_importable_without_mitmproxy_loaded() -> None:
    """The engine imports cleanly in a process that has never touched mitmproxy."""
    import subprocess
    import sys

    src = ENGINE_ROOT.parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import pporlock.engine.models, pporlock.engine.provenance\n"
            "assert 'mitmproxy' not in sys.modules, sorted(m for m in sys.modules if 'mitm' in m)\n"
            "print('clean')",
        ],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(src), "PATH": ""},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
