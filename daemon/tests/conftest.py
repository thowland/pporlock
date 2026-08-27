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
