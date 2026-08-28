# Open issues

Findings that are real but were out of scope for the sprint that found them.
Each says who found it, why it was not fixed there, and what closing it needs.

---

## OI-3 — `POST /state` silently discards `proxy_running`

**Found:** Sprint 14 (MCP), while coding `proxy_start` / `proxy_stop` against the
contract.

`contracts/openapi.yaml` `StatePatch` declares `proxy_running: boolean` and
SPEC-0 §6.4 says the route starts and stops the proxy listener.
`control/app.py::post_state` reads `dev_toggles` and discards everything else.

The failure mode is the bad kind: a caller gets **200** plus a state payload
saying the proxy is still running. An agent calling `proxy_stop` is told it
worked. A silent discard on a route whose contract promises an effect is worse
than a 501.

**To close:** either implement listener start/stop, or reject unknown
`StatePatch` keys with a 400 so the contract and the code agree. The daemon was
owned by the Sprint 13 agent when this was found.

---

## OI-4 — `clients.mcp_connected` is hard-coded to `0`

**Found:** Sprint 14 (MCP).

`_state_payload` returns `{"clients": {"mcp_connected": 0, "mcp_read_only": false}}`
unconditionally. REQ MCP-033 wants an MCP activity indicator in the web UI, and
there is nothing for it to read.

There is also no mechanism for the MCP server to *register* its connection — the
control API has no endpoint for it. So MCP-033 is not implementable as specified
from either side.

**To close:** a design decision first (a registration endpoint? inferred from
`X-Pporlock-Client` on recent requests, with a TTL?), then daemon work. Inferring
from recent request tags is cheaper and needs no new endpoint, but reports
"recently active" rather than "connected", which is arguably the more useful
signal anyway.

---

## OI-5 — `GET /sessions/{id}/flows` filter vocabulary disagrees with the prose

**Found:** Sprint 14 (MCP). **CLOSED.**

SPEC-0 §6.8 said "the same filter vocabulary as §6.5" and the OpenAPI declared
four of the seventeen. The prose is normative under CLAUDE.md's precedence rule,
so the OpenAPI was widened rather than the prose narrowed — a TST-005 contract
test written against the narrow version would have passed while being wrong.

`tab_id` is the deliberate exception. Attribution is a property of the live
browser session, and a recorded session's tab ids refer to tabs that no longer
exist.

Also added while there: `PATCH /sessions/{id}` (rename, REQ CAP-021) and
`GET /sessions/{id}/export` (REQ CAP-024), both implemented and tested in
Sprint 13 with no OpenAPI entry.

---

## OI-6 — No audit tool in the MCP surface

**Found:** Sprint 14 (MCP).

REQ MCP-031 requires MCP actions to be auditable and visible in the web UI.
SPEC-1 §11.2's tool table lists no audit tool, so an agent cannot read back its
own recorded actions.

Not added, because inventing a tool outside the specified table is exactly the
kind of quiet scope drift the spec exists to prevent. But an agent that cannot
see what it is recorded as having done is a gap worth an explicit decision.

---

## OI-7 — Duplicated not-found guard in `get_module`

**Found:** Sprint 14 (MCP), reading the control app.

`control/app.py::get_module` has the same `if module is None: return
self._not_found(...)` block twice. Harmless, and the second is unreachable.
Cosmetic, but it reads as a merge artefact and should go.

---

## OI-8 — Module enablement does not survive a daemon restart

**Found:** Sprint 11.

Enablement is registry state, not manifest state. Persisting it means rewriting
user-authored `module.yaml` files, which is a design call rather than a bug fix.

**To close:** decide where user state lives. Writing it back into the manifest
means the daemon edits files the user owns; a sidecar state file avoids that but
adds a second source of truth for "is this module on". Needs deciding before
v1.0.

---

## OI-9 — `Profile.exclusions_add` is persisted but not applied

**Found:** Sprint 11.

Parsed and stored, never applied on activation. Unwinding exclusions on a
profile switch has no defined semantics in the requirements, and inventing them
mid-sprint would be a design decision made by accident.

**To close:** define what happens to a connection already tunnelled under the
outgoing profile's exclusions, then implement.


---

## OI-10 — `state_dir` does not cascade to `modules.root`

**Found:** Sprint 15, writing the banner E2E test.

`ModulesConfig.root` defaults to `DEFAULT_STATE_DIR / "modules"` — a constant
resolved at import, not derived from the configured `state_dir`. Setting
`state_dir` in a config file therefore moves the token, the sessions and the
rules file, and leaves modules loading from `~/.pporlock/modules`.

The E2E test that found it was reading the developer's real modules while
believing it had an isolated state directory. A test that silently uses
production data is a test whose result means nothing.

**To close:** derive the path-valued defaults from the effective `state_dir`
while still honouring an explicitly-set `modules.root`. Assigned to the Sprint
14 daemon work.

---

## OI-11 — the running daemon did not build what the sprints delivered

**Found:** Sprint 15, writing the banner E2E test. **CLOSED** — recorded because
the lesson outlives the fix.

`cli/runner.py` built no `ModuleRegistry` and no `ProfileManager`. Sprint 11
delivered the loader, the registry, contexts, quarantine, profiles, and 13
control API routes, with 1248 tests passing. None of it was connected to the
process `pporlock run` starts. `ControlApp` got `registry=None`, so every module
route answered 404; the `Evaluator` got no registry, so no module rule and no
Python hook ever touched live traffic. Sprint 13 closed on top of that state.

**Unit tests cannot catch this class of bug**, because a unit test constructs
the objects it exercises and so cannot notice that the daemon does not. Only
running the real thing finds it.

Two consequences, both now standing practice:

- `tests/unit/test_runner.py::TestStartupWiring` asserts the wiring exists, and
  anything new that must run in the daemon gets a case there.
- A sprint's exit demo is not optional and is not a formality. Both the sprints
  that shipped this passed every automated gate.
