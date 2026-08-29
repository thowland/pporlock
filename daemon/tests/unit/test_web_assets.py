"""Locating the built web UI, and explaining it when we cannot.

REQ DOC-001, REQ WUI-001. `web_assets_dir()` decides whether the UI is served
at all, and had no test. The gap showed up in the field: a user ran `make web`,
the build landed correctly in `web/dist`, and the daemon still reported "not
built — run `make web`".

The cause was a non-editable `uv tool install`, which copies the package into
its own venv. From there nothing above the package is the repo, so the built
assets are unreachable and rebuilding them changes nothing. The message named
the one command that was not the problem, which is the part worth testing: a
wrong diagnostic costs more than a missing one, because it is followed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pporlock.cli import runner


def test_prefers_assets_packaged_inside_the_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel that ships the UI wins over any repo lookup.

    This is the distribution path, and it must not depend on a checkout being
    anywhere nearby.
    """
    packaged = tmp_path / "pporlock" / "web"
    packaged.mkdir(parents=True)
    monkeypatch.setattr(runner, "__file__", str(tmp_path / "pporlock" / "cli" / "runner.py"))

    assert runner.web_assets_dir() == packaged


def test_falls_back_to_the_repo_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source checkout serves straight out of `web/dist` (REQ WUI-001).

    `uv run` and an editable install both keep `__file__` inside the checkout,
    so this is the path that covers ordinary development.
    """
    repo = tmp_path / "repo"
    (repo / "web" / "dist").mkdir(parents=True)
    monkeypatch.setattr(
        runner, "__file__", str(repo / "daemon" / "src" / "pporlock" / "cli" / "runner.py")
    )

    assert runner.web_assets_dir() == repo / "web" / "dist"


def test_returns_none_rather_than_refusing_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No UI is not a fatal condition — the proxy is useful without it."""
    monkeypatch.setattr(
        runner, "__file__", str(tmp_path / "daemon" / "src" / "pporlock" / "cli" / "runner.py")
    )

    assert runner.web_assets_dir() is None


def test_hint_says_build_it_when_the_checkout_is_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a checkout with no build, `make web` really is the fix."""
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text("{}")
    monkeypatch.setattr(
        runner, "__file__", str(repo / "daemon" / "src" / "pporlock" / "cli" / "runner.py")
    )

    assert "make web" in runner.web_assets_hint()


def test_hint_does_not_say_build_it_when_the_repo_is_out_of_reach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug, pinned.

    Installed non-editable, there is no `web/package.json` above the package, so
    no amount of `make web` will help. The hint must say so and name the fix —
    and must NOT say "run `make web`", which is what sent the reporter in a
    circle.
    """
    monkeypatch.setattr(
        runner,
        "__file__",
        str(tmp_path / "tools" / "pporlock" / "lib" / "python3.12" / "pporlock" / "cli" / "r.py"),
    )

    hint = runner.web_assets_hint()

    assert "make web" not in hint
    assert "--editable" in hint
