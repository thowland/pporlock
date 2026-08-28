# pporlock MCP server

A stdio MCP server that lets an agent read what pporlock captured and author the
modules that change it. It is an ordinary HTTP client of the daemon's control
API on `127.0.0.1:8081` (SPEC-1 §11, REQ MCP-001): it imports nothing from the
`pporlock` package, opens no database, and touches no state on disk except the
bearer token, which it reads and never writes.

## Running it

```bash
uv run pporlock-mcp                      # defaults: http://127.0.0.1:8081, ~/.pporlock/token
uv run pporlock-mcp --read-only          # introspection tools only (REQ MCP-032)
uv run pporlock-mcp --base-url http://127.0.0.1:8081 --state-dir ~/.pporlock
```

`PPORLOCK_TOKEN` overrides the token file; `PPORLOCK_STATE_DIR` overrides where
it is looked for. Nothing else is required beyond a daemon that has run once
(REQ MCP-002).

Register it with an MCP client:

```jsonc
{ "mcpServers": { "pporlock": { "command": "pporlock-mcp", "args": [] } } }
```

## The intended loop (SPEC-1 §11.4)

1. `start_recording` → reproduce the problem in the browser → `stop_recording`
2. `list_session_flows` + `get_provenance` to find what broke and why
3. `suggest_rule_from_flow` or hand-written YAML → `validate_module`
4. `create_module` (**not** enabled) → `dry_run` against the session → read diffs
5. iterate on 3–4 until the dry run is clean
6. `set_module_enabled` — the one step that touches live browsing

## Guardrails

| Requirement | How it is enforced | Test |
|---|---|---|
| MCP-003 / CAP-043 — no unmasking | `unmask`, `unredact`, `reveal`, `unmask_field` are refused in `client.request` and again in `PporlockMCP.call_tool`, before any request is made. No tool schema mentions them. | `test_client.py::test_unmask_query_parameter_is_refused`, `test_server.py::test_an_unmask_attempt_through_a_tool_fails` |
| MCP-004 — provenance always | `require_provenance` raises `ContractViolation` on any flow-bearing response whose flows lack provenance. | `test_tools_introspection.py::test_list_flows_rejects_a_page_without_provenance` |
| MCP-030 — create never enables | `create_module`/`update_module` build a body of `{name, files}` only; `enabled` is not in either schema, and the response restates that a separate `set_module_enabled` call is required. | `test_tools_authoring.py::test_no_authoring_tool_can_send_an_enabled_flag` |
| MCP-031 — audit tagging | `X-Pporlock-Client: mcp` is set on every request in `ControlClient._headers`, so no tool can forget it. | `test_tools_control.py::test_every_mutating_control_tool_sends_the_client_tag` |
| MCP-032 — read-only mode | `ToolRegistry.build(read_only=True)` registers only the introspection family, so the write tools are not advertised at all. | `test_server.py::test_read_only_mode_hides_the_write_families` |
| MCP-005 — token discipline | Bounded page sizes, summary-by-default detail, truncated module sources and diffs, every cap stated in the tool description. | `test_server.py::test_every_tool_description_states_its_cost_or_its_guardrail` |

## Default response caps (REQ MCP-005)

| Tool | Default | Ceiling |
|---|---|---|
| `list_flows` | `detail=summary`, 50 flows | 200 |
| `list_session_flows` | `detail=summary`, 50 flows | 200 |
| `get_flow` | `detail=full` (no large bodies) | `detail=bodies` on request |
| `get_provenance` | provenance only | — |
| `flow_stats` | aggregates 200 summary flows | 1000 |
| `list_websocket_messages` | 50 frames | 200 |
| `read_module` | each file truncated to 8000 chars | `full=true` |
| `dry_run` | 200 flows evaluated, 20 per-flow results, `include_diffs=false` | 500 flows; diff text capped at 2000 chars |

Everything else returns a single small object.

## Development

```bash
uv sync --group dev
uv run ruff format . && uv run ruff check .
uv run mypy src            # strict
uv run pytest --cov        # >= 80% (G2); currently ~99%
uv run bandit -r src
```

Every test runs against an `httpx.MockTransport` fake of the control API. No
test starts a daemon or opens a socket.

## Build integration

No root `Makefile` change is required: the `mcp` component is already wired into
`setup`, `mcp`, `test`, `coverage`, `lint`, `format`, and `security`. For
reference, these are the lines that carry it:

```make
MCP        := mcp
MCP_SRC    := $(shell find $(MCP)/src/pporlock_mcp -name '*.py' ! -name '__init__.py' 2>/dev/null)

setup:
	cd $(MCP) && $(UV) sync --group dev

.PHONY: mcp
mcp:
	cd $(MCP) && $(UV) build

test:
	@echo "==> G3 mcp"
	cd $(MCP) && $(UV) run pytest

coverage:
	@echo "==> G2 mcp"
ifeq ($(strip $(MCP_SRC)),)
	@echo "    SKIP mcp — no product source yet (exempt)"
else
	cd $(MCP) && $(UV) run pytest --cov --cov-report=term-missing --cov-fail-under=80
endif

lint:
	cd $(MCP) && $(UV) run ruff format --check . && $(UV) run ruff check .
	cd $(MCP) && $(UV) run mypy src

security:
	cd $(MCP) && $(UV) run bandit -q -c pyproject.toml -r src
	cd $(MCP) && $(UV) run pip-audit --skip-editable
```

Note that the `coverage` target's `MCP_SRC` guard now evaluates non-empty, so
the mcp component is no longer exempt from G2 — which is correct, it has product
source as of this sprint.
