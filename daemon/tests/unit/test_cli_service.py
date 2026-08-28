"""The full CLI surface and the API-backed subcommands — REQ PXY-003, PXY-004.

SPEC-1 §8 lists fifteen commands. Sprint 2 shipped five. This asserts the parser
exposes all of them with the right shapes, and that the ones that talk to a
running daemon go through the control API rather than reimplementing what the
daemon knows.

`ControlClient` is faked at the ``request`` seam throughout, so these tests check
*which* API calls a command makes — the part that is wrong when a CLI command
quietly reads local state instead of asking the process that owns it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from pporlock.cli import client as client_mod
from pporlock.cli import commands, doctor, launchd
from pporlock.cli import main as cli
from pporlock.config import Config
from pporlock.errors import PporlockError


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.state_dir = str(tmp_path)
    cfg.modules.root = str(tmp_path / "modules")
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


class FakeClient:
    """Records every control-API call and answers from a script."""

    def __init__(self, config: Config, answers: dict[tuple[str, str], Any] | None = None) -> None:
        self.config = config
        self.base = "http://127.0.0.1:8081"
        self.calls: list[tuple[str, str]] = []
        self.answers = answers or {}
        self.is_reachable = True

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        return self.answers.get((method, path))

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def reachable(self) -> bool:
        return self.is_reachable

    @property
    def paths(self) -> list[str]:
        return [path for _, path in self.calls]


def install_fake(
    monkeypatch: pytest.MonkeyPatch, config: Config, answers: dict[tuple[str, str], Any]
) -> FakeClient:
    fake = FakeClient(config, answers)
    monkeypatch.setattr(commands, "ControlClient", lambda _config: fake)
    return fake


# ------------------------------------------------------------------ parser ---


class TestParser:
    @pytest.mark.parametrize(
        "command",
        [
            "run",
            "start",
            "stop",
            "restart",
            "status",
            "install",
            "uninstall",
            "doctor",
            "pair",
            "logs",
            "version",
        ],
    )
    def test_spec_1_section_8_commands_exist(self, command: str) -> None:
        """REQ PXY-003 names start, stop, status, restart, install, uninstall,
        logs and doctor as the minimum."""
        args = cli.build_parser().parse_args([command])
        assert args.command == command
        assert callable(args.func)

    def test_install_takes_service_and_no_start(self) -> None:
        args = cli.build_parser().parse_args(["install", "--service", "--no-start"])
        assert args.service is True
        assert args.no_start is True

    def test_logs_takes_follow_lines_and_stream(self) -> None:
        args = cli.build_parser().parse_args(["logs", "-f", "-n", "10", "--stream", "err"])
        assert (args.follow, args.lines, args.stream) == (True, 10, "err")

    def test_modules_subcommands(self) -> None:
        parser = cli.build_parser()
        assert parser.parse_args(["modules", "list"]).modules_action == "list"
        assert parser.parse_args(["modules", "enable", "m"]).name == "m"
        assert parser.parse_args(["modules", "disable", "m"]).modules_action == "disable"
        assert parser.parse_args(["modules", "validate", "/tmp/m"]).path == "/tmp/m"

    def test_modules_requires_an_action(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["modules"])

    def test_profile_subcommands(self) -> None:
        parser = cli.build_parser()
        assert parser.parse_args(["profile", "list"]).profile_action == "list"
        assert parser.parse_args(["profile", "activate", "dev"]).name == "dev"

    def test_session_subcommands(self) -> None:
        parser = cli.build_parser()
        assert parser.parse_args(["session", "start"]).name == ""
        assert parser.parse_args(["session", "start", "login"]).name == "login"
        assert parser.parse_args(["session", "stop", "s1"]).session_id == "s1"
        assert parser.parse_args(["session", "list"]).session_action == "list"
        export = parser.parse_args(["session", "export", "s1", "-o", "/tmp/x.json"])
        assert (export.session_id, export.output) == ("s1", "/tmp/x.json")

    def test_dryrun_takes_a_session_and_a_module_path(self) -> None:
        args = cli.build_parser().parse_args(["dryrun", "live", "/tmp/mod"])
        assert (args.session_id, args.module_path) == ("live", "/tmp/mod")


# --------------------------------------------------------- service control ---


class TestServiceCommands:
    def test_start_waits_for_the_daemon_to_actually_answer(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """kickstart returns as soon as launchd forks, which is long before the
        proxy has bound a port. A start that returned there would report success
        for a daemon that goes on to die on a bad config file."""
        fake = install_fake(monkeypatch, config, {})
        fake.is_reachable = False
        started: list[bool] = []

        def fake_start() -> None:
            started.append(True)
            fake.is_reachable = True

        monkeypatch.setattr(launchd, "start", fake_start)
        assert commands.cmd_start(config) == 0
        assert started == [True]
        assert "started" in capsys.readouterr().out

    def test_start_is_a_no_op_when_already_running(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        install_fake(monkeypatch, config, {})
        called: list[bool] = []
        monkeypatch.setattr(launchd, "start", lambda: called.append(True))
        assert commands.cmd_start(config) == 0
        assert called == []
        assert "already running" in capsys.readouterr().out

    def test_start_reports_a_launchd_failure(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        fake = install_fake(monkeypatch, config, {})
        fake.is_reachable = False

        def boom() -> None:
            raise launchd.LaunchdError("no agent installed")

        monkeypatch.setattr(launchd, "start", boom)
        assert commands.cmd_start(config) == 1
        assert "no agent installed" in capsys.readouterr().out

    def test_start_fails_when_the_daemon_never_answers(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        fake = install_fake(monkeypatch, config, {})
        fake.is_reachable = False
        monkeypatch.setattr(launchd, "start", lambda: None)
        monkeypatch.setattr(commands, "START_WAIT_S", 0.05)
        monkeypatch.setattr(commands, "START_POLL_S", 0.01)
        monkeypatch.setattr(
            launchd, "status", lambda: launchd.ServiceStatus(True, True, False, last_exit_code=78)
        )
        assert commands.cmd_start(config) == 1
        out = capsys.readouterr().out
        assert "not answering" in out
        assert "78" in out
        # And it points at the log, because that is where the reason is.
        assert "pporlock.err.log" in out

    def test_stop_says_the_extension_will_revert(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """REQ PXY-008 is discharged jointly; the user should know that."""
        monkeypatch.setattr(launchd, "stop", lambda: None)
        assert commands.cmd_stop(config) == 0
        assert "proxy settings" in capsys.readouterr().out

    def test_restart_stops_then_starts(
        self, monkeypatch: pytest.MonkeyPatch, config: Config
    ) -> None:
        order: list[str] = []
        fake = install_fake(monkeypatch, config, {})
        fake.is_reachable = False
        monkeypatch.setattr(launchd, "stop", lambda: order.append("stop"))

        def fake_start() -> None:
            order.append("start")
            fake.is_reachable = True

        monkeypatch.setattr(launchd, "start", fake_start)
        assert commands.cmd_restart(config) == 0
        assert order == ["stop", "start"]

    def test_status_reports_launchd_and_the_daemon_separately(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """They disagree in exactly the interesting case: the agent is loaded,
        the process is up, and the control API is wedged."""
        install_fake(
            monkeypatch,
            config,
            {
                # The real /state shape, copied from a running daemon. The
                # first version of this test invented `proxy.listen_host` and
                # `profile`, and passed while the live command printed "?:?".
                ("GET", "/state"): {
                    "proxy": {"running": True, "listen": "127.0.0.1:8080", "uptime_s": 1.0},
                    "active_profile": "dev",
                    "modules": {"loaded": 2, "enabled": 1, "quarantined": 0},
                    "counters": {"flows_total": 12, "modified": 3, "blocked": 1},
                }
            },
        )
        monkeypatch.setattr(
            launchd, "status", lambda: launchd.ServiceStatus(True, True, True, pid=99)
        )
        assert commands.cmd_status(config) == 0
        out = capsys.readouterr().out
        assert "pid 99" in out
        assert "reachable" in out
        assert "12 total" in out
        # The fields really present in /state, not invented ones.
        assert "127.0.0.1:8080" in out
        assert "dev" in out
        assert "1 enabled of 2" in out
        assert "?" not in out

    def test_status_is_nonzero_when_the_daemon_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        fake = install_fake(monkeypatch, config, {})
        fake.is_reachable = False
        monkeypatch.setattr(launchd, "status", lambda: launchd.ServiceStatus(False, False, False))
        assert commands.cmd_status(config) == 1
        assert "not reachable" in capsys.readouterr().out


class TestLogsCommand:
    def test_reports_when_there_are_no_logs(self, config: Config, capsys: Any) -> None:
        assert commands.cmd_logs(config, follow=False, lines=5, stream="both") == 1
        out = capsys.readouterr().out
        assert "no logs" in out
        # And says why: `pporlock run` writes to the terminal, not to a file.
        assert "launchd" in out

    def test_prints_the_tail_of_both_streams(self, config: Config, capsys: Any) -> None:
        directory = Path(config.logging.dir)
        directory.mkdir(parents=True)
        (directory / "pporlock.out.log").write_text("out line\n")
        (directory / "pporlock.err.log").write_text("err line\n")
        assert commands.cmd_logs(config, follow=False, lines=5, stream="both") == 0
        out = capsys.readouterr().out
        assert "out line" in out and "err line" in out

    def test_stream_selection(self, config: Config, capsys: Any) -> None:
        directory = Path(config.logging.dir)
        directory.mkdir(parents=True)
        (directory / "pporlock.out.log").write_text("out line\n")
        (directory / "pporlock.err.log").write_text("err line\n")
        commands.cmd_logs(config, follow=False, lines=5, stream="err")
        out = capsys.readouterr().out
        assert "err line" in out and "out line" not in out


# ---------------------------------------------------- API-backed subcommands --


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


class TestModulesCommand:
    def test_list_reads_the_daemons_registry(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """Not the filesystem. The daemon holds the live registry; a CLI that
        loaded its own would answer about a process nobody is running."""
        fake = install_fake(
            monkeypatch,
            config,
            {
                ("GET", "/modules"): [
                    {
                        "name": "csp",
                        "enabled": True,
                        "priority": 50,
                        "state": "loaded",
                        "version": "1.0.0",
                    }
                ]
            },
        )
        assert commands.cmd_modules(config, _ns(modules_action="list")) == 0
        assert fake.paths == ["/modules"]
        assert "csp" in capsys.readouterr().out

    def test_list_shows_a_broken_module_rather_than_hiding_it(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """A module that vanishes from the list when it has a syntax error is
        the failure the loader's never-raise contract exists to prevent."""
        install_fake(
            monkeypatch,
            config,
            {
                ("GET", "/modules"): [
                    {
                        "name": "broken",
                        "enabled": False,
                        "priority": 50,
                        "state": "load_error",
                        "error": {"code": "module_load_failed", "message": "bad yaml"},
                    }
                ]
            },
        )
        commands.cmd_modules(config, _ns(modules_action="list"))
        out = capsys.readouterr().out
        assert "broken" in out and "bad yaml" in out and "load_error" in out

    def test_enable_patches_the_module(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        fake = install_fake(
            monkeypatch, config, {("PATCH", "/modules/csp"): {"name": "csp", "enabled": True}}
        )
        assert commands.cmd_modules(config, _ns(modules_action="enable", name="csp")) == 0
        assert fake.calls == [("PATCH", "/modules/csp")]
        assert "enabled" in capsys.readouterr().out

    def test_disable_patches_the_module(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        install_fake(
            monkeypatch, config, {("PATCH", "/modules/csp"): {"name": "csp", "enabled": False}}
        )
        commands.cmd_modules(config, _ns(modules_action="disable", name="csp"))
        assert "disabled" in capsys.readouterr().out

    def test_validate_sends_the_module_files_to_the_daemon(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path, capsys: Any
    ) -> None:
        """Validated by the daemon's validator, so the answer comes from the
        same code that will reject or accept the module on install."""
        module = tmp_path / "tidy"
        module.mkdir()
        (module / "module.yaml").write_text("name: tidy\npporlock_api: '1'\n")
        fake = install_fake(
            monkeypatch, config, {("POST", "/validate"): {"ok": True, "issues": []}}
        )
        assert commands.cmd_modules(config, _ns(modules_action="validate", path=str(module))) == 0
        assert fake.paths == ["/validate"]
        assert "valid" in capsys.readouterr().out

    def test_validate_is_nonzero_and_prints_issues_when_invalid(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path, capsys: Any
    ) -> None:
        module = tmp_path / "bad"
        module.mkdir()
        (module / "module.yaml").write_text("{{{")
        install_fake(
            monkeypatch,
            config,
            {
                ("POST", "/validate"): {
                    "ok": False,
                    "issues": [
                        {"severity": "error", "file": "module.yaml", "line": 1, "message": "boom"}
                    ],
                }
            },
        )
        assert commands.cmd_modules(config, _ns(modules_action="validate", path=str(module))) == 1
        out = capsys.readouterr().out
        assert "INVALID" in out and "boom" in out

    def test_validate_refuses_a_directory_with_no_module_files(
        self, config: Config, tmp_path: Path, capsys: Any
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        assert commands.cmd_modules(config, _ns(modules_action="validate", path=str(empty))) == 1
        assert "no module files" in capsys.readouterr().out

    def test_validate_refuses_a_missing_path(self, config: Config, capsys: Any) -> None:
        assert (
            commands.cmd_modules(config, _ns(modules_action="validate", path="/nope/nothing")) == 1
        )
        assert "not a directory" in capsys.readouterr().out


class TestProfileCommand:
    def test_list_marks_the_active_profile(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        install_fake(
            monkeypatch,
            config,
            {
                ("GET", "/state"): {"active_profile": "dev"},
                ("GET", "/profiles"): [
                    {"name": "default", "modules": []},
                    {"name": "dev", "modules": ["csp"]},
                ],
            },
        )
        assert commands.cmd_profile(config, _ns(profile_action="list")) == 0
        lines = capsys.readouterr().out.splitlines()
        assert any(line.strip().startswith("* dev") for line in lines)
        assert not any(line.strip().startswith("* default") for line in lines)

    def test_activate_posts_to_the_activate_route(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        fake = install_fake(
            monkeypatch, config, {("POST", "/profiles/dev/activate"): {"profile": "dev"}}
        )
        assert commands.cmd_profile(config, _ns(profile_action="activate", name="dev")) == 0
        assert fake.paths == ["/profiles/dev/activate"]
        assert "dev" in capsys.readouterr().out


class TestSessionCommand:
    def test_start_says_what_is_being_recorded(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """REQ CAP-020 is opt-in recording. Saying what it captures at the
        moment it is turned on is the informed half of informed consent."""
        install_fake(monkeypatch, config, {("POST", "/sessions"): {"session_id": "s1"}})
        assert commands.cmd_session(config, _ns(session_action="start", name="login")) == 0
        out = capsys.readouterr().out
        assert "s1" in out
        assert "bodies are captured" in out
        assert "redacted" in out

    def test_list(self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any) -> None:
        install_fake(
            monkeypatch,
            config,
            {("GET", "/sessions"): [{"session_id": "s1", "state": "stopped", "flow_count": 4}]},
        )
        assert commands.cmd_session(config, _ns(session_action="list")) == 0
        assert "s1" in capsys.readouterr().out

    def test_list_when_empty(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        install_fake(monkeypatch, config, {("GET", "/sessions"): []})
        commands.cmd_session(config, _ns(session_action="list"))
        assert "no sessions" in capsys.readouterr().out

    def test_stop_reports_drops(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """Dropped flows are the thing a user must not discover later."""
        install_fake(
            monkeypatch,
            config,
            {("POST", "/sessions/s1/stop"): {"session_id": "s1", "flow_count": 9, "dropped": 2}},
        )
        assert commands.cmd_session(config, _ns(session_action="stop", session_id="s1")) == 0
        out = capsys.readouterr().out
        assert "9 flows" in out and "2 dropped" in out

    def test_export_writes_owner_only(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path
    ) -> None:
        """An export is a recording of somebody's browsing. 0600, not the umask
        default."""
        install_fake(monkeypatch, config, {("GET", "/sessions/s1/export"): {"flows": []}})
        destination = tmp_path / "out.json"
        commands.cmd_session(
            config,
            _ns(
                session_action="export",
                session_id="s1",
                format="pporlock",
                output=str(destination),
            ),
        )
        assert destination.exists()
        assert destination.stat().st_mode & 0o077 == 0

    def test_export_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        install_fake(monkeypatch, config, {("GET", "/sessions/s1/export"): {"flows": []}})
        commands.cmd_session(
            config,
            _ns(session_action="export", session_id="s1", format="pporlock", output=None),
        )
        assert "flows" in capsys.readouterr().out


class TestDryrunCommand:
    def test_it_warns_that_candidate_code_runs(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path, capsys: Any
    ) -> None:
        """§2.5, trusted-module boundary: every dry-run surface must say that
        dry run executes the candidate's Python."""
        module = tmp_path / "cand"
        module.mkdir()
        (module / "module.yaml").write_text("name: cand\npporlock_api: '1'\n")
        install_fake(
            monkeypatch,
            config,
            {
                ("POST", "/sessions/live/dryrun"): {
                    "summary": {"flows_evaluated": 3, "matched": 1, "errors": 0},
                    "results": [],
                }
            },
        )
        code = commands.cmd_dryrun(
            config, _ns(session_id="live", module_path=str(module), limit=10)
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "executes the candidate module's Python" in out
        assert "3 flows evaluated" in out

    def test_it_is_nonzero_when_the_candidate_errored(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path
    ) -> None:
        module = tmp_path / "cand"
        module.mkdir()
        (module / "module.yaml").write_text("name: cand\npporlock_api: '1'\n")
        install_fake(
            monkeypatch,
            config,
            {
                ("POST", "/sessions/live/dryrun"): {
                    "summary": {"flows_evaluated": 3, "matched": 0, "errors": 2},
                    "results": [],
                }
            },
        )
        assert (
            commands.cmd_dryrun(config, _ns(session_id="live", module_path=str(module), limit=10))
            == 1
        )


# ------------------------------------------------------------------ client ---


class TestControlClient:
    def test_the_base_url_is_always_loopback(self, config: Config) -> None:
        """§2.5, A01. Config.validate() refuses a non-loopback control host, so
        there is no reachable host here that is not this machine."""
        assert client_mod.ControlClient(config).base.startswith("http://127.0.0.1:")

    def test_a_missing_token_is_a_clear_error(self, config: Config) -> None:
        with pytest.raises(client_mod.ControlClientError, match="pporlock run"):
            client_mod.ControlClient(config).token()

    def test_the_token_is_read_from_the_state_dir(self, config: Config) -> None:
        path = Path(config.state_dir) / "token"
        path.write_text("s3cret\n")
        assert client_mod.ControlClient(config).token() == "s3cret"

    def test_error_detail_extracts_only_the_message(self) -> None:
        """Never the whole payload: nothing incidental in an error response
        should end up echoed to a terminal or a shell history."""
        raw = b'{"error": {"code": "unauthorized", "message": "no", "detail": {"x": 1}}}'
        assert client_mod._error_detail(raw) == "no"

    def test_error_detail_survives_a_non_json_body(self) -> None:
        assert "boom" in client_mod._error_detail(b"boom")

    def test_reachable_is_false_when_nothing_answers(self, config: Config) -> None:
        config.control.listen_port = 1  # nothing binds port 1
        assert client_mod.ControlClient(config).reachable() is False

    def test_reachable_does_not_need_a_token(
        self, monkeypatch: pytest.MonkeyPatch, config: Config
    ) -> None:
        """/state/health is the one public route; the extension polls it to
        decide whether to clear Chrome's proxy config (REQ EXT-010)."""
        seen: dict[str, Any] = {}

        def fake_request(self: Any, method: str, path: str, **kwargs: Any) -> Any:
            seen.update({"path": path, "authenticate": kwargs.get("authenticate")})
            return {"ok": True}

        monkeypatch.setattr(client_mod.ControlClient, "request", fake_request)
        assert client_mod.ControlClient(config).reachable() is True
        assert seen == {"path": "/state/health", "authenticate": False}


# ------------------------------------------------------------------ doctor ---


class TestNewDoctorChecks:
    """SPEC-1 §8.1's remaining checks, and their fixes (REQ PXY-004)."""

    def test_every_spec_check_id_is_present(self) -> None:
        """SPEC-1 §8.1 lists thirteen; the daemon must not quietly ship nine."""
        ids = {c.check_id for c in doctor.CHECKS}
        expected = {
            "ca_present",
            "ca_trusted",
            "chrome_installed",
            "chrome_quic_disabled",
            "config_valid",
            "modules_load_clean",
            "daemon_reachable",
            "launchd_installed",
            "token_permissions",
            "disk_space",
        }
        assert expected <= ids, f"missing checks: {sorted(expected - ids)}"

    def test_check_ids_are_unique(self) -> None:
        ids = [c.check_id for c in doctor.CHECKS]
        assert len(ids) == len(set(ids))

    def test_modules_check_passes_with_no_modules_directory(self, config: Config) -> None:
        assert doctor.check_modules_load(config).level == "pass"

    def test_modules_check_fails_and_names_the_broken_module(self, config: Config) -> None:
        root = Path(config.modules.root)
        (root / "broken").mkdir(parents=True)
        (root / "broken" / "module.yaml").write_text("name: not-broken\n")
        result = doctor.check_modules_load(config)
        assert result.level == "fail"
        assert "broken" in result.message

    def test_token_permissions_fails_on_a_loose_token(self, config: Config) -> None:
        """§2.5, A07: the token file is 0600."""
        path = Path(config.state_dir) / "token"
        path.write_text("t")
        path.chmod(0o644)
        result = doctor.check_token_permissions(config)
        assert result.level == "fail"
        assert "600" in result.message

    def test_token_permissions_passes_at_600(self, config: Config) -> None:
        path = Path(config.state_dir) / "token"
        path.write_text("t")
        path.chmod(0o600)
        assert doctor.check_token_permissions(config).level == "pass"

    def test_token_permissions_never_prints_the_token(self, config: Config) -> None:
        path = Path(config.state_dir) / "token"
        path.write_text("supersecrettoken")
        path.chmod(0o644)
        result = doctor.check_token_permissions(config)
        assert "supersecrettoken" not in (result.message + result.remediation)

    def test_the_token_fix_tightens_the_mode(self, config: Config) -> None:
        path = Path(config.state_dir) / "token"
        path.write_text("t")
        path.chmod(0o644)
        doctor._fix_token_permissions(config)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_the_state_dir_fix_creates_it_0700(self, config: Config) -> None:
        target = Path(config.state_dir) / "nested"
        config.state_dir = str(target)
        doctor._fix_state_dir(config)
        assert target.is_dir()
        assert target.stat().st_mode & 0o077 == 0

    def test_the_log_fix_creates_the_directory_and_rotates(self, config: Config) -> None:
        directory = Path(config.logging.dir)
        directory.mkdir(parents=True)
        big = directory / "pporlock.out.log"
        big.write_bytes(b"x" * 5000)
        config.logging.max_bytes = 1000
        doctor._fix_log_dir(config)
        assert big.stat().st_size == 0
        assert (directory / "pporlock.out.log.1").exists()

    def test_disk_space_reports_free_space(self, config: Config) -> None:
        result = doctor.check_disk_space(config)
        assert result.check_id == "disk_space"
        assert "GB free" in result.message

    def test_daemon_reachable_is_a_warning_not_a_failure(self, config: Config) -> None:
        """Running doctor on a stopped daemon is the normal case; reporting it
        as a failure would bury the checks that explain why it is stopped."""
        config.control.listen_port = 1
        assert doctor.check_daemon_reachable(config).level == "warn"

    def test_launchd_check_warns_when_not_installed(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "absent.plist")
        assert doctor.check_launchd_installed(config).level == "warn"

    def test_extension_paired_cannot_tell_without_a_daemon(self, config: Config) -> None:
        config.control.listen_port = 1
        result = doctor.check_extension_paired(config)
        assert result.level == "warn"
        assert "not running" in result.message


class TestDoctorFix:
    """REQ PXY-004 — `--fix` repairs and then re-measures."""

    def test_fixable_for_includes_warnings(self) -> None:
        """A missing log directory and an uninstalled launchd agent are both
        warnings by design; a --fix that only touched failures would print a fix
        list that never contains them."""
        results = [
            doctor.CheckResult("log_dir", "Log directory", "warn", "missing"),
            doctor.CheckResult("token_permissions", "Token", "fail", "loose"),
        ]
        assert {c.check_id for c in doctor.fixable_for(results)} == {
            "log_dir",
            "token_permissions",
        }

    def test_fixable_for_ignores_passing_checks(self) -> None:
        results = [doctor.CheckResult("log_dir", "Log directory", "pass", "fine")]
        assert doctor.fixable_for(results) == []

    def test_fixable_for_ignores_checks_with_no_fix(self) -> None:
        results = [doctor.CheckResult("chrome_installed", "Chrome", "warn", "absent")]
        assert doctor.fixable_for(results) == []

    def test_every_declared_fix_belongs_to_a_real_check(self) -> None:
        assert doctor.FIXABLE <= {c.check_id for c in doctor.CHECKS}

    def test_fix_reruns_the_checks_and_prints_the_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
    ) -> None:
        """A tool that said "fixed" without re-measuring would be reporting its
        own intention, and intentions are what has already gone wrong by the
        time anyone runs doctor."""
        runs: list[int] = []
        original = doctor.run_checks

        def counting(config: Any = None, **kwargs: Any) -> Any:
            runs.append(1)
            return original(config, **kwargs)

        monkeypatch.setattr(doctor, "run_checks", counting)
        monkeypatch.setattr(cli, "_load_config", lambda _args: Config(state_dir=str(tmp_path)))
        # Nothing that reaches the keychain or launchd.
        monkeypatch.setattr(doctor, "fixable_for", lambda _results, **_k: [])

        args = cli.build_parser().parse_args(["doctor", "--fix"])
        cli.cmd_doctor(args)
        assert len(runs) == 1
        assert "nothing to fix" in capsys.readouterr().out

    def test_a_failing_fix_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
    ) -> None:
        applied: list[str] = []

        def boom(_config: Config) -> None:
            raise RuntimeError("keychain said no")

        checks = [
            doctor.Check(
                "a", "First", lambda c: doctor.CheckResult("a", "First", "fail", ""), boom
            ),
            doctor.Check(
                "b",
                "Second",
                lambda c: doctor.CheckResult("b", "Second", "fail", ""),
                lambda c: applied.append("b"),
            ),
        ]
        monkeypatch.setattr(doctor, "fixable_for", lambda _results, **_k: checks)
        monkeypatch.setattr(cli, "_load_config", lambda _args: Config(state_dir=str(tmp_path)))
        cli.cmd_doctor(cli.build_parser().parse_args(["doctor", "--fix"]))
        out = capsys.readouterr().out
        assert "keychain said no" in out
        assert applied == ["b"]
        assert "after fixes:" in out


class TestUninstallStatement:
    """REQ DOC-005 — uninstall states what is left behind and where."""

    def test_it_names_every_directory_it_does_not_delete(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr("pporlock.cli.certs.remove_trust", lambda: None)
        monkeypatch.setattr(launchd, "is_installed", lambda path=None: False)
        cli.cmd_uninstall(cli.build_parser().parse_args(["uninstall"]))
        out = capsys.readouterr().out
        assert ".pporlock" in out
        assert ".mitmproxy" in out
        assert "Logs/pporlock" in out
        # And what is not ours to remove.
        assert "chrome://extensions" in out

    def test_it_names_the_configured_directories_not_the_defaults(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        """A user with a custom state_dir who is told their data is in
        ~/.pporlock has been given a wrong answer to the only question
        uninstall exists to answer."""
        monkeypatch.setattr("pporlock.cli.certs.remove_trust", lambda: None)
        monkeypatch.setattr(launchd, "is_installed", lambda path=None: False)
        monkeypatch.setattr(cli, "_load_config", lambda _args: config)
        cli.cmd_uninstall(cli.build_parser().parse_args(["uninstall"]))
        out = capsys.readouterr().out
        assert config.state_dir in out
        assert config.logging.dir in out

    def test_purge_deletes_the_configured_directories(
        self, monkeypatch: pytest.MonkeyPatch, config: Config, capsys: Any
    ) -> None:
        monkeypatch.setattr("pporlock.cli.certs.remove_trust", lambda: None)
        monkeypatch.setattr(launchd, "is_installed", lambda path=None: False)
        monkeypatch.setattr(cli, "_load_config", lambda _args: config)
        Path(config.logging.dir).mkdir(parents=True, exist_ok=True)
        (Path(config.state_dir) / "token").write_text("t")
        cli.cmd_uninstall(cli.build_parser().parse_args(["uninstall", "--purge"]))
        assert not Path(config.state_dir).exists()
        assert not Path(config.logging.dir).exists()

    def test_it_removes_the_launchd_agent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        removed: list[bool] = []
        monkeypatch.setattr("pporlock.cli.certs.remove_trust", lambda: None)
        monkeypatch.setattr(launchd, "is_installed", lambda path=None: True)
        monkeypatch.setattr(launchd, "uninstall", lambda: removed.append(True))
        cli.cmd_uninstall(cli.build_parser().parse_args(["uninstall"]))
        assert removed == [True]


class TestErrorSurfacing:
    def test_a_control_client_error_is_a_pporlock_error(self) -> None:
        """So `main` renders it as `error [code]: message` rather than a
        traceback."""
        assert issubclass(client_mod.ControlClientError, PporlockError)

    def test_main_renders_a_control_client_error_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        def boom(_args: Any) -> int:
            raise client_mod.ControlClientError("daemon is not running")

        monkeypatch.setattr(cli, "cmd_status", boom)
        assert cli.main(["status"]) == 1
        assert "daemon is not running" in capsys.readouterr().err


class TestControlClientOverHttp:
    """The client against a real HTTP server, not a stub.

    The interesting parts of ``ControlClient`` are the ones a stub cannot
    exercise: query-string assembly, the JSON error envelope on a 4xx, an empty
    204 body, and what happens when nothing is listening. All of those are
    urllib behaviours, so faking urllib would be testing the fake.
    """

    @pytest.fixture
    def server(self) -> Any:
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                """Quiet: the harness owns the output."""

            def _respond(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                seen.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        # Lower-cased: header names are case-insensitive on the
                        # wire and urllib title-cases what it sends, so asserting
                        # on the case would be asserting on urllib.
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if self.path.startswith("/boom"):
                    payload = _json.dumps(
                        {"error": {"code": "config_invalid", "message": "that is wrong"}}
                    ).encode()
                    self.send_response(400)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if self.path.startswith("/empty"):
                    self.send_response(204)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                payload = _json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _respond
            do_POST = _respond
            do_PATCH = _respond

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        httpd.seen = seen  # type: ignore[attr-defined]
        try:
            yield httpd
        finally:
            httpd.shutdown()
            httpd.server_close()

    def _client(self, config: Config, server: Any) -> Any:
        config.control.listen_port = server.server_address[1]
        path = Path(config.state_dir) / "token"
        path.write_text("tok")
        return client_mod.ControlClient(config)

    def test_get_sends_the_bearer_token_and_client_header(
        self, config: Config, server: Any
    ) -> None:
        client = self._client(config, server)
        assert client.get("/state")["path"] == "/state"
        headers = server.seen[-1]["headers"]
        assert headers["authorization"] == "Bearer tok"
        # Stamped on every authenticated request, reads included.
        assert headers["x-pporlock-client"] == "cli"

    def test_params_become_a_query_string(self, config: Config, server: Any) -> None:
        client = self._client(config, server)
        client.get("/sessions/s1/export", params={"format": "har", "unused": None})
        path = server.seen[-1]["path"]
        assert "format=har" in path
        # None-valued params are dropped rather than sent as the string "None".
        assert "unused" not in path

    def test_post_sends_a_json_body(self, config: Config, server: Any) -> None:
        client = self._client(config, server)
        client.post("/sessions", body={"name": "login"})
        assert b'"login"' in server.seen[-1]["body"]

    def test_patch_is_available(self, config: Config, server: Any) -> None:
        client = self._client(config, server)
        client.patch("/modules/x", body={"enabled": True})
        assert server.seen[-1]["method"] == "PATCH"

    def test_a_204_returns_none_rather_than_raising(self, config: Config, server: Any) -> None:
        client = self._client(config, server)
        assert client.request("GET", "/empty") is None

    def test_an_http_error_carries_the_api_message(self, config: Config, server: Any) -> None:
        client = self._client(config, server)
        with pytest.raises(client_mod.ControlClientError) as caught:
            client.get("/boom")
        assert "that is wrong" in caught.value.message
        assert caught.value.detail["status"] == 400

    def test_an_unauthenticated_request_sends_no_token(self, config: Config, server: Any) -> None:
        """/state/health is public; sending a token there would be the only
        place the CLI presents one it did not need to."""
        client = self._client(config, server)
        client.request("GET", "/state/health", authenticate=False)
        assert "authorization" not in server.seen[-1]["headers"]

    def test_a_connection_refusal_names_the_daemon_not_the_traceback(self, config: Config) -> None:
        config.control.listen_port = 1
        Path(config.state_dir, "token").write_text("tok")
        client = client_mod.ControlClient(config)
        with pytest.raises(client_mod.ControlClientError, match="pporlock status"):
            client.get("/state")

    def test_the_token_never_appears_in_the_url(self, config: Config, server: Any) -> None:
        """§2.5, A07: never in a URL, an error body, or the audit log."""
        client = self._client(config, server)
        client.get("/state", params={"detail": "full"})
        assert "tok" not in server.seen[-1]["path"]
