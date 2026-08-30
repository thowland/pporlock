"""`pporlock doctor` and CA management. SPEC-1 §8.1, §8.2."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from pporlock.cli import certs, doctor
from pporlock.config import Config


class TestCheckResult:
    def test_pass_and_warn_are_ok_but_fail_is_not(self) -> None:
        """Only a failure blocks. A warning is information, not a stop sign —
        QUIC state cannot be enforced from outside Chrome, so treating it as a
        failure would make doctor useless."""
        assert doctor.CheckResult("i", "t", "pass", "m").ok
        assert doctor.CheckResult("i", "t", "warn", "m").ok
        assert not doctor.CheckResult("i", "t", "fail", "m").ok


class TestChecks:
    def test_mitmproxy_check_passes_and_names_the_version(self) -> None:
        result = doctor.check_mitmproxy(Config())
        assert result.level == "pass"
        assert "mitmproxy" in result.message

    def test_config_check_passes_for_defaults(self) -> None:
        assert doctor.check_config_valid(Config()).level == "pass"

    def test_config_check_fails_for_a_non_loopback_bind(self) -> None:
        cfg = Config()
        cfg.control.listen_host = "0.0.0.0"
        result = doctor.check_config_valid(cfg)
        assert result.level == "fail"
        assert "loopback" in result.message.lower()

    def test_exclusion_check_reports_the_count(self) -> None:
        result = doctor.check_exclusions(Config())
        assert result.level == "pass"
        assert "documented" in result.message

    def test_no_exclusions_at_all_is_a_failure_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OI-33 — the state this tool existed to notice, and reported as fine.

        The shipped default list was missing from every clone for the life of
        the project. `check_exclusions` loaded it, got nothing, found no
        undocumented entries in that nothing, and said `pass`. Meanwhile the
        proxy was decrypting OS update endpoints, certificate revocation and
        banking hosts.

        An empty list is a broken installation, not a configuration choice:
        there is no supported way to have no exclusions (REQ PXY-013).
        """
        from pporlock.engine.exclusions import ExclusionList

        monkeypatch.setattr(doctor, "load_exclusions", lambda: ExclusionList([]))
        result = doctor.check_exclusions(Config())
        assert result.level == "fail"
        assert "no exclusions" in result.message
        assert result.remediation

    def test_ca_present_check_reflects_the_filesystem(self) -> None:
        result = doctor.check_ca_present(Config())
        assert result.level in ("pass", "fail")
        if result.level == "fail":
            assert result.remediation

    def test_port_check_reports_a_free_port(self) -> None:
        cfg = Config()
        cfg.proxy.listen_port = 49999
        assert doctor.check_proxy_port(cfg).level == "pass"

    def test_port_check_warns_when_the_port_is_taken(self) -> None:
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            cfg = Config()
            cfg.control.listen_port = sock.getsockname()[1]
            result = doctor.check_control_port(cfg)
        assert result.level == "warn"
        assert result.remediation

    def test_quic_check_never_fails(self) -> None:
        """REQ PXY-012 — QUIC cannot be reliably enforced or detected from
        outside Chrome, so this warns and explains rather than blocking."""
        result = doctor.check_quic_disabled(Config())
        assert result.level in ("pass", "warn")
        if result.level == "warn":
            assert "bypasses the proxy" in result.remediation

    def test_quic_check_survives_an_unreadable_local_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        bad = tmp_path / "Local State"
        bad.write_text("{not json")
        monkeypatch.setattr(doctor, "CHROME_LOCAL_STATE", bad)
        assert doctor.check_quic_disabled(Config()).level == "warn"

    def test_quic_check_detects_the_disabled_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state = tmp_path / "Local State"
        state.write_text('{"browser":{"enabled_labs_experiments":["enable-quic@2"]}}')
        monkeypatch.setattr(doctor, "CHROME_LOCAL_STATE", state)
        assert doctor.check_quic_disabled(Config()).level == "pass"

    def test_quic_check_warns_when_the_profile_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(doctor, "CHROME_LOCAL_STATE", tmp_path / "nope")
        assert doctor.check_quic_disabled(Config()).level == "warn"

    def test_state_dir_warns_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = Config()
        cfg.state_dir = str(tmp_path / "nope")
        assert doctor.check_state_dir(cfg).level == "warn"

    def test_state_dir_warns_on_loose_permissions(self, tmp_path: Path) -> None:
        """It holds the control API token (REQ API-011)."""
        loose = tmp_path / "state"
        loose.mkdir(mode=0o755)
        cfg = Config()
        cfg.state_dir = str(loose)
        result = doctor.check_state_dir(cfg)
        assert result.level == "warn"
        assert "token" in result.message

    def test_state_dir_passes_when_locked_down(self, tmp_path: Path) -> None:
        tight = tmp_path / "state"
        tight.mkdir(mode=0o700)
        cfg = Config()
        cfg.state_dir = str(tight)
        assert doctor.check_state_dir(cfg).level == "pass"

    def test_chrome_check_returns_a_verdict(self) -> None:
        assert doctor.check_chrome_installed(Config()).level in ("pass", "warn")


