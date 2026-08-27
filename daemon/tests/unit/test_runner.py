"""Foreground runner. SPEC-1 §8, REQ PXY-005.

The DumpMaster wiring itself is covered by the integration suite, which runs a
real proxy. What is worth testing here is the part that has bitten already: the
console output and its buffering.
"""

from __future__ import annotations

from typing import Any

import pytest

from pporlock.cli.runner import ConsoleSink, emit, run_foreground
from pporlock.config import Config
from pporlock.engine.provenance import NoteCode, ProvenanceBuilder


def make_request(**kwargs: Any) -> Any:
    return type(
        "R",
        (),
        {
            "method": kwargs.get("method", "GET"),
            "url": kwargs.get("url", "https://cdn.example.com/a.js"),
        },
    )()


def make_response(status: int = 200, size: int = 1234) -> Any:
    return type("R", (), {"status": status, "body_size": size})()


class TestEmit:
    def test_flushes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """stdout is block-buffered when redirected. For this command the live
        output *is* the product, so an unflushed feed is the same as no feed."""
        emit("hello")
        assert capsys.readouterr().out == "hello\n"


class TestConsoleSink:
    def test_prints_a_flow_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        sink = ConsoleSink()
        sink.record_http(
            make_request(),
            make_response(),
            ProvenanceBuilder("default").build(),
            {"pporlock_ms": 1.86},
        )
        out = capsys.readouterr().out
        assert "GET" in out
        assert "200" in out
        assert "1,234b" in out
        assert "1.86ms" in out
        assert "cdn.example.com" in out

    def test_counts_even_when_quiet(self, capsys: pytest.CaptureFixture[str]) -> None:
        sink = ConsoleSink(quiet=True)
        sink.record_http(make_request(), make_response(), ProvenanceBuilder("default").build(), {})
        assert capsys.readouterr().out == ""
        assert sink.http == 1

    def test_handles_a_flow_with_no_response(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A flow can complete without a response — an upstream error, or a
        client that hung up. It must still be reported, not crash the feed."""
        sink = ConsoleSink()
        sink.record_http(make_request(), None, ProvenanceBuilder("default").build(), {})
        assert "---" in capsys.readouterr().out

    def test_prints_a_tunnel_line_with_the_matching_pattern(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The user needs to see *why* a host was tunneled, not merely that it was."""
        builder = ProvenanceBuilder("default")
        builder.note(
            NoteCode.PASSTHROUGH_EXCLUDED, "tunneled", pattern="*.apple.com", reason="update"
        )
        sink = ConsoleSink()
        sink.record_passthrough("www.apple.com", None, builder.build(), {})
        out = capsys.readouterr().out
        assert "TUNNEL" in out
        assert "www.apple.com" in out
        assert "*.apple.com" in out

    def test_tunnel_line_falls_back_to_ip(self, capsys: pytest.CaptureFixture[str]) -> None:
        builder = ProvenanceBuilder("default")
        builder.note(NoteCode.PASSTHROUGH_EXCLUDED, "tunneled", pattern="10.0.0.0/8")
        sink = ConsoleSink()
        sink.record_passthrough(None, "10.1.2.3", builder.build(), {})
        assert "10.1.2.3" in capsys.readouterr().out

    def test_tunnel_line_with_no_notes(self, capsys: pytest.CaptureFixture[str]) -> None:
        sink = ConsoleSink()
        sink.record_passthrough("h", None, ProvenanceBuilder("default").build(), {})
        assert "TUNNEL" in capsys.readouterr().out

    def test_quiet_suppresses_tunnel_lines_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        sink = ConsoleSink(quiet=True)
        sink.record_passthrough("h", None, ProvenanceBuilder("default").build(), {})
        assert capsys.readouterr().out == ""
        assert sink.passthrough == 1

    def test_long_urls_are_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        sink = ConsoleSink()
        sink.record_http(
            make_request(url="https://example.com/" + "x" * 500),
            make_response(),
            ProvenanceBuilder("default").build(),
            {},
        )
        line = capsys.readouterr().out.strip()
        assert len(line) < 200


class TestRunForeground:
    def test_port_conflict_is_reported_not_traced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A busy port is an ordinary condition — another pporlock is running —
        and deserves a message rather than a stack trace."""
        import pporlock.cli.runner as runner_mod

        async def boom(*_args: Any, **_kwargs: Any) -> int:
            raise OSError("address already in use")

        monkeypatch.setattr(runner_mod, "_run", boom)
        assert run_foreground(Config()) == 1
        assert "could not start proxy" in capsys.readouterr().err

    def test_keyboard_interrupt_exits_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pporlock.cli.runner as runner_mod

        async def boom(*_args: Any, **_kwargs: Any) -> int:
            raise KeyboardInterrupt

        monkeypatch.setattr(runner_mod, "_run", boom)
        assert run_foreground(Config()) == 130

    def test_clean_exit_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pporlock.cli.runner as runner_mod

        async def clean(*_args: Any, **_kwargs: Any) -> int:
            return 0

        monkeypatch.setattr(runner_mod, "_run", clean)
        assert run_foreground(Config()) == 0
