# Sprint Log

Running record of sprint execution. One section per sprint, appended at close.

Each entry records: requirements delivered, requirements deferred **with reasons**, gate results (G1–G7), decisions made, and the merge commit SHA.

**Status:** Sprint 0 complete. Next: Sprint 1 (contracts and scaffolding).

---

## Template

### Sprint NN — <name>

**Branch:** `sprint-NN-slug`
**Merged:** `<sha>` on `<date>`
**Tag:** `sprint-NN-complete`

**Requirements delivered:** REQ-xxx, REQ-yyy, …

**Requirements deferred:** REQ-zzz → Sprint MM. Reason: …

**Gate results:**
- G1 requirements met / exit demo: 
- G2 coverage: daemon __%, engine __%, mcp __%, web __%, extension __%
- G3 all tests pass: 
- G4 tests removed: 
- G5 lint: 
- G6 security (scanners + §2.5 areas reviewed): 
- G7 merged --no-ff: 

**Decisions:**

**Notes for the next sprint:**

---

## Sprint 00 — Environment and tooling

**Branch:** `sprint-00-environment`
**Tag:** `sprint-00-complete`

**Requirements delivered:** none by design — Sprint 0 produces no product code
(implementation-plan.md §3). REQ PXY-006 (exact mitmproxy pin) is established
here and asserted by test, though the addon that depends on it lands in S2.
REQ API-010 (loopback-only) gets its first client-side enforcement.

**Requirements deferred:** none.

**Gate results:**
- **G1** — exempt (no product requirements). All §3.7 exit criteria demonstrated:
  `make setup`, `make gate`, and `make fixtures` succeed; the gate was proven
  able to fail by planting a failing test and watching G3 break; the pre-commit
  hook was proven to reject planted private-key material and a lint error.
- **G2** — daemon SKIP, mcp SKIP (no product source, exempt). web 100%,
  extension 93.75%, both above the 80% floor. Both branches of the exemption
  rule exercised, which was the point of running it now.
- **G3** — 21 daemon + 4 mcp + 18 web + 18 extension tests pass. 3 Playwright
  E2E pass (2 web, 1 extension).
- **G4** — one test removed: the deliberate `test_gate_proof.py` failure probe,
  removed after demonstrating the gate can fail. No coverage laundering.
- **G5** — ruff, mypy (strict on engine/), eslint, tsc, prettier all clean.
- **G6** — bandit, pip-audit, npm audit (all three node components), gitleaks
  all clean. §2.5 areas touched: loopback binding (client-side guard added and
  tested), token handling (hook and gitignore guards against committing
  `~/.pporlock/token` and `.mitmproxy/`), dependency currency (see below).
- **G7** — merged `--no-ff`.

**Decisions:**

1. **mitmproxy pinned at 11.1.3** on Python 3.12.14. Verified `mitmdump` runs.
   Two tests guard the pin: one asserts `==` is used rather than a range, one
   asserts the resolved environment matches the declared version.

2. **Docker dropped from Sprint 0.** It was an errant paste in the original
   brief. The daemon is a macOS launchd agent intercepting local Chrome and
   cannot meaningfully run containerized. The one case that would have earned a
   container — fixture origin servers — is served by a plain Python server
   instead, which is faster to start and easier to debug.

3. **`uv` and Python 3.12 installed on this machine.** Neither was present;
   the system had 3.10/3.11/3.13/3.14 but not 3.12. `uv` manages the 3.12
   toolchain itself, which is why it was the right first install.

4. **Node dependency chain upgraded rather than suppressed.** The initial
   install carried two critical and one high advisory in the vite/vitest/esbuild
   chain. Upgraded to Vite 7 / Vitest 4 to clear them. A security gate that
   begins life with standing exceptions will not survive contact with schedule
   pressure.

5. **Lock files committed, overriding `~/.gitignore_global`.** The user's global
   ignore excludes `*.lock` and `package-lock.json`. Negation rules were added
   to the repository `.gitignore` with the reasoning recorded inline: an exact
   mitmproxy pin and a clean `npm audit` are both meaningless without a resolved
   lock.

6. **Extension E2E is a separate Playwright project and runs headed.** MV3
   extensions do not load in headless Chromium — confirmed empirically, not
   assumed. Proving the unpacked-load and service-worker-startup path works now
   removes a discovery risk from Sprint 5.

