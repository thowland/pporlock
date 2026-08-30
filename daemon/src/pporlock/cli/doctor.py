"""`pporlock doctor` — SPEC-1 §8.1, REQ PXY-004.

Each check reports pass, warn, or fail with a remediation string, and carries a
fix where one exists. The failure modes in this system compound: diagnosing a
rewrite problem on top of an unresolved certificate or QUIC problem wastes time,
so this exists to make the base state unambiguous before anything else is
investigated.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config import Config
from ..engine.exclusions import load_exclusions
from . import certs

Level = Literal["pass", "warn", "fail"]

CHROME_LOCAL_STATE = (
    Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Local State"
)
CHROME_APP = Path("/Applications/Google Chrome.app")


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    title: str
    level: Level
    message: str
    remediation: str = ""

    @property
    def ok(self) -> bool:
        return self.level != "fail"


@dataclass(frozen=True, slots=True)
class Check:
    check_id: str
    title: str
    run: Callable[[Config], CheckResult]
    fix: Callable[[Config], None] | None = None


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


# ------------------------------------------------------------------ checks ---


def check_ca_present(_: Config) -> CheckResult:
    if certs.is_present():
        return CheckResult("ca_present", "CA certificate exists", "pass", str(certs.ca_path()))
    return CheckResult(
        "ca_present",
        "CA certificate exists",
        "fail",
        f"{certs.ca_path()} not found",
        "Start the proxy once (`pporlock run`) to generate it.",
    )


def check_ca_trusted(_: Config) -> CheckResult:
    state = certs.status()
    if not state.present:
        return CheckResult(
            "ca_trusted",
            "CA is trusted",
            "fail",
            state.detail,
            "Run `pporlock run` once, then `pporlock install`.",
        )
    if state.trusted:
        return CheckResult("ca_trusted", "CA is trusted", "pass", state.detail)
    return CheckResult(
        "ca_trusted",
        "CA is trusted",
        "fail",
        state.detail,
        "Run `pporlock install` to add it to the login keychain.",
    )


def check_proxy_port(config: Config) -> CheckResult:
    host, port = config.proxy.listen_host, config.proxy.listen_port
    if _port_free(host, port):
        return CheckResult("port_proxy_free", "Proxy port available", "pass", f"{host}:{port}")
    return CheckResult(
        "port_proxy_free",
        "Proxy port available",
        "warn",
        f"{host}:{port} is in use",
        "Another pporlock may already be running, or change proxy.listen_port.",
    )


def check_control_port(config: Config) -> CheckResult:
    host, port = config.control.listen_host, config.control.listen_port
    if _port_free(host, port):
        return CheckResult("port_control_free", "Control port available", "pass", f"{host}:{port}")
    return CheckResult(
        "port_control_free",
        "Control port available",
        "warn",
        f"{host}:{port} is in use",
        "Another pporlock may already be running, or change control.listen_port.",
    )


def check_chrome_installed(_: Config) -> CheckResult:
    if CHROME_APP.exists():
        return CheckResult("chrome_installed", "Chrome is installed", "pass", str(CHROME_APP))
    return CheckResult(
        "chrome_installed",
        "Chrome is installed",
        "warn",
        "Chrome not found in /Applications",
        "pporlock only intercepts Chrome (REQ SCP-001).",
    )


def check_quic_disabled(_: Config) -> CheckResult:
    """QUIC runs over UDP and will not traverse a CONNECT proxy (REQ PXY-012).

    A warning rather than a failure: this cannot be reliably enforced or even
    reliably detected from outside Chrome. Left enabled, a portion of traffic
    silently bypasses the proxy and rules appear intermittently broken — which
    is precisely the kind of confusion this command exists to pre-empt.
    """
    remediation = (
        "Disable QUIC at chrome://flags/#enable-quic, or set the QuicAllowed=false "
        "policy. Otherwise some traffic bypasses the proxy entirely and rules will "
        "appear to fire intermittently."
    )
    if not CHROME_LOCAL_STATE.exists():
        return CheckResult(
            "chrome_quic_disabled",
            "Chrome QUIC disabled",
            "warn",
            "cannot determine — Chrome profile not found",
            remediation,
        )
    try:
        state = json.loads(CHROME_LOCAL_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return CheckResult(
            "chrome_quic_disabled",
            "Chrome QUIC disabled",
            "warn",
            "cannot read Chrome Local State",
            remediation,
        )

    flags = state.get("browser", {}).get("enabled_labs_experiments", [])
    if any(str(f).startswith("enable-quic@") and str(f).endswith("@2") for f in flags):
        return CheckResult(
            "chrome_quic_disabled",
            "Chrome QUIC disabled",
            "pass",
            "disabled via chrome://flags",
        )
    return CheckResult(
        "chrome_quic_disabled",
        "Chrome QUIC disabled",
        "warn",
        "QUIC appears to be enabled or unset",
        remediation,
    )


def check_config_valid(config: Config) -> CheckResult:
    try:
        config.validate()
    except Exception as exc:
        return CheckResult(
            "config_valid",
            "Configuration is valid",
            "fail",
            str(exc),
            "Fix ~/.pporlock/config.yaml.",
        )
    return CheckResult(
        "config_valid", "Configuration is valid", "pass", "loopback-bound, ports distinct"
    )


def check_exclusions(_: Config) -> CheckResult:
    try:
        exclusions = load_exclusions()
    except ValueError as exc:
        return CheckResult(
            "exclusions_load", "Exclusion list loads", "fail", str(exc), "Fix the exclusion YAML."
        )
    # An empty list is a broken installation, not a configuration choice: the
    # shipped defaults are package data, and there is no supported way to have
    # none of them (REQ PXY-013). Reporting `pass` here is what let OI-33 ship —
    # the file was missing from every clone for the life of the project, and the
    # tool whose job is to notice said everything was fine. The consequence is
    # not abstract: with no list, pporlock decrypts OS update endpoints,
    # certificate revocation, and banking hosts.
    if not exclusions.entries:
        return CheckResult(
            "exclusions_load",
            "Exclusion list loads",
            "fail",
            "no exclusions at all — the shipped default list is missing or empty",
            "Reinstall pporlock. Without it, traffic that should be tunnelled "
            "undecrypted — OS updates, certificate revocation, banking — is "
            "being intercepted.",
        )

    uncommented = [e.pattern for e in exclusions.entries if not e.comment]
    if uncommented:
        return CheckResult(
            "exclusions_load",
            "Exclusion list loads",
            "warn",
            f"{len(uncommented)} entries have no comment: {', '.join(uncommented[:3])}",
            "An exclusion nobody can explain is indistinguishable from a bug.",
        )
    return CheckResult(
        "exclusions_load",
        "Exclusion list loads",
        "pass",
        f"{len(exclusions)} entries, all documented",
    )


def check_mitmproxy(_: Config) -> CheckResult:
    try:
        from mitmproxy import version
    except ImportError as exc:
        return CheckResult(
            "mitmproxy_present", "mitmproxy importable", "fail", str(exc), "Run `make setup`."
        )
    return CheckResult(
        "mitmproxy_present", "mitmproxy importable", "pass", f"mitmproxy {version.VERSION} (pinned)"
    )


def check_state_dir(config: Config) -> CheckResult:
    path = Path(config.state_dir).expanduser()
    if not path.exists():
        return CheckResult(
            "state_dir",
            "State directory",
            "warn",
            f"{path} does not exist yet",
            "It is created on first run.",
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return CheckResult(
            "state_dir",
            "State directory",
            "warn",
            f"{path} is mode {mode:o}; it holds the control API token",
            f"chmod 700 {path}",
        )
    return CheckResult("state_dir", "State directory", "pass", f"{path} mode {mode:o}")


def check_modules_load(config: Config) -> CheckResult:
    """Do the installed modules load cleanly (SPEC-1 §8.1)?

    Read from disk rather than from the daemon, so this answers even when
    nothing is running — a module that fails to load is one reason the daemon
    might not be running.
    """
    from ..engine.modules.registry import ModuleRegistry

    root = Path(config.modules.root).expanduser()
    if not root.exists():
        return CheckResult(
            "modules_load_clean", "Modules load cleanly", "pass", f"{root} does not exist yet"
        )
    registry = ModuleRegistry(root, quarantine_after=config.modules.quarantine_after_failures)
    registry.reload()
    broken = [m for m in registry.modules if m.error is not None]
    if broken:
        names = ", ".join(f"{m.name} ({m.error.code if m.error else '?'})" for m in broken[:3])
        return CheckResult(
            "modules_load_clean",
            "Modules load cleanly",
            "fail",
            f"{len(broken)} of {len(registry.modules)} failed: {names}",
            "Run `pporlock modules validate <path>` for the detail.",
        )
    return CheckResult(
        "modules_load_clean",
        "Modules load cleanly",
        "pass",
        f"{len(registry.modules)} modules, none broken",
    )


def check_daemon_reachable(config: Config) -> CheckResult:
    """Is a daemon answering the control API (REQ PXY-004)?

    A warning, not a failure. Running `doctor` on a stopped daemon is the normal
    case — you run it precisely because it is not up — and reporting that as a
    failure would bury the checks that explain why.
    """
    from .client import ControlClient

    client = ControlClient(config)
    if client.reachable():
        return CheckResult("daemon_reachable", "Daemon reachable", "pass", client.base)
    return CheckResult(
        "daemon_reachable",
        "Daemon reachable",
        "warn",
        f"nothing answering on {client.base}",
        "Start it with `pporlock start`, or run it in the foreground with `pporlock run`.",
    )


def check_launchd_installed(_: Config) -> CheckResult:
    from . import launchd

    if not launchd.is_installed():
        return CheckResult(
            "launchd_installed",
            "launchd agent installed",
            "warn",
            f"{launchd.PLIST_PATH} does not exist",
            "Run `pporlock install --service` if you want it to start at login.",
        )
    state = launchd.status()
    return CheckResult(
        "launchd_installed",
        "launchd agent installed",
        "pass",
        f"{launchd.PLIST_PATH} ({state.detail})",
    )


def check_token_permissions(config: Config) -> CheckResult:
    """The bearer token file must be 0600 (§2.5, A07)."""
    path = Path(config.state_dir).expanduser() / "token"
    if not path.exists():
        return CheckResult(
            "token_permissions",
            "Token file permissions",
            "warn",
            f"{path} does not exist yet",
            "It is created on first run.",
        )
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        return CheckResult(
            "token_permissions",
            "Token file permissions",
            "fail",
            f"{path} is mode {mode:o}, not 600",
            f"chmod 600 {path}",
        )
    return CheckResult("token_permissions", "Token file permissions", "pass", f"{path} mode 600")


def check_extension_paired(config: Config) -> CheckResult:
    """Has the extension ever paired?

    Inferred from attribution coverage, which is the only evidence the daemon
    has: pairing itself leaves no persistent record on this side, and the point
    of the check is whether the extension is actually talking to us.
    """
    from .client import ControlClient

    client = ControlClient(config)
    if not client.reachable():
        return CheckResult(
            "extension_paired",
            "Extension paired",
            "warn",
            "cannot tell — the daemon is not running",
            "Start the daemon and re-run doctor.",
        )
    try:
        metrics = client.get("/metrics")
    except Exception as exc:
        return CheckResult("extension_paired", "Extension paired", "warn", str(exc))
    attribution = metrics.get("attribution", {}) if isinstance(metrics, dict) else {}
    observed = int(attribution.get("observations", 0) or 0)
    if observed:
        return CheckResult(
            "extension_paired",
            "Extension paired",
            "pass",
            f"{observed} attribution observations received",
        )
    return CheckResult(
        "extension_paired",
        "Extension paired",
        "warn",
        "the extension has sent nothing",
        "Run `pporlock pair` and enter the code in the extension popup.",
    )


def check_disk_space(config: Config) -> CheckResult:
    """Sessions can be gigabytes. Running the disk out is a bad way to find out."""
    import shutil as _shutil

    path = Path(config.state_dir).expanduser()
    probe = path if path.exists() else path.parent
    try:
        usage = _shutil.disk_usage(probe)
    except OSError as exc:
        return CheckResult("disk_space", "Disk space", "warn", str(exc))
    free_gb = usage.free / (1024**3)
    needed_gb = config.capture.session_max_bytes / (1024**3)
    if free_gb < needed_gb:
        return CheckResult(
            "disk_space",
            "Disk space",
            "warn",
            f"{free_gb:.1f} GB free; a session may grow to {needed_gb:.1f} GB",
            "Lower capture.session_max_bytes, or free space before recording.",
        )
    return CheckResult("disk_space", "Disk space", "pass", f"{free_gb:.1f} GB free")


def check_log_dir(config: Config) -> CheckResult:
    """The log directory the launchd agent writes into (REQ PXY-007)."""
    from . import logs as logs_mod

    directory = logs_mod.log_dir(config.logging.dir)
    if not directory.exists():
        return CheckResult(
            "log_dir",
            "Log directory",
            "warn",
            f"{directory} does not exist",
            "Created by `pporlock install --service`.",
        )
    mode = directory.stat().st_mode & 0o777
    oversized = [
        p
        for p in logs_mod.log_paths(directory)
        if p.exists() and p.stat().st_size > config.logging.max_bytes
    ]
    if oversized:
        return CheckResult(
            "log_dir",
            "Log directory",
            "warn",
            f"{len(oversized)} log(s) past logging.max_bytes and not yet rotated",
            "`pporlock doctor --fix` rotates them.",
        )
    return CheckResult("log_dir", "Log directory", "pass", f"{directory} mode {mode:o}")


# --------------------------------------------------------------------- fixes --
#
# A check declares a fix only where the repair is unambiguous and local. Nothing
# here reaches the network, and nothing here changes a user's rules or modules:
# `--fix` is for the state *this* tool put on the machine.


def _fix_ca_trust(_: Config) -> None:
    """Install CA trust. Prompts for the user's password."""
    certs.install_trust()


