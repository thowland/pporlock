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
