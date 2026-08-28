"""A minimal control-API client for the CLI — SPEC-1 §8, REQ PXY-003.

The module, profile, session and dry-run subcommands are all thin clients of the
running daemon's control API rather than second implementations of its logic.
That is deliberate: a CLI that loaded modules itself would be a second module
loader to keep in step with the first, and the two would disagree the first time
either changed.

``urllib`` rather than a client library because the CLI must start fast and this
is a handful of loopback requests. The host is always the configured control
host, which ``Config.validate()`` has already refused unless it is loopback
(REQ API-010), so there is no scheme or host reachable from here that is not
this machine.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..config import Config
from ..control.auth import CLIENT_HEADER
from ..errors import PporlockError


class ControlClientError(PporlockError):
    """The daemon could not be reached, or answered with an error."""

    code = "control_client_error"


class ControlClient:
    """Talks to a running daemon. One instance per CLI invocation."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.base = f"http://{config.control.listen_host}:{config.control.listen_port}"

    # -- token -----------------------------------------------------------

    @property
    def token_path(self) -> Path:
        return Path(self.config.state_dir).expanduser() / "token"

    def token(self) -> str:
        """Read the bearer token from disk.

        The CLI may do this; the extension deliberately may not (REQ API-012),
        which is what `pporlock pair` exists to bridge.
        """
        try:
            return self.token_path.read_text().strip()
        except OSError as exc:
            raise ControlClientError(
                f"no control token at {self.token_path}. Start the daemon once with "
                f"`pporlock run`.",
                path=str(self.token_path),
            ) from exc

    # -- requests --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
        authenticate: bool = True,
    ) -> Any:
        url = self.base + path
        if params:
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"

        headers = {"Content-Type": "application/json", CLIENT_HEADER: "cli"}
        if authenticate:
            headers["Authorization"] = f"Bearer {self.token()}"

        data = json.dumps(body).encode() if body is not None else None
        # Suppressed below: the scheme is the literal "http" in self.base and the
        # host is config.control.listen_host, which Config.validate() has refused
        # unless it is loopback. No file:// or custom scheme is reachable here.
        request = urllib.request.Request(  # noqa: S310  # nosec B310
            url, method=method, data=data, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc.read())
            raise ControlClientError(
                f"{method} {path} failed: {exc.code} {detail}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise ControlClientError(
                f"could not reach the daemon at {self.base}: {exc.reason}. "
                f"Is it running? `pporlock status`"
            ) from exc
        if not payload:
            return None
        return json.loads(payload)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def reachable(self) -> bool:
        """Is a daemon answering? Uses the one unauthenticated route."""
        try:
            payload = self.request("GET", "/state/health", timeout=2.0, authenticate=False)
        except PporlockError:
            return False
        return bool(isinstance(payload, dict) and payload.get("ok"))


def _error_detail(raw: bytes) -> str:
    """The message out of an API error body, or the body itself.

    Deliberately narrow: it returns only the ``error.message`` field, never the
    whole payload, so nothing incidental in an error response ends up echoed to
    a terminal or a shell history.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw.decode("utf-8", errors="replace")[:200]
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))[:200]
    return ""
