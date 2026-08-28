"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DAEMON_ROOT = Path(__file__).resolve().parents[1]
# REPO_ROOT for the shared fixture origin; DAEMON_ROOT so `tests.stubs` imports.
for entry in (str(REPO_ROOT), str(DAEMON_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from testfixtures.origin.server import FixtureServer  # noqa: E402


@pytest.fixture(scope="session")
def fixture_origin() -> Iterator[FixtureServer]:
    """The fixture origin server, on an ephemeral port, for the whole session."""
    server = FixtureServer(port=0).start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# A test must never be able to delete something outside a temp directory.
#
# One did. `test_uninstall_purge_removes_the_state_directory` monkeypatched
# Path.home() and relied on `uninstall` reading it at command time. When
# `--purge` began deleting the *configured* state_dir instead, and because
# Config binds DEFAULT_STATE_DIR at import (docs/open-issues.md OI-10), the test
# became import-order-dependent — and in a full-suite run it deleted the real
# ~/.pporlock.
#
# That specific test is fixed. This exists because the next one will be written
# by someone who does not know the story, and "we were careful" is not a
# control. Recursive deletion outside a temp directory now fails loudly, in the
# test that attempted it, naming the path.


def _is_safe_to_remove(path: object) -> bool:
    """True when `path` is somewhere a test is allowed to destroy."""
    import tempfile

    try:
        resolved = Path(str(path)).resolve()
    except (OSError, ValueError):
        return False

    roots = [Path(tempfile.gettempdir()).resolve()]
    # macOS hands out /var/folders/... which resolves under /private.
    private = Path("/private")
    if private.is_dir():
        roots.append(private)

    for root in roots:
        if resolved == root or root in resolved.parents:
            return True
    return False


@pytest.fixture(autouse=True)
def _refuse_to_delete_outside_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse, so it cannot be forgotten by the test that needs it most."""
    import shutil

    real_rmtree = shutil.rmtree

    def guarded(path: object, *args: object, **kwargs: object) -> object:
        if not _is_safe_to_remove(path):
            raise AssertionError(
                f"a test tried to recursively delete {path!r}, which is not under a "
                f"temp directory. If this is deliberate, the target is wrong — point "
                f"it at tmp_path. See tests/conftest.py."
            )
        return real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", guarded)