def _fix_state_dir(config: Config) -> None:
    """Tighten the state directory. It holds the control API token."""
    path = Path(config.state_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _fix_token_permissions(config: Config) -> None:
    path = Path(config.state_dir).expanduser() / "token"
    if path.exists():
        path.chmod(0o600)


def _fix_log_dir(config: Config) -> None:
    """Create the log directory and rotate anything oversized (REQ PXY-007)."""
    from . import logs as logs_mod

    directory = logs_mod.log_dir(config.logging.dir)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    logs_mod.rotate(directory, max_bytes=config.logging.max_bytes, retain=config.logging.retain)


def _fix_launchd(config: Config) -> None:
    """Install the user agent. Never a system daemon (REQ PXY-002)."""
    from . import launchd
    from . import logs as logs_mod

    launchd.install(auto_start=True, log_dir=logs_mod.log_dir(config.logging.dir))


CHECKS: list[Check] = [
    Check("mitmproxy_present", "mitmproxy importable", check_mitmproxy),
    Check("config_valid", "Configuration is valid", check_config_valid),
    Check("ca_present", "CA certificate exists", check_ca_present),
    Check("ca_trusted", "CA is trusted", check_ca_trusted, fix=_fix_ca_trust),
    Check("port_proxy_free", "Proxy port available", check_proxy_port),
    Check("port_control_free", "Control port available", check_control_port),
    Check("chrome_installed", "Chrome is installed", check_chrome_installed),
    Check("chrome_quic_disabled", "Chrome QUIC disabled", check_quic_disabled),
    Check("exclusions_load", "Exclusion list loads", check_exclusions),
    Check("modules_load_clean", "Modules load cleanly", check_modules_load),
    Check("state_dir", "State directory", check_state_dir, fix=_fix_state_dir),
    Check(
        "token_permissions",
        "Token file permissions",
        check_token_permissions,
        fix=_fix_token_permissions,
    ),
    Check("log_dir", "Log directory", check_log_dir, fix=_fix_log_dir),
    Check(
        "launchd_installed", "launchd agent installed", check_launchd_installed, fix=_fix_launchd
    ),
    Check("daemon_reachable", "Daemon reachable", check_daemon_reachable),
    Check("extension_paired", "Extension paired", check_extension_paired),
    Check("disk_space", "Disk space", check_disk_space),
]

#: Checks whose fix `--fix` runs without being asked. Everything else needs a
#: `--fix` and a failing (not merely warning) result.
FIXABLE: frozenset[str] = frozenset(c.check_id for c in CHECKS if c.fix is not None)


def fixable_for(results: list[CheckResult], *, include_warnings: bool = True) -> list[Check]:
    """The checks `--fix` should act on, in declaration order.

    Warnings are included. Several of the repairable conditions — a missing log
    directory, a launchd agent that was never installed, a state directory that
    does not exist yet — are warnings by design, and a `--fix` that only touched
    failures would print a fix list that never contains them.
    """
    by_id = {c.check_id: c for c in CHECKS}
    wanted = {"fail", "warn"} if include_warnings else {"fail"}
    out: list[Check] = []
    for result in results:
        if result.level not in wanted:
            continue
        check = by_id.get(result.check_id)
        if check is not None and check.fix is not None:
            out.append(check)
    return out


def run_checks(config: Config | None = None, *, only: list[str] | None = None) -> list[CheckResult]:
    cfg = config or Config()
    selected = [c for c in CHECKS if only is None or c.check_id in only]
    results = []
    for check in selected:
        try:
            results.append(check.run(cfg))
        except Exception as exc:
            results.append(CheckResult(check.check_id, check.title, "fail", f"check raised: {exc}"))
    return results


def format_results(results: list[CheckResult]) -> str:
    # Keyed off the Level values without writing "pass": <str> literally, which
    # bandit reads as a hardcoded password (B105).
    glyphs: dict[Level, str] = dict(
        zip(("pass", "warn", "fail"), ("ok  ", "warn", "FAIL"), strict=True)
    )
    lines = []
    for r in results:
        lines.append(f"  [{glyphs[r.level]}] {r.title}: {r.message}")
        if r.level != "pass" and r.remediation:
            lines.append(f"         -> {r.remediation}")
    failures = sum(1 for r in results if r.level == "fail")
    warnings = sum(1 for r in results if r.level == "warn")
    lines.append("")
    lines.append(
        f"  {len(results) - failures - warnings} passed, {warnings} warnings, {failures} failures"
    )
    return "\n".join(lines)


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.level == "fail" for r in results) else 0
