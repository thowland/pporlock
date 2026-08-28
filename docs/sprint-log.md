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

---

## Sprint 05 — Extension proxy control

**Branch:** `sprint-05-extension-control`
**Tag:** `sprint-05-complete`

**Requirements delivered:** EXT-001 (minimal MV3 permissions), EXT-002
(fixed-server proxy control), EXT-003 (PAC mode), **EXT-010 / PXY-008 (the
fail-safe)**, EXT-011 (popup), EXT-022 (pairing), EXT-024 (loadable unpacked),
and EXT-012 in part — see below.

**Requirements deferred:** EXT-012 per-tab badge counts → Sprint 6, which is
where attribution arrives. Counts are browser-wide until then and the popup says
so. EXT-013 DevTools panel → Sprint 8. EXT-020/021 in-page banner → Sprint 15.

**Gate results:**
- **G1** — exit demo is the E2E suite itself: real unpacked extension in headed
  Chromium, real daemon on an ephemeral port, real SIGKILL. Six Playwright tests
  pass, including the four that matter: the proxy is applied, traffic routes
  through it, the configuration is cleared when the daemon dies, and ordinary
  browsing still works afterwards.
- **G2** — extension 96.7% statements / 94.9% functions. Entry points
  (`main.tsx`, `options.tsx`, `background/index.ts`, `manifest.config.ts`) are
  excluded with the reason recorded in `vitest.config.ts`: they compose tested
  units and cannot run outside an extension host, and the E2E suite covers the
  wiring. The thin `chrome.*` adapters are *not* excluded — they are tested,
  because they are the one place a typing cast would go unnoticed.
- **G3** — 123 extension + 693 daemon + 125 web + 4 mcp, plus 6 E2E.
- **G4** — no tests removed.
- **G5** — clean. Four `security/detect-object-injection` suppressions, each
  with the reason stated inline (module constants and numeric tab ids, no path
  from user input to a property name), and one file-level suppression on the E2E
  harness for `node:fs` calls on paths it created itself.
- **G6** — scanners clean. §2.5 areas walked: **token handling** — the extension
  never reads the filesystem and obtains its token by pairing; **fail-safe** —
  the mandatory automated test exists and gates this merge; **origin/CSRF** —
  every mutating call sends `X-Pporlock-Client: extension`, which is what makes
  the audit log's origin field trustworthy.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **The fail-safe was built third, immediately after the toggle**, per the plan.
   That ordering was right: everything from here on involves leaving the proxy
   running for long stretches, and a crash without the fail-safe leaves the
   browser unusable with no explanation.

2. **Two consecutive failures, not one.** A single dropped request during a
   daemon restart should not tear down a working setup. The health check also
   carries an abort signal, because a daemon that accepts the connection and
   never answers is exactly as broken as one that is gone — and would otherwise
   hang the check rather than failing it.

3. **No auto-re-enable.** SPEC-3 §4.4 rule 4, and it has a test. A daemon that
   crashed once may crash again mid-page-load.

4. **`host_permissions` widened from `:8081` to loopback on any port.** The
   control port is configurable, so an extension pinned to the default is simply
   broken for anyone who changes it. Found by an E2E test using an ephemeral
   port; it would otherwise have arrived as a bug report. Still loopback only —
   the extension cannot read any page.

5. **The manifest is asserted on by test.** It is a security surface, not
   configuration: exactly five permissions, no `webRequestBlocking`, no
   `declarativeNetRequest`, no `<all_urls>`, no content scripts yet. This
   required typing it with a local interface rather than CRXJS's
   `ManifestV3Export`, which is a union including a Promise and hides the
   object's fields from a test.

6. **The E2E drives the service worker from an extension page, not from itself.**
   `chrome.runtime.sendMessage` does not dispatch to the sender's own listener,
   so a worker cannot message itself — and using a page exercises the same path
   the popup does.

