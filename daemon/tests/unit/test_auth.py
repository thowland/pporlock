"""Access control. SPEC-0 §6.1, SPEC-1 §7.2.

The threat these defend against is specific and easy to overlook: any page you
visit can issue requests to http://127.0.0.1:8081. Loopback binding does not
stop that. These tests exist to keep the three layers that do.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from pporlock.control.auth import (
    CLIENT_HEADER,
    OriginPolicy,
    PairingWindow,
    TokenStore,
    bearer_token,
    require_client,
)
from pporlock.errors import AuthError, PairingError


class TestTokenStore:
    def test_generates_on_first_use(self, tmp_path: Path) -> None:
        token = TokenStore(tmp_path).ensure()
        assert len(token) >= 32
        assert (tmp_path / "token").exists()

    def test_is_stable_across_calls(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path)
        assert store.ensure() == store.ensure()

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        first = TokenStore(tmp_path).ensure()
        assert TokenStore(tmp_path).ensure() == first

    def test_file_mode_is_0600(self, tmp_path: Path) -> None:
        """REQ API-011. Anything able to read this can read your traffic."""
        TokenStore(tmp_path).ensure()
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600

    def test_created_restrictively_not_chmodded_afterwards(self, tmp_path: Path) -> None:
        """Writing then chmod-ing leaves a window where the token is readable."""
        TokenStore(tmp_path).ensure()
        assert (tmp_path / "token").stat().st_mode & 0o077 == 0

    def test_loose_permissions_are_tightened_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / "token"
        path.write_text("preexisting-token-value")
        os.chmod(path, 0o644)
        TokenStore(tmp_path).ensure()
        assert path.stat().st_mode & 0o077 == 0

    def test_state_directory_is_created_0700(self, tmp_path: Path) -> None:
        nested = tmp_path / "state"
        TokenStore(nested).ensure()
        assert nested.stat().st_mode & 0o777 == 0o700

    def test_verify_accepts_the_token(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path)
        assert store.verify(store.ensure())

    def test_verify_rejects_anything_else(self, tmp_path: Path) -> None:
        store = TokenStore(tmp_path)
        store.ensure()
        assert not store.verify("wrong")
        assert not store.verify("")
        assert not store.verify(None)

    def test_empty_file_is_regenerated(self, tmp_path: Path) -> None:
        (tmp_path / "token").write_text("")
        assert TokenStore(tmp_path).ensure()


class TestBearerParsing:
    def test_extracts_the_token(self) -> None:
        assert bearer_token("Bearer abc123") == "abc123"

    def test_scheme_is_case_insensitive(self) -> None:
        assert bearer_token("bearer abc") == "abc"

    @pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   "])
    def test_rejects_anything_else(self, header: str | None) -> None:
        assert bearer_token(header) is None


class TestOriginPolicy:
    @pytest.fixture
    def policy(self) -> OriginPolicy:
        return OriginPolicy("127.0.0.1", 8081)

    def test_allows_its_own_origin(self, policy: OriginPolicy) -> None:
        assert policy.allows("http://127.0.0.1:8081")
        assert policy.allows("http://localhost:8081")

    def test_rejects_an_ordinary_web_page(self, policy: OriginPolicy) -> None:
        """This is the whole point: a page you visit must not reach the API."""
        assert not policy.allows("https://evil.example")
        assert not policy.allows("http://127.0.0.1:9999")

    def test_absent_origin_is_allowed(self, policy: OriginPolicy) -> None:
        """Browsers always send Origin cross-origin, so a request without one
        did not come from a page. curl and the CLI omit it and still need the
        bearer token."""
        assert policy.allows(None)

    def test_unpaired_extension_is_rejected(self, policy: OriginPolicy) -> None:
        assert not policy.allows("chrome-extension://" + "a" * 32)

    def test_paired_extension_is_allowed(self, policy: OriginPolicy) -> None:
        origin = "chrome-extension://" + "a" * 32
        policy.pair_extension(origin)
        assert policy.allows(origin)

    def test_only_the_paired_extension_is_allowed(self, policy: OriginPolicy) -> None:
        policy.pair_extension("chrome-extension://" + "a" * 32)
        assert not policy.allows("chrome-extension://" + "b" * 32)

    def test_pairing_rejects_a_non_extension_origin(self, policy: OriginPolicy) -> None:
        with pytest.raises(PairingError):
            policy.pair_extension("https://evil.example")

    def test_pairing_rejects_a_malformed_extension_id(self, policy: OriginPolicy) -> None:
        with pytest.raises(PairingError):
            policy.pair_extension("chrome-extension://short")


class TestPairingWindow:
    def test_closed_by_default(self) -> None:
        assert not PairingWindow().is_open

    def test_open_produces_a_code(self) -> None:
        window = PairingWindow()
        code = window.open()
        assert window.is_open
        assert code.count("-") == 3

    def test_redeem_pairs_the_extension(self) -> None:
        window, policy = PairingWindow(), OriginPolicy("127.0.0.1", 8081)
        code = window.open()
        window.redeem(code, "chrome-extension://" + "a" * 32, policy)
        assert policy.extension_id == "a" * 32

    def test_redeem_closes_the_window(self) -> None:
        window, policy = PairingWindow(), OriginPolicy("127.0.0.1", 8081)
        code = window.open()
        window.redeem(code, "chrome-extension://" + "a" * 32, policy)
        assert not window.is_open

    def test_a_wrong_code_closes_the_window(self) -> None:
        """Single-use regardless of outcome: a wrong guess must not leave the
        window open for another attempt."""
        window, policy = PairingWindow(), OriginPolicy("127.0.0.1", 8081)
        window.open()
        with pytest.raises(PairingError, match="does not match"):
            window.redeem("wrong", "chrome-extension://" + "a" * 32, policy)
        assert not window.is_open

    def test_redeem_without_a_window_fails(self) -> None:
        with pytest.raises(PairingError, match="no pairing window"):
            PairingWindow().redeem(
                "x", "chrome-extension://" + "a" * 32, OriginPolicy("127.0.0.1", 8081)
            )

    def test_expired_window_fails(self) -> None:
        window = PairingWindow(ttl=0.01)
        code = window.open()
        time.sleep(0.05)
        with pytest.raises(PairingError, match="expired"):
            window.redeem(code, "chrome-extension://" + "a" * 32, OriginPolicy("127.0.0.1", 8081))

    def test_close_is_idempotent(self) -> None:
        window = PairingWindow()
        window.open()
        window.close()
        window.close()
        assert not window.is_open


class TestClientHeader:
    """REQ API-013 — the CSRF defence.

    A cross-origin HTML form can POST to loopback, but it cannot set a custom
    header: doing so forces a CORS preflight, which the origin policy rejects.
    """

    @pytest.mark.parametrize("client", ["ui", "extension", "mcp", "cli"])
    def test_accepts_known_clients(self, client: str) -> None:
        assert require_client(client) == client

    def test_is_case_insensitive(self) -> None:
        assert require_client("UI") == "ui"

    def test_absent_header_is_refused(self) -> None:
        with pytest.raises(AuthError, match=CLIENT_HEADER):
            require_client(None)

    def test_empty_header_is_refused(self) -> None:
        with pytest.raises(AuthError):
            require_client("")

    def test_unknown_client_is_refused(self) -> None:
        with pytest.raises(AuthError, match="unknown client"):
            require_client("attacker")
