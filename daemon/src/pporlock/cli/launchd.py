"""launchd user agent — SPEC-1 §8.3, REQ PXY-002, PXY-007.

A **user** agent in ``~/Library/LaunchAgents``, never a system daemon in
``/Library/LaunchDaemons``. That is not a packaging preference. pporlock
terminates TLS for one person's browser and trusts its root in that person's
login keychain (REQ PXY-011); running it as root would widen the blast radius of
every one of those decisions to the whole machine, and would need an
administrator password to install a tool that otherwise needs none.

``launchctl`` has two generations of subcommands. The modern ones
(``bootstrap``/``bootout``/``kickstart``) take a domain target and report real
errors; the legacy ones (``load``/``unload``) are deprecated and silently
succeed in cases where nothing happened. We use the modern set and fall back to
the legacy set only when the modern one is unavailable, which is what a macOS
older than the ``gui/`` domain would do.
"""

from __future__ import annotations

import os
import plistlib

# `subprocess` is required: launchd is only reachable through `launchctl`.
# Every argv here is a fixed list built by this module, shell=False, and the
# only interpolated values are a path this module constructs and the current
# uid from os.getuid().
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..limits import DESIRED_NOFILE

LABEL = "com.pporlock.daemon"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / f"{LABEL}.plist"
DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "pporlock"

#: Seconds launchd waits before restarting a crashed agent. Without a throttle a
#: daemon that dies on a bad config file spins as fast as the kernel can fork it,
#: and the log fills with the same traceback thousands of times a minute.
THROTTLE_INTERVAL_S = 10


def plist_path() -> Path:
    """Where the agent definition lives.

    A function rather than a bare constant read at import. Every entry point
    below defaulted to the module-level ``PLIST_PATH`` in its signature, which
    Python binds once at *definition* time — so overriding it (in a test, or for
    a second install root) changed the constant and nothing that used it. The
    functions went on operating on the real ``~/Library/LaunchAgents`` while
    appearing to be redirected, which is the shape of a test that passes for the
    wrong reason and an install that writes somewhere nobody asked for.
    """
    return PLIST_PATH


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """What launchd currently believes about the agent."""

    installed: bool
    loaded: bool
    running: bool
    pid: int | None = None
    last_exit_code: int | None = None
    #: Resolved at construction, not bound at class-definition time — see
    #: ``plist_path()``.
    plist_path: Path = field(default_factory=lambda: plist_path())
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
            "last_exit_code": self.last_exit_code,
            "plist_path": str(self.plist_path),
            "detail": self.detail,
        }


class LaunchdError(RuntimeError):
    """A launchctl invocation failed. Carries what launchctl actually said."""


# ------------------------------------------------------------------ helpers --