7. **Coverage exemption implemented in the Makefile, not left to discipline.**
   `make coverage` detects whether a component has product source and either
   enforces its threshold or prints an explicit SKIP naming the rule. Both
   branches were exercised this sprint.

8. **gitleaks installed** with a pporlock-specific bearer-token rule. A fallback
   scanner (`scripts/secret-scan.sh`) exists for machines without it and warns
   loudly rather than passing silently.

**Notes for the next sprint:**

- Sprint 1 replaces `contracts/schemas/health.schema.json` with the full set
  from SPEC-0 §3–§5 and adds the forbidden-import test (SPEC-1 §2.2).
- `web/src/lib/control-origin.ts` and `extension/src/shared/control-origin.ts`
  are currently duplicated by copy. The repo layout has no npm workspace by
  decision, so this is expected. If a third consumer appears, revisit.
- The Playwright web suite runs against `vite preview` on 4173. From Sprint 4
  it should target the daemon on 8081, which serves the built assets
  (REQ API-003).
- `mypy` reports the `mitmproxy.*` override as unused because no source imports
  it yet. That resolves in Sprint 2 when the addon lands.

---

## Sprint 01 — Contracts and scaffolding

**Branch:** `sprint-01-contracts`
**Tag:** `sprint-01-complete`

**Requirements delivered:** MOD-015 (rule JSON Schema published), MOD-002
(manifest fields), MOD-014 (strict validation, unknown keys are errors),
API-029 (OpenAPI specification), CAP-010 (provenance as a structural engine
return value), API-010 (loopback enforced in code), DD-2 / TST-001 (engine
purity, asserted per-module).

**Requirements deferred:** none. PXY-006 was re-established on a new pin — see
decision 2.

**Gate results:**
- **G1** — exit demo run. `make contracts` generates TypeScript that compiles;
  49 schema-conformance tests round-trip Python structures through the published
  schemas; the boundary test was demonstrated failing on both a planted
  `mitmproxy` import and a planted sibling reach-in, with actionable messages,
  then cleaned.
- **G2** — daemon 98.71%, engine 99.32% against a 90% bar (REQ TST-002), mcp
  exempt, web 100%, extension 93.75%.
- **G3** — 306 tests pass across all four components. No regressions in Sprint 0
  tests.
- **G4** — no tests removed.
- **G5** — ruff, mypy, eslint, tsc, prettier clean. One new suppression, in
  `[tool.ruff.lint.per-file-ignores]`: S104 in `tests/**`, because the literal
  `0.0.0.0` appears in test_config.py precisely to assert we refuse to bind it.
  Scoped to tests so the rule stays live everywhere it could catch a real
  mistake.
- **G6** — see decision 1. Now genuinely enforcing; clean after remediation.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **G6 was not actually enforcing, and it found real problems.** The Sprint 0
   Makefile carried `|| true` on `pip-audit`, so fifteen advisories were being
   printed and discarded. Removed. The gate now fails on any advisory not
   explicitly accepted in `.pip-audit-ignore`. An advisory that does not fail the
   gate is an advisory nobody reads.

2. **mitmproxy pin moved 11.1.3 -> 12.2.3.** 11.1.3 hard-caps `tornado<=6.4.2`
   and `pyOpenSSL<=25.0.0`, both below their fixed versions, so the advisories
   were unfixable without moving. SPEC-1 §2.1 treats a mitmproxy upgrade as
   deliberate work against the adapter layer — and the adapter does not exist
   until Sprint 2, so this is the cheapest this upgrade will ever be. Doing it
   later would have meant reworking the adapter to pay for a security fix.

3. **Transitive floors added inside mitmproxy's own caps**, closing seven more
   advisories: `cryptography>=48.0.1`, `pyOpenSSL>=26.0.0`, `flask>=3.1.3`,
   `Brotli>=1.2.0`. The resolver had settled below what mitmproxy actually
   permits. Brotli matters most: we decode brotli response bodies (REQ PXY-023),
   so that DoS was genuinely reachable.

