"""The subcommands that talk to a running daemon — SPEC-1 §8, REQ PXY-003.

`modules`, `profile`, `session` and `dryrun` are clients of the control API, not
second implementations. The daemon holds the live module registry and the live
rule set; a CLI that loaded its own would answer questions about a process
nobody is running.

Service control (`start`, `stop`, `restart`, `status`) and `logs` are the
exception: they act on launchd and on files, and are the commands you need
precisely when the daemon is *not* answering.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ..config import Config
from ..engine.modules.loader import WRITABLE_FILES
from ..errors import PporlockError
from . import launchd, logs
from .client import ControlClient

#: How long `pporlock start` waits for the daemon to answer /state/health.
START_WAIT_S = 10.0
START_POLL_S = 0.25


def _out(line: str = "") -> None:
    print(line, flush=True)


def module_files(path: Path) -> dict[str, str]:
    """The writable files of a module directory, for validate and dry run."""
    return {
        name: (path / name).read_text()
        for name in sorted(WRITABLE_FILES)
        if (path / name).is_file()
    }


# ------------------------------------------------------- service control ----


def cmd_start(config: Config) -> int:
    """Start the launchd agent and wait until the daemon actually answers.

    Waiting matters. ``launchctl kickstart`` returns as soon as launchd has
    forked, which is well before the proxy has bound a port — so a start command
    that returned there would report success for a daemon that goes on to die on
    a bad config file, and the user would find out from Chrome instead.
    """
    client = ControlClient(config)
    if client.reachable():
        _out("already running")
        return 0
    try:
        launchd.start()
    except launchd.LaunchdError as exc:
        _out(f"could not start: {exc}")
        return 1

    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        if client.reachable():
            _out(f"started; control API on {client.base}")
            return 0
        time.sleep(START_POLL_S)

    state = launchd.status()
    _out(f"launchd loaded the agent but it is not answering on {client.base}")
    if state.last_exit_code:
        _out(f"  last exit code {state.last_exit_code}")
    _out(f"  check {logs.log_dir(config.logging.dir) / 'pporlock.err.log'}")
    return 1


def cmd_stop(config: Config) -> int:
    try:
        launchd.stop()
    except launchd.LaunchdError as exc:
        _out(f"could not stop: {exc}")
        return 1
    _out("stopped")
    # REQ PXY-008 is discharged jointly: the daemon cannot reach into Chrome on
    # its way down, so the extension health-checks and reverts. Saying so here
    # is the difference between a user who waits and a user who knows why the
    # browser took a moment to come back.
    _out("the extension will clear Chrome's proxy settings on its next health check")
    return 0


def cmd_restart(config: Config) -> int:
    try:
        launchd.stop()
    except launchd.LaunchdError as exc:
        _out(f"could not stop: {exc}")
        return 1
    return cmd_start(config)


def cmd_status(config: Config) -> int:
    """Report both halves: what launchd thinks, and what the daemon answers.

    They disagree in exactly the interesting case — the agent is loaded, the
    process is up, and the control API is wedged or listening somewhere else.
    Reporting only one of them is how that case looks like success.
    """
    state = launchd.status()
    client = ControlClient(config)
    reachable = client.reachable()

    _out("pporlock status")
    _out(
        f"  launchd agent   {'installed' if state.installed else 'not installed'}"
        f"  ({state.plist_path})"
    )
    _out(f"  launchd state   {state.detail}" + (f", pid {state.pid}" if state.pid else ""))
    if state.last_exit_code:
        _out(f"  last exit       {state.last_exit_code}")
    _out(f"  control API     {'reachable' if reachable else 'not reachable'} at {client.base}")

    if not reachable:
        return 1

    try:
        payload = client.get("/state")
    except PporlockError as exc:
        _out(f"  (could not read /state: {exc.message})")
        return 1

    proxy = payload.get("proxy", {}) if isinstance(payload, dict) else {}
    counters = payload.get("counters", {}) if isinstance(payload, dict) else {}
    modules = payload.get("modules", {}) if isinstance(payload, dict) else {}
    _out(
        f"  proxy           {'running' if proxy.get('running') else 'stopped'} "
        f"on {proxy.get('listen', '?')}"
    )
    _out(f"  profile         {payload.get('active_profile', '?')}")
    _out(
        f"  modules         {modules.get('enabled', 0)} enabled of "
        f"{modules.get('loaded', 0)}"
        + (f", {modules.get('quarantined', 0)} quarantined" if modules.get("quarantined") else "")
    )
    _out(
        f"  flows           {counters.get('flows_total', 0)} total, "
        f"{counters.get('modified', 0)} modified, {counters.get('blocked', 0)} blocked"
    )
    return 0


def cmd_logs(config: Config, *, follow: bool, lines: int, stream: str) -> int:
    """Show the daemon's log files (REQ PXY-003, PXY-007)."""
    directory = logs.log_dir(config.logging.dir)
    wanted = {
        "out": ["pporlock.out.log"],
        "err": ["pporlock.err.log"],
        "both": list(logs.LOG_NAMES),
    }[stream]
    paths = [directory / name for name in wanted]
    present = [p for p in paths if p.exists()]
    if not present:
        _out(f"no logs in {directory}")
        _out(
            "The daemon only writes files when run under launchd; "
            "`pporlock run` writes to this terminal."
        )
        return 1

    for path in present:
        if len(present) > 1:
            _out(f"==> {path} <==")
        for line in logs.tail(path, lines):
            _out(line)

    if not follow:
        return 0

    offsets = {p: p.stat().st_size for p in present}
    try:
        while True:
            for path in present:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                # A rotation truncates in place, so the file gets *smaller*.
                # Without this the reader would sit past the end of a file that
                # is being written from zero again and show nothing forever.
                if size < offsets[path]:
                    offsets[path] = 0
                if size == offsets[path]:
                    continue
                with path.open("r", errors="replace") as handle:
                    handle.seek(offsets[path])
                    chunk = handle.read()
                    offsets[path] = handle.tell()
                sys.stdout.write(chunk)
                sys.stdout.flush()
            time.sleep(0.4)
    except KeyboardInterrupt:
        return 130