def _run(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    # Fixed argv, shell=False, no user-supplied words; suppressed on the call.
    return subprocess.run(  # noqa: S603  # nosec B603
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def domain_target() -> str:
    """The launchd domain this agent lives in: the current user's GUI session."""
    return f"gui/{os.getuid()}"


def service_target() -> str:
    return f"{domain_target()}/{LABEL}"


def daemon_argv(config_path: Path | None = None) -> list[str]:
    """The command launchd runs.

    Prefers the installed ``pporlock`` console script, because that is what the
    user typed to install and what they will see in ``ps``. Falls back to
    ``python -m pporlock.cli.main``, which works from a source checkout where no
    console script has been placed on PATH.

    ``--config`` is carried through when the install was made with one. Without
    this the agent silently runs against ``~/.pporlock/config.yaml`` instead:
    ``pporlock --config X install --service`` would appear to install X and
    install something else, and the ports, state directory and log directory in
    the running daemon would not be the ones the user just configured. Found by
    installing the agent and reading the plist back.
    """
    head = (
        [str(Path(sys.executable).parent / "pporlock")]
        if (Path(sys.executable).parent / "pporlock").exists()
        else [sys.executable, "-m", "pporlock.cli.main"]
    )
    config = ["--config", str(Path(config_path).expanduser())] if config_path else []
    return [*head, *config, "run", "--quiet"]


def plist_dict(
    *,
    log_dir: Path | None = None,
    argv: list[str] | None = None,
    run_at_load: bool = True,
    config_path: Path | None = None,
) -> dict[str, object]:
    """The agent definition, as a dict, so a test can assert on it directly.

    ``KeepAlive`` is a dict rather than ``True``: a bare ``True`` restarts the
    agent even after a clean ``pporlock stop``, which makes stopping impossible.
    ``SuccessfulExit: False`` means "restart it unless it exited zero", which is
    the crash-restart REQ PXY-002 asks for and leaves a deliberate shutdown
    alone.
    """
    logs = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    return {
        "Label": LABEL,
        "ProgramArguments": list(argv) if argv is not None else daemon_argv(config_path),
        "RunAtLoad": run_at_load,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL_S,
        "StandardOutPath": str(logs / "pporlock.out.log"),
        "StandardErrorPath": str(logs / "pporlock.err.log"),
        "WorkingDirectory": str(Path.home()),
        # A GUI-session agent that talks to the browser. Not "Background", which
        # asks the scheduler to deprioritise it and shows up as latency on every
        # intercepted request.
        "ProcessType": "Interactive",
        # OI-36. launchd hands an agent macOS's 256-descriptor soft limit, and
        # an interception proxy holding two per flow exhausts that during
        # ordinary browsing. The daemon also raises this itself at startup —
        # both, because the plist covers a daemon launchd restarts after a
        # crash before any of our code runs, and the startup raise covers every
        # way of starting it that is not launchd at all.
        "SoftResourceLimits": {"NumberOfFiles": DESIRED_NOFILE},
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        },
    }


def write_plist(
    path: Path | None = None,
    *,
    log_dir: Path | None = None,
    argv: list[str] | None = None,
    run_at_load: bool = True,
    config_path: Path | None = None,
) -> Path:
    """Serialise the agent definition to disk. Returns the path written."""
    path = Path(path if path is not None else plist_path()).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = plist_dict(log_dir=log_dir, argv=argv, run_at_load=run_at_load, config_path=config_path)
    with path.open("wb") as handle:
        plistlib.dump(data, handle)
    path.chmod(0o644)
    ensure_log_dir(Path(str(data["StandardOutPath"])).parent)
    return path


def ensure_log_dir(directory: Path) -> Path:
    """Create the log directory 0700. It holds header lines, so not world-readable."""
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def is_installed(path: Path | None = None) -> bool:
    return Path(path if path is not None else plist_path()).expanduser().exists()


# ------------------------------------------------------------- launchctl ----


def _bootstrap(path: Path) -> subprocess.CompletedProcess[str]:
    result = _run(["launchctl", "bootstrap", domain_target(), str(path)])
    if result.returncode != 0 and _looks_unsupported(result):
        return _run(["launchctl", "load", "-w", str(path)])
    return result


def _bootout() -> subprocess.CompletedProcess[str]:
    result = _run(["launchctl", "bootout", service_target()])
    if result.returncode != 0 and _looks_unsupported(result):
        return _run(["launchctl", "unload", "-w", str(plist_path())])
    return result


def _looks_unsupported(result: subprocess.CompletedProcess[str]) -> bool:
    """Did launchctl reject the subcommand itself rather than the request?

    Distinguishing "this launchctl does not know `bootstrap`" from "bootstrap
    failed" matters: falling back to the deprecated verbs on a real failure
    would hide the failure behind a command that succeeds silently.
    """
    text = (result.stderr + result.stdout).lower()
    return "unrecognized" in text or "unknown subcommand" in text or "usage:" in text


def install(
    auto_start: bool = True,
    *,
    log_dir: Path | None = None,
    config_path: Path | None = None,
) -> Path:
    """Write the plist and bootstrap it into the user's GUI domain.

    Idempotent: an already-loaded agent is booted out first, so re-running
    install after editing the plist actually picks up the edit rather than
    leaving the old definition resident.
    """
    if is_installed():
        _bootout()
    path = write_plist(log_dir=log_dir, run_at_load=auto_start, config_path=config_path)
    if not auto_start:
        return path
    result = _bootstrap(path)
    if result.returncode != 0:
        raise LaunchdError(
            f"launchctl could not load {path}: "
            f"{result.stderr.strip() or result.stdout.strip() or result.returncode}"
        )
    return path


def uninstall() -> None:
    """Boot the agent out and delete the plist. Never raises on a missing agent."""
    if is_installed():
        _bootout()
    plist_path().unlink(missing_ok=True)


def start() -> None:
    if not is_installed():
        raise LaunchdError(
            f"no launchd agent at {plist_path()}. Run `pporlock install --service` first."
        )
    result = _bootstrap(plist_path())
    if result.returncode != 0 and "already" not in (result.stderr + result.stdout).lower():
        raise LaunchdError(result.stderr.strip() or result.stdout.strip() or "bootstrap failed")
    kick = _run(["launchctl", "kickstart", service_target()])
    if kick.returncode != 0 and not _looks_unsupported(kick):
        raise LaunchdError(kick.stderr.strip() or kick.stdout.strip() or "kickstart failed")


def stop() -> None:
    """Stop the agent without uninstalling it.

    ``bootout`` rather than ``kill``: the agent's KeepAlive would restart it
    after a kill, and a stop command that does not stop things is worse than no
    stop command.
    """
    result = _bootout()
    text = (result.stderr + result.stdout).lower()
    if result.returncode != 0 and "could not find" not in text and "no such" not in text:
        raise LaunchdError(result.stderr.strip() or result.stdout.strip() or "bootout failed")


def restart() -> None:
    stop()
    start()


def parse_print(text: str) -> tuple[int | None, int | None]:
    """Pull ``pid`` and ``last exit code`` out of ``launchctl print`` output.

    Parsed rather than trusted wholesale: the output is a large, unstable
    property dump, and pulling two named scalars out of it is far less likely to
    break across macOS releases than depending on its structure.
    """
    pid: int | None = None
    exit_code: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("pid = "):
            with_value = line[6:].strip()
            pid = int(with_value) if with_value.isdigit() else None
        elif line.startswith("last exit code = "):
            with_value = line[17:].strip()
            exit_code = int(with_value) if with_value.lstrip("-").isdigit() else None
    return pid, exit_code


def status() -> ServiceStatus:
    """What launchd thinks is going on. Never raises."""
    installed = is_installed()
    path = plist_path()
    result = _run(["launchctl", "print", service_target()], timeout=10.0)
    loaded = result.returncode == 0
    if not loaded:
        return ServiceStatus(
            installed=installed,
            loaded=False,
            running=False,
            plist_path=path,
            detail=("not loaded" if installed else f"not installed ({path} does not exist)"),
        )
    pid, exit_code = parse_print(result.stdout)
    return ServiceStatus(
        installed=installed,
        loaded=True,
        running=pid is not None,
        pid=pid,
        last_exit_code=exit_code,
        plist_path=path,
        detail="running" if pid is not None else "loaded, not running",
    )