4. **Ten advisories accepted, each with a written justification and a re-check
   trigger** in `.pip-audit-ignore`. The justifications are empirical, not
   assumed: `import mitmproxy.tools.dump` leaves both `tornado` and `msgpack`
   absent from `sys.modules`, and tornado is imported only by mitmweb and the
   console TUI, neither of which we run. Three of the four tornado advisories
   are in its HTTP *client*, which nothing on our path constructs.
   - The cryptography acceptance is a stated trade, not a shrug: 44.0.3 carried
     one advisory for a stale bundled OpenSSL, which for a TLS-terminating proxy
     is the broadest exposure available; 48.0.1 carries three narrow ones in
     PKCS#7 and X.509 path-building. We took the current OpenSSL.
   - **h2 PYSEC-2026-3628 is the one real residual.** Duplicate-Host request
     smuggling, availability-only, and h2 *is* loaded by mitmdump. mitmproxy
     pins h2 to exactly 4.3.0; the fix is 4.4.1. Accepted because pporlock is
     single-user and loopback-bound, so an attacker would need the user's own
     Chrome to emit the malformed request at their own proxy. Flagged to be
     revisited if the deferred system-wide mode (REQ SCP-004) is ever built.

5. **`validate-openapi.mjs` added to the gate.** It checks structure, dangling
   external `$ref`s, and undocumented operations. It found nine undocumented
   operations on its first run — all since written. A contract that lies about
   the server is worse than no contract.

6. **Generated-type defects fixed at the schema, not by hand-editing output.**
   Draft-2020 `prefixItems` tuple syntax was emitting `never[][]` for header
   pairs, and `Request`/`Response`/`Headers` would have shadowed DOM globals in
   every consuming file. Renamed to `FlowRequest`/`FlowResponse`/`HeaderPairs`
   and switched to a form the generator expresses correctly — it now infers
   `[string, string][]`.

7. **Schema conformance is tested from the Python side.** `jsonschema` +
   `referencing` validate what the daemon emits against the published schemas,
   parameterised over every Phase, Action, Outcome, and NoteCode. Without this
   the schemas quietly become documentation and the two halves drift.

**Notes for the next sprint:**

- Sprint 2 writes the first code that may import mitmproxy: `addon/normalize.py`
  and `addon/apply.py`. Everything mitmproxy-shaped stays above that line.
- The mypy `mitmproxy.*` override is still reported as unused; it starts earning
  its keep in Sprint 2.
- `pporlock_api` is `"1"`; nothing enforces it yet. The version gate lands with
  the module loader in Sprint 11.
- Rule `$defs` include `passthrough` in the action enum, but the exclusion list
  (Sprint 2) is the mechanism that actually implements it at ClientHello. Keep
  the two consistent when the loader lands.

---

## Sprint 02 — Baseline interception

**Branch:** `sprint-02-interception`
**Tag:** `sprint-02-complete`

**Requirements delivered:** PXY-001 (explicit proxy on loopback), PXY-005
(`pporlock run` foreground), PXY-004 (`doctor`), PXY-010/011 (CA into the login
keychain), PXY-012 (QUIC warning), PXY-013/014/015 (exclusions, editable,
recorded as passthrough), PXY-023 (transparent decode/re-encode), PXY-051
(WebSocket capture, inspection only), DD-2 (adapter boundary), DOC-005
(uninstall states what it leaves behind).

**Requirements deferred:** PXY-021/022 buffering guard → Sprint 9. PXY-030–036
actions → Sprints 7 and 9. PXY-043 dev toggles → Sprint 10, and they need the
same treatment as `flow_detail` (decision 1). PXY-002/003/007 launchd, full CLI,
log rotation → Sprint 16.

**Gate results:**
- **G1** — exit demo run. Four real sites (example.com, iana.org, httpbin.org,
  and a 314KB Wikipedia page) plus seven fixture endpoints proxied with no
  certificate warnings; a gzip response arrived as 81 bytes on the wire and
  6,000 decoded, demonstrating REQ PXY-023; `www.apple.com` and `www.chase.com`
  both tunneled undecrypted with the matching pattern named in the output.
  `doctor` reported 0 failures.
- **G2** — daemon 95.85%, engine 99.4% against the 90% bar. mcp exempt.
- **G3** — 474 daemon + 4 mcp + 18 web + 18 extension. No Sprint 0/1 regressions.
- **G4** — no tests removed. Stub mitmproxy objects were moved out of
  `test_adapter.py` into `tests/stubs.py` so more than one module can use them;
  no assertions were lost.
