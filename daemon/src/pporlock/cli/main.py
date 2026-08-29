"""`pporlock` CLI — SPEC-1 §8, REQ PXY-003.

The full surface: ``run``, ``start``, ``stop``, ``restart``, ``status``,
``install``, ``uninstall``, ``doctor``, ``pair``, ``logs``, ``modules``,
``profile``, ``session``, ``dryrun``, ``version``.

Everything that acts on a *running* daemon lives in ``commands.py`` and goes
through the control API. Everything that acts on this machine — launchd, the
keychain, the log files — is here or in its own module, because those are the
commands you need when nothing is answering.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

from ..version import VERSION


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
    """Report the local environment, and with ``--fix`` repair what it can.

    ``--fix`` re-runs every check afterwards and prints the result. A tool that
    said "fixed" without re-measuring would be reporting its own intention, and
    the checks that exist here exist because intentions are exactly what has
    already gone wrong by the time someone runs `doctor`.
    """
    from . import doctor

    config = _load_config(args)
    results = doctor.run_checks(config)
    print("pporlock doctor")
    print(doctor.format_results(results))

    if not args.fix:
        return doctor.exit_code(results)

    checks = doctor.fixable_for(results)
    if not checks:
        print("\nnothing to fix (no failing or warning check declares a repair)")
        return doctor.exit_code(results)

    for check in checks:
        print(f"\nfixing: {check.title}")
        if check.fix is None:  # pragma: no cover — fixable_for filters these out
            continue
        try:
            check.fix(config)
            print("  applied")
        except Exception as exc:
            print(f"  could not fix: {exc}")

    results = doctor.run_checks(config)
    print("\nafter fixes:")
    print(doctor.format_results(results))
    return doctor.exit_code(results)


def cmd_install(args: argparse.Namespace) -> int:
    """Install CA trust and, with ``--service``, the launchd user agent."""
    from . import certs, launchd

    status_ok = True

    if args.no_ca:
        print("skipping CA trust (--no-ca)")
    elif not certs.is_present():
        print(
            f"CA not found at {certs.ca_path()}.\n"
            "Run `pporlock run` once to generate it, then re-run install."
        )
        status_ok = False
    else:
        print(f"installing CA trust for {certs.ca_path()} into the login keychain")
        print("(not the System keychain: no admin rights, blast radius is this account)")
        try:
            trust = certs.install_trust()
        except Exception as exc:
            print(f"failed: {exc}")
            return 1
        print("trusted" if trust.trusted else "installed, but trust could not be verified")
        status_ok = trust.trusted

    if args.service:
        config = _load_config(args)
        from . import logs as logs_mod

        log_dir = logs_mod.log_dir(config.logging.dir)
        print(f"installing the launchd user agent at {launchd.plist_path()}")
        print("(a user agent, not a system daemon: it needs no administrator rights)")
        try:
            launchd.install(
                auto_start=not args.no_start,
                log_dir=log_dir,
                # Carried through, or the agent runs against the default config
                # rather than the one just installed with.
                config_path=_config_path(args),
            )
        except launchd.LaunchdError as exc:
            print(f"failed: {exc}")
            return 1
        print(f"  logs: {log_dir}")
        print(f"  starts at login, restarts on crash (throttled to {launchd.THROTTLE_INTERVAL_S}s)")

    return 0 if status_ok else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove everything pporlock installed outside its own state (REQ DOC-005)."""
    from . import certs, launchd
    from . import logs as logs_mod

    # The *configured* directories, not the home-directory defaults. A user with
    # a custom `state_dir` who is told their data is in ~/.pporlock has been
    # given a wrong answer to the only question uninstall exists to answer.
    config = _load_config(args)
    state_dir = Path(config.state_dir).expanduser()
    log_dir = logs_mod.log_dir(config.logging.dir)

    if launchd.is_installed():
        print(f"removing the launchd agent {launchd.plist_path()}")
        launchd.uninstall()

    print("removing CA trust")
    certs.remove_trust()
    print("done")
    print("")
    # DOC-005 requires an explicit statement of what is left behind and where.
    # A tool that terminates TLS owes the user a precise answer to "is it gone".
    print("Left in place (delete manually if you want them gone):")
    print(f"  {state_dir}  modules, profiles, sessions, token, config")
    print(f"  {certs.MITMPROXY_DIR}  the CA key material itself")
    print(f"  {log_dir}  daemon logs")
    print("")
    print("Not ours to remove:")
    print("  Chrome's proxy settings — the extension clears them when the daemon stops;")
    print("  uninstall the extension from chrome://extensions to be certain.")
    if args.purge:
        import shutil

        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(log_dir, ignore_errors=True)
        print(f"\n--purge: removed {state_dir} and {log_dir}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    from .commands import cmd_start as run_start

    return run_start(_load_config(args))


def cmd_stop(args: argparse.Namespace) -> int:
    from .commands import cmd_stop as run_stop

    return run_stop(_load_config(args))


def cmd_restart(args: argparse.Namespace) -> int:
    from .commands import cmd_restart as run_restart

    return run_restart(_load_config(args))


def cmd_status(args: argparse.Namespace) -> int:
    from .commands import cmd_status as run_status

    return run_status(_load_config(args))


def cmd_logs(args: argparse.Namespace) -> int:
    from .commands import cmd_logs as run_logs

    return run_logs(_load_config(args), follow=args.follow, lines=args.lines, stream=args.stream)


def cmd_modules(args: argparse.Namespace) -> int:
    from .commands import cmd_modules as run_modules

    return run_modules(_load_config(args), args)


def cmd_profile(args: argparse.Namespace) -> int:
    from .commands import cmd_profile as run_profile

    return run_profile(_load_config(args), args)


def cmd_session(args: argparse.Namespace) -> int:
    from .commands import cmd_session as run_session

    return run_session(_load_config(args), args)


def cmd_dryrun(args: argparse.Namespace) -> int:
    from .commands import cmd_dryrun as run_dryrun

    return run_dryrun(_load_config(args), args)


def cmd_pair(args: argparse.Namespace) -> int:
    """Open a pairing window so the extension can obtain the token.

    The CLI can read the token file; the extension deliberately cannot
    (REQ API-012). This bridges the two with a short-lived, single-use code the
    user types into the extension.
    """
    from .client import ControlClient, ControlClientError

    config = _load_config(args)
    client = ControlClient(config)
    try:
        payload = client.post("/pair/begin", body={})
    except ControlClientError as exc:
        print(exc.message)
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

    p_install = sub.add_parser("install", help="install CA trust and the launchd agent")
    p_install.add_argument("--no-ca", action="store_true", help="skip CA trust")
    p_install.add_argument(
        "--service", action="store_true", help="also install the launchd user agent"
    )
    p_install.add_argument(
        "--no-start", action="store_true", help="with --service, install without starting"
    )
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser("uninstall", help="remove CA trust and the launchd agent")
    p_uninstall.add_argument(
        "--purge", action="store_true", help="also delete ~/.pporlock (modules, sessions)"
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    for name, help_text, func in (
        ("start", "start the launchd agent", cmd_start),
        ("stop", "stop the launchd agent", cmd_stop),
        ("restart", "restart the launchd agent", cmd_restart),
        ("status", "report launchd and daemon state", cmd_status),
    ):
        service_parser = sub.add_parser(name, help=help_text)
        service_parser.set_defaults(func=func)

    p_logs = sub.add_parser("logs", help="show the daemon log")
    p_logs.add_argument("-f", "--follow", action="store_true", help="follow new output")
    p_logs.add_argument("-n", "--lines", type=int, default=50, help="lines of history")
    p_logs.add_argument(
        "--stream", choices=("out", "err", "both"), default="both", help="which log"
    )
    p_logs.set_defaults(func=cmd_logs)

    p_pair = sub.add_parser("pair", help="open a pairing window for the extension")
    p_pair.set_defaults(func=cmd_pair)

    p_modules = sub.add_parser("modules", help="list, enable, disable or validate modules")
    m_sub = p_modules.add_subparsers(dest="modules_action", required=True)
    m_sub.add_parser("list", help="list installed modules")
    for verb in ("enable", "disable"):
        m_parser = m_sub.add_parser(verb, help=f"{verb} a module")
        m_parser.add_argument("name")
    m_validate = m_sub.add_parser("validate", help="validate a module directory")
    m_validate.add_argument("path")
    p_modules.set_defaults(func=cmd_modules)

    p_profile = sub.add_parser("profile", help="list or activate profiles")
    pr_sub = p_profile.add_subparsers(dest="profile_action", required=True)
    pr_sub.add_parser("list", help="list profiles")
    pr_activate = pr_sub.add_parser("activate", help="activate a profile")
    pr_activate.add_argument("name")
    p_profile.set_defaults(func=cmd_profile)

    p_session = sub.add_parser("session", help="record, list and export sessions")
    s_sub = p_session.add_subparsers(dest="session_action", required=True)
    s_start = s_sub.add_parser("start", help="start recording")
    s_start.add_argument("name", nargs="?", default="")
    s_stop = s_sub.add_parser("stop", help="stop recording")
    s_stop.add_argument("session_id")
    s_sub.add_parser("list", help="list sessions")
    s_export = s_sub.add_parser("export", help="export a session")
    s_export.add_argument("session_id")
    s_export.add_argument("--format", default="pporlock", help="export format")
    s_export.add_argument("-o", "--output", help="write to a file instead of stdout")
    p_session.set_defaults(func=cmd_session)

    p_dryrun = sub.add_parser("dryrun", help="replay a session through a candidate module")
    p_dryrun.add_argument("session_id", help="a session id, or 'live' for the ring buffer")
    p_dryrun.add_argument("module_path", help="directory holding module.yaml")
    p_dryrun.add_argument("--limit", type=int, default=200, help="flows to replay")
    p_dryrun.set_defaults(func=cmd_dryrun)

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