# ------------------------------------------------------------- modules ------


def cmd_modules(config: Config, args: argparse.Namespace) -> int:
    action = args.modules_action
    if action == "validate":
        return _modules_validate(config, Path(args.path).expanduser())

    client = ControlClient(config)
    if action == "list":
        modules = client.get("/modules")
        if not modules:
            _out("no modules")
            return 0
        for module in modules:
            mark = "on " if module.get("enabled") else "off"
            state = module.get("state", "?")
            note = "" if state == "loaded" else f"  [{state}]"
            _out(
                f"  {mark}  {module.get('name'):<28} p{module.get('priority', 0):<5}"
                f" {module.get('version', '')}{note}"
            )
            error = module.get("error")
            if error:
                _out(f"        ! {error.get('code')}: {error.get('message')}")
        return 0

    enabled = action == "enable"
    module = client.patch(f"/modules/{args.name}", body={"enabled": enabled})
    _out(f"{module.get('name')}: {'enabled' if module.get('enabled') else 'disabled'}")
    return 0


def _modules_validate(config: Config, path: Path) -> int:
    """Validate a module directory (REQ API-027).

    Sent to the daemon rather than validated locally, so the answer comes from
    the same validator the daemon will use when the module is installed. If no
    daemon is running there is nothing useful to say — a second validator here
    could pass a module the daemon then rejects.
    """
    if not path.is_dir():
        _out(f"{path} is not a directory")
        return 1
    files = module_files(path)
    if not files:
        _out(f"{path} contains no module files ({', '.join(sorted(WRITABLE_FILES))})")
        return 1

    client = ControlClient(config)
    result = client.post("/validate", body={"name": path.name, "files": files})
    for issue in result.get("issues", []):
        where = issue.get("file") or ""
        line = f":{issue['line']}" if issue.get("line") else ""
        _out(f"  [{issue.get('severity', 'error')}] {where}{line} {issue.get('message', '')}")
    ok = bool(result.get("ok"))
    _out(f"{path.name}: {'valid' if ok else 'INVALID'}")
    return 0 if ok else 1


