"""The stdio MCP server (SPEC-1 §11, REQ MCP-001, MCP-002).

`PporlockMCP` owns three things and nothing else: a `ToolRegistry` filtered by
read-only mode, a `ControlClient` pointed at the daemon, and the MCP protocol
plumbing that connects them. All the interesting behaviour is in `tools.py` and
`guardrails.py`, which are testable without any transport at all.

Startup requires nothing but an installed daemon that has run once: the base URL
defaults to the documented loopback address and the token is read from the state
directory (REQ MCP-002).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import mcp.types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from .client import DEFAULT_BASE_URL, ControlClient, assert_no_forbidden_params, read_token
from .errors import ConfigurationError, PporlockMcpError
from .tools import CONTROL, ToolRegistry, ToolSpec

SERVER_NAME = "pporlock"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = """\
pporlock intercepts and modifies your HTTPS traffic locally. These tools read what
it captured and author the modules that change it.

The intended loop (SPEC-1 §11.4):
  1. start_recording -> reproduce the problem in the browser -> stop_recording
  2. list_session_flows + get_provenance to find what broke and why
  3. suggest_rule_from_flow or hand-written YAML -> validate_module
  4. create_module (it is NOT enabled) -> dry_run against the session -> read diffs
  5. iterate on 3-4 until the dry run is clean
  6. set_module_enabled -- the one step that touches live browsing

Two things this interface deliberately cannot do:
  * It cannot enable a module as a side effect of creating or updating one
    (REQ MCP-030). Enabling is always a separate, explicit call.
  * It cannot unmask redacted values (REQ MCP-003). Cookies, Authorization
    headers, and credential-shaped JSON keys arrive as
    «redacted:sha1=abcd,len=42» and stay that way. Unmasking exists only in the
    web UI, only on live flows, only per value.

Module Python is trusted and unsandboxed. Anything you write into a module runs
with full access to intercepted traffic.

Every tool states its token cost. Listings default to summary detail and a
bounded page size; ask for bodies one flow at a time.
"""


def _tool_annotations(spec: ToolSpec) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=spec.name,
        read_only_hint=not spec.mutating,
        destructive_hint=spec.mutating and spec.family == CONTROL,
        idempotent_hint=not spec.mutating,
        open_world_hint=False,
    )


def to_mcp_tool(spec: ToolSpec) -> types.Tool:
    return types.Tool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        annotations=_tool_annotations(spec),
    )


def _text(payload: Any) -> types.TextContent:
    return types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))


class PporlockMCP:
    """MCP stdio server; an ordinary HTTP client of the control API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        read_only: bool = False,
        *,
        client: ControlClient | None = None,
    ) -> None:
        if client is None and not token:
            raise ConfigurationError(
                "a bearer token is required; read it with read_token() or inject a client"
            )
        self.base_url = base_url
        self.read_only = read_only
        self.registry = ToolRegistry.build(read_only=read_only)
        self.client = client or ControlClient(base_url, token or "")
        self.server: Server[None] = Server(
            SERVER_NAME,
            version=SERVER_VERSION,
            instructions=INSTRUCTIONS,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
        )

    # -- MCP handlers ----------------------------------------------------

    async def _on_list_tools(
        self,
        _context: ServerRequestContext[None],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.list_tools())

    async def _on_call_tool(
        self,
        _context: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        return await self.call_tool(params.name, dict(params.arguments or {}))

    # -- the testable surface --------------------------------------------

    def list_tools(self) -> list[types.Tool]:
        return [to_mcp_tool(spec) for spec in self.registry.specs]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Dispatch one tool call.

        Errors are returned as ``is_error`` results rather than raised: an agent
        that asked for something refused needs to read *why* — particularly for
        the guardrail refusals, which are the difference between "this failed"
        and "this interface will never do that, stop trying".
        """
        try:
            # REQ MCP-003 — refuse an unmask attempt at the door rather than
            # silently dropping the argument. An agent that is quietly ignored
            # will try again; one that is told no, and why, will not.
            assert_no_forbidden_params(arguments)
            spec = self.registry.get(name)
            payload = await spec.handler(self.client, arguments)
        except PporlockMcpError as exc:
            return types.CallToolResult(content=[_text(exc.to_dict())], is_error=True)
        except Exception as exc:
            return types.CallToolResult(
                content=[_text({"error": {"code": "unexpected", "message": str(exc)}})],
                is_error=True,
            )
        return types.CallToolResult(content=[_text(payload)])

    async def serve_stdio(self) -> None:
        options = InitializationOptions(
            server_name=SERVER_NAME,
            server_version=SERVER_VERSION,
            capabilities=self.server.get_capabilities(
                notification_options=None, experimental_capabilities=None
            ),
            instructions=INSTRUCTIONS,
        )
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, options)

    async def aclose(self) -> None:
        await self.client.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pporlock-mcp",
        description="MCP stdio server for the pporlock control API.",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Control API base URL (default {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Where to read the bearer token from (default ~/.pporlock).",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Expose only the introspection tools (REQ MCP-032).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import anyio

    args = build_parser().parse_args(argv)
    try:
        token = read_token(args.state_dir)
    except PporlockMcpError as exc:
        print(json.dumps(exc.to_dict()), file=sys.stderr)
        return 2

    server = PporlockMCP(args.base_url, token, args.read_only)

    async def run() -> None:
        try:
            await server.serve_stdio()
        finally:
            await server.aclose()

    anyio.run(run)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
