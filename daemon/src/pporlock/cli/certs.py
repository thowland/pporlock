"""Certificate authority management — SPEC-1 §8.2.

Installs mitmproxy's generated root into the macOS **login** keychain, never the
System keychain (REQ PXY-011). That choice is deliberate: it needs no
administrator privileges, and the blast radius of a trusted MITM root is one
user account rather than the whole machine. Uninstall removes it (REQ DOC-005).
"""

from __future__ import annotations

# `subprocess` is required here and nowhere else: macOS keychain trust is only
# reachable through the `security` binary. Every call site builds a fixed argv
# with no shell and no user-supplied words; the only interpolated value is a
# path this module constructs itself.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

MITMPROXY_DIR = Path.home() / ".mitmproxy"
CA_CERT = MITMPROXY_DIR / "mitmproxy-ca-cert.pem"
CA_PEM = MITMPROXY_DIR / "mitmproxy-ca.pem"
LOGIN_KEYCHAIN = Path.home() / "Library" / "Keychains" / "login.keychain-db"

CA_COMMON_NAME = "mitmproxy"


@dataclass(frozen=True, slots=True)
class TrustStatus:
    present: bool
    trusted: bool
    path: Path
    detail: str = ""


def ca_path() -> Path:
    return CA_CERT


def is_present() -> bool:
    return CA_CERT.exists()


def _run(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    # nosec B603 — argv is a fixed list built by this module, shell=False, and
    # the only variable component is a path under the user's own home directory.
    # A timeout is set so a keychain prompt cannot hang the CLI forever.
    return subprocess.run(  # noqa: S603  # nosec B603
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def is_trusted() -> bool:
    """Is the mitmproxy root trusted in the login keychain?

    ``security verify-cert`` is the honest check: ``find-certificate`` only
    proves the certificate was imported, not that it is trusted, and an imported
    but untrusted root produces exactly the certificate warnings this is
    supposed to rule out.
    """
    if not is_present():
        return False
    result = _run(["security", "verify-cert", "-c", str(CA_CERT), "-p", "ssl"])
    return result.returncode == 0


def status() -> TrustStatus:
    if not is_present():
        return TrustStatus(
            present=False,
            trusted=False,
            path=CA_CERT,
            detail="not generated yet — run the proxy once to create it",
        )
    trusted = is_trusted()
    return TrustStatus(
        present=True,
        trusted=trusted,
        path=CA_CERT,
        detail="trusted in the login keychain" if trusted else "present but not trusted",
    )


def install_trust() -> TrustStatus:
    """Add the root to the login keychain and mark it trusted for SSL.

    Prompts for the user's password. Never touches the System keychain.
    """
    if not is_present():
        raise FileNotFoundError(f"{CA_CERT} does not exist. Start the proxy once to generate it.")

    result = _run(
        [
            "security",
            "add-trusted-cert",
            "-k",
            str(LOGIN_KEYCHAIN),
            "-p",
            "ssl",
            str(CA_CERT),
        ],
        timeout=120.0,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to trust the CA: {result.stderr.strip() or result.stdout.strip()}"
        )
    return status()


def remove_trust() -> None:
    """Remove the root from the login keychain (REQ DOC-005)."""
    if not is_present():
        return
    _run(["security", "remove-trusted-cert", str(CA_CERT)], timeout=60.0)
    _run(["security", "delete-certificate", "-c", CA_COMMON_NAME, str(LOGIN_KEYCHAIN)])