- **G5** — clean. Three `mypy --strict` findings fixed properly rather than
  ignored: a lambda leaking `TrustStatus` into a `-> None` slot, an untyped
  mitmproxy call, and a missing return annotation that `ruff --fix` had
  previously stripped.
- **G6** — bandit surfaced three findings. One, a "hardcoded password" on the
  string `"pass"`, was a genuine false positive and was restructured away rather
  than suppressed. The other two are justified inline: macOS keychain trust is
  only reachable through the `security` binary, argv is fixed, and there is no
  shell. §2.5 areas walked: loopback binding (unchanged, still asserted),
  logging hygiene (no bodies logged; the console feed prints URLs and sizes
  only), and the trusted-CA decision below.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **mitmproxy 12 does not register `flow_detail` on bare `Options`.** It comes
   from the dumper addon, which we disable, so passing it raised at startup.
   This is precisely the version-churn surface SPEC-1 §2.1 confines to the
   adapter, and it showed up on the first run. `anticache` and `anticomp` are in
   the same category and must be set through `master.options` after the addon
   set is built — noted in `runner.py` for Sprint 10.

2. **CA goes into the login keychain, never the System keychain.** No admin
   rights required, and the blast radius of a trusted MITM root is one user
   account rather than the machine. Trust is verified with `security
   verify-cert` rather than `find-certificate`: the latter proves the
   certificate was imported, not that it is trusted, and an imported-but-
   untrusted root produces exactly the certificate warnings the check exists to
   rule out.

3. **QUIC is a warning, not a failure.** It cannot be reliably enforced or even
   reliably detected from outside Chrome. A check that blocked on it would make
   `doctor` unusable on a normal machine, and `doctor` is what you run when
   things are already broken.

4. **33 default exclusions, every one commented, in four named categories.** A
   test asserts none is undocumented. The financial set is deliberately short
   and says so in the file: it establishes the category and the shape of an
   entry rather than pretending to be exhaustive.

5. **Excluded connections are recorded, not silent.** A passthrough flow carries
   host, timing, and the matching pattern but no content. Making excluded
   traffic invisible would be a different failure from the one exclusion solves.

6. **`flow.id` is reused as our flow identity** rather than minting a ULID.
   Maintaining a second identity map would buy nothing and the two would
   eventually disagree. Note that SPEC-0 §2 describes flow_id as a ULID; the
   contract only requires it be stable and sortable, which mitmproxy's UUID is
   for our purposes, but this is worth revisiting if session ordering ever
   depends on lexical sort.

7. **Renamed `tests/` to `testfixtures/`.** Making `daemon/tests` a package
   shadowed the repo-root `tests` package, so the fixture origin became
   unimportable. A distinct top-level name removes the collision permanently
   rather than papering over it with sys.path ordering.

8. **Console output is flushed explicitly.** stdout is block-buffered when
   redirected, so `pporlock run > log` produced nothing until the process
   exited. For this command the live feed is the product.

**Notes for the next sprint:**

- Sprint 3 replaces `NullSink`/`ConsoleSink` with the ring buffer behind the
  same `FlowSink` interface, and starts the control server from
  `Interceptor.running()` — which is currently an empty hook waiting for it.
- The `_stash`/`_unstash` helpers carry per-flow state between hooks through
  mitmproxy's flow metadata. Sprint 7 will add the evaluator's decision to the
  same channel.
- `runner.py` is at 71% coverage; the uncovered lines are the `_run` coroutine
  that binds a real port, which the integration harness covers by equivalent
  means rather than directly.
- Sprint 9's buffering guard needs `responseheaders`, which is currently an
  empty hook with a docstring explaining that everything buffers until then.

---

## Sprint 03 — Capture and control API

**Branch:** `sprint-03-capture-api`
**Tag:** `sprint-03-complete`

**Requirements delivered:** CAP-001 (dual-bound ring buffer), CAP-003 (body cap,
truncation flagged), CAP-004 (filter vocabulary, one implementation), API-001
(control server on the proxy's loop), API-002 (loop discipline, classified and
tested), API-004 (origin policy), API-010 (loopback asserted), API-011 (token,
0600), API-012 (pairing), API-013 (CSRF defence), API-020/021/025/028 (routes),
MCP-031 (audit log with actor origin), and the SPEC-0 §6.3 detail levels.

