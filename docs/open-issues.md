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

**Found:** Sprint 14 (MCP).

SPEC-0 §6.8 says the route takes "the same filter vocabulary as §6.5". The
OpenAPI path declares only `host`, `limit`, `cursor`, `detail`.

TST-005 adds contract tests against the OpenAPI in Sprint 16. If the OpenAPI is
authoritative there, and the prose is authoritative for implementers, the two
will disagree and the test will be right for the wrong reason.

**To close:** decide which is normative (CLAUDE.md's precedence rule says SPEC-0
outranks generated artefacts, so the OpenAPI should be widened) and make them
match before TST-005 lands.

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
