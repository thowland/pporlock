# Open issues

Findings that are real but were out of scope for the sprint that found them.
Each says who found it, why it was not fixed there, and what closing it needs.

---

## OI-3 — `POST /state` silently discards `proxy_running`

**Found:** Sprint 14 (MCP), while coding `proxy_start` / `proxy_stop` against the
contract. **CLOSED** — implemented rather than refused. The listener really
starts and stops, and the route polls until it observably has, raising 409 on
timeout. Unknown `StatePatch` keys and unknown dev toggles are now 400 rather
than discarded. Refusing with 400 alone would have been the cheaper close, but
`proxy_start`/`proxy_stop` are in the MCP tool table and would have become dead
tools.

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

**Found:** Sprint 14 (MCP). **PARTLY CLOSED.** `mcp_connected` is now inferred
from recent `X-Pporlock-Client: mcp` requests with a 60s TTL — no new endpoint,
and "recently active" is the more useful signal anyway.

`mcp_read_only` remains `false` and is documented as **unobservable**: nothing
on the wire carries the MCP server's `--read-only` flag, and inferring it from
an absence of mutating calls would present a guess as a fact. Closing that half
needs a protocol field.

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

**Found:** Sprint 14 (MCP), reading the control app. **CLOSED** — it was already
gone from HEAD by the time anyone looked. A regression test now asserts its
absence rather than pretending to have removed it.

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

**CLOSED.** `load_config` tracks which settings the caller actually stated at any
precedence level and moves only the unstated ones, so an explicit `modules.root`
still wins — including one that happens to equal the default.

An adjacent bug fell out: `PPORLOCK_STATE_DIR` was parsed as section `state`,
key `dir`, and rejected. The one setting the whole layout hangs off could not be
set from the environment at all.

**Still open:** assigning `cfg.state_dir` *after* construction does not cascade.
Several tests do exactly that, and a property setter was more surgery than the
sprint warranted.

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

---

## OI-12 — PRF-001 is not met, and not by tuning

**Found:** Sprint 16, measuring it.

```
PRF-002  PASS   0.0057 ms p95 engine overhead    budget 2 ms    (~350x headroom)
PRF-001  FAIL   +327% p50 added page latency     budget 15% p50
```

The benchmark was not adjusted until it passed. The decomposition says where
the time goes and it is not the rules engine:

| | |
|---|---|
| engine decision path | 0.004 ms/flow |
| total added | 1.54 ms/request |
| over one reused connection | 0.70 ms/request |
| against a 30 ms-RTT origin | +16.4% p50 |

**99.7% of the added latency is mitmproxy's per-request pipeline**, not
pporlock's. The loopback figure is unfairly harsh — a 0.4 ms baseline makes any
fixed cost look enormous — but even corrected for realistic origin latency the
15% p50 budget is missed.

**To close:** this is a scoping decision, not an optimisation task. Either
PRF-001 is restated in terms the architecture can meet (a per-flow overhead
budget, which PRF-002 already covers with enormous margin), or it is measured
the way it is written — Chrome against a real origin — which needs a harness
this project does not have. Making the rules engine faster cannot move it.

---

## OI-13 — `Outcome.SKIPPED_SHORT_CIRCUIT` is declared and never emitted

**Found:** Sprint 16.

It is in the SPEC-0 §4.3 taxonomy, in the contract, and in the completeness
tests every client renders against — for a state nothing in `src/` produces.

Not removed, because removing a value from a published enum is a contract
decision rather than a tidy-up. Either the engine should emit it (a rule skipped
*because* an earlier one short-circuited is arguably worth distinguishing from
never being reached) or it should leave the taxonomy.

---

## OI-14 — three routes the OpenAPI does not describe

**Found:** Sprint 16, by TST-005.

- `GET /rules` and `PUT /rules` — served, implemented, tested, used by the web
  UI's rule editor, described in SPEC-0 §6, absent from `contracts/openapi.yaml`.
- `POST /pair/begin` — served, and driven by `pporlock pair`. `/pair`
  (redemption) is declared; the half that mints the code is not.
- `POST /flows/{flow_id}/suggest-rule` declares only `200` but validates
  `intent` and answers `400`, so a generated client has no error case.

The first two sit in a named `UNDECLARED_ROUTES` allowlist with a test that
**fails once the OpenAPI catches up**, so the exemption cannot quietly become
permanent.

---

## OI-15 — should a module be able to extend the note taxonomy?

**Found:** Sprint 16.

`ctx.note("some_new_code", …)` from a module-registered transform used to raise
`ValueError` and take down the entire body phase — one module's typo breaking
every rule after it. That is fixed: an unrecognised code degrades to a
`MODULE_ERROR` note carrying the requested code.

The design question underneath is open. Every client renders notes from a closed
vocabulary with a completeness test, so a module-invented code has nowhere to be
described. Either modules are confined to the taxonomy (current behaviour) or
the taxonomy gains an explicit extension mechanism with a rendering fallback.

---

## A note on process

Three of this project's most serious defects were invisible to a full, green
test suite, and each was found by running the real thing:

- **OI-11** — two sprints shipped a module system the daemon never constructed.
  Found by an end-to-end banner test.
- **The wire-shape bugs** — `GET /modules` returned an array the client did not
  expect, and the module library threw on first contact with a real daemon.
  Found by taking a screenshot.
- **Query-string secrets written to disk unredacted** — the header path was
  masked, the query path was not. Found by walking the security checklist by
  hand rather than trusting the scanners.

The common shape: a test that constructs its own subject cannot notice the
subject is never built, and a test that stubs its own client agrees with
whatever the client already believed. Exit demos, real-system screenshots, and
hand-walked security review are not ceremony here — they are the only things
that have ever found this class of bug.
