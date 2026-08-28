"""CLI surface. SPEC-1 §8."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pporlock.cli import main as cli


class TestParser:
    def test_requires_a_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    @pytest.mark.parametrize("command", ["run", "doctor", "install", "uninstall", "version"])
    def test_sprint_two_commands_exist(self, command: str) -> None:
        args = cli.build_parser().parse_args([command])
        assert args.command == command
        assert callable(args.func)

    def test_run_accepts_port_overrides(self) -> None:
        args = cli.build_parser().parse_args(["run", "--port", "9090", "--control-port", "9091"])
        assert args.port == 9090
        assert args.control_port == 9091

    def test_doctor_accepts_fix(self) -> None:
        assert cli.build_parser().parse_args(["doctor", "--fix"]).fix is True

    def test_uninstall_accepts_purge(self) -> None:
        assert cli.build_parser().parse_args(["uninstall", "--purge"]).purge is True


class TestConfigResolution:
    def test_port_flags_become_overrides(self, tmp_path: Path) -> None:
        args = cli.build_parser().parse_args(
            ["--config", str(tmp_path / "nope.yaml"), "run", "--port", "9090"]
        )
        assert cli._load_config(args).proxy.listen_port == 9090

    def test_control_port_flag(self, tmp_path: Path) -> None:
        args = cli.build_parser().parse_args(
            ["--config", str(tmp_path / "nope.yaml"), "run", "--control-port", "9091"]
        )
        assert cli._load_config(args).control.listen_port == 9091

    def test_explicit_config_file_is_used(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("proxy:\n  listen_port: 7777\n")
        args = cli.build_parser().parse_args(["--config", str(path), "doctor"])
        assert cli._load_config(args).proxy.listen_port == 7777

    def test_missing_explicit_config_falls_back_to_defaults(self, tmp_path: Path) -> None:
        args = cli.build_parser().parse_args(["--config", str(tmp_path / "nope.yaml"), "doctor"])
        assert cli._load_config(args).proxy.listen_port == 8080


class TestCommands:
    def test_version_reports_the_pinned_mitmproxy(self, capsys: Any) -> None:
        assert cli.main(["version"]) == 0
        out = capsys.readouterr().out
        assert "pporlock" in out
        assert "mitmproxy" in out
        assert "pinned" in out

    def test_doctor_runs_and_prints(self, capsys: Any, tmp_path: Path) -> None:
        code = cli.main(["--config", str(tmp_path / "nope.yaml"), "doctor"])
        out = capsys.readouterr().out
        assert "pporlock doctor" in out
        assert code in (0, 1)

    def test_install_no_ca_is_a_no_op(self, capsys: Any) -> None:
        assert cli.main(["install", "--no-ca"]) == 0
        assert "skipping CA trust" in capsys.readouterr().out

    def test_install_without_a_ca_explains_what_to_do(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from pporlock.cli import certs

        monkeypatch.setattr(certs, "is_present", lambda: False)
        assert cli.main(["install"]) == 1
        assert "pporlock run" in capsys.readouterr().out

    def test_install_reports_success(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        from pporlock.cli import certs

        monkeypatch.setattr(certs, "is_present", lambda: True)
        monkeypatch.setattr(
            certs,
            "install_trust",
            lambda: certs.TrustStatus(True, True, Path("/x"), "trusted"),
        )
        assert cli.main(["install"]) == 0
        assert "trusted" in capsys.readouterr().out

    def test_install_reports_failure(self, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        from pporlock.cli import certs

        monkeypatch.setattr(certs, "is_present", lambda: True)

        def boom() -> None:
            raise RuntimeError("keychain said no")

        monkeypatch.setattr(certs, "install_trust", boom)
        assert cli.main(["install"]) == 1
        assert "keychain said no" in capsys.readouterr().out

    def test_uninstall_states_what_it_leaves_behind(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """REQ DOC-005 — an uninstall that silently leaves a trusted MITM root
        or session data behind is worse than one that says so."""
        from pporlock.cli import certs

        monkeypatch.setattr(certs, "remove_trust", lambda: None)
        assert cli.main(["uninstall"]) == 0
        out = capsys.readouterr().out
        assert "Left in place" in out
        assert ".pporlock" in out
        assert ".mitmproxy" in out


class TestErrorHandling:
    def test_pporlock_errors_are_reported_not_traced(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        from pporlock.errors import NonLoopbackBindError

        def boom(_: Any) -> int:
            raise NonLoopbackBindError("refused", setting="control.listen_host")

        parser = cli.build_parser()
        original = parser.parse_args

        def patched(argv: Any = None) -> Any:
            args = original(argv)
            args.func = boom
            return args

        monkeypatch.setattr(
            cli, "build_parser", lambda: type("P", (), {"parse_args": staticmethod(patched)})()
        )
        assert cli.main(["version"]) == 1
        assert "non_loopback_bind" in capsys.readouterr().err

    def test_unexpected_errors_are_not_swallowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bug must surface as a traceback, not a bare exit code."""

        def boom(_: Any) -> int:
            raise ValueError("unexpected")

        parser = cli.build_parser()
        original = parser.parse_args

        def patched(argv: Any = None) -> Any:
            args = original(argv)
            args.func = boom
            return args

        monkeypatch.setattr(
            cli, "build_parser", lambda: type("P", (), {"parse_args": staticmethod(patched)})()
        )
        with pytest.raises(ValueError, match="unexpected"):
            cli.main(["version"])

    def test_keyboard_interrupt_exits_130(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_: Any) -> int:
            raise KeyboardInterrupt

        parser = cli.build_parser()
        original = parser.parse_args

        def patched(argv: Any = None) -> Any:
            args = original(argv)
            args.func = boom
            return args

        monkeypatch.setattr(
            cli, "build_parser", lambda: type("P", (), {"parse_args": staticmethod(patched)})()
        )
        assert cli.main(["version"]) == 130


