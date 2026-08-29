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

**Found:** Sprint 11. **CLOSED** — sidecar state file.

Enablement and priority are *user* state; the manifest is the *author's* file.
Recording the first in the second means the daemon rewrites a file it does not
own, losing comments and formatting the first time someone flips a switch in the
UI. So it does not.

`<state_dir>/module-state.json` holds `{name: {enabled, priority}}`. The manifest
**seeds** an entry the first time a module is seen and the sidecar wins
thereafter — exactly the in-memory rule `ModuleRegistry.reload` already applied
across a reload; it just did not outlive the process. One consequence is worth
stating plainly: **editing `enabled:` in a manifest after first sight does
nothing.** The API is where enablement is set, and the only place it is audited.

The path is passed explicitly by `cli/runner.build_evaluator` from
`config.state_dir`, deliberately *not* derived from `modules.root` — that is
separately configurable (OI-10) and holds module content, not user state.

- Every change writes through immediately. This daemon is a launchd agent that
  gets killed rather than stopped; there is no shutdown hook to trust.
- Writes are atomic (temp file + rename), so a crash leaves the previous file
  rather than a truncated one that would read as corrupt.
- `reload` prunes rows for modules no longer on disk, so a reinstall does not
  inherit a decision made about different code. A module that merely failed to
  *load* keeps its state: a typo is not a deletion.
- A corrupt or unwritable sidecar is skipped the way a malformed profile is —
  manifest defaults, the reason on `registry.state.error`, and the startup banner
  prints it. Silently reverting every module to "off" would look like the modules
  had stopped working.
- The file holds a module name, a boolean and an integer. No secret, so no
  redaction concern; a test asserts the written shape so that stays true.