7. **The routing test uses an external host deliberately.** The local fixture
   origin cannot prove routing: it is on loopback, and the bypass list excludes
   loopback on purpose so the extension's own API calls and the health check do
   not route through the proxy. `ignoreHTTPSErrors` is set on the test context
   because a fresh Chrome profile does not inherit the keychain trust that
   `pporlock install` establishes — keeping CA trust and routing separate means
   a cert problem cannot masquerade as a routing problem.

8. **`make lint` covered `src` but not `e2e`.** The pre-commit hook caught it on
   this sprint's commit. Both now cover both.

**Notes for the next sprint:**

- Sprint 6 is the decision gate. Attribution turns the popup's browser-wide
  counters into per-tab ones and makes `EventFilter.tab_id` — which exists and
  is tested but does nothing useful yet — meaningful.
- `webRequest` is already in the manifest for exactly that purpose and is
  currently unused. If the attribution spike fails and the fallback does not
  need it, the permission should be removed rather than left held.
- `chrome.notifications` is called optionally in the trip handler but is not in
  the permissions list, so it silently no-ops. Either add the permission in
  Sprint 15 with the rest of the notification surface, or drop the call.
- The pairing flow is implemented and tested at the unit level; the E2E writes
  the token directly into storage rather than redeeming a code, because opening
  a pairing window needs a CLI invocation the harness does not yet make. Worth
  closing in Sprint 15.

---

## Sprint 06 — Attribution and platform spikes  ◀ DECISION GATE

**Branch:** `sprint-06-attribution`
**Tag:** `sprint-06-complete`

**Requirements delivered:** SPEC-0 §3.6 (attribution, both join directions),
API-012 (`pporlock pair`, which did not previously exist), API-028 (coverage in
`/metrics`), EXT-012 (per-tab badge counts), and the resolution of **OI-1** and
**OI-2**.

**Requirements amended:** EXT-001. It assumed attribution needed no broad host
access. Measurement says otherwise — see decision 3.

**Gate results:**
- **G1** — both spikes measured, not argued. OI-1 probe against Chrome 151; OI-2
  measured at 49/49 flows attributed (100%) against a 95% criterion, over a real
  browsing session with a real extension and a real daemon.
- **G2** — daemon 92.88%, engine 99.56%, extension 95.73%, web 95.05%.
- **G3** — 732 daemon + 144 extension + 125 web + 4 mcp, plus 8 E2E.
- **G4** — no tests removed. Two were rewritten after they were found to assert
  impossible states (see decision 6).
- **G5** — clean.
- **G6** — one new bandit finding, justified inline: `urlopen` in `pporlock
  pair`, where the scheme is a literal and the host has already been asserted
  loopback by `Config.validate()`. §2.5 areas walked: **origin/CSRF policy** —
  the pairing gap below was found through it; **token handling** — the CLI reads
  the token file, the extension still never does.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **OI-1 resolved: the HTTP control channel survives; Native Messaging is not
   needed.** Measured on Chrome 151: extension service worker → loopback 200,
   extension page → loopback 200, ordinary public page → loopback **blocked by
   Chrome**. That third result is the notable one — Chrome now refuses
   public→private requests itself, so our origin policy became defence in depth
   rather than the only barrier. It stays: it also covers curl, other local
   processes, and older Chrome versions.

2. **OI-2 resolved: 100% coverage, criterion 95%.** The primary mechanism is
   adopted; neither named fallback is needed. The measurement lives in the
   product (`GET /metrics`) and in an E2E test, so it can be re-run rather than
   remembered.

3. **`<all_urls>` is genuinely required for attribution, and is therefore
   optional.** `chrome.webRequest` reports only requests the extension has host
   access to: coverage measured 0% with loopback-only permissions and 100% with
   `<all_urls>`. Since REQ EXT-001 forbids broad host access, the permission is
   declared in `optional_host_permissions` and requested only when the user asks
   for per-tab counts. Installing pporlock prompts for nothing broad; proxy
   control, the fail-safe, and browser-wide counts all work without it; and the
   popup states the limitation rather than hiding it.

