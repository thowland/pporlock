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

**A second half of the same gap, found while writing `user-agent-switcher`
(0.8.0).** There is no code in SPEC-0 §4.4 for *a module did the ordinary thing
it is enabled to do*. The nearest surface, `ctx.log`, is drained by the
evaluator and consumed by nothing — `ModuleContext.logs` has no reader in
`daemon/src/` at all — so a module wanting to say "I sent this request as
Googlebot" has a choice between a note that arrives as `MODULE_ERROR` and a log
line that goes nowhere. `user-agent-switcher` does neither: it keeps a tally and
renders it through `on_report`, which works but is a workaround for a missing
surface, not a use of one. Closing OI-15 should decide both — an `info`-severity
module note, or a reader for `ctx.log`.

---

## OI-31 — module settings

**Found:** while adding `user-agent-switcher` (0.8.0). **CLOSED** — implemented,
because there was no way to configure a module short of editing YAML.

A module's `config:` block always reached `ctx.config`, and nothing could ask a
module *what* it could be configured with. So the web UI could offer nothing but
the file editor, and "browse as GPTBot instead of Googlebot" meant opening
Monaco and finding the right line — the exact interaction the module library
already exists to avoid for `enabled`.

`settings:` is the missing declaration (SPEC-0 §5.2.1): a flat list of typed
fields with labels and defaults, which the daemon validates against and the web
UI renders as a form. Six types, no nesting. **Deliberately not JSON Schema** —
the expressive half of JSON Schema is unrenderable as a form, and a declaration
that can say more than the UI can show is a declaration whose author will be
surprised.

Decisions worth not re-litigating:

- **Values go in the sidecar, not the manifest.** The same rule as `enabled`
  (OI-8): the daemon does not rewrite a file it does not own. It also means an
  author improving a `default:` still moves every value nobody has changed.
- **Only what the user changed is stored.** Storing the whole form would freeze
  today's defaults into the user's state forever. `GET /modules/{name}` therefore
  serves each field's *effective* default — the manifest's `config:` value where
  it states one — so a client can tell "unchanged" from "set to the same thing".
- **A refused PATCH writes nothing at all.** Not the good fields with the bad
  one dropped: a module running on a mixture of what the user asked for and what
  survived validation is worse than one that refused.
- **A settings change does not reload the module.** A reload re-runs `on_load`,
  which for a module accumulating an audit is "throw the audit away". The live
  `ctx.config` is replaced in place and `on_config` is called if declared.
- **There is no secret type.** Values are stored in the sidecar and served in
  clear; the absence of the type is what keeps the sidecar's "nothing here is a
  secret" docstring true. The file is written `0600` anyway.

**Verified by using it**, not only by testing it: through a daemon started by
`pporlock run` with real traffic crossing the proxy, and then through the built
web UI in a real browser — where selecting an identity and pressing Save wrote
only the changed keys and the next request through the proxy went out under the
new user agent. What is *not* covered is a Playwright E2E, so that run guards
nothing tomorrow; see the sprint-log entry.


---

## OI-32 — `proxy_stop` could be silently dropped, for ever

**Found:** as an intermittent `make gate` failure while releasing 0.8.0.
**CLOSED** — and it was not the flaky test it looked like.

`test_start_brings_it_back` failed intermittently with:

```
pporlock.errors.ProxyControlError: the proxy listener did not stop within 1.0s
```

which reads as "the 1.0s budget is too tight". It is not. Measured, a stop or a
start completes in about **50 ms**; raising the budget to 20 s changed nothing,
because in the failing case the listener never stopped **at all**.

**The actual cause is in mitmproxy's contract.** `Master.run()` binds the
listener in `setup_servers()` and only *afterwards* triggers the `running` hook,
which is what sets `Proxyserver.is_running`. And `Proxyserver.configure` drops an
option change outright while that flag is False — no queue, no retry. So there is
a window in which:

