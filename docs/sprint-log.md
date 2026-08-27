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
