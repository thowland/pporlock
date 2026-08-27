"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures.origin.server import FixtureServer  # noqa: E402


@pytest.fixture(scope="session")
def fixture_origin() -> Iterator[FixtureServer]:
    """The fixture origin server, on an ephemeral port, for the whole session."""
    server = FixtureServer(port=0).start()
    try:
        yield server
    finally:
        server.stop()