- the port is bound and accepting, so every external check says the proxy is up;
- `master.options.update(server=False)` is accepted and silently does nothing;
- the listener then stays up for ever, because nothing re-applies the change.

We reported that as a timeout, which sent everyone looking at the budget.

**This was a daemon bug, not a test bug.** `POST /state {"proxy_running": false}`
arriving in that window returns 409 on a listener that is still serving traffic —
the OI-3 failure mode arriving from the other direction, and worse, because here
the proxy keeps intercepting after the user asked it to stop. The tests only made
it visible: they create many masters in one process, so they hit the window far
more often than a daemon that creates one.

**Fixed in the adapter**, which is where mitmproxy's version churn belongs
(SPEC-1 §2.1). `set_proxy_running` now waits for the listener addon to be
*accepting* configuration changes before commanding one, and only then waits for
the state to change — two waits, because "not delivered" and "not honoured" are
different failures that want different things done about them, and they no longer
share a message. An addon with no `is_running` is treated as ready: a disappearing
attribute should cost the guard, not the feature.

**Evidence, since "it stopped failing" is not evidence for an intermittent bug.**
A standalone reproduction creates six proxies in one process and stops each:
before the fix, five of six stops hung until timeout and the port kept accepting;
after it, twelve of twelve start/stop operations succeed and the port really
closes. Three consecutive full `make gate` runs are green. The two new unit tests
in `test_interceptor.py` encode mitmproxy's contract in a fake and were watched
failing against the unfixed code, with the same "did not stop" message the
original bug produced.

**One correction to the record.** The 0.8.0 release notes said this failed "3 in 3
under coverage", which was wrong: that measurement ran a single test file with
`--cov`, and the non-zero exit was the `fail-under` threshold, not a test failure.
The real trigger is many masters in one process, which is why the whole suite
reproduced it and one file rarely did. The number was wrong; the bug was real, and
worse than the number suggested.


---

## OI-33 — the shipped exclusion list was never in the repository

**Found:** by CI, on its first run. **CLOSED** in 0.9.0.

`daemon/src/pporlock/data/exclusions-default.yaml` — the 33 default ClientHello
exclusions required by REQ PXY-013 — was absent from this repository **from the
first commit until 0.9.0**. A *global* gitignore on the author's machine
(`~/.gitignore_global`, containing a bare `data`) matched the directory, so
`git add -A` never picked it up and `git status` was always clean.

**What a clone actually got.** A proxy with an empty exclusion list, which would
terminate TLS for every one of the hosts that list exists to leave alone: OS
update endpoints (`swscan.apple.com`), browser updates
(`update.googleapis.com`), certificate revocation (`ocsp.digicert.com`),
banking (`www.chase.com`) and payments (`api.stripe.com`). Every one of those
entries is there because interception breaks that host or because decrypting it
is a thing this tool should not do by default. This shipped in every public
release up to and including v0.8.1.

**Why nothing caught it.** Six tests assert the list is present, correct and
non-trivial — `TestShippedDefaults` in `test_exclusions.py`, and the E2E's
"the daemon ships a non-trivial default exclusion list", which is described in
its own comment as *"the precondition for the test that matters"*. All of them
passed on every machine that had ever run the daemon, because the file was
sitting right there. A test cannot distinguish "shipped" from "present on this
disk" without asking something outside the working tree. Nothing did.

This is the fifth entry in the same family as the four in `CLAUDE.md`, and the
purest one yet: **the tests were correct, comprehensive, and unanimous, and the
artefact was still wrong.** It is also precisely the failure the never-run exit
demo — a clean install on a fresh machine, following only `docs/install.md` —
exists to catch. CI was the first thing that had ever looked at a fresh clone.

**Closed with three things, not one.**

- The file is tracked (`git add -f`).
- `.gitignore` carries an explicit negation for `daemon/src/pporlock/data/`. A
  repository's own ignore file takes precedence over the global one, so this is
  what stops a personal ignore rule deciding what the project ships.