4. **An MV3 timer cannot be trusted to fire.** A 500 ms batch timer produced
   zero submissions: the worker is suspended with the timeout pending and the
   batch is lost. Batching is now driven by round-trip time — flush immediately
   when idle, buffer while in flight — which also keeps the worker alive for the
   duration of the batch it is carrying.

5. **The attributor was attributing its own POSTs**, which scheduled another
   POST, which was attributed in turn. On an idle browser that loop generates
   traffic forever. The control origin is now excluded from observation, and it
   follows a control-origin change.

6. **Attribution has to join in both directions.** The extension observes at
   `onBeforeRequest`, so its association usually arrives *before* the flow
   completes; the daemon joined only on submission. Roughly half of all flows
   would have been unattributed. Both paths now exist: a resolve at flow-record
   time, and backfill when the flow wins the race.

7. **The coverage metric was measuring the wrong thing.** It counted join
   attempts, and backfill re-tries every unattributed flow on every submission,
   so one unattributable flow inflated the denominator without bound — reporting
   77.8% where the figure over flows was 100%. SPEC-0 §3.6 states the criterion
   over *flows*; it is now measured that way, with attempt counters kept
   separately and labelled as diagnostics.

8. **`pporlock pair` did not exist.** The popup and the documentation both
   referenced it. Its absence surfaced as a 403: an extension holding a token
   but never having completed the handshake cannot make mutating calls, because
   pairing is what registers its origin with the daemon. Worth noting that GET
   requests from an extension carry no `Origin` header and so were passing,
   while the JSON POST triggered CORS and was refused — the failure was
   therefore partial and would have been confusing to diagnose later.

9. **The attribution E2E loads a copy of the built extension with `<all_urls>`
   moved into `host_permissions`.** That is exactly the post-grant state, and it
   is the only way to reach it automatically: `chrome.permissions.request`
   requires a user gesture and raises a Chrome-native dialog no driver can
   dismiss. The extension code under test is byte-identical to what ships.

**Notes for the next sprint:**

- Sprint 7 builds the rules engine and blocking. The seams in
  `addon/interceptor.py` are marked and unchanged since Sprint 2.
- `engine/` must reach 90% coverage in Sprint 7 (REQ TST-002); it currently sits
  at 99.56% on a much smaller surface.
- The `?` unattributed flag in the web UI (suppressed in Sprint 4 until
  attribution existed) will now appear for genuinely unattributed flows — worth
  confirming it reads correctly once someone browses without the optional grant.
- `chrome.notifications` is still called optionally in the fail-safe trip
  handler without the permission being held, so it silently no-ops. Carry to
  Sprint 15 with the rest of the notification surface.

---

## Sprint 07 — Rules engine and blocking

**Branch:** `sprint-07-rules-engine`
**Tag:** `sprint-07-complete`

**Requirements delivered:** MOD-010/011 (match criteria, response-side
validation), MOD-012 (both evaluation semantics), MOD-014 (action parameters
validated at load), MOD-004 (hot reload via the API), PXY-020 (fixed phase
order), PXY-025 (regexes compiled once), PXY-026 (time budget), PXY-030/031/032
(block, stub vs kill, the derivation table), PXY-033 (stub library), PXY-034
(map_local failing loudly), PXY-021/022 (buffering guard and its provenance),
CAP-010–013 (provenance throughout).

**Requirements deferred:** the transform registry itself → Sprint 10. Body rules
match, order, and record correctly today; they record `no_change` with the
reason, so the phase and ordering are established rather than retrofitted.

**Gate results:**
- **G1** — exit demo run against a live daemon: a blocked host returns a
  synthesised script, a blocked pixel a real 1×1 GIF the browser renders, GTM a
  stub keeping `dataLayer` alive. A page with all three blocked reported
  "Rendered correctly" with zero console errors. A rule installed through the
  API took effect on the next request with no restart.