class TestRunAndFix:
    def test_run_delegates_to_the_foreground_runner(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_run(config: Any, *, quiet: bool = False) -> int:
            seen["port"] = config.proxy.listen_port
            seen["quiet"] = quiet
            return 0

        import pporlock.cli.runner as runner_mod

        monkeypatch.setattr(runner_mod, "run_foreground", fake_run)
        code = cli.main(
            ["--config", str(tmp_path / "nope.yaml"), "run", "--port", "9090", "--quiet"]
        )
        assert code == 0
        assert seen == {"port": 9090, "quiet": True}

    def test_doctor_fix_attempts_repairs_and_rechecks(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path
    ) -> None:
        from pporlock.cli import doctor

        calls: list[str] = []

        def failing(_: Any) -> doctor.CheckResult:
            return doctor.CheckResult("fixable", "Fixable thing", "fail", "broken", "fix it")

        def fix(_: Any) -> None:
            calls.append("fixed")

        monkeypatch.setattr(
            doctor, "CHECKS", [doctor.Check("fixable", "Fixable thing", failing, fix=fix)]
        )
        code = cli.main(["--config", str(tmp_path / "nope.yaml"), "doctor", "--fix"])
        out = capsys.readouterr().out
        assert calls == ["fixed"]
        assert "after fixes:" in out
        assert code == 1  # still failing after the fix, and says so

    def test_doctor_fix_reports_a_failed_repair(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path
    ) -> None:
        from pporlock.cli import doctor

        def failing(_: Any) -> doctor.CheckResult:
            return doctor.CheckResult("fixable", "Fixable thing", "fail", "broken", "fix it")

        def fix(_: Any) -> None:
            raise RuntimeError("cannot fix that")

        monkeypatch.setattr(
            doctor, "CHECKS", [doctor.Check("fixable", "Fixable thing", failing, fix=fix)]
        )
        cli.main(["--config", str(tmp_path / "nope.yaml"), "doctor", "--fix"])
        assert "could not fix: cannot fix that" in capsys.readouterr().out

    def test_doctor_fix_skips_checks_with_no_fix(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path
    ) -> None:
        from pporlock.cli import doctor

        def failing(_: Any) -> doctor.CheckResult:
            return doctor.CheckResult("nofix", "Unfixable", "fail", "broken", "do it yourself")

        monkeypatch.setattr(doctor, "CHECKS", [doctor.Check("nofix", "Unfixable", failing)])
        cli.main(["--config", str(tmp_path / "nope.yaml"), "doctor", "--fix"])
        assert "fixing:" not in capsys.readouterr().out

    def test_uninstall_purge_removes_the_state_directory(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
    ) -> None:
        """Same assertion as before, driven through an explicit config.

        This previously monkeypatched ``Path.home`` and relied on ``uninstall``
        calling it at command time. Sprint 16 made ``--purge`` delete the
        *configured* ``state_dir`` — so a user with a custom state directory is
        told, and purged, the truth — and ``Config``'s default binds
        ``DEFAULT_STATE_DIR`` at import (OI-10). Patching ``home`` afterwards
        therefore no longer redirects anything, and the test became
        import-order-dependent: in a full run it deleted the *real*
        ``~/.pporlock``. A unit test that can delete the developer's state is a
        worse problem than the one it was written to catch. Pointing it at a
        config makes it deterministic and confines it to tmp_path.
        """
        from pporlock.cli import certs

        state = tmp_path / ".pporlock"
        state.mkdir()
        (state / "token").write_text("secret")
        config = tmp_path / "config.yaml"
        config.write_text(f"state_dir: {state}\nlogging: {{dir: {tmp_path / 'logs'}}}\n")
        monkeypatch.setattr(certs, "remove_trust", lambda: None)
        assert cli.main(["--config", str(config), "uninstall", "--purge"]) == 0
        assert not state.exists()
        assert "--purge" in capsys.readouterr().out