- `test_toolchain.py::test_every_file_the_package_needs_is_actually_in_the_repository`
  compares every file under `src/pporlock` against `git ls-files` and names any
  that are missing. Watched failing: untracking the file makes it fail with that
  filename and nothing else. This generalises — the next file a stray ignore
  rule swallows fails here rather than in someone's clone.

**A second half, found while proving the first.** Cloning `v0.8.1` from GitHub
and loading it confirms the severity is not theoretical: `load_exclusions()`
returns **0 entries**, and `swscan.apple.com`, `ocsp.digicert.com` and
`www.chase.com` are all reported as *not* excluded. It fails **silently** — and
`pporlock doctor`, whose entire job is to notice this, reported
`exclusions_load: pass`. It loaded the list, got nothing, found no undocumented
entries in that nothing, and said everything was fine. The tool that should have
caught this was actively reassuring.

`check_exclusions` now fails on an empty list, with remediation naming what is
being intercepted as a result. An empty list is a broken installation, not a
configuration choice: there is no supported way to have no exclusions
(REQ PXY-013). Watched failing.

**Left open deliberately:** whether `load_exclusions()` should *raise* when the
shipped default is missing, rather than returning an empty list. Its own
docstring already argues the case for the user file — "silently falling back to
defaults would leave the user believing an exclusion is in force when it is not,
which for a financial or pinning entry is exactly the wrong way to fail" — and
that reasoning applies at least as strongly to package data. But
`test_missing_default_file_is_tolerated` says the tolerance is deliberate and
tested, and making it fatal decides whether a broken install refuses to start.
That is a maintainer's call, not a drive-by change. The `doctor` check makes the
condition loud either way.

**The lesson is about the guard, not the file.** A test that reads the working
tree is asking the wrong machine. Anything that must be *shipped* has to be
checked against what was *committed*.


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

---

## OI-19 — the extension's pairing did not survive a daemon restart

**Found:** by a user, whose recording failed with `403 origin not permitted`
after the daemon had been restarted. **CLOSED** (pairing persisted to a
`state_dir` sidecar, with validation).

`OriginPolicy` held the paired extension id in memory only. Both construction
sites — `control/server.py::build_state` and `ControlApp.__init__` — passed
`extension_id=None`, and nothing ever wrote it down. So every daemon restart
silently revoked the extension.

The failure is worse than "unpaired" because the token *is* persisted. The
extension came back holding a valid bearer token, from an origin the policy no
longer recognised, and got a 403 whose message — "origin not permitted" —
describes the mechanism and not the cause. Nothing in it suggests re-pairing,
and the extension had not changed. The daemon is a launchd agent that restarts
at login, so this was routine rather than an edge case.

This is structural rule 8's other half. OI-8 and OI-9 moved module enablement
and the active profile into `state_dir` sidecars; the pairing was missed. It
had no test asserting it outlived the process, because every test that pairs
also constructs the policy it pairs against.

**The sidecar is validated, not trusted.** `paired-extension` holds one
extension id and nothing else — not a secret, since the id rides in the Origin
header of every request the extension makes — but it decides which origin may
drive the control API. A value that is not a well-formed id is discarded and the
daemon starts unpaired, which `pporlock pair` recovers from; refusing to boot
over a corrupt file would not be recoverable.

Verified against a real daemon: pair, restart, and the paired origin gets 200
where it previously got 403, while a different extension origin is still
refused.

**Adjacent, not fixed:** `pporlock run` reports `Error logged during startup,
exiting...` with no detail when its port is already held. Hit three times while
verifying this. Same shape as OI-18 — a true message that names nothing
actionable.

---

## OI-20 — attribution coverage is below the OI-2 criterion, deterministically

**Found:** while adding the restart E2E spec. **OPEN.**

`attribution.spec.ts::OI-2 DECISION` measures 0.9038 (47 of 52 flows) against a
0.95 threshold, and does so on every run — the same ratio to sixteen decimal
places four times in a row. It is not flake, and re-running will not clear it.

