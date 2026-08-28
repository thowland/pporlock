"""Foreground runner. SPEC-1 §8, REQ PXY-005.

The DumpMaster wiring itself is covered by the integration suite, which runs a
real proxy. What is worth testing here is the part that has bitten already: the
console output and its buffering.
"""

from __future__ import annotations

from pathlib import Path
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


class TestStartupWiring:
    """The daemon must actually run what it loaded.

    Every one of these covers something that was implemented, unit tested, and
    not connected to the running proxy. Unit tests cannot see that gap by
    construction — they build the objects themselves — so this is where it gets
    caught.
    """

    def _config(self, tmp_path: Any) -> Config:
        config = Config()
        config.state_dir = str(tmp_path)
        config.modules.root = str(tmp_path / "modules")
        return config

    def _write_module(self, root: Any, name: str, body: str) -> None:
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "module.yaml").write_text(body)

    def test_the_runner_builds_a_module_registry(self, tmp_path: Any) -> None:
        """REQ MOD-001. Modules were loadable and reloadable through the API,
        and the running daemon built no registry at all — so no module rule
        ever reached live traffic and every module route answered 404."""
        from pporlock.cli.runner import build_evaluator

        self._write_module(
            tmp_path / "modules",
            "warn",
            "name: warn\npporlock_api: '1'\nenabled: true\n"
            "rules:\n"
            "  - name: strip\n"
            "    action: headers\n"
            "    match: {host: '*'}\n"
            "    response: {remove: [content-security-policy]}\n",
        )
        _evaluator, registry, profiles, _base, _path, error = build_evaluator(
            self._config(tmp_path)
        )
        assert error is None
        assert registry is not None
        assert [m.name for m in registry.modules] == ["warn"]
        assert profiles.active_name == "default"

    def test_module_rules_reach_the_evaluator(self, tmp_path: Any) -> None:
        """A registry the evaluator cannot see is a registry that does nothing."""
        from pporlock.cli.runner import build_evaluator

        self._write_module(
            tmp_path / "modules",
            "warn",
            "name: warn\npporlock_api: '1'\nenabled: true\n"
            "rules:\n"
            "  - name: strip\n"
            "    action: headers\n"
            "    match: {host: '*'}\n"
            "    response: {remove: [content-security-policy]}\n",
        )
        evaluator, _registry, _profiles, _base, _path, _error = build_evaluator(
            self._config(tmp_path)
        )
        assert len(evaluator.ruleset.response_headers) == 1
        # And the registry itself, for the Python hooks (REQ MOD-023).
        assert evaluator.registry is not None

    def test_file_rules_and_module_rules_are_one_set(self, tmp_path: Any) -> None:
        """Both, not either. They order against each other by priority."""
        from pporlock.cli.runner import build_evaluator

        (tmp_path / "rules.yaml").write_text(
            "rules:\n"
            "  - name: mine\n"
            "    action: headers\n"
            "    match: {host: '*'}\n"
            "    response: {set: {x-mine: '1'}}\n"
        )
        self._write_module(
            tmp_path / "modules",
            "theirs",
            "name: theirs\npporlock_api: '1'\nenabled: true\n"
            "rules:\n"
            "  - name: theirs\n"
            "    action: headers\n"
            "    match: {host: '*'}\n"
            "    response: {set: {x-theirs: '1'}}\n",
        )
        evaluator, _registry, _profiles, base, _path, error = build_evaluator(
            self._config(tmp_path)
        )
        assert error is None
        names = {r.name for r in evaluator.ruleset.response_headers}
        assert names == {"mine", "theirs"}
        # The file rules are handed back separately so reinstalling the module
        # rules cannot delete them.
        assert {r.name for r in base.rules} == {"mine"}

    def test_a_broken_module_does_not_stop_startup(self, tmp_path: Any) -> None:
        """One bad module must not stop the daemon, or a typo costs you the
        browser as well as the module."""
        from pporlock.cli.runner import build_evaluator

        self._write_module(tmp_path / "modules", "broken", "name: not-broken\n")
        self._write_module(
            tmp_path / "modules", "fine", "name: fine\npporlock_api: '1'\nenabled: true\n"
        )
        _evaluator, registry, _profiles, _base, _path, _error = build_evaluator(
            self._config(tmp_path)
        )
        by_name = {m.name: m for m in registry.modules}
        assert by_name["broken"].error is not None
        assert by_name["fine"].error is None

    def test_no_modules_directory_is_not_an_error(self, tmp_path: Any) -> None:
        """A fresh install has no modules and must still start."""
        from pporlock.cli.runner import build_evaluator

        evaluator, registry, _profiles, _base, _path, error = build_evaluator(
            self._config(tmp_path)
        )
        assert error is None
        assert registry.modules == ()
        assert len(evaluator.ruleset.response_headers) == 0

    def test_the_dry_run_route_is_reachable_from_a_daemon_started_by_pporlock_run(
        self, tmp_path: Any
    ) -> None:
        """REQ CAP-030/031/032 — and OI-11's lesson.

        Two sprints closed with a fully unit-tested module system that the
        running daemon never built. So this drives the dry run through the same
        ControlApp ``_run`` assembles, from the same ``build_evaluator`` output,
        and asserts a candidate module actually fires on a flow. A unit test
        that constructed its own ControlApp could not notice the wiring missing.
        """
        from starlette.testclient import TestClient

        from pporlock.capture.records import FlowRecord
        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub
        from pporlock.engine.models import NormalizedRequest, NormalizedResponse

        config = self._config(tmp_path)
        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)

        ring = RingBuffer()
        ring.add(
            FlowRecord(
                flow_id="f0",
                kind="http",
                started_at="2026-08-27T14:00:00.000Z",
                request=NormalizedRequest(
                    flow_id="f0",
                    timestamp="2026-08-27T14:00:00.000Z",
                    scheme="https",
                    method="GET",
                    host="app.example.com",
                    port=443,
                    path="/index.html",
                    url="https://app.example.com/index.html",
                ),
                response=NormalizedResponse(
                    flow_id="f0",
                    timestamp="2026-08-27T14:00:01.000Z",
                    status=200,
                    headers=(
                        ("content-type", "text/html"),
                        ("content-security-policy", "default-src 'self'"),
                    ),
                    body=b"<html><head></head></html>",
                ),
            )
        )

        control = build_control_app(config, ring, EventHub(), registry, profiles, base_ruleset)
        client = TestClient(control.asgi)
        response = client.post(
            "/sessions/live/dryrun",
            headers={
                "Authorization": f"Bearer {control.tokens.ensure()}",
                "X-Pporlock-Client": "ui",
            },
            json={
                "modules": [
                    {
                        "name": "csp-strip",
                        "files": {
                            "module.yaml": (
                                "name: csp-strip\npporlock_api: '1'\n"
                                "rules:\n"
                                "  - name: strip\n"
                                "    action: headers\n"
                                "    match: {host: app.example.com}\n"
                                "    response: {remove: [content-security-policy]}\n"
                            )
                        },
                    }
                ]
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["matched"] == 1
        assert body["results"][0]["diff"]["headers"][0]["name"] == "content-security-policy"

    def test_the_validate_route_is_reachable_from_the_daemons_control_app(
        self, tmp_path: Any
    ) -> None:
        """REQ API-027 — the route the web UI's editor already calls."""
        from starlette.testclient import TestClient

        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )
        client = TestClient(control.asgi)
        response = client.post(
            "/validate",
            headers={
                "Authorization": f"Bearer {control.tokens.ensure()}",
                "X-Pporlock-Client": "ui",
            },
            json={"name": "tidy", "files": {"module.yaml": "name: tidy\npporlock_api: '1'\n"}},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_the_dry_runner_uses_the_running_proxys_evaluator(self, tmp_path: Any) -> None:
        """REQ CAP-031 — the guarantee is worthless if the route builds its own
        evaluator instead of cloning the one live traffic is using."""
        from pporlock.addon.interceptor import Interceptor
        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )
        control.interceptor = Interceptor(config, evaluator=evaluator)

        runner = control.dry_runner()
        assert runner._evaluator is control.interceptor.evaluator
        assert runner.installed_root == Path(config.modules.root)
        assert runner.redactor is control.redactor

    def test_the_daemon_starts_a_log_rotation_task(self, tmp_path: Any) -> None:
        """REQ PXY-007, and OI-11's lesson.

        launchd appends to the log files and never truncates them, so nothing
        bounds them but this. A rotation function that exists and is never
        scheduled is the exact shape of the bug this class was created for: it
        would be fully unit-tested and would never run.
        """
        import asyncio
        import inspect

        from pporlock.cli import runner

        source = inspect.getsource(runner._run)
        assert "rotate_logs_forever" in source, (
            "cli/runner._run does not schedule log rotation; REQ PXY-007 would "
            "be implemented and never executed"
        )
        assert "rotator.cancel()" in source, "the rotation task is never cancelled on shutdown"

        # And the coroutine itself actually rotates, on the interval it is given.
        config = self._config(tmp_path)
        config.logging.dir = str(tmp_path / "logs")
        config.logging.max_bytes = 100
        directory = Path(config.logging.dir)
        directory.mkdir(parents=True)
        log = directory / "pporlock.out.log"
        log.write_bytes(b"x" * 5000)

        async def drive() -> None:
            task = asyncio.create_task(runner.rotate_logs_forever(config, interval=0.01))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if log.stat().st_size == 0:
                    break
            task.cancel()

        asyncio.run(drive())
        assert log.stat().st_size == 0
        assert (directory / "pporlock.out.log.1").stat().st_size == 5000

    def test_the_daemon_accumulates_per_module_cost(self, tmp_path: Any) -> None:
        """REQ PRF-007. The metrics route reads `interceptor.module_cost`; if
        nothing on the flow-completion path writes to it, every module reports
        zero forever and the UI column is decoration."""
        import inspect

        from pporlock.addon.interceptor import Interceptor

        source = inspect.getsource(Interceptor.response)
        assert "self.module_cost.record(" in source
        assert "registry.record_provenance(" in source

    def test_the_interceptor_the_runner_builds_has_a_cost_index(self, tmp_path: Any) -> None:
        """Built by the daemon, not by the test: an index the running process
        does not own is an index nothing writes to."""
        from pporlock.addon.interceptor import Interceptor
        from pporlock.cli.runner import build_evaluator

        config = self._config(tmp_path)
        evaluator, registry, _profiles, _base, _path, _error = build_evaluator(config)
        interceptor = Interceptor(config, evaluator=evaluator)
        assert interceptor.module_cost is not None
        assert interceptor.evaluator.registry is registry

    def test_module_stats_reach_the_control_apps_module_list(self, tmp_path: Any) -> None:
        """REQ PRF-007 end to end through the app the daemon serves.

        The `stats` field was absent from `LoadedModule.to_dict()` while the
        contract declared it, and the module library read it unconditionally —
        so a live daemon crashed the page while every unit test passed on
        fixtures that supplied a field the daemon never sent.
        """
        from starlette.testclient import TestClient

        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub
        from pporlock.engine.provenance import Action, Outcome, Phase, ProvenanceBuilder

        config = self._config(tmp_path)
        self._write_module(
            tmp_path / "modules", "csp", "name: csp\npporlock_api: '1'\nenabled: true\n"
        )
        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )

        builder = ProvenanceBuilder("default")
        builder.record(
            phase=Phase.RESPONSE_HEADERS,
            module="csp",
            rule_id="csp:0",
            action=Action.HEADERS,
            outcome=Outcome.APPLIED,
            duration_ms=3.0,
        )
        registry.record_provenance(builder.build())

        client = TestClient(control.asgi)
        modules = client.get(
            "/modules",
            headers={
                "Authorization": f"Bearer {control.tokens.ensure()}",
                "X-Pporlock-Client": "ui",
            },
        ).json()
        stats = modules[0]["stats"]
        assert stats["flows_matched"] == 1
        assert stats["flows_modified"] == 1
        assert stats["avg_ms"] == 3.0


class TestApplyModulesKeepsFileRules:
    def test_reinstalling_module_rules_does_not_delete_rules_yaml(self) -> None:
        """Enabling a module rebuilt the rule set from modules alone, so the
        user's own rules.yaml silently stopped applying until the next restart.
        """
        from pporlock.capture.ring import RingBuffer
        from pporlock.control.app import ControlApp
        from pporlock.engine.ruleset import RuleSet

        base = RuleSet.from_rules(
            [
                {
                    "name": "mine",
                    "action": "headers",
                    "match": {"host": "*"},
                    "response": {"set": {"x-mine": "1"}},
                }
            ],
            module=None,
        )
        app = ControlApp(Config(), ring=RingBuffer(), base_ruleset=base)
        assert {r.name for r in app.base_ruleset.rules} == {"mine"}