**Requirements deferred:** API-022 SSE → Sprint 4. API-003 static asset serving
→ Sprint 4, when there are assets. CAP-040–045 redaction → Sprint 13; the
serializer reports `redacted: false` honestly until then.

**Gate results:**
- **G1** — exit demo run. Browsed through the proxy and read flows back over the
  API with filters; the three security layers each refused what they exist to
  refuse (see decision 2); the audit log attributed the successful change; an
  excluded host came back carrying its pattern and reason.
- **G2** — daemon 94.90%, engine 99.56% against the 90% bar.
- **G3** — 660 daemon + 4 mcp + 18 web + 18 extension. No earlier regressions.
- **G4** — no tests removed.
- **G5** — clean. Two `mypy --strict` findings fixed rather than ignored.
- **G6** — scanners clean. §2.5 areas walked: **token handling** (0600 via
  `open()` flags rather than write-then-chmod, constant-time verify, never in a
  URL or error body — asserted by test), **origin/CSRF policy** (a real
  cross-origin form POST is refused, tested end to end), **loopback binding**
  (`ControlServer.__init__` asserts it), **logging hygiene** (no bodies logged).
- **G7** — merged `--no-ff`.

**Decisions:**

1. **starlette + uvicorn rather than hand-rolled HTTP.** HTTP/1.1 parsing is
   exactly the class of thing not to write by hand — request smuggling lives
   there — and the control server is security-relevant. uvicorn runs on the
   existing loop via `Server.serve()` as a task, which satisfies DD-3. Both were
   checked against pip-audit before adoption and add no advisories. Signal
   handler installation is disabled; the proxy owns ctrl-c.

2. **Three layers of access control, because loopback binding is not one.** The
   threat is that any page you visit can POST to 127.0.0.1:8081. Bearer token,
   origin allowlist, and a required non-simple header. Verified end to end
   against a running server: a cross-origin form POST is refused *with* a valid
   token, and a token-bearing request without the client header is also refused.

3. **`/pair` is exempt from the origin allowlist.** It has to be — an extension
   cannot be on the allowlist until it pairs, and pairing is how it gets there.
   Found by test, not by reasoning: the first pairing test returned 403. The
   route is still guarded (origin shape, single-use code, human-opened window),
   and the exemption is commented in place so it does not read as an oversight.

4. **The token is generated when the control app is constructed, not lazily.**
   The runner prints its path at startup; a path that does not exist yet is
   worse than no path. Found by running the exit demo.

5. **Contract gap: passthrough flows had nowhere to put the host.** A
   passthrough has no request or response, so the excluded host never reached
   the wire — REQ PXY-015's "visible but not readable" was only half true.
   Added a `passthrough` object to `flow.schema.json` with host, ip, pattern,
   and the entry's comment. Found by looking at real API output, not by reading
   the schema.

6. **`peer_ip_of` was reporting a hostname in an `ip` field.** Before
   resolution, a connection's address is the CONNECT hostname. It now returns
   only genuine addresses, which also keeps CIDR exclusion matching honest.

7. **Bodies are capped at write time, not read time.** The ring's memory bound
   has to reflect what is actually held, and truncation is always flagged.

**Notes for the next sprint:**

- Sprint 4 attaches the SSE hub to `RingSink.on_flow`, which exists and is
  tested but is currently unused in production wiring.
- `INLINE_ROUTES` and the classification test are in place; every route added
  from here must be classified or the test fails. `/config` is deliberately
  outside it and offloads.
- `ControlServer` polls for `server.started` with a 2-second ceiling. That is
  adequate on loopback but is a poll, not a signal; if it proves flaky under
  load, uvicorn exposes a startup event worth switching to.
- The `starlette.testclient` deprecation warning about httpx2 is cosmetic and
  will resolve when starlette's own guidance settles.

---

## Sprint 04 — SSE and the live flow table  ◀ VISUAL MVP

**Branch:** `sprint-04-visual-mvp`
**Tag:** `sprint-04-complete`

**Requirements delivered:** API-022 (SSE stream), API-003 (static asset
serving), WUI-001/002 (React + Vite SPA served by the daemon), WUI-003
(live flow table with the filter vocabulary and flag icons), WUI-012 (dev-toggle
indicator), WUI-013 (disconnected state), plus SPEC-0 §7 in full.

