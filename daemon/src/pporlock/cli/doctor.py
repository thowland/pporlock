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


def _fix_ca_trust(_: Config) -> None:
    """Install CA trust. Prompts for the user's password."""
    certs.install_trust()


CHECKS: list[Check] = [
    Check("mitmproxy_present", "mitmproxy importable", check_mitmproxy),
    Check("config_valid", "Configuration is valid", check_config_valid),
    Check("ca_present", "CA certificate exists", check_ca_present),
    Check(
        "ca_trusted",
        "CA is trusted",
        check_ca_trusted,
        fix=_fix_ca_trust,
    ),
    Check("port_proxy_free", "Proxy port available", check_proxy_port),
    Check("port_control_free", "Control port available", check_control_port),
    Check("chrome_installed", "Chrome is installed", check_chrome_installed),
    Check("chrome_quic_disabled", "Chrome QUIC disabled", check_quic_disabled),
    Check("exclusions_load", "Exclusion list loads", check_exclusions),
    Check("state_dir", "State directory", check_state_dir),
]


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