Verified against a daemon started by `pporlock run`, not only in unit tests
(OI-11's lesson): enable through the API, restart, `GET /rules` still serves the
module's rules at the priority that was set.


## OI-9 — `Profile.exclusions_add` is persisted but not applied

**Found:** Sprint 11. **CLOSED** — semantics defined, then implemented.

The effective exclusion list is the **base** list (shipped defaults plus the
user's own) **plus** the active profile's `exclusions_add`, the additions tagged
`source: "profile"`. `ControlApp.apply_exclusions` recomputes it from the base on
profile activation, on deletion of the active profile, and on `PUT /exclusions`,
and installs it on both the interceptor and its evaluator. Because it always
recomputes from the base, switching away takes the outgoing profile's entries
off; the base is never mutated by a profile.

`PUT /exclusions` replaces the *base* and drops entries marked
`source: "profile"`, so a GET-then-PUT round trip in the UI cannot silently adopt
a profile's additions as the user's own — which would have made them impossible
to get rid of.

**The connection already tunnelled cannot be un-tunnelled.** `ignore_connection`
means mitmproxy never terminated the TLS: there are no plaintext bytes to reach
into and no session key to acquire after the fact. So a list change applies to
**new connections only**, and Chrome holds keep-alive connections — a host can
keep tunnelling for as long as one stays open after the entry that excluded it
has gone. The inverse holds too: an addition does not reach into a connection
already being decrypted. Stated in the docstring, covered by tests, and guarded
by one asserting `ignore_connection = True` appears exactly once in the addon, so
a second decision point in a later sprint fails the claim rather than rotting it.

**A bug fell out.** `interceptor.exclusions` and `interceptor.evaluator.exclusions`
are separate references, and `PUT /exclusions` updated only the first. The two
then disagreed about which hosts were excluded — which is how a dry run stops
predicting live behaviour (REQ CAP-031).

**And a dependency, now also closed.** The active profile itself was not
persisted: `ProfileManager._active` was in-memory, so a restart returned to
`default` and with it to the base exclusion list. The startup application of
profile exclusions therefore had no effect at all, and the feature worked until
the first restart and then silently stopped. The active profile is now remembered
in `<state_dir>/active-profile`; a profile deleted meanwhile falls back to
`default`, and neither an unreadable file nor an unwritable location stops the
daemon or fails the activation.


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

**Found:** Sprint 16, by TST-005. **CLOSED.**

`contracts/openapi.yaml` declares `GET /rules`, `PUT /rules` and
`POST /pair/begin`, and `POST /flows/{flow_id}/suggest-rule` has its `400`.

The `UNDECLARED_ROUTES` allowlist and its guard test are **both removed**, not
emptied. With an empty allowlist `test_every_served_route_is_declared` already
catches the next undeclared route, which is all the allowlist protected; the
guard's `frozenset() & declared` would have been vacuously empty forever, which
is precisely the "exemption that quietly becomes permanent" it existed to
prevent.

**A mismatch surfaced while closing it, and it is fixed.** `rule.schema.json` is
the schema a human writes a rule *against*, and it is
`unevaluatedProperties: false` deliberately, so a misspelled rule key is an error
rather than a setting that silently does nothing. `GET`/`PUT /rules` return
**compiled** rules, which additionally carry `rule_id`, `module` and `priority` —
so a compiled rule could not validate against the schema those responses pointed
at.

There is now a `CompiledRule` schema describing what the routes actually return.
It deliberately does not `$ref` the authoring schema: `unevaluatedProperties`
inside a referenced schema cannot see properties contributed by an adjacent
`allOf` branch, so composing the two produces a schema nothing can satisfy.
Loosening the authoring schema instead would have let someone write `priority:`
on a *rule* and have the editor accept it — and rule-versus-module priority is
already the easiest thing here to confuse. The authoring shape is validated where
authoring happens: the loader, and `POST /validate`.


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

---

## OI-16 — SPEC-0 §8 described a module API that did not exist

**Found:** while writing the sample module library. **CLOSED** (spec corrected,
one implementation gap fixed).

SPEC-0 §8 is the module API *stability contract* — the thing user- and
agent-authored modules are supposed to be written against and survive upgrades
on. It disagreed with the implementation in six places, and a module written
faithfully from the spec would have raised `TypeError` on its first flow:

| Member | Spec said | Reality |
|---|---|---|
| `ctx.matches` | `matches(*, host, path, …)` | takes `request` positionally first — the context is per-module and long-lived, so it cannot know which request you mean |
| `ctx.note` | `note(code, severity, message)` | `note(code, message, severity="warning")` |
| `ctx.register_transform` | `(name, fn, schema)` | `(name, fn, cost="expensive")` — cost is what the scheduler needs; there is no schema validation for module transforms |
| `ctx.asset_path` | returns `str` | returns `pathlib.Path` |
| `ctx.stub_for` | `stub_for(dest)` | `stub_for(dest, request)` — the Accept-header fallback needs the request |
| `ctx.asset_text` | absent | exists |

In each case the implementation was right and the spec was wrong — two of the
spec's signatures are not implementable as written. §8.2 and §8.3 now describe
what actually exists, and additionally state what mutation objects hooks return,
which the contract had never said at all.

**One real gap, fixed:** `on_websocket_message` was listed in `HOOK_NAMES`, was
loadable, and **was never invoked by anything**. A module defining it loaded
cleanly, reported healthy, and did nothing. `Evaluator.observe_websocket_message`
now dispatches it from the interceptor's WebSocket path, with the same error
isolation and quarantine as the other hooks, and a returned value is explicitly
ignored because frames are inspection-only in v1 (REQ PXY-051).

**Why this matters more than a documentation slip.** SPEC-0 is the one document
an agent authoring a module through MCP is pointed at. Every module written from
it would have been broken in the same way, and the failure would have surfaced
as a `module_error` note blaming the author's code.

---

## OI-17 — the install guide named a make target that never existed

**Found:** by a user following `docs/install.md`. **CLOSED** (docs corrected,
guard added).

`docs/install.md` §2 and `README.md`'s quickstart both said `make ext`. The
target is `make extension`, and has been for the whole life of the project.
Anyone following the install guide literally — which is exactly what its own
header instructs — hit `No rule to make target 'ext'` on the third command.

Pulling that thread found the larger one immediately behind it. `make setup`
builds the daemon into `daemon/.venv/` and does **not** put `pporlock` on the
`PATH`, yet §3 opens with `pporlock run`. On a machine that has never installed
the CLI globally, `which pporlock` fails — including the machine this was found
on. The guide had no step between "build it" and "run it". §2 now has one, with
both forms: `uv tool install ./daemon`, or `uv run` from the repo, matching the
two-form pattern `docs/llm-with-mcp.md` already used for `pporlock-mcp`.

**Why it survived everything.** The Makefile is not imported by any test, the
docs are prose, and every developer who ever built the extension already knew
the real target name and already had the CLI on their PATH. `make gate` was
green throughout. It is the same shape as OI-11 and the wire-shape bugs: the
suite verifies the system, and nothing verified the documented path *to* the
system.

**Guard:** `daemon/tests/unit/test_docs_commands.py` extracts every
`make <target>` from the fenced and inline code spans of the six user-facing
docs and asserts each target is declared in the Makefile. It matches only code
spans, not prose — an earlier wordlist-based version flagged "make it
impossible" and "make sure". It was watched failing against the real `make ext`
string before being kept.

**Still not closed by this.** A guard on `make` targets is the cheapest slice of
the documented path, not the path itself. Sprint 16's exit demo on a genuinely
fresh machine remains unrun and remains the highest-value outstanding item in
the project — this issue is a direct sample of what it would turn up.

---

## OI-18 — a non-editable install can never find the web UI, and said so wrongly

**Found:** by a user, immediately after OI-17's fix told them to install the CLI.
**CLOSED** (docs corrected, diagnostic split, `web_assets_dir()` given tests).

`uv tool install ./daemon` copies the daemon into its own venv. `web_assets_dir()`
resolves the UI either from `<package>/web` inside a wheel or from
`<repo>/web/dist` four parents up — and from a tool venv, neither exists. The
wheel does not bundle the UI, and nothing above the installed package is the
repo. So the UI is unreachable, and **rebuilding it changes nothing**.

The install docs written for OI-17 recommended exactly that command, so the fix
for one install bug created the next one. `uv tool install --editable ./daemon`
keeps the CLI pointed at the checkout and resolves both.

**The worse half was the message.** Both the startup line and the 404 body said
`not built — run make web`, naming the one command that was already done and
could not help. A wrong diagnostic is more expensive than a missing one, because
it gets followed: the reporter ran `make web`, watched it succeed, and got the
same error. `web_assets_hint()` now distinguishes the two causes — a checkout
has `web/package.json` whether or not it has been built — and names the actual
fix for each.

**Why it survived.** `web_assets_dir()` had no test at all, despite deciding
whether the UI is served. Every developer ran from the repo, where the fallback
happens to work. `daemon/tests/unit/test_web_assets.py` now covers all three
lookup outcomes and both hints; the hint test asserts the string `make web` is
*absent* from the unreachable-install case, which is the exact regression.

Verified against a real daemon started from outside the repo: `GET /` returns
200 and the index, where before it returned 404.
