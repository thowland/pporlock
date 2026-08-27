"""Access control — SPEC-0 §6.1, SPEC-1 §7.2.

Loopback binding is the primary control, and it is asserted in code rather than
defaulted (REQ API-010). Everything here is the second layer, and it defends
against a specific, real threat that loopback binding does not:

    Any web page you visit can issue requests to http://127.0.0.1:8081.

Without a token and an origin policy, a page could enable a module, flip a
development toggle, or read your captured traffic. That is not a remote
attacker; it is any tab you have open. Hence:

* a per-install bearer token, file mode 0600 (REQ API-011)
* an Origin allowlist — our own origin and the paired extension (REQ API-004)
* a required non-simple header on mutating requests, which forces a CORS
  preflight and so cannot be produced by a cross-origin HTML form (REQ API-013)

The extension never reads the filesystem; it redeems a short-lived pairing code
instead (REQ API-012).
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import AuthError, PairingError

TOKEN_BYTES = 32
TOKEN_FILE_MODE = 0o600
STATE_DIR_MODE = 0o700

#: Required on every mutating request. Any non-simple header would do; naming it
#: after the client also makes the audit log's origin field trustworthy.
CLIENT_HEADER = "x-pporlock-client"
VALID_CLIENTS = frozenset({"ui", "extension", "mcp", "cli"})

EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")

PAIRING_TTL_SECONDS = 120
PAIRING_CODE_WORDS = 4


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The outcome of authenticating one request."""

    client: str
    origin: str | None


class TokenStore:
    """The per-install bearer token."""

    __slots__ = ("_token", "path")

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir).expanduser() / "token"
        self._token: str | None = None

    def ensure(self) -> str:
        """Load the token, generating it on first run.

        Written 0600 inside a 0700 directory. This is the only secret pporlock
        stores, and anything able to read it can read your traffic.
        """
        if self._token is not None:
            return self._token

        if self.path.exists():
            self._token = self.path.read_text().strip()
            self._enforce_permissions()
            if self._token:
                return self._token

        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, STATE_DIR_MODE)
        token = secrets.token_urlsafe(TOKEN_BYTES)
        # Create with restrictive permissions from the outset rather than
        # writing then chmod-ing, which leaves a window where it is readable.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(token)
        self._token = token
        return token

    def _enforce_permissions(self) -> None:
        try:
            mode = self.path.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            os.chmod(self.path, TOKEN_FILE_MODE)

    def verify(self, presented: str | None) -> bool:
        """Constant-time comparison.

        A timing side channel on a loopback socket is a stretch, but the cost of
        getting this right is one function call.
        """
        if not presented:
            return False
        return hmac.compare_digest(presented, self.ensure())


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an Authorization header."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


class OriginPolicy:
    """Which origins may talk to the control server."""

    __slots__ = ("_extension_id", "_self_origins")

    def __init__(self, host: str, port: int, extension_id: str | None = None) -> None:
        self._self_origins = {
            f"http://{host}:{port}",
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        }
        self._extension_id = extension_id

    @property
    def extension_id(self) -> str | None:
        return self._extension_id

    def pair_extension(self, origin: str) -> str:
        """Record the paired extension. Only this origin is accepted afterwards."""
        if not EXTENSION_ORIGIN.match(origin):
            raise PairingError(f"not a chrome extension origin: {origin!r}", origin=origin)
        self._extension_id = origin.removeprefix("chrome-extension://")
        return self._extension_id

    def allows(self, origin: str | None) -> bool:
        """An absent Origin is allowed; a present, unrecognised one is not.

        Browsers always send Origin on cross-origin requests, so a request with
        none did not come from a page. curl and the CLI legitimately omit it,
        and they still need the bearer token.
        """
        if origin is None:
            return True
        if origin in self._self_origins:
            return True
        if self._extension_id is not None:
            return origin == f"chrome-extension://{self._extension_id}"
        return False


class PairingWindow:
    """A short-lived code the extension exchanges for the token (REQ API-012).

    Short-lived and single-use: the window is opened by an explicit human action
    (``pporlock pair`` or a button in the web UI), so a code that outlived that
    action would be a standing credential nobody remembered issuing.
    """

    __slots__ = ("_code", "_expires_at", "ttl")

    def __init__(self, ttl: float = PAIRING_TTL_SECONDS) -> None:
        self._code: str | None = None
        self._expires_at = 0.0
        self.ttl = ttl

    def open(self, *, now: float | None = None) -> str:
        moment = time.time() if now is None else now
        self._code = "-".join(secrets.token_hex(2) for _ in range(PAIRING_CODE_WORDS))
        self._expires_at = moment + self.ttl
        return self._code

    def close(self) -> None:
        self._code = None
        self._expires_at = 0.0

    @property
    def is_open(self) -> bool:
        return self._code is not None and time.time() < self._expires_at

    def redeem(self, code: str, origin: str, policy: OriginPolicy) -> None:
        """Consume the code and record the extension origin.

        Single-use regardless of outcome: a wrong guess closes the window rather
        than allowing another attempt.
        """
        expected = self._code
        expires_at = self._expires_at
        self.close()

        if expected is None:
            raise PairingError("no pairing window is open — run `pporlock pair` first")
        if time.time() >= expires_at:
            raise PairingError("the pairing window has expired")
        if not hmac.compare_digest(code, expected):
            raise PairingError("pairing code does not match")
        policy.pair_extension(origin)


def require_client(header: str | None) -> str:
    """Validate the mutating-request client header (REQ API-013).

    This is the CSRF defence. A cross-origin HTML form can POST to loopback, but
    it cannot set a custom header — doing so forces a CORS preflight, which our
    origin policy rejects. So a request carrying a valid client header came from
    something that was allowed to make it.
    """
    if not header:
        raise AuthError(
            f"mutating requests require the {CLIENT_HEADER} header",
            header=CLIENT_HEADER,
        )
    client = header.strip().lower()
    if client not in VALID_CLIENTS:
        raise AuthError(f"unknown client {client!r}", header=CLIENT_HEADER)
    return client