# ------------------------------------------------------------- profiles -----


def cmd_profile(config: Config, args: argparse.Namespace) -> int:
    client = ControlClient(config)
    if args.profile_action == "list":
        state = client.get("/state")
        active = state.get("active_profile") if isinstance(state, dict) else None
        for profile in client.get("/profiles"):
            mark = "*" if profile.get("name") == active else " "
            _out(f"  {mark} {profile.get('name'):<24} {len(profile.get('modules') or [])} modules")
        return 0

    result = client.post(f"/profiles/{args.name}/activate")
    _out(f"active profile: {result.get('profile', args.name)}")
    return 0


# ------------------------------------------------------------- sessions -----


def cmd_session(config: Config, args: argparse.Namespace) -> int:
    client = ControlClient(config)
    action = args.session_action

    if action == "list":
        sessions = client.get("/sessions")
        if not sessions:
            _out("no sessions")
            return 0
        for meta in sessions:
            _out(
                f"  {meta.get('session_id'):<26} {meta.get('state', '?'):<9} "
                f"{meta.get('flow_count', 0):>6} flows  {meta.get('name', '')}"
            )
        return 0

    if action == "start":
        meta = client.post("/sessions", body={"name": args.name or ""})
        _out(f"recording {meta.get('session_id')}")
        # Recording is opt-in and holds request and response bodies. Saying what
        # was turned on, at the moment it is turned on, is the whole of the
        # informed part of informed consent (REQ CAP-020).
        _out("  bodies are captured; secrets are redacted at write time (REQ CAP-045)")
        return 0

    if action == "stop":
        meta = client.post(f"/sessions/{args.session_id}/stop")
        _out(
            f"stopped {meta.get('session_id')}: {meta.get('flow_count', 0)} flows, "
            f"{meta.get('dropped', 0)} dropped"
        )
        return 0

    payload = client.get(f"/sessions/{args.session_id}/export", params={"format": args.format})
    text = json.dumps(payload, indent=2)
    if args.output:
        destination = Path(args.output).expanduser()
        destination.write_text(text)
        destination.chmod(0o600)
        _out(f"wrote {destination}")
    else:
        _out(text)
    return 0


# --------------------------------------------------------------- dryrun -----


def cmd_dryrun(config: Config, args: argparse.Namespace) -> int:
    """Replay a session through a candidate module (REQ CAP-030-032).

    Prints the warning every dry-run surface prints, because this *executes the
    candidate module's Python*. A module written by an agent is code that agent
    wanted to run on this machine, and "dry" describes the traffic, not the code.
    """
    path = Path(args.module_path).expanduser()
    if not path.is_dir():
        _out(f"{path} is not a directory")
        return 1
    files = module_files(path)
    if not files:
        _out(f"{path} contains no module files")
        return 1

    _out(f"dry run: {path.name} against session {args.session_id}")
    _out("  NOTE: this executes the candidate module's Python hooks on this machine.")

    client = ControlClient(config)
    result = client.post(
        f"/sessions/{args.session_id}/dryrun",
        body={"modules": [{"name": path.name, "files": files}], "limit": args.limit},
    )
    summary = result.get("summary", {})
    _out(
        f"  {summary.get('flows_evaluated', 0)} flows evaluated, "
        f"{summary.get('matched', 0)} matched, {summary.get('errors', 0)} errors"
    )
    for entry in result.get("results", [])[: args.limit]:
        diff = entry.get("diff") or {}
        changes = len(diff.get("headers") or []) + (1 if diff.get("body") else 0)
        if changes:
            _out(f"    {entry.get('url', '')[:80]}  {changes} change(s)")
    return 0 if not summary.get("errors") else 1
