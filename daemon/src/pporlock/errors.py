"""Exception hierarchy.

Every error carries a stable ``code``. That code appears verbatim in the API
error body (SPEC-0 §6.2) and the web UI, the DevTools panel, and the MCP client
all branch on it, so codes are part of the contract and are not renamed casually.
"""

from __future__ import annotations

from typing import Any


class PporlockError(Exception):
    """Base for every error this system raises deliberately."""

    code: str = "internal_error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def to_dict(self) -> dict[str, Any]:
        """The SPEC-0 §6.2 error body, minus ``trace`` which the server adds."""
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigError(PporlockError):
    code = "config_invalid"


class NonLoopbackBindError(ConfigError):
    """A listener was configured on something other than loopback.

    Refused at startup rather than defaulted around (REQ API-010). pporlock
    terminates TLS and holds session cookies in memory; exposing that on a
    routable interface is not a configuration option we offer.
    """

    code = "non_loopback_bind"


class ModuleLoadError(PporlockError):
    """A module failed to load. Only that module is affected (REQ MOD-005)."""

    code = "module_load_failed"


class ModuleApiVersionError(ModuleLoadError):
    """The module targets an unsupported module API version (REQ MOD-026)."""

    code = "module_api_unsupported"


class ModuleRuntimeError(PporlockError):
    """A module hook raised. Caught, attributed, and does not affect the flow."""

    code = "module_runtime_error"


class RuleValidationError(PporlockError):
    """A rule is structurally invalid. Raised at load time, never at request time."""

    code = "rule_invalid"

    def __init__(
        self,
        message: str,
        *,
        module: str | None = None,
        rule_index: int | None = None,
        field: str | None = None,
        **detail: Any,
    ) -> None:
        super().__init__(message, module=module, rule_index=rule_index, field=field, **detail)
        self.module = module
        self.rule_index = rule_index
        self.field = field


class TransformError(PporlockError):
    code = "transform_failed"


class AssetPathError(PporlockError):
    """An asset or map_local path escaped the module's assets/ directory.

    Path containment is checked after symlink resolution
    (implementation-plan.md §2.5 "Path traversal").
    """

    code = "asset_path_escape"


class SessionError(PporlockError):
    code = "session_error"


class AuthError(PporlockError):
    code = "unauthorized"


class PairingError(PporlockError):
    code = "pairing_failed"