- **G2** — daemon 93.43%, **engine 98.68%** against the 90% bar (REQ TST-002).
- **G3** — 911 daemon + 144 extension + 125 web + 4 mcp, plus 8 E2E.
- **G4** — no tests removed. Three were rewritten because they asserted the old
  always-buffer behaviour, which the buffering guard correctly replaced; the
  replacement asserts the new behaviour in both directions (streams when nothing
  wants the body, buffers when a rule does).
- **G5** — clean.
- **G6** — scanners clean. §2.5 areas walked: **path traversal** —
  `_resolve_asset` refuses absolute paths and resolves symlinks *before* the
  containment check, with a test that plants a symlink pointing out of the root;
  **SSRF via redirect** — the action can retarget any host, documented as
  intended under the trusted-rules model, and the target comes only from the
  rule, never from response content.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **Sec-Fetch-Dest is only sent on secure contexts.** Measured, not assumed: on
   a plain-HTTP page Chrome omits the Sec-Fetch headers entirely, so `stub:
   auto` fell through to 204 and a blocked tracking pixel never rendered.
   `Accept` is always sent and distinguishes documents, stylesheets, images, and
   fonts, so it is the fallback. It cannot separate a script from an XHR — both
   send `*/*` — and that resolves toward script: an empty script body is
   harmless, and a blocked tracker requested with `*/*` is overwhelmingly a
   script tag. An explicit `stub:` overrides all of it.

2. **A headers rule spans two phases, and the phase decides what it may match
   on.** Deriving the phase from the action alone rejected a response-header
   rule matching on `status`, which is a legitimate and obvious thing to write.
   The phase is now resolved per rule from the side it declares.

3. **A bad rule set never replaces a good one.** `PUT /rules` compiles before it
   swaps, so a typo leaves the rules in force untouched rather than emptying
   them. The swap replaces an immutable snapshot, so an in-flight flow finishes
   against the rules it started with — which is what removes the need for
   locking under DD-3.

4. **A broken `rules.yaml` does not stop the daemon.** It reports the failure
   loudly at startup and runs with no rules. The daemon is still useful for
   inspection, and the alternative is a user who cannot browse because of a typo.

5. **Rule loading moved off the event loop.** `ASYNC240` flagged filesystem work
   inside the async runner — precisely what DD-3 exists to prevent. Exclusions
   and rules are now loaded before the loop is created, where doing it
   synchronously costs nothing.

6. **`wants_body` drives the buffering guard.** When no enabled rule could
   produce a body transform for a flow, the response streams regardless of size
   or type. That is the cheapest optimisation available and applies to the
   overwhelming majority of flows on any real page — and it is recorded, so a
   transform that did not run is never silent.

7. **Header rules still run on a short-circuited request.** A rule adding a
   header the synthesised response should carry is legitimate, and skipping it
   silently would be surprising.

**Notes for the next sprint:**

- Sprint 8 builds the provenance UI. Everything it renders now exists and is
  populated: phases, outcomes, note codes, `short_circuited_by`, and the
  per-rule detail blocks.
- The `stubs/` directory ships at the repository root and is resolved relative
  to the package. Packaging in Sprint 16 must include it, or `stub:` by name
  silently stops working — worth an install test.
- `RuleSet.wants_body` walks every body rule per request. With a large rule set
  that becomes the hot path; if PRF-002 tightens, index body rules by host.
- The `matches_everything` flag is computed but not yet surfaced. The UI should
  warn on a rule with an empty match block, since it fires on every flow.

---

## Sprint 08 — Provenance UI

**Branch:** `sprint-08-provenance-ui`
**Tag:** `sprint-08-complete`

**Requirements delivered:** WUI-004 (flow detail), CAP-013 / DOC-003 (the
provenance view), EXT-013 (DevTools panel), EXT-014 (jump to module — the
targets land in Sprint 12), and the first part of WUI-015 (keyboard-navigable
rows, Escape to close).

**Requirements deferred:** WUI-014 body diff → Sprint 10, when there is a
transform to diff. CAP-043 unmasking → Sprint 13; masked values render
correctly today, the reveal control arrives with redaction itself.