It is not caused by the OI-17/18/19 work: none of those diffs touch the
attribution path, and the spec uses a fresh temp `state_dir` and never restarts
its daemon, so the pairing sidecar is written and never read during it. The
failure is a coverage *ratio*, not an auth error — a broken pairing would show
up as a 403 on `POST /attribution` instead.

The likely cause is environmental drift: OI-2's criterion was established by a
spike that measured 100% with `<all_urls>` granted, and which requests
`chrome.webRequest` reports has moved with Chrome versions before.

**Do not lower the threshold to close this.** 0.95 is the OI-2 decision
criterion — the number that chooses between the primary attribution mechanism
and the fallback. Changing it changes the decision, and would be exactly the
coverage laundering G4 exists to prevent. What this needs is identifying which
five flows are unattributed and whether they are a class the mechanism cannot
reach, which is an OI-2 question and not a test-tuning one.

---

## OI-21 — the throughput ceiling is mitmproxy's, and it is one core

**Found:** a user reporting that some sites overwhelm the system. **OPEN** as a
scoping question; the measurement is done and `make bench-saturation` keeps it
reproducible.

`bench.run` measures serial added latency (PRF-001/002). Nothing measured
concurrency, which is what a heavy page actually produces — a hundred
subresources in flight at once. Measured against the fixture origin on a
14-core machine:

| | peak rps | p50 @ 32 clients |
|---|---|---|
| direct, no proxy | ~3200 | 1.3 ms |
| bare `mitmdump`, no pporlock addon | ~730 | 42 ms |
| pporlock, real daemon | ~630 | 47 ms |

Throughput plateaus at ~680 rps and stops responding to concurrency entirely
(677 rps at 16 clients, 678 at 32) while p50 grows linearly with client count —
the signature of a saturated single-threaded server. Under load the daemon sits
at **~85% of one core with thirteen idle**, because mitmproxy is a
single-threaded asyncio program. More cores do nothing.

**This closes the "can we compile it" question.** Between 86% and 96% of the
achievable ceiling is already reached (the range is capture: an addon-only
harness with a `NullSink` measures ~96%, a real daemon with the real `RingSink`
~86%). A perfect compiler that made every line of pporlock's Python free would
lift ~630 rps to ~730. mypyc or Cython would win a fraction of that, against
OI-12's finding that the engine decision path is already 0.004 ms/flow. The
optimisation target is not this codebase.

**What the user is feeling is probably queueing, not throughput.** 680 rps is
ample for browsing; 47 ms p50 at 32 concurrent requests is not, and a page with
200 subresources exceeds that concurrency instantly.

**Options, none free:**

1. *Reduce per-flow work in capture.* The honest ~14% — body caps copy up to
   512 KiB per flow, the ring holds it, and the SSE hub fans out per flow. Real,
   bounded, and does not touch the ceiling.
2. *Run several mitmproxy instances.* The only thing that uses the other
   thirteen cores. It fragments capture across processes, so the ring, sessions
   and provenance would all need to merge — a substantial architectural change
   to a system whose single-process model is load-bearing.
3. *Restate the goal.* Consistent with OI-12's conclusion: PRF-001 and this are
   the same finding on two axes, and both say the budget describes an
   architecture the project did not choose.

Do not close this by tuning. Anything claiming a speed-up should show it on
`make bench-saturation`, against the baseline row rather than in isolation.

---

## OI-22 — the fail-safe could not tell a busy daemon from a dead one

**Found:** by a user, whose extension kept disabling itself on complex sites.
**CLOSED** (failure kinds distinguished; thresholds and messages split).

`HealthMonitor.check` wrapped the health request in `catch { ok = false }`,
discarding *why* it failed. Two consecutive failures of any kind tripped the
fail-safe, cleared Chrome's proxy and told the user to start a daemon that was
already running.

**The diagnosis came from the user, not the tests:** they could re-enable the
extension without restarting the daemon. That is only possible if the daemon
was alive throughout, which makes every one of those trips a false positive.

The two causes are now separate facts:

