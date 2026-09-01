"""The launchd user agent and log rotation — REQ PXY-002, PXY-007.

The plist is checked two ways. Its structure is asserted directly against
``plist_dict``, and the file that is written is put through ``plutil -lint``,
because a plist that Python is happy to serialise and launchd refuses to parse
is exactly the failure that only shows up at install time on someone else's
machine.

Nothing here talks to the real launchd. ``launchctl`` is faked at the
``_run`` seam, so the tests assert *which* subcommands are issued and in what
order — which is the part that is wrong when a service command silently does
nothing.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pporlock.cli import launchd, logs


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeLaunchctl:
    """Records the argv of every launchctl call and answers with a script."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, args: list[str], timeout: float = 30.0) -> Any:
        self.calls.append(list(args))
        return self.answers.get(args[1], _completed())

    @property
    def subcommands(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLaunchctl:
    stub = FakeLaunchctl()
    monkeypatch.setattr(launchd, "_run", stub)
    return stub


# ------------------------------------------------------------------- plist ---


class TestPlist:
    def test_it_is_a_user_agent_not_a_system_daemon(self) -> None:
        """REQ PXY-002. A system daemon would need root and would put a trusted
        MITM root's blast radius on the whole machine rather than one account."""
        assert launchd.LAUNCH_AGENTS_DIR == Path.home() / "Library" / "LaunchAgents"
        assert "LaunchDaemons" not in str(launchd.PLIST_PATH)
        assert launchd.PLIST_PATH.parent == launchd.LAUNCH_AGENTS_DIR

    def test_it_targets_the_users_own_gui_domain(self) -> None:
        assert launchd.domain_target() == f"gui/{os.getuid()}"
        assert launchd.service_target().endswith(f"/{launchd.LABEL}")

    def test_run_at_load_and_crash_restart(self) -> None:
        """REQ PXY-002: starts at login, restarts on crash."""
        data = launchd.plist_dict()
        assert data["RunAtLoad"] is True
        assert data["KeepAlive"] == {"SuccessfulExit": False}
        assert data["ThrottleInterval"] == launchd.THROTTLE_INTERVAL_S

    def test_the_agent_gets_descriptor_headroom(self) -> None:
        """OI-36. launchd hands an agent macOS's 256-descriptor soft limit, and
        an interception proxy holding two per flow exhausts that during ordinary
        browsing — at which point everything that opens a file fails in the
        words of whatever tried to open it.

        Belt and braces with the raise in `run_foreground`, deliberately: the
        plist covers the daemon launchd restarts after a crash, before any of
        our code runs, and the startup raise covers every way of starting it
        that is not launchd.
        """
        from pporlock.limits import DESIRED_NOFILE

        limits = launchd.plist_dict()["SoftResourceLimits"]
        assert isinstance(limits, dict)
        assert limits["NumberOfFiles"] == DESIRED_NOFILE
        assert DESIRED_NOFILE > 256

    def test_keepalive_is_not_a_bare_true(self) -> None:
        """A bare `KeepAlive: true` restarts the agent after a deliberate stop,
        which makes `pporlock stop` a command that does not stop anything."""
        assert launchd.plist_dict()["KeepAlive"] is not True

    def test_logs_go_under_library_logs(self, tmp_path: Path) -> None:
        """REQ PXY-007."""
        data = launchd.plist_dict(log_dir=tmp_path)
        assert data["StandardOutPath"] == str(tmp_path / "pporlock.out.log")
        assert data["StandardErrorPath"] == str(tmp_path / "pporlock.err.log")
        assert launchd.DEFAULT_LOG_DIR.parts[-3:] == ("Library", "Logs", "pporlock")

    def test_the_argv_runs_the_proxy_quietly(self) -> None:
        argv = launchd.plist_dict()["ProgramArguments"]
        assert isinstance(argv, list)
        assert argv[-2:] == ["run", "--quiet"]

    def test_written_plist_round_trips(self, tmp_path: Path) -> None:
        path = launchd.write_plist(tmp_path / "agent.plist", log_dir=tmp_path / "logs")
        assert plistlib.loads(path.read_bytes())["Label"] == launchd.LABEL

    def test_written_plist_is_not_group_or_world_writable(self, tmp_path: Path) -> None:
        path = launchd.write_plist(tmp_path / "agent.plist", log_dir=tmp_path / "logs")
        assert path.stat().st_mode & 0o022 == 0

    def test_writing_creates_the_log_directory_private(self, tmp_path: Path) -> None:
        """Logs carry redacted headers, so 0700 rather than the umask default."""
        log_dir = tmp_path / "logs"
        launchd.write_plist(tmp_path / "agent.plist", log_dir=log_dir)
        assert log_dir.is_dir()
        assert log_dir.stat().st_mode & 0o077 == 0

    @pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
    def test_plutil_accepts_it(self, tmp_path: Path) -> None:
        """REQ PXY-002 — launchd's own parser, not Python's.

        plistlib will happily serialise something launchd rejects. This is the
        only check in the suite that asks the platform.
        """
        path = launchd.write_plist(tmp_path / "agent.plist", log_dir=tmp_path / "logs")
        result = subprocess.run(  # noqa: S603
            ["plutil", "-lint", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS-only")
    def test_plutil_reads_back_every_key(self, tmp_path: Path) -> None:
        path = launchd.write_plist(tmp_path / "agent.plist", log_dir=tmp_path / "logs")
        result = subprocess.run(  # noqa: S603
            ["plutil", "-convert", "json", "-o", "-", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        import json

        parsed = json.loads(result.stdout)
        assert parsed["Label"] == launchd.LABEL
        assert parsed["KeepAlive"] == {"SuccessfulExit": False}


# --------------------------------------------------------------- launchctl ---


class TestServiceControl:
    def test_install_bootstraps_into_the_gui_domain(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "agent.plist")
        launchd.install(log_dir=tmp_path / "logs")
        assert fake.subcommands == ["bootstrap"]
        assert fake.calls[0][2] == launchd.domain_target()

    def test_install_is_idempotent(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Re-installing after editing the plist must pick the edit up, which
        means booting the resident definition out first."""
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.install(log_dir=tmp_path / "logs")
        fake.calls.clear()
        launchd.install(log_dir=tmp_path / "logs")
        assert fake.subcommands == ["bootout", "bootstrap"]

    def test_install_without_auto_start_writes_but_does_not_load(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.install(auto_start=False, log_dir=tmp_path / "logs")
        assert plist.exists()
        assert fake.subcommands == []
        assert plistlib.loads(plist.read_bytes())["RunAtLoad"] is False

    def test_install_raises_with_what_launchctl_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub = FakeLaunchctl({"bootstrap": _completed(5, stderr="Load failed: 5: Input/output")})
        monkeypatch.setattr(launchd, "_run", stub)
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "agent.plist")
        with pytest.raises(launchd.LaunchdError, match="Input/output"):
            launchd.install(log_dir=tmp_path / "logs")

    def test_uninstall_removes_the_plist(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.install(log_dir=tmp_path / "logs")
        launchd.uninstall()
        assert not plist.exists()
        assert "bootout" in fake.subcommands

    def test_uninstall_on_a_missing_agent_is_silent(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "absent.plist")
        launchd.uninstall()
        assert fake.calls == []

    def test_start_requires_an_installed_agent(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "absent.plist")
        with pytest.raises(launchd.LaunchdError, match="install --service"):
            launchd.start()

    def test_start_bootstraps_then_kickstarts(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.write_plist(plist, log_dir=tmp_path / "logs")
        launchd.start()
        assert fake.subcommands == ["bootstrap", "kickstart"]

    def test_start_tolerates_an_already_loaded_agent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub = FakeLaunchctl({"bootstrap": _completed(17, stderr="service already bootstrapped")})
        monkeypatch.setattr(launchd, "_run", stub)
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.write_plist(plist, log_dir=tmp_path / "logs")
        launchd.start()
        assert stub.subcommands == ["bootstrap", "kickstart"]

    def test_stop_boots_out_rather_than_killing(self, fake: FakeLaunchctl) -> None:
        """KeepAlive would restart a killed process, so a kill-based stop would
        not stop anything."""
        launchd.stop()
        assert fake.subcommands == ["bootout"]

    def test_stop_is_not_an_error_when_nothing_is_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = FakeLaunchctl({"bootout": _completed(3, stderr="Could not find service")})
        monkeypatch.setattr(launchd, "_run", stub)
        launchd.stop()

    def test_stop_raises_on_a_real_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = FakeLaunchctl({"bootout": _completed(9, stderr="Operation not permitted")})
        monkeypatch.setattr(launchd, "_run", stub)
        with pytest.raises(launchd.LaunchdError, match="not permitted"):
            launchd.stop()

    def test_legacy_fallback_only_on_an_unknown_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Falling back to the deprecated verbs on a *real* failure would hide
        the failure behind a command that succeeds silently."""
        stub = FakeLaunchctl({"bootstrap": _completed(64, stderr="Unrecognized subcommand")})
        monkeypatch.setattr(launchd, "_run", stub)
        plist = tmp_path / "agent.plist"
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.write_plist(plist, log_dir=tmp_path / "logs")
        launchd.install(log_dir=tmp_path / "logs")
        # bootout first because the plist already exists — install is idempotent.
        assert stub.subcommands == ["bootout", "bootstrap", "load"]

    def test_no_legacy_fallback_on_a_substantive_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub = FakeLaunchctl({"bootstrap": _completed(5, stderr="Load failed: 5")})
        monkeypatch.setattr(launchd, "_run", stub)
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "agent.plist")
        with pytest.raises(launchd.LaunchdError):
            launchd.install(log_dir=tmp_path / "logs")
        assert "load" not in stub.subcommands


class TestStatus:
    PRINT_OUTPUT = """
    com.pporlock.daemon = {
        active count = 1
        pid = 4242
        last exit code = 0
        state = running
    }
    """

    def test_parses_pid_and_exit_code(self) -> None:
        assert launchd.parse_print(self.PRINT_OUTPUT) == (4242, 0)

    def test_parses_a_negative_exit_code(self) -> None:
        assert launchd.parse_print("last exit code = -1")[1] == -1

    def test_tolerates_a_symbolic_pid(self) -> None:
        """launchd prints `pid = (never exited)` in some states."""
        assert launchd.parse_print("pid = (none)") == (None, None)

    def test_not_installed(
        self, fake: FakeLaunchctl, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launchd, "PLIST_PATH", tmp_path / "absent.plist")
        fake.answers["print"] = _completed(113, stderr="Could not find service")
        state = launchd.status()
        assert state.installed is False
        assert state.loaded is False
        assert state.running is False

    def test_running(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        plist = tmp_path / "agent.plist"
        stub = FakeLaunchctl({"print": _completed(0, stdout=self.PRINT_OUTPUT)})
        monkeypatch.setattr(launchd, "_run", stub)
        monkeypatch.setattr(launchd, "PLIST_PATH", plist)
        launchd.write_plist(plist, log_dir=tmp_path / "logs")
        state = launchd.status()
        assert state.installed and state.loaded and state.running
        assert state.pid == 4242
        assert state.to_dict()["pid"] == 4242

    def test_status_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = FakeLaunchctl({"print": _completed(1, stderr="boom")})
        monkeypatch.setattr(launchd, "_run", stub)
        assert launchd.status().running is False


# ------------------------------------------------------------------- logs ----


class TestLogRotation:
    """REQ PXY-007 — rotation by size with a retained-file count."""

    def _write(self, path: Path, size: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    def test_a_small_file_is_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / "pporlock.out.log"
        self._write(path, 100)
        assert logs.rotate_file(path, max_bytes=1000) is False
        assert path.stat().st_size == 100

    def test_rotation_truncates_in_place_rather_than_renaming(self, tmp_path: Path) -> None:
        """launchd holds this descriptor open for the life of the agent. A
        rename leaves it writing into the renamed inode, so the live file stays
        empty forever and the rotated one keeps growing."""
        path = tmp_path / "pporlock.out.log"
        self._write(path, 2000)
        inode_before = path.stat().st_ino

        assert logs.rotate_file(path, max_bytes=1000) is True

        assert path.exists()
        assert path.stat().st_ino == inode_before
        assert path.stat().st_size == 0
        assert (tmp_path / "pporlock.out.log.1").stat().st_size == 2000

    def test_generations_shift_down(self, tmp_path: Path) -> None:
        path = tmp_path / "pporlock.out.log"
        for generation in range(1, 4):
            self._write(path, 2000)
            logs.rotate_file(path, max_bytes=1000, retain=3)
            assert (tmp_path / f"pporlock.out.log.{generation}").exists()

    def test_the_oldest_generation_falls_off(self, tmp_path: Path) -> None:
        """Otherwise "rotation" is just a slower way to fill the disk."""
        path = tmp_path / "pporlock.out.log"
        for marker in range(5):
            path.write_bytes(bytes([marker]) * 2000)
            logs.rotate_file(path, max_bytes=1000, retain=2)
        assert not (tmp_path / "pporlock.out.log.3").exists()
        assert (tmp_path / "pporlock.out.log.1").exists()
        assert (tmp_path / "pporlock.out.log.2").exists()

    def test_rotated_files_are_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "pporlock.out.log"
        self._write(path, 2000)
        logs.rotate_file(path, max_bytes=1000)
        assert (tmp_path / "pporlock.out.log.1").stat().st_mode & 0o077 == 0

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """Rotation is housekeeping; housekeeping that can take the daemon down
        is worse than a large log."""
        assert logs.rotate_file(tmp_path / "nothing.log", max_bytes=1) is False

    def test_rotate_covers_both_streams(self, tmp_path: Path) -> None:
        for name in logs.LOG_NAMES:
            self._write(tmp_path / name, 2000)
        rotated = logs.rotate(tmp_path, max_bytes=1000)
        assert {p.name for p in rotated} == set(logs.LOG_NAMES)

    def test_log_dir_defaults_under_library_logs(self) -> None:
        assert logs.log_dir().parts[-3:] == ("Library", "Logs", "pporlock")

    def test_log_dir_honours_configuration(self, tmp_path: Path) -> None:
        assert logs.log_dir(str(tmp_path)) == tmp_path


class TestTail:
    def test_returns_the_last_n_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "a.log"
        path.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
        assert logs.tail(path, 3) == ["line 497", "line 498", "line 499"]

    def test_handles_a_file_shorter_than_the_request(self, tmp_path: Path) -> None:
        path = tmp_path / "a.log"
        path.write_text("only\n")
        assert logs.tail(path, 50) == ["only"]

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert logs.tail(tmp_path / "nope.log") == []

    def test_does_not_read_the_whole_file(self, tmp_path: Path) -> None:
        """A rotation threshold of 8 MiB means readlines() would pull megabytes
        into memory to show fifty lines."""
        path = tmp_path / "big.log"
        path.write_text("\n".join(f"line {i}" for i in range(200_000)) + "\n")
        assert logs.tail(path, 2) == ["line 199998", "line 199999"]

    def test_undecodable_bytes_do_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "a.log"
        path.write_bytes(b"good\n\xff\xfe bad\n")
        assert len(logs.tail(path, 5)) == 2


class TestConfigPassthrough:
    """`--config` reaches the agent, or the agent runs a different daemon.

    Found by installing the agent and reading the plist back: the argv carried
    `run --quiet` and nothing else, so `pporlock --config X install --service`
    appeared to install X and installed the default. The ports, state directory
    and log directory of the running daemon would all have been the wrong ones.
    """

    def test_argv_carries_the_config_path(self, tmp_path: Path) -> None:
        argv = launchd.daemon_argv(tmp_path / "config.yaml")
        assert "--config" in argv
        assert argv.index("--config") < argv.index("run")
        assert argv[argv.index("--config") + 1] == str(tmp_path / "config.yaml")

    def test_argv_omits_config_when_none_was_given(self) -> None:
        assert "--config" not in launchd.daemon_argv()

    def test_the_written_plist_carries_it(self, tmp_path: Path) -> None:
        path = launchd.write_plist(
            tmp_path / "agent.plist",
            log_dir=tmp_path / "logs",
            config_path=tmp_path / "config.yaml",
        )
        argv = plistlib.loads(path.read_bytes())["ProgramArguments"]
        assert str(tmp_path / "config.yaml") in argv