**Gate results:**
- **G1** — exit demo run against live blocked traffic. The panel named the
  module, the rule, its id, the action, the outcome and duration; called out
  that the rule short-circuited the flow; expanded the synthesised status,
  content type, and stub; and carried the streaming note explaining why body
  transforms could not run. Zero console errors.
- **G2** — daemon 92.92%, engine unchanged, web 92.9%, extension 93.38%.
- **G3** — 916 daemon + 189 extension + 212 web + 4 mcp, plus 8 E2E.
- **G4** — no tests removed.
- **G5** — clean. Three suppressions, each justified inline: two
  `security/detect-object-injection` on indexing a label table with a value
  that comes from a module constant rather than the wire, and one severity map
  rewritten as a `Map` rather than suppressed, because the rule was right that
  indexing an object with a wire value is worth avoiding.
- **G6** — scanners clean. §2.5 areas walked: **redaction correctness** — the
  UI detects the SPEC-0 §9.1 mask format and never attempts to reconstruct a
  value, only to display its length and fingerprint.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **Non-applied outcomes are as prominent as applied ones.** This runs against
   the usual instinct to foreground successes, and it is the point: the view
   exists to explain why something did *not* happen. A skipped or errored rule
   is bordered and labelled, never greyed out.

2. **`short_circuited_by` is stated, not inferred.** "An earlier rule ate it" is
   the single most common confusion when debugging a rule set, so the culprit
   entry says so in place rather than leaving the reader to work it out from
   ordering.

3. **The panel and the web UI keep separate label tables.** They are separately
   built with no shared package, and a panel that silently disagreed with the
   web UI about what an outcome means would be worse than one that repeats a
   table. Both suites iterate the full enum from the contract, which is what
   keeps them honest.

4. **A flags-column fix found by looking at real output.** Blocked flows were
   also showing MOD, because `modified` was derived from "any module fired". A
   short-circuited flow was blocked, not modified — and the flags column is
   precisely how you scan a hundred rows for the one that went wrong, so two
   flags on one row makes it harder to read. `modified` now means an applied
   headers or body action; a redirect counts as neither, since it changed where
   the request went rather than what came back.

5. **`Panel.tsx` and `panel.tsx` are the same file on macOS.** One silently
   overwrote the other, and the failure surfaced as a nonsensical "module
   declares X locally but does not export it". Renamed to `PanelView.tsx`. Worth
   remembering: a case-only filename difference is not a difference here.

6. **Flow detail opens on the provenance tab.** It is the reason the panel
   exists; making it one click away would be the wrong default.

7. **A streamed response says its body was never buffered**, rather than
   rendering as an empty body. Those are different facts, and the second is the
   reason a transform may not have run.

**Notes for the next sprint:**

- Sprint 9 adds `map_local`, `redirect`, and header actions end to end. The
  evaluator already implements all three; the sprint is the buffering guard's
  remaining edges plus executor offload.
- The panel polls every two seconds. SSE with a `tab_id` filter already exists
  server-side and would be a straight substitution — worth doing when the panel
  gets its Sprint 15 pass.
- `FlowDetail` fetches at `bodies` detail on open. With the ring buffer's 512
  KiB body cap that is bounded, but a session browser showing large recorded
  bodies should reconsider.
- The `matches_everything` flag from Sprint 7 is still unsurfaced. The module
  editor in Sprint 12 should warn on a rule with an empty match block.

---

## Sprint 09 — Short-circuit actions, buffering, and offload

**Branch:** `sprint-09-actions-buffering`
**Tag:** `sprint-09-complete`

**Requirements delivered:** PXY-034 (map_local, verified on the wire), PXY-035
(redirect), PXY-036 (header actions, both sides), PXY-021/022 (buffering guard
with its provenance), PXY-026 (time budget, now actually charged), PXY-024
(offload classification).

**Requirements deferred:** none. The transform registry remains Sprint 10 as
planned; body rules match, order, classify their offload cost, and record.

