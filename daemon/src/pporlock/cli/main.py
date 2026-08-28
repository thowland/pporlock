"""`pporlock` CLI — SPEC-1 §8.

Sprint 2 ships the subset that baseline interception needs: ``run``, ``doctor``,
``install``, ``uninstall``, and ``version``. Service control (``start``/``stop``
via launchd), pairing, module, profile, and session commands arrive in their own
sprints.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

VERSION = "0.1.0"


def _config_path(args: argparse.Namespace) -> Path | None:
    if args.config:
        return Path(args.config).expanduser()
    default = Path.home() / ".pporlock" / "config.yaml"
    return default if default.exists() else None


def _load_config(args: argparse.Namespace) -> Config:
    """Build the effective config. Imported lazily to keep CLI startup cheap."""
    from ..config import load_config

    overrides: dict[str, dict[str, int]] = {}
    if getattr(args, "port", None):
        overrides["proxy"] = {"listen_port": args.port}
    if getattr(args, "control_port", None):
        overrides["control"] = {"listen_port": args.control_port}
    return load_config(_config_path(args), overrides=overrides or None)


# ------------------------------------------------------------------ commands --


def cmd_version(_: argparse.Namespace) -> int:
    from mitmproxy import version as mitm_version

    print(f"pporlock {VERSION}")
    print(f"mitmproxy {mitm_version.VERSION} (pinned)")
    print(f"python {sys.version.split()[0]}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import certs, doctor

    config = _load_config(args)
    results = doctor.run_checks(config)
    print("pporlock doctor")
    print(doctor.format_results(results))

    if args.fix:
        failed = [r for r in results if r.level == "fail"]
        fixable = {c.check_id: c for c in doctor.CHECKS if c.fix is not None}
        for result in failed:
            check = fixable.get(result.check_id)
            if check is None or check.fix is None:
                continue
            print(f"\nfixing: {result.title}")
            try:
                check.fix(config)
                print("  fixed")
            except Exception as exc:
                print(f"  could not fix: {exc}")
        results = doctor.run_checks(config)
        print("\nafter fixes:")
        print(doctor.format_results(results))

    _ = certs
    return doctor.exit_code(results)


def cmd_install(args: argparse.Namespace) -> int:
    """Install CA trust. launchd installation lands in Sprint 16."""
    from . import certs

    if args.no_ca:
        print("skipping CA trust (--no-ca)")
        return 0

    if not certs.is_present():
        print(
            f"CA not found at {certs.ca_path()}.\n"
            "Run `pporlock run` once to generate it, then re-run install."
        )
        return 1

    print(f"installing CA trust for {certs.ca_path()} into the login keychain")
    print("(not the System keychain: no admin rights, blast radius is this account)")
    try:
        status = certs.install_trust()
    except Exception as exc:
        print(f"failed: {exc}")
        return 1
    print("trusted" if status.trusted else "installed, but trust could not be verified")
    return 0 if status.trusted else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    from . import certs

    print("removing CA trust")
    certs.remove_trust()
    print("done")
    print("")
    print("Left in place (delete manually if you want them gone):")
    print(f"  {Path.home() / '.pporlock'}  modules, profiles, sessions, token")
    print(f"  {certs.MITMPROXY_DIR}  the CA key material itself")
    if args.purge:
        import shutil

        shutil.rmtree(Path.home() / ".pporlock", ignore_errors=True)
        print("\n--purge: removed ~/.pporlock")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    """Open a pairing window so the extension can obtain the token.

    The CLI can read the token file; the extension deliberately cannot
    (REQ API-012). This bridges the two with a short-lived, single-use code the
    user types into the extension.
    """
    import json
    import urllib.error
    import urllib.request
    from pathlib import Path as _Path

    config = _load_config(args)
    token_path = _Path(config.state_dir).expanduser() / "token"
    if not token_path.exists():
        print(f"No token at {token_path}. Start the daemon once with `pporlock run`.")
        return 1

    url = f"http://{config.control.listen_host}:{config.control.listen_port}/pair/begin"
    request = urllib.request.Request(
        url,
        method="POST",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token_path.read_text().strip()}",
            "Content-Type": "application/json",
            "X-Pporlock-Client": "cli",
        },
    )
    try:
        # nosec B310 — the scheme is the literal "http" in the f-string above,
        # and the host is config.control.listen_host, which Config.validate()
        # has already asserted is a loopback address (REQ API-010). There is no
        # path here to file:// or a custom scheme.
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310
            payload = json.load(response)
    except urllib.error.URLError as exc:
        print(f"Could not reach the daemon at {url}: {exc}")
        print("Is it running? `pporlock run`")
        return 1

    print("Pairing code:\n")
    print(f"    {payload['code']}\n")
    print(
        f"Enter it in the pporlock extension popup within {payload['expires_in']:.0f} seconds.\n"
        "It is single-use: a wrong entry closes the window and you run this again."
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the proxy in the foreground (REQ PXY-005)."""
    from .runner import run_foreground

    config = _load_config(args)
    return run_foreground(config, quiet=args.quiet)


# ------------------------------------------------------------------- parser --


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pporlock",
        description="Local HTTPS interception and modification proxy for Chrome.",
    )
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the proxy in the foreground")
    p_run.add_argument("--port", type=int, help="proxy listen port")
    p_run.add_argument("--control-port", type=int, help="control server port")
    p_run.add_argument("--quiet", action="store_true", help="suppress per-flow output")
    p_run.set_defaults(func=cmd_run)

    p_doctor = sub.add_parser("doctor", help="check the local environment")
    p_doctor.add_argument("--fix", action="store_true", help="attempt fixes where available")
    p_doctor.set_defaults(func=cmd_doctor)

    p_install = sub.add_parser("install", help="install CA trust")
    p_install.add_argument("--no-ca", action="store_true", help="skip CA trust")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="remove CA trust")
    p_uninstall.add_argument(
        "--purge", action="store_true", help="also delete ~/.pporlock (modules, sessions)"
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_pair = sub.add_parser("pair", help="open a pairing window for the extension")
    p_pair.set_defaults(func=cmd_pair)

    p_version = sub.add_parser("version", help="show versions")
    p_version.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from ..errors import PporlockError

        if isinstance(exc, PporlockError):
            print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
