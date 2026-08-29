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

    # -- OI-8: module enablement survives a restart ----------------------

    def test_the_registry_the_daemon_builds_persists_to_the_state_directory(
        self, tmp_path: Any
    ) -> None:
        """OI-8, and OI-11's lesson applied to persistence.

        A sidecar written to a path a *test* constructed proves nothing about a
        daemon that writes somewhere else. This asserts the path
        ``build_evaluator`` actually gives the registry, against the configured
        ``state_dir`` — not against ``modules.root``, which is separately
        configurable (OI-10) and is module content rather than user state.
        """
        from pporlock.cli.runner import build_evaluator
        from pporlock.engine.modules.state import STATE_FILENAME

        config = self._config(tmp_path)
        config.modules.root = str(tmp_path / "elsewhere" / "modules")
        _evaluator, registry, _profiles, _base, _path, _error = build_evaluator(config)

        assert registry.state.path == Path(config.state_dir) / STATE_FILENAME

    def test_enabling_a_module_through_the_daemons_api_survives_a_restart(
        self, tmp_path: Any
    ) -> None:
        """OI-8 end to end, through the app ``_run`` assembles.

        Two sprints shipped a module system the daemon never constructed, so the
        proof that persistence works has to run through the daemon's own
        ControlApp and then through a *second* ``build_evaluator`` — standing in
        for the restart — rather than through a registry the test kept alive.
        """
        from starlette.testclient import TestClient

        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        self._write_module(
            tmp_path / "modules", "csp", "name: csp\npporlock_api: '1'\nenabled: false\n"
        )

        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )
        client = TestClient(control.asgi)
        headers = {
            "Authorization": f"Bearer {control.tokens.ensure()}",
            "X-Pporlock-Client": "ui",
        }
        response = client.patch("/modules/csp", headers=headers, json={"enabled": True})
        assert response.status_code == 200
        assert response.json()["enabled"] is True

        # Restart: nothing carried over but the state directory.
        _e2, restarted, _p2, _b2, _path2, _err2 = build_evaluator(config)
        module = restarted.get("csp")
        assert module is not None
        assert module.enabled is True
        # And the enabled module's rules are in force on the fresh evaluator,
        # not merely recorded as enabled.
        assert "csp" in restarted.build_ruleset(None).modules

    def test_a_priority_set_through_the_api_survives_a_restart(self, tmp_path: Any) -> None:
        """Ordering is user state too (REQ MOD-023)."""
        from starlette.testclient import TestClient

        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        self._write_module(
            tmp_path / "modules", "csp", "name: csp\npporlock_api: '1'\nenabled: true\n"
        )
        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )
        client = TestClient(control.asgi)
        client.patch(
            "/modules/csp",
            headers={
                "Authorization": f"Bearer {control.tokens.ensure()}",
                "X-Pporlock-Client": "ui",
            },
            json={"priority": 7},
        )

        _e2, restarted, _p2, _b2, _path2, _err2 = build_evaluator(config)
        assert restarted.get("csp").priority == 7

    def test_a_setting_changed_through_the_daemons_api_survives_a_restart(
        self, tmp_path: Any
    ) -> None:
        """Declared module settings, through the daemon's own app.

        The same shape as enablement, and for the same reason: a value that
        reverts on restart is worse than one that cannot be set, because it
        looks like it worked. Asserted on `ctx.config` of the module the
        *restarted* registry built, not on the sidecar — the file is a means,
        and a value that reaches disk but not the module is the failure this
        checks for.
        """
        from starlette.testclient import TestClient

        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        self._write_module(
            tmp_path / "modules",
            "ua",
            "name: ua\npporlock_api: '1'\nenabled: true\n"
            "settings:\n"
            "  - key: identity\n"
            "    type: enum\n"
            "    default: googlebot\n"
            "    options: [googlebot, claudebot]\n",
        )
        _evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset
        )
        client = TestClient(control.asgi)
        headers = {
            "Authorization": f"Bearer {control.tokens.ensure()}",
            "X-Pporlock-Client": "ui",
        }
        response = client.patch(
            "/modules/ua", headers=headers, json={"config": {"identity": "claudebot"}}
        )
        assert response.status_code == 200

        _e2, restarted, _p2, _b2, _path2, _err2 = build_evaluator(config)
        context = restarted.context("ua")
        assert context is not None
        assert context.config["identity"] == "claudebot"

    def test_a_corrupt_sidecar_does_not_stop_the_daemon_starting(self, tmp_path: Any) -> None:
        """As a malformed profile is skipped rather than fatal. The modules fall
        back to their manifest defaults and the reason is reported."""
        from pporlock.cli.runner import build_evaluator
        from pporlock.engine.modules.state import STATE_FILENAME

        config = self._config(tmp_path)
        self._write_module(
            tmp_path / "modules", "csp", "name: csp\npporlock_api: '1'\nenabled: true\n"
        )
        (tmp_path / STATE_FILENAME).write_text("{ this is not json")

        _evaluator, registry, _profiles, _base, _path, error = build_evaluator(config)
        assert error is None
        assert registry.get("csp").enabled is True
        assert registry.state.error is not None

    def test_the_startup_banner_reports_an_unreadable_sidecar(self) -> None:
        """Silently reverting every module to its default would look like the
        modules had stopped working."""
        import inspect

        from pporlock.cli import runner

        assert "registry.state.error" in inspect.getsource(runner._run)

    # -- OI-9: profile exclusions are applied ----------------------------

    def test_the_daemon_applies_the_active_profiles_exclusions(self, tmp_path: Any) -> None:
        """REQ MOD-044, OI-9, and OI-11's lesson.

        ``exclusions_add`` was parsed, stored and never applied. The fix lives
        in ``ControlApp.apply_exclusions``, so what matters is that ``_run``
        calls it *after* attaching the interceptor — a recompute performed while
        ``self.interceptor`` is still None installs onto nothing.
        """
        import inspect

        from pporlock.cli import runner

        source = inspect.getsource(runner._run)
        assert "control.apply_exclusions()" in source, (
            "cli/runner._run never applies the active profile's exclusions_add; "
            "REQ MOD-044 would be implemented and never executed"
        )
        assert source.index("control.interceptor = interceptor") < source.index(
            "control.apply_exclusions()"
        ), "apply_exclusions runs before there is an interceptor to install onto"

    def test_the_daemon_hands_the_control_app_the_users_base_exclusion_list(
        self, tmp_path: Any
    ) -> None:
        """Without a base to recompute from, switching profiles cannot take the
        outgoing profile's entries back off."""
        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset, evaluator.exclusions
        )
        assert len(control.base_exclusions) == len(evaluator.exclusions)
        assert len(control.base_exclusions) > 0

    def test_a_profiles_exclusions_reach_the_interceptor_the_daemon_builds(
        self, tmp_path: Any
    ) -> None:
        """Through the same objects ``_run`` assembles, not a stand-in."""
        import yaml

        from pporlock.addon.interceptor import Interceptor
        from pporlock.capture.ring import RingBuffer
        from pporlock.cli.runner import build_control_app, build_evaluator
        from pporlock.control.events import EventHub

        config = self._config(tmp_path)
        profile_dir = tmp_path / "profiles"
        profile_dir.mkdir(parents=True)
        (profile_dir / "banking.yaml").write_text(
            yaml.safe_dump(
                {"name": "banking", "modules": [], "exclusions_add": ["*.stripe.example"]}
            )
        )

        evaluator, registry, profiles, base_ruleset, _path, _error = build_evaluator(config)
        control = build_control_app(
            config, RingBuffer(), EventHub(), registry, profiles, base_ruleset, evaluator.exclusions
        )
        interceptor = Interceptor(config, exclusions=evaluator.exclusions, evaluator=evaluator)
        control.interceptor = interceptor
        profiles.activate("banking")
        control.active_profile = "banking"
        control.apply_exclusions()

        assert interceptor.exclusions.should_exclude("pay.stripe.example") is True
        assert interceptor.evaluator.exclusions is interceptor.exclusions

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