**Scope note:** most of this sprint's nominal scope landed early in Sprint 7,
where the evaluator implemented all three actions. What remained was what had
not been exercised — whether the mutations reach the wire, whether the budget
can fire, and where expensive work runs. Two of those turned out to be broken.

**Gate results:**
- **G1** — exit demo run against a live daemon: map_local substituted a remote
  script from disk with the correct content type and `no-store`; a redirect
  retargeted a 4 MiB response to an 11-byte one; CSP was removed and a marker
  set, both confirmed on the wire; a request header we added appears in the
  captured record; provenance showed the phases in order with the buffering
  decision and offload classification alongside.
- **G2** — daemon 92.92%, **engine 99%**, web and extension unchanged.
- **G3** — 959 daemon + 189 extension + 212 web + 4 mcp, plus 8 E2E.
- **G4** — no tests removed.
- **G5** — clean.
- **G6** — scanners clean. §2.5 areas walked in full for this sprint's actions:
  **path traversal** — `_resolve_asset` refuses absolute paths and resolves
  symlinks before the containment check, tested with a planted symlink;
  **SSRF via redirect** — documented in the code where it lives, with an
  integration test asserting the target comes only from the rule.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **Response headers must be applied at `responseheaders`, not `response`.**
   Once a response streams, mitmproxy has already put its headers on the wire,
   so a mutation computed later is recorded as applied and silently changes
   nothing. This was invisible to unit tests — the mutation object was correct —
   and only surfaced when an end-to-end test read the header off the wire. It is
   also where SPEC-0 §4.2 places the phase; the implementation had drifted from
   the spec rather than the spec being wrong.

2. **The time budget could never fire.** Created and checked since Sprint 7,
   never consumed. `skipped_budget` was unreachable in production. Both phases
   now charge it, because a budget counting only body transforms would let
   request-side matching overrun it unnoticed.

3. **Work is classified, not uniformly offloaded.** Thread-pool handoff for a
   header edit would cost more than the edit. HTML transforms are
   unconditionally expensive — they parse a document, so cost tracks structure
   rather than length and a size threshold would not predict it. Scanning
   transforms offload above a body threshold. An unknown transform defaults to
   expensive: a module-provided transform we know nothing about must not be
   assumed fast on the proxy's loop.

4. **The offload decision is recorded in provenance.** An operator diagnosing a
   slow page should be able to see why, not infer it.

5. **The captured request is the one that was sent.** It had been the
   pre-mutation snapshot, so the provenance panel would have displayed a request
   that never went out. Re-normalising costs one extra pass and only when a
   mutation was actually applied.

6. **`offload_reason`, not `reason`, in the detail block.** A provenance entry
   already carries a `reason` for what the rule did; two different facts under
   one key is how a detail block becomes misleading. Found by a key collision at
   runtime.

**Notes for the next sprint:**

- Sprint 10 implements the transform registry. The phase, ordering, offload
  classification, and provenance are all in place around it; what lands is the
  transforms themselves plus CSP/SRI handling and the dev toggles.
- `TRANSFORM_COST` currently classifies by name. When modules can register their
  own transforms (Sprint 11), the registration should carry a cost so the
  default-to-expensive fallback is a genuine last resort.
- The executor is classified but not yet wired: nothing calls
  `run_in_executor` because no transform runs work yet. Sprint 10 must honour
  the decision rather than leave it advisory.
- `evaluate_response` (the combined form) exists only for the dry runner. If
  Sprint 14 finds it does not need it, remove it rather than leaving two paths.

---

## Sprint 10 — Body rewriting  ◀ v0.3

**Branch:** `sprint-10-body-rewriting`
**Tag:** `sprint-10-complete`

**Requirements delivered:** MOD-013 (transform registry), MOD-014 (parameters
validated at load), PXY-040 (SRI stripped on any rewritten document), PXY-041
(nonce reuse before relaxation), PXY-042 (both CSP headers), PXY-043/044 (dev
toggles and their notes), PXY-024 (offload actually honoured).