| | Meaning | Trips after |
|---|---|---|
| `refused` | Nothing is listening. Definitive. | 2 — unchanged |
| `timeout` | No answer within the budget. A saturated daemon is still a live one. | 5, with the budget doubling each time to a 12 s cap |

A total-failure ceiling of 5 catches any mixture, so alternating causes cannot
reset each other's counter forever and leave the fail-safe permanently disarmed.

**The change can only make tripping less likely, never more.** An unrecognised
error shape classifies as `refused` and gets the pre-existing behaviour, so a
genuinely dead daemon is never detected more slowly than before. The E2E suite
still SIGKILLs a real daemon and asserts the proxy clears.

The message is split too, for the reason OI-18 records: telling someone to run
`pporlock run` when it is already running sends them to re-run the one thing
that cannot help. `daemon_unresponsive` says the daemon may be overloaded and
points at `pporlock status`.

**A negative result worth keeping: the listen backlog is not the problem.**
The other half of this work was to be raising the accept backlog, on the theory
that a saturated single-threaded acceptor refuses connections and Chrome shows
that as a hard failure. It does not. mitmproxy calls `asyncio.start_server`
with the default backlog of 100, and **800 simultaneous connections were
accepted with zero refusals — on both the proxy and control ports, against both
an idle and a fully saturated daemon.** No change was made, because the premise
did not survive measurement. The refusals the user saw are better explained by
the false trips above: the proxy is cleared mid-page-load and requests already
in flight fail. If refusals persist after this fix, that theory is wrong and
this is the place to start again.

---

## OI-23 — a flow that failed left no trace in the flow table

**Found:** chasing a user's 502 on `vumerity.com`. **CLOSED** (failed flows are
recorded with their reason).

`Interceptor.error` incremented a counter and stopped. A request that never
completed produced no row in the ring, so a user hitting a 502 saw the browser
fail, opened the flow table whose entire job is explaining traffic, and found
the request missing. `/metrics` showed `errors: 4` while `ring_flows` was 1 —
the tool counted the failure somewhere nobody was looking.

For a traffic inspector this is the worst possible omission: the flows that
fail are the ones being investigated.

Failed flows now carry a `FlowError` (`message`, `from_client`) through
`contracts/schemas/flow.schema.json`, the ring, the API and the console. There
is deliberately no synthesised response — a row with a reason and no status is
the honest shape; inventing a status would make the table lie about what the
browser received. `from_client` separates a browser cancelling from an origin
refusing, which are opposite events and identical in a count.

**Two bugs found on the way, both worth recording.**

*The tee.* Every unit test constructed the sink it exercised, so all of them
passed while the running daemon dropped every error record. `cli/runner.py`
wraps `RingSink` in a `TeeSink`, and `TeeSink` inherits `NullSink` — so it
inherited a `record_error` that counted and returned. Not a crash: a silent
no-op that looked correct. This is OI-11 exactly, and it is why
`test_flow_errors.py` now builds the tee the daemon actually builds.
`NullSink`'s counting default is right for a stub and dangerous for a base
class, and a test now pins every sink the daemon can construct.

*Colliding ids.* `_flow_id_of` fell back to `unknown-<iso timestamp>` at second
resolution, so flows with neither request nor response in the same second
collided and the ring kept one. Latent while only completed flows were
recorded; reachable the moment failures were — and a page failing to load
produces many at once, which is precisely when losing all but one is worst.

**The original 502 was not reproduced.** `vumerity.com` and `avonex.com` were
tried through the proxy with curl, headless Chromium and real Chrome — apex,
`www`, `http://`, and the explicit `https://host:443/` form their redirect
uses. All returned 200. Their certificates cover both apex and `www`, and
neither publishes an ECH config. The `https://vumerity.com:443/` redirect comes
from the origin and is byte-identical with and without the proxy. If it recurs,
the flow table will now say why — which is the actual deliverable here.

---

## OI-24 — the popup showed one version, and it was the other one's