class TestTheDaemonRemembersTheActiveProfile:
    """OI-11's lesson applied to OI-9's dependency.

    The active profile's exclusions are applied at startup. A daemon that
    always came back on `default` applied none of them, so the feature worked
    until the first restart and then silently stopped. These assert the path
    the *daemon* uses, not one a test constructed.
    """

    def _config(self, tmp_path: Any) -> Config:
        config = Config()
        config.state_dir = str(tmp_path)
        config.modules.root = str(tmp_path / "modules")
        return config

    def test_the_state_file_lives_in_the_state_directory(self, tmp_path: Any) -> None:
        from pporlock.cli.runner import build_evaluator

        _ev, _reg, profiles, _base, _path, _err = build_evaluator(self._config(tmp_path))
        assert profiles.state_path == Path(tmp_path) / "active-profile"

    def test_a_profile_activated_through_the_daemon_survives_a_restart(self, tmp_path: Any) -> None:
        from pporlock.cli.runner import build_evaluator
        from pporlock.engine.profiles import Profile

        config = self._config(tmp_path)
        _ev, _reg, profiles, _base, _path, _err = build_evaluator(config)
        profiles.save(Profile(name="banking", exclusions_add=["*.stripe.example"]))
        profiles.activate("banking")

        # The restart: a completely fresh build from the same directory.
        _ev2, _reg2, restored, _b2, _p2, _e2 = build_evaluator(config)
        assert restored.active_name == "banking"
        assert restored.active.exclusions_add == ["*.stripe.example"]

    def test_the_module_filter_comes_back_with_it(self, tmp_path: Any) -> None:
        """Which modules a profile admits is the other half of activation."""
        from pporlock.cli.runner import build_evaluator
        from pporlock.engine.profiles import Profile

        config = self._config(tmp_path)
        _ev, _reg, profiles, _base, _path, _err = build_evaluator(config)
        profiles.save(Profile(name="minimal", modules=["adblock"]))
        profiles.activate("minimal")

        _ev2, _reg2, restored, _b2, _p2, _e2 = build_evaluator(config)
        assert restored.module_filter() == ["adblock"]


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