**Requirements deferred:** CAP-014 / WUI-014 body diff → Sprint 13. A real
before/after diff needs both bodies retained, which doubles memory for every
rewritten flow against CAP-001's bounds. Sessions are where the original can be
stored deliberately, so the diff belongs there rather than half-built here.

**Gate results:**
- **G1** — the v0.3 checkpoint, run against a live daemon and a real browser: a
  script injected into a nonce-bearing page ran with zero console errors **and
  the page's CSP intact**; the same injection into a page with no nonce worked
  after relaxation; an SRI-protected script still executed after we rewrote the
  document around it. Confirmed on the wire that CSP survived on the first page
  and was removed on the second.
- **G2** — daemon 92%+, engine unchanged, web and extension unchanged.
- **G3** — 1,025 daemon + 189 extension + 212 web + 4 mcp, plus 8 E2E.
- **G4** — no tests removed. Five were updated because they used `strip_csp` as
  a body transform, which now correctly runs in the header phase; they were
  changed to use a genuine body transform, and a new test asserts the
  header-phase behaviour directly.
- **G5** — clean.
- **G6** — scanners clean. §2.5 areas walked: **injection into rewritten
  pages** — injected content comes only from the rule, never from response
  data; the nonce is read from the page's own header and reused on the injected
  tag alone, and CSP/SRI changes are always recorded and surfaced.
- **G7** — merged `--no-ff`.

**Decisions:**

1. **The response hook is now async.** Sprint 9 classified expensive work but
   left the decision advisory. A synchronous hook submitting to a thread pool
   and blocking on the result stalls the loop exactly as much as doing the work
   inline — the `await` is the entire point. This changed the addon's hook
   signature, so the affected tests became async too.

2. **`strip_csp` runs in the header phase despite being a body transform in the
   schema.** It operates on headers, and Sprint 9 established that once a
   response streams its headers are already on the wire. Registered for the
   schema's sake, applied where it can take effect. Worth noting as a genuine
   spec/implementation divergence: SPEC-0 §5.5 lists it among body transforms.

3. **The implicit SRI strip is unconditional and recorded.** Any document whose
   body we rewrote gets it, whether or not a rule asked. The breakage is
   invisible from the proxy's side, so relying on the operator to remember would
   guarantee it is eventually forgotten. It appears in provenance as "implicit
   SRI strip" rather than happening quietly.

4. **Regex over HTML, deliberately.** The alternative — parsing and
   re-serialising with a full HTML parser — rewrites markup the page never asked
   us to touch, which for a tool whose whole job is not breaking pages is the
   worse risk. Each pattern is scoped to the smallest thing that does its job.

5. **The charset bug.** Re-encoding read `content_type`, which strips parameters
   by design, so a latin-1 page would have been re-encoded as UTF-8 and rendered
   as mojibake. A page that loads and is subtly wrong is the exact failure this
   system exists to avoid, and it took a test with an accented character to find
   it.

6. **The captured response is the served one.** It had been the pre-mutation
   body: the wire got the rewritten page while the record showed the original.
   Same reasoning as the request side in Sprint 9.

7. **`json_patch` supports add, remove, and replace only.** Move, copy, and test
   are omitted rather than half-implemented. A path that does not exist is not an
   error — the rule describes a shape the body may or may not have — and a
   non-JSON body is left alone and reported rather than failing the flow.

**Notes for the next sprint:**

- Sprint 11 builds the module system. `TransformRegistry.register` is the
  extension point; module-registered transforms should carry their own cost so
  the default-to-expensive fallback becomes a genuine last resort.
- `TransformContext` is deliberately small. A module transform needing more than
  URL, content type, and headers is a signal the rule model should express it
  instead.
- The implicit SRI strip runs only on `text/html`. A rewritten SVG or XHTML
  document carrying `integrity` would not be covered; worth revisiting if a real
  case appears.
- The dev toggles are per-daemon, not per-profile. REQ MOD-044 wants
  profile-scoped toggles, which lands with profiles in Sprint 11.
