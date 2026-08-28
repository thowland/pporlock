"""Errors surfaced to the MCP client.

Two kinds, deliberately distinguished:

* ``ControlApiError`` — the daemon answered, and said no. The daemon's uniform
  error body (SPEC-0 §6.2) is carried through so the agent sees the real reason.
* ``GuardrailError`` — the MCP server itself refused. These are the requirements
  of REQ MCP-003/MCP-030/MCP-032 and never reach the network.

``ContractViolation`` is a third, rarer case: the daemon answered with something
the contract says cannot happen (a flow with no provenance, REQ MCP-004). It is
a bug report, not a user error, and it is loud on purpose.
"""

from __future__ import annotations

from typing import Any


class PporlockMcpError(Exception):
    """Base for everything this server raises deliberately."""

    code = "mcp_error"

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self)}}


class ControlApiError(PporlockMcpError):
    """The control API returned a non-2xx response."""

    code = "control_api_error"

    def __init__(self, status: int, body: dict[str, Any] | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}

    def to_dict(self) -> dict[str, Any]:
        detail = self.body.get("error") if isinstance(self.body.get("error"), dict) else {}
        return {
            "error": {
                "code": self.code,
                "status": self.status,
                "message": str(self),
                "daemon": detail,
            }
        }


class GuardrailError(PporlockMcpError):
    """The MCP server refused the call before making a request."""

    code = "guardrail"

    def __init__(self, message: str, requirement: str) -> None:
        super().__init__(message)
        self.requirement = requirement

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": str(self),
                "requirement": self.requirement,
            }
        }


class ContractViolation(PporlockMcpError):
    """The daemon returned something SPEC-0 says it cannot return."""

    code = "contract_violation"


class ConfigurationError(PporlockMcpError):
    """The MCP server cannot start — usually a missing token file."""

    code = "configuration_error"