**Found:** a user asking whether the reported version was stale. **CLOSED**
(both versions shown, labelled, with a mismatch marker).

The popup footer rendered a bare `v0.1.0`. That number came from
`GET /state` — it was the **daemon's** version. The extension's own version
appeared nowhere in the UI.

The version itself was correct: `0.1.0` has been the declared version of the
daemon, MCP server, web UI and extension since Sprint 0 (`6b5492c`) and has
never been bumped in any of them. What moves is the sprint tags, which is
probably what made it look stale.

The real problem is what the number could not tell you. The four components are
built and installed separately, and the extension is loaded unpacked — so it
goes stale the moment it is rebuilt without being reloaded, which is the
ordinary case while developing. In that state the footer reports the half that
was updated and says nothing about the half that was not, so a fix that has not
actually been loaded looks like a fix that did not work.

The footer now reads `ext 0.1.0 · daemon 0.1.0`, with a warning marker when
they differ and `daemon —` when it is unreachable. Absent is shown as unknown
rather than blank: a missing daemon version is a fact worth stating, not an
empty slot.

**Not done: nothing was bumped.** Choosing a version scheme and deciding what a
release means here is a separate decision, and inventing one to make a number
look fresher would be the wrong kind of fix.

---

## OI-25 — one version, declared nine times and checked nowhere

**Found:** following OI-24. **CLOSED** (single source, generated everywhere,
gated).

The version appeared independently in nine places: `daemon/pyproject.toml`,
`mcp/pyproject.toml`, two `package.json`s, the extension manifest, and four
Python literals. Nothing checked that they agreed, and predictably nothing ever
moved them — every one still said `0.1.0` from Sprint 0 (`6b5492c`) while
eighteen sprints and a dozen fixes shipped.

That is not a cosmetic problem. "Which version are you on" is the first
question of every diagnosis, and a number that has never changed cannot answer
it. It is the same failure as OI-24 one level down: the system knew what it was
and had no way to say so.

`VERSION` at the repository root is now the source. `scripts/version.py`
propagates it, `make version-check` fails the gate on drift, and `bump-minor` /
`bump-patch` move it. Python no longer carries a literal at all — the daemon and
MCP server read their own installed distribution metadata, so the number
reported at runtime is the version of the package actually installed rather
than a string someone remembered to edit.