class TestRunner:
    def test_runs_every_check(self) -> None:
        assert len(doctor.run_checks()) == len(doctor.CHECKS)

    def test_can_select_a_subset(self) -> None:
        results = doctor.run_checks(only=["config_valid"])
        assert [r.check_id for r in results] == ["config_valid"]

    def test_a_raising_check_becomes_a_failure_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """doctor is what you run when things are already broken; it must not be
        the next thing that breaks."""

        def explode(_: Config) -> doctor.CheckResult:
            raise RuntimeError("boom")

        monkeypatch.setattr(doctor, "CHECKS", [doctor.Check("x", "Exploding check", explode)])
        results = doctor.run_checks()
        assert results[0].level == "fail"
        assert "boom" in results[0].message

    def test_exit_code_is_zero_without_failures(self) -> None:
        assert doctor.exit_code([doctor.CheckResult("i", "t", "warn", "m")]) == 0

    def test_exit_code_is_one_with_a_failure(self) -> None:
        assert doctor.exit_code([doctor.CheckResult("i", "t", "fail", "m")]) == 1

    def test_formatting_includes_remediation_for_problems_only(self) -> None:
        text = doctor.format_results(
            [
                doctor.CheckResult("a", "Fine", "pass", "ok", "should not appear"),
                doctor.CheckResult("b", "Broken", "fail", "bad", "do this"),
            ]
        )
        assert "should not appear" not in text
        assert "do this" in text
        assert "1 passed, 0 warnings, 1 failures" in text


class TestCerts:
    def test_ca_path_is_the_mitmproxy_default(self) -> None:
        assert certs.ca_path().name == "mitmproxy-ca-cert.pem"

    def test_status_when_absent(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(certs, "CA_CERT", tmp_path / "nope.pem")
        state = certs.status()
        assert not state.present
        assert not state.trusted

    def test_is_trusted_is_false_when_the_cert_is_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(certs, "CA_CERT", tmp_path / "nope.pem")
        assert not certs.is_trusted()

    def test_trust_uses_verify_cert_not_find_certificate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """find-certificate proves import, not trust. An imported-but-untrusted
        root produces exactly the warnings this check exists to rule out."""
        cert = tmp_path / "ca.pem"
        cert.write_text("x")
        monkeypatch.setattr(certs, "CA_CERT", cert)
        seen: list[list[str]] = []

        def fake_run(args: list[str], timeout: float = 30.0) -> Any:
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(certs, "_run", fake_run)
        assert certs.is_trusted()
        assert seen[0][:2] == ["security", "verify-cert"]

    def test_install_targets_the_login_keychain_never_the_system_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """REQ PXY-011. The login keychain needs no admin rights and limits the
        blast radius of a trusted MITM root to one account."""
        cert = tmp_path / "ca.pem"
        cert.write_text("x")
        monkeypatch.setattr(certs, "CA_CERT", cert)
        seen: list[list[str]] = []

        def fake_run(args: list[str], timeout: float = 30.0) -> Any:
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(certs, "_run", fake_run)
        certs.install_trust()
        add = next(a for a in seen if "add-trusted-cert" in a)
        assert "login.keychain-db" in " ".join(add)
        assert "/Library/Keychains/System.keychain" not in " ".join(add)

    def test_install_without_a_cert_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(certs, "CA_CERT", tmp_path / "nope.pem")
        with pytest.raises(FileNotFoundError, match="Start the proxy once"):
            certs.install_trust()

    def test_install_surfaces_a_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cert = tmp_path / "ca.pem"
        cert.write_text("x")
        monkeypatch.setattr(certs, "CA_CERT", cert)
        monkeypatch.setattr(
            certs,
            "_run",
            lambda args, timeout=30.0: subprocess.CompletedProcess(args, 1, "", "denied"),
        )
        with pytest.raises(RuntimeError, match="denied"):
            certs.install_trust()

    def test_remove_trust_is_a_no_op_when_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(certs, "CA_CERT", tmp_path / "nope.pem")
        certs.remove_trust()
