"""File-descriptor headroom. OI-36.

The bug these describe: the daemon accepted whatever descriptor limit it was
launched with. macOS gives a launchd agent 256, an interception proxy holds two
per flow, and at the ceiling everything that opens a file fails in the
vocabulary of whatever tried to open it. It was found as
``sqlite3.OperationalError: unable to open database file`` from a session
export, on a file that was present and readable — SQLite's message for EMFILE.
"""

from __future__ import annotations

import resource
from typing import Any

import pytest

from pporlock import limits
from pporlock.limits import (
    DESIRED_NOFILE,
    DescriptorUsage,
    FileLimit,
    open_descriptors,
    raise_file_limit,
    sample,
)


class TestRaiseFileLimit:
    """`raise_file_limit` runs before the proxy opens anything (REQ PXY-005)."""

    def test_raises_a_launchd_sized_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The case that mattered: 256 is what launchd hands an agent."""
        state = {"soft": 256, "hard": resource.RLIM_INFINITY}

        def fake_get(_: int) -> tuple[int, int]:
            return state["soft"], state["hard"]

        def fake_set(_: int, values: tuple[int, int]) -> None:
            state["soft"] = values[0]

        monkeypatch.setattr(resource, "getrlimit", fake_get)
        monkeypatch.setattr(resource, "setrlimit", fake_set)

        result = raise_file_limit()

        assert result.raised
        assert result.soft == DESIRED_NOFILE
        assert result.was == 256

    def test_leaves_an_already_generous_limit_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A shell-launched daemon may already have far more than we ask for,
        and lowering it would be a regression dressed as a fix."""
        monkeypatch.setattr(resource, "getrlimit", lambda _: (1_048_576, 1_048_576))

        def refuse(*_: Any) -> None:
            raise AssertionError("setrlimit must not be called when the limit is already high")

        monkeypatch.setattr(resource, "setrlimit", refuse)

        result = raise_file_limit()

        assert not result.raised
        assert result.soft == 1_048_576

    def test_clamps_to_the_hard_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unprivileged process cannot exceed its hard limit, and asking is
        an error rather than a clamp."""
        state = {"soft": 256, "hard": 1024}
        monkeypatch.setattr(resource, "getrlimit", lambda _: (state["soft"], state["hard"]))

        def fake_set(_: int, values: tuple[int, int]) -> None:
            if values[0] > state["hard"]:
                raise ValueError("current limit exceeds maximum limit")
            state["soft"] = values[0]

        monkeypatch.setattr(resource, "setrlimit", fake_set)

        result = raise_file_limit()

        assert result.soft == 1024
        assert result.raised

    def test_falls_back_rather_than_giving_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS caps a process at `kern.maxfilesperproc` whatever the hard
        limit says, and refuses outright above it. Accepting the refusal would
        leave the daemon on 256 because 8192 was ambitious — the failure this
        whole module exists to prevent."""
        state = {"soft": 256, "hard": resource.RLIM_INFINITY}
        monkeypatch.setattr(resource, "getrlimit", lambda _: (state["soft"], state["hard"]))

        def fake_set(_: int, values: tuple[int, int]) -> None:
            if values[0] > 2048:
                raise OSError(22, "Invalid argument")
            state["soft"] = values[0]

        monkeypatch.setattr(resource, "setrlimit", fake_set)

        result = raise_file_limit()

        assert result.soft == 2048
        assert result.raised

    def test_never_raises_when_the_kernel_refuses_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A daemon that cannot lift its limit is worse off, not broken. It is
        the daemon we shipped until now, and refusing to start would trade a
        degraded proxy for no proxy."""
        monkeypatch.setattr(resource, "getrlimit", lambda _: (256, 256))

        def always_fail(*_: Any) -> None:
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(resource, "setrlimit", always_fail)

        result = raise_file_limit()

        assert not result.raised
        assert result.soft == 256
        assert "could not raise" in result.detail


class TestDescriptorUsage:
    def test_reports_pressure_against_the_soft_limit(self) -> None:
        # The measured failure: 247 of 256, and every export 500ing.
        usage = DescriptorUsage(soft=256, hard=256, open_count=247)
        assert usage.pressure == pytest.approx(0.965, abs=0.001)
        assert usage.tight

    def test_a_healthy_daemon_is_not_tight(self) -> None:
        # And after a restart: 34 of 256, exports working.
        assert not DescriptorUsage(soft=256, hard=256, open_count=34).tight

    def test_an_unknown_count_is_unknown_rather_than_zero(self) -> None:
        """A platform we cannot count on must not read as "nothing open",
        which is the most reassuring possible wrong answer."""
        usage = DescriptorUsage(soft=8192, hard=8192, open_count=None)
        assert usage.pressure is None
        assert not usage.tight
        assert usage.to_dict()["open"] is None

    def test_an_infinite_hard_limit_serialises_as_null(self) -> None:
        usage = DescriptorUsage(soft=8192, hard=resource.RLIM_INFINITY, open_count=10)
        assert usage.to_dict()["hard"] is None


class TestOpenDescriptors:
    def test_counts_this_process(self) -> None:
        count = open_descriptors()
        # Every process holds at least stdin, stdout and stderr.
        assert count is not None and count >= 3

    def test_sees_a_descriptor_appear(self, tmp_path: Any) -> None:
        """The guard proper: a count that never moves would satisfy the test
        above while measuring nothing."""
        before = open_descriptors()
        assert before is not None
        handle = (tmp_path / "f").open("w")
        try:
            after = open_descriptors()
            assert after is not None and after > before
        finally:
            handle.close()

    def test_reports_none_where_it_cannot_look(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(_: Any) -> list[str]:
            raise OSError("no such directory")

        monkeypatch.setattr(limits.os, "listdir", refuse)
        assert open_descriptors() is None


def test_sample_reports_the_live_limit() -> None:
    reading = sample()
    assert reading.soft == resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    assert isinstance(reading, DescriptorUsage)


def test_file_limit_is_serialisable() -> None:
    payload = FileLimit(soft=8192, hard=8192, was=256, detail="raised from 256").to_dict()
    assert payload["soft"] == 8192
    assert payload["was"] == 256