**Policy** (the user's, recorded so it is not re-derived): a significant merge
bumps the minor; a bundle of small ones bumps the patch. Bump on the branch,
before the merge.

**One trap handled rather than discovered later.** A Chrome manifest `version`
must be one to four dotted integers, so `0.3.0-rc.1` is not a legal value and
an extension carrying one fails to load — at install time, far from the change
that caused it. The manifest takes the numeric core and the full semver goes in
`version_name`, which is the field Chrome provides for this and which the popup
now prefers.

Set to **0.2.0**. The eighteen sprints and everything before this are 0.1.0 as
a matter of record; retroactively renumbering merged history would be fiction.

---

## OI-26 — `map_local` and `redirect` were reported as blocked

**Found:** a user enabling `css-tamper` and seeing its own stylesheet appear in
the flow table as blocked. **CLOSED** (contract, SPEC-0, daemon and UI).

Three actions short-circuit request evaluation — `block`, `map_local`,
`redirect` (REQ MOD-012) — and the sink derived a single flag from all three:

```python
blocked = provenance.short_circuited_by is not None
```

So `css-tamper`'s `serve-user-stylesheet` rule, which serves a file from the
module's `assets/` and returns **200**, produced a row flagged `BLK`. The module
was working exactly as designed and the flow table said it had blocked the page
it was styling. The row also carried `modified` at the same time, contradicting
the invariant the sink's own comment asserted.

**This was not drift — the contract said the same thing.** SPEC-0 §6.5 read
"`blocked` is true when the flow was short-circuited", and `flow.schema.json`
described the field as "The flow was short-circuited". Implementation and
contract agreed with each other and both misdescribed the system: a field named
`blocked` that is true when a file was served successfully will be misread by
everyone, every time. Fixing the code alone would have re-introduced the drift
this project keeps finding, so the schema and SPEC-0 were corrected too.

Now `blocked` means the client was **denied** the response it asked for — a
`block`, by stub or by kill — and a new `short_circuit` field names which of
the three ended evaluation, or null. `map_local` and `redirect` count as
modifications, because both hand the browser a response it uses. The web UI
renders `LOC` and `RDR` in the accent colour rather than `BLK` in the error
colour, so they no longer read as failures at a glance.

**A regression caught by an existing test, worth recording.** The first attempt
set the action at the top of `_apply_short_circuit`, before dispatch — which
reported a *failed* block (an unknown stub, an asset outside the module) as
having blocked a flow that actually proceeded. `test_an_unknown_stub_is_an_error
_not_a_crash` failed immediately. The action is now recorded only when the
decision shows it took effect, and each action signals that differently: a block
kills or substitutes, `map_local` substitutes, a redirect retargets.

**Test replaced, not deleted (G4):** `test_blocked_is_derived_from_short_circuit`
asserted that any short-circuit sets `blocked`, which is the bug stated as a
requirement. It is replaced by four cases pinning each action separately plus
the no-short-circuit case.

**Counters changed with it.** `counters.blocked` counted every short-circuit, so
the status bar reported blocks that never happened; it now counts refusals, and
`map_local`/`redirect` count as modified.

---

## OI-27 — a Python hook that replaces a response is invisible in the flags column

**Found:** writing the Python module tutorial, by running it. **OPEN.**

`short_circuit` (OI-26) is set from the declarative path: `_apply_short_circuit`
records which rule ended evaluation. A Python hook returning
`RequestMutation(short_circuit=ctx.synthesize(...))` never goes through that
path, so the flow carries `short_circuit: null` and `blocked: false` and shows
no badge — despite having replaced the network as completely as a `map_local`
rule would.

Provenance does record it, but as `action: headers, outcome: applied,
rule_name: on_request` — traceable, and mislabelled. A hook that synthesised a
whole response did not edit headers.

**Why it matters more than it looks.** The flags column is how a hundred rows
are scanned for the one that went wrong. A module author checking whether their
hook fired looks there first, sees nothing, and concludes it did not run — the
exact failure mode this system exists to prevent, and the reason the tutorial
now tells the reader not to trust the flags column for hooks.

**To close:** the hook path should record a short-circuit the same way the
declarative path does, and the provenance entry should carry an action that
describes what happened rather than `headers`. Both are small; the reason this
is filed rather than fixed is that it wants a decision about what the action is
*called* — a module-synthesised response is not `map_local`, and inventing a
value has contract consequences for every client that renders the field.

---

## OI-28 — the DevTools panel was never built

**Found:** by a user opening the pporlock tab in DevTools and seeing nothing.
**CLOSED** (entry point declared, guard added).

`devtools.ts` registers the panel with

```js
chrome.devtools.panels.create('pporlock', '', 'src/devtools/panel.html');
```

and that path appears nowhere else — not in the manifest, not in an import.
CRXJS discovers pages by walking the manifest, and `vite.config.ts` listed only
`src/popup/options.html` as an extra entry point. So nothing in the chain knew
`panel.html` existed and it was never emitted.

**Everything looked fine.** The build succeeded. The extension loaded. Chrome
created a DevTools tab named "pporlock" — the registration worked, the panel was
just pointing at a file that was not there — and rendered it blank, with no
error in the page console, the extension console, or the build output.

A page referenced only from a **string literal inside JavaScript** is invisible
to the bundler, the manifest validator, the type checker and every unit test,
because all of them see the panel's *source*, which was present and correct the
whole time. `PanelView.test.tsx` passed throughout.

**Guard:** `src/build-inputs.test.ts` extracts the path from every
`panels.create()` call and asserts it is an entry point the build knows about —
declared to rollup or reachable from the manifest. It was watched failing
against the original config before being kept.

Verified by loading the built extension in Chrome and opening
`chrome-extension://<id>/src/devtools/panel.html`: 200, React mounts, and it
renders its "Not paired" state on an unpaired profile, with no page errors.

---

## OI-29 — a module could accumulate a report and had nowhere to put it

**Found:** a user asking how a module's findings are meant to be discovered.
**CLOSED** (`on_report` hook, `GET /modules/{name}/report`, link in the module
library).

`ctx.store_*` is persistent (REQ MOD-022) and **no API can read it**. A module
that tallies something — an audit, a diff, a count — could accumulate for weeks
into a store nothing outside the module could open.

`gpc-audit` worked around it by answering a magic path through the proxy. That
seemed fine until the same user opened the web UI and could not find it: the
control origin is not proxied traffic, so `/__pporlock__/gpc-report` returns
401 there. The report was readable **only while browsing some other site**, by
someone who remembered the URL. A feature that exists and cannot be found.

Modules now render their own report via `on_report(ctx)` and the daemon serves
it at `GET /modules/{name}/report`, linked from the module library where
someone would actually look. `has_report` on the module summary keeps the link
off modules that have none, so the column is not a row of 404s. `gpc-audit` was
migrated and dropped its short-circuit rule — it no longer inspects every
request to check whether it is the report.

**Why the module renders rather than the daemon.** The alternative was a
generic store-reading endpoint. It was rejected for two reasons: raw key/value
is not a report and the UI cannot present arbitrary shapes usefully, and
module stores are **not redacted** — a module storing captured values would
have had them served through the control API by a route that never considered
it.

**Sandboxed, and honest about why.** The body is module-authored and this
origin also serves the web UI and holds the bearer token, so responses go out
under a `sandbox` CSP with `nosniff`, and the content type is restricted to
four text-ish types. Module code is trusted and unsandboxed and could reach the
token by other means: this is not a security boundary, it is a refusal to add a
*convenient* one, and it costs nothing. The library links with `target=_blank`
rather than embedding, for the same reason.

A report that raises is a 502 naming the module, not a quarantine — a broken
report is no reason to stop a module modifying traffic correctly, and the two
failures are unrelated.

---

## OI-30 — the report link could never have worked

**Found:** by a user clicking the report link one commit after it shipped.
**CLOSED** (fetched with the token, rendered in a sandboxed frame).

OI-29 linked to the report with a plain anchor:

```html
<a href="/modules/gpc-audit/report" target="_blank">report</a>
```

A `<a href>` is a navigation, and a navigation carries no `Authorization`
header. The route requires a bearer token, so every click returned
`missing or invalid bearer token`. Not a race, not a config problem — the
design could not have worked, and it shipped with three passing tests because
all three asserted the anchor's attributes rather than that clicking it
produced a report.

**The obvious repair is forbidden.** Putting the token in the URL would make it
work immediately and is ruled out outright: it would land in browser history,
referrer headers, and the audit log. So the UI fetches with the header it
already holds and renders the result itself.

**Rendering it was the second trap.** A `blob:` URL opened in a tab is the
natural way to show fetched HTML — and blob URLs *inherit the creating origin*,
so module-authored HTML would have run same-origin with the page that holds the
bearer token. That is strictly worse than the magic URL it replaced. The report
is shown in an `<iframe sandbox="" srcdoc=...>` instead: no `allow-scripts`, no
`allow-same-origin`, a unique opaque origin that can render a table and nothing
else. Non-HTML types render as text, so CSV or JSON cannot smuggle markup past
the daemon's content-type allowlist.

**Tests replaced, not deleted (G4).** The two that asserted `href` and
`target=_blank` described a design that could never have worked. Five replace
them, including one that asserts the client method is *called* — the thing the
originals could not see.

Verified by driving the real web UI against a real daemon: the button appears,
the frame renders the audit, and no error is shown. The previous version passed
its unit tests and failed on the first click, which is the whole reason that
check now exists.