**Requirements deferred:** WUI-004 flow detail and provenance view → Sprint 8.
PRF-004 is partially addressed (event batching on an animation frame); the
virtualization requirement is not yet exercised because the ring buffer caps
what can be shown — revisit when a session browser can show 10,000 rows.

**Gate results:**
- **G1** — exit demo run against a live daemon and captured as screenshots:
  11 flows rendered live including two tunneled hosts; filtering to
  `host=example.com` narrowed 11 rows to 1 and restored them on clear; zero
  console errors; killing the daemon produced the disconnected banner within a
  poll interval.
- **G2** — daemon 94.9%, engine 99.6%, web 95.1%, extension 93.75%.
- **G3** — 693 daemon + 125 web + 18 extension + 4 mcp, plus 5 Playwright E2E.
- **G4** — `e2e/web/smoke.spec.ts` was removed and replaced by `mvp.spec.ts`.
  The smoke test asserted the Sprint 0 placeholder page still rendered, which
  ceased to describe real behaviour once the shell existed. Its one meaningful
  assertion — no requests outside the serving origin — was carried across
  verbatim, and three exit-criteria tests were added alongside it.
- **G5** — clean. Two `security/detect-object-injection` warnings suppressed
  inline with the reason stated: the key is constrained to `keyof FlowFilter`
  at the type level and every call site passes a literal.
- **G6** — scanners clean. §2.5 areas walked: **token handling** — the decision
  below is the substantive one this sprint; **logging hygiene** — the SSE stream
  carries flow bodies only at `bodies` detail, and the live subscription uses
  `summary`.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **The event stream uses streaming fetch, not EventSource.** EventSource
   cannot set request headers, which is why token-in-query-string is the usual
   shortcut for authenticated SSE. We did not take it: a URL lands in logs,
   history, and Referer, and this token grants read access to captured traffic.
   The cost is that reconnection, which EventSource provides free, is ours to
   implement — with backoff and `Last-Event-ID` resume. This resolves the open
   question the plan left for Sprint 4 ("SSE auth via header or a short-lived
   stream ticket, decided and documented this sprint").

2. **The daemon injects the token into the document it serves.** The UI is
   same-origin, served by the process that holds the token, so there is no third
   party in between. The shell is sent `no-store` with `no-referrer`. The origin
   policy still governs every subsequent call, so another page cannot use what
   it cannot read.

3. **Drop-don't-backpressure, everywhere.** A subscriber's queue is bounded and
   overflow discards the oldest with a `stream.gap`. There is a test that
   publishes 1536 events into a deliberately stalled subscriber and asserts the
   publisher does not block — because the publisher runs on the proxy's own
   event loop, and blocking it means blocking browsing.

4. **Three bugs found by running it rather than reading it.** A late refetch
   clobbering live events; the fix for that then making filters unable to remove
   rows; and `requestAnimationFrame` assumed to exist. All three now have
   regression tests. The middle one is worth remembering: fixing a race by
   merging is correct only when the two sides describe the same query.

5. **`eslint-plugin-react-hooks` was a declared dependency that was never
   running.** It is now wired into the flat config, and it is exactly the linter
   that catches the identity-churn bug this sprint hit — a new object per render
   churning a subscription effect.

6. **The unattributed flag is suppressed until attribution exists.** Every flow
   is unattributed until Sprint 6, so the marker appeared on every row and
   conveyed nothing. A flag that is always on trains the eye to ignore it. It
   now appears only once at least one flow carries a tab.

**Notes for the next sprint:**

- Sprint 5 builds the extension. The daemon side it needs is already in place:
  `/state/health` is public and cheap for the fail-safe poll, and `/pair`
  issues the token without filesystem access.
- The web UI reads its token from an injected meta tag; the extension will use
  the pairing flow instead. Both paths exist and are tested.
- `EventFilter` supports `tab_id`, which the DevTools panel will use in Sprint 8
  and which does nothing useful until attribution lands in Sprint 6.
- The flow table is windowed rather than virtualized. That is adequate while the
  ring buffer bounds what exists; the session browser in Sprint 13 will show
  more than that and should revisit it against PRF-004.
