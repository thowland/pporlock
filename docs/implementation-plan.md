# pporlock — Implementation Plan and Sprint Procedures

**Version:** 1.0
**Status:** For review
**Date:** 2026-08-27
**Governs:** all development work on this repository
**References:** `pporlock_approach-rev1.md`, `pporlock_requirements-v1.md`, `spec-0-contracts.md`, `spec-1-daemon.md`, `spec-2-web-ui.md`, `spec-3-extension.md`

---

## 1. Shape of the plan

Seventeen sprints (S0–S16). Each is one coherent, mergeable capability, developed on its own branch and merged into `master` with a true merge commit.

The ordering is driven by one goal: **a visual artifact as early as possible**, followed by retiring the platform risks that could invalidate the architecture, and only then deepening the engine.

```
S0  Environment                     ─┐
S1  Contracts & scaffolding          │  foundation
S2  Baseline interception            │
S3  Capture + control API           ─┘
S4  SSE + web UI flow table          ◀── VISUAL MVP
S5  Extension proxy control          ◀── v0.1 complete
S6  Attribution & platform spikes    ◀── OI-1 / OI-2 resolved
S7  Rules engine + blocking
S8  Provenance UI
S9  Short-circuit actions + buffering guard
S10 Body rewriting
S11 Module system + profiles
S12 Authoring UI
S13 Sessions + redaction
S14 Dry run + MCP
S15 Extension completion
S16 Packaging & hardening            ◀── v1.0
```

**Why this order.** S2–S5 exist to reach a screen showing real intercepted traffic in four sprints. S6 immediately follows because Private Network Access (OI-1) and tab attribution (OI-2) are the two unknowns that could force an architecture change, and discovering either at S12 would be expensive. Everything from S7 onward is deepening a proven skeleton.

---

## 2. Sprint procedure

### 2.1 Branch

One branch per sprint, named `sprint-NN-slug`:

```bash
git checkout master
git pull
git checkout -b sprint-07-rules-engine
```

Commit granularly and often. Commit messages reference requirement IDs where applicable (`REQ PXY-021`, `REQ MOD-012`). Do not squash — the granular history is the point.

### 2.2 Work

Proceed until the sprint's requirements are met. If a requirement turns out to be blocked or wrong, do not silently drop it: record it in the sprint's section of `docs/sprint-log.md` with the reason, and either move it explicitly to a later sprint or raise it for a requirements change.

### 2.3 Close gates

**Every sprint after S0 must pass all seven gates before merging. No exceptions, no "we'll fix it next sprint."**

| # | Gate | Verification |
|---|---|---|
| G1 | **Sprint requirements met** | Every requirement ID in the sprint's scope table is implemented and demonstrated. The sprint's exit demo runs. |
| G2 | **Coverage ≥ 80% per component** | `make coverage`. Thresholds are per-component, not repo-wide: `daemon` ≥ 80%, `daemon/src/pporlock/engine` ≥ 90%, `mcp` ≥ 80%, `web` ≥ 80%, `extension` ≥ 80%. A component with no code this sprint is exempt; a component with any code is not. |
| G3 | **All tests pass — including previous sprints'** | `make test`. Every test in the repository, not just this sprint's. A regression in Sprint 3's tests blocks Sprint 9's merge. |
| G4 | **Obsolete tests removed, coverage still met** | Deleting a test that no longer describes real behavior is correct and expected. Deleting a test to make a number go up is not. If removal drops a component below threshold, write new tests before closing. |
| G5 | **Lint clean** | `make lint`. `ruff` + `mypy --strict` on `engine/`, `eslint` + `tsc --noEmit` on TS. Minor, individually-justified exceptions are acceptable and must carry an inline suppression with a reason. Blanket file- or directory-level disables are not. |
| G6 | **Security gate** | `make security` — `bandit`, `pip-audit`, `npm audit`, `eslint-plugin-security`, `gitleaks`. Fails on high severity. Plus the §2.5 checklist for the areas this sprint touched. |
| G7 | **Merge with history preserved** | `git merge --no-ff` into `master`. Never squash. |

### 2.4 Merge

```bash
make gate                                   # G2, G3, G5, G6 in one command
# ... G1 demo, G4 review ...
git checkout master
git merge --no-ff sprint-07-rules-engine -m "Merge sprint 07: rules engine and blocking"
git tag -a sprint-07-complete -m "Sprint 07 complete: rules engine, blocking, stub synthesis"
git branch -d sprint-07-rules-engine
```

The tag makes milestones navigable in `git log --oneline --graph`.

### 2.5 Security checklist

`make security` catches the mechanical issues. These are this system's actual risks and must be reviewed by hand for every sprint that touches the named area. Threat coverage maps to OWASP Top 10 categories in parentheses.

| Area | Check | Sprints |
|---|---|---|
| **Loopback binding** (A01) | Every listener asserts a loopback bind address in code, not merely defaults to one. A config value that is not loopback is rejected at startup. | S3, S16 |
| **Token handling** (A07) | Token file is `0600`. Token never appears in logs, error bodies, URLs, or the audit log. Extension stores it in `chrome.storage.local` and never reads the filesystem. Constant-time comparison on verify. | S3, S5, S14 |
| **Origin / CSRF policy** (A01) | Mutating routes require the bearer token, a recognized `Origin`, and the non-simple `X-Pporlock-Client` header. A cross-origin form POST from an ordinary page must be rejected — test this explicitly. | S3, S16 |
| **Path traversal** (A01) | `map_local` file paths and module asset resolution are confined to the module's `assets/` directory. `../` and absolute paths are rejected. Symlinks resolved before the containment check. | S9, S11 |
| **SSRF via redirect** (A10) | The `redirect` action can rewrite a request to any host, including internal ones. Document this as intended behavior with a trusted-rules justification, and ensure a redirect target cannot be injected from response content. | S9 |
| **Redaction correctness** (A02) | Session data is redacted at write time, not read time. MCP responses cannot carry an unmask parameter. Mask format leaks only a 4-hex-char hash prefix and a length. | S13, S14 |
| **Trusted module boundary** (A08) | Module code is deliberately unsandboxed. Verify the *documentation* says so on every authoring surface, that MCP create/update cannot enable, and that dry run warns it executes candidate code. | S11, S12, S14 |
| **Injection into rewritten pages** (A03) | `inject_script` content is not assembled from untrusted response data. Nonce reuse does not leak the nonce cross-origin. Stripping SRI/CSP is recorded and surfaced, never silent. | S10, S15 |
| **Deserialization / schema strictness** (A08) | Manifest and rule parsing is strict-mode YAML with unknown keys rejected. No `yaml.load` without a safe loader. JSON Schema validation before any use. | S1, S11 |
| **Dependency currency** (A06) | `pip-audit` and `npm audit` clean at high severity. mitmproxy pin is deliberate and documented. | every sprint |
| **Logging hygiene** (A09) | Bodies never logged at default level. Headers logged only with redaction applied. Audit log records actor origin for every state change. | S3, S13 |
| **Fail-safe** (A04) | The extension clears Chrome's proxy configuration when the daemon dies. Verified by an automated test that kills the daemon. | S5, S15 |

### 2.6 Definition of done for a requirement

A requirement is done when: code implements it, a test asserts it, the test is in the suite that runs at G3, and — where user-visible — the sprint's exit demo shows it.

---

## 3. Sprint 0 — Environment and tooling

**Branch:** `sprint-00-environment`
**Produces no product code.** Its output is the machinery every later gate depends on.

### 3.1 Repository skeleton

Create the layout of SPEC-0 §1 with empty package scaffolding, plus `.gitignore`, `.editorconfig`, and `docs/sprint-log.md`.

### 3.2 Python toolchain

| Tool | Purpose | Config |
|---|---|---|
| `uv` | Dependency and venv management | `daemon/pyproject.toml`, `mcp/pyproject.toml`, locked |
| Python 3.12 | Runtime | `.python-version` |
| `pytest` | Test runner | `pytest.ini` / `[tool.pytest]` |
| `pytest-cov` | Coverage | Per-component thresholds, `fail_under` set |
| `pytest-asyncio` | Async tests | |
| `ruff` | Lint + format | `[tool.ruff]`, line length and rule set fixed |
| `mypy` | Types | `--strict` on `daemon/src/pporlock/engine/`, standard elsewhere |
| `bandit` | Security lint | `[tool.bandit]` |
| `pip-audit` | Dependency CVEs | |

mitmproxy is pinned exactly in this sprint (REQ PXY-006), and the pinned version is recorded in `daemon/pyproject.toml` and in the sprint log.

### 3.3 TypeScript toolchain

| Tool | Purpose |
|---|---|
| Node 20, npm | Runtime and packages |
| Vite | Build for `web/` and (with CRXJS) `extension/` |
| TypeScript strict | `tsconfig.json` per component, `noUncheckedIndexedAccess` on |
| Vitest + c8 | Test runner and coverage, thresholds set |
| ESLint + `eslint-plugin-security` | Lint and security lint |
| Prettier | Format |
| `json-schema-to-typescript` | Generated types (SPEC-0 §1.1) |

### 3.4 End-to-end and fixtures

| Item | Detail |
|---|---|
| Playwright | Chromium, headed and headless; persistent-context extension loading for MV3 (SPEC-3 §11) |
| Fixture origin server | `tests/fixtures/origin/` — a plain Python HTTP/HTTPS server (no container) serving known fixtures: a CSP-bearing page with and without a nonce, a script with an `integrity` attribute, a large body above the buffering threshold, a `Sec-Fetch-Dest` exerciser for every value, a WebSocket echo, a slow response, a gzip/brotli-encoded body, and a conditional-request endpoint returning `304` |
| Test CA | A separate CA for fixture HTTPS so tests never depend on the user's real mitmproxy CA |

The fixture server is a test dependency, started and stopped by the test harness, and is the target for every integration test in S2 onward.

### 3.5 Gate machinery

`Makefile` targets — these are the vocabulary the rest of the plan and `CLAUDE.md` use:

```
make setup       # install all toolchains, generate contracts, install hooks
make contracts   # validate schemas, generate contracts/generated/types.ts
make daemon      # build daemon package
make web         # build web UI to web/dist
make extension   # build extension to extension/dist
make all         # contracts -> daemon, web, extension

make test        # ALL tests, all components  (G3)
make coverage    # per-component thresholds   (G2)
make lint        # ruff, mypy, eslint, tsc    (G5)
make security    # bandit, pip-audit, npm audit, gitleaks (G6)
make gate        # coverage + test + lint + security  (G2,G3,G5,G6)

make bench       # PRF-001/002 harness
make e2e         # Playwright suites
make fixtures    # start the fixture origin server standalone
```

### 3.6 Pre-commit hooks

Fast checks only — the full gate is `make gate`, run before merge:

- `ruff format --check`, `ruff check`
- `prettier --check`, `eslint` on staged TS
- `gitleaks protect --staged`
- Blocks commits containing `~/.pporlock/token`, `.mitmproxy`, or `.env` paths

### 3.7 Sprint 0 exit

- `make setup` succeeds on a clean checkout.
- `make gate` runs to completion and passes trivially (no code yet).
- `make fixtures` serves every fixture listed in §3.4 and the fixture CA validates.
- A deliberately-failing placeholder test is shown failing `make test`, then removed — proving the gate can actually fail.
- Pre-commit hooks demonstrated rejecting a lint error and a planted fake secret.

Sprint 0 merges under the same §2.4 procedure but is exempt from G1 (no product requirements) and G2 (no product code).

---

## 4. Sprints

Each sprint lists its branch, the specs it implements, the requirement IDs in scope, and the exit demo that satisfies G1.

---

### S1 — Contracts and scaffolding

**Branch:** `sprint-01-contracts`
**Specs:** SPEC-0 §1–§5, §8, §9

| Scope | Requirements |
|---|---|
| JSON Schemas: module manifest, rule, flow, provenance, events | MOD-015, MOD-002 |
| `contracts/openapi.yaml` — full route surface, no implementation | API-029 |
| Generated `contracts/generated/types.ts` wired into `make contracts` | SPEC-0 §1.1 |
| `engine/models.py` — SPEC-0 §3.1–3.3 dataclasses | DD-2 |
| `engine/provenance.py` — SPEC-0 §4, all phases, outcomes, note codes | CAP-010 |
| `config.py` with precedence and loopback validation; `errors.py` hierarchy | API-010 |
| The forbidden-import test (SPEC-1 §2.2) | TST-001, DD-2 |

**Exit demo:** `make contracts` generates TS types that compile; a round-trip test serializes a `FlowRecord` and a `Provenance` through the schemas and back; the forbidden-import test passes and is shown failing when a `mitmproxy` import is planted in `engine/`.

**Security focus:** strict YAML/JSON parsing, schema strictness, loopback config validation.

---

### S2 — Baseline interception

**Branch:** `sprint-02-interception`
**Specs:** SPEC-1 §2.1, §3.1–§3.2, §3.5, §8.1–§8.2

| Scope | Requirements |
|---|---|
| Addon skeleton, `mitmdump` wiring, pinned version | PXY-001, PXY-006 |
| `normalize.py` — the adapter boundary | DD-2 |
| `apply.py` — mutation application (unused yet, but the boundary exists) | — |
| Exclusion list at `tls_clienthello`, seeded default file with per-entry rationale | PXY-013, PXY-014 |
| Passthrough flow records | PXY-015 |
| `pporlock run` foreground mode | PXY-005 |
| `pporlock doctor` — CA, ports, QUIC, config checks | PXY-004, PXY-012 |
| CA install/trust into the **login** keychain | PXY-010, PXY-011 |

**Exit demo (v0.1 exit criterion 1):** thirty minutes of ordinary browsing through the proxy with zero certificate warnings and zero broken sites. `pporlock doctor` reports all green. An excluded host tunnels undecrypted and appears as a passthrough record.

**Security focus:** login keychain not System keychain; exclusion list correctness for pinning and financial hosts.

---

### S3 — Capture and control API

**Branch:** `sprint-03-capture-api`
**Specs:** SPEC-1 §6.1–§6.2, §7.1–§7.2, §7.4–§7.6; SPEC-0 §6

| Scope | Requirements |
|---|---|
| Ring buffer, dual bounds, body cap, eviction | CAP-001, CAP-003 |
| Filter vocabulary, one implementation | CAP-004 |
| Control server on the proxy event loop, loopback-locked | API-001, API-010 |
| Loop discipline: inline vs executor route classification + its test | API-002 |
| Token store, pairing window, origin and CSRF policy | API-011, API-012, API-013, API-004 |
| Routes: `/state`, `/state/health`, `/flows`, `/flows/{id}`, `/config`, `/exclusions`, `/audit` | API-020, API-021, API-025, API-029 |
| Detail levels and serialization | SPEC-0 §6.3 |
| Audit log | MCP-031 |

**Exit demo:** browse; `curl` the flows endpoint and see real traffic with filters applied. A cross-origin form POST from a fixture page is rejected. An unauthenticated mutating request is rejected. `/state/health` answers without a token.

**Security focus:** the full §2.5 loopback, token, origin/CSRF, and logging-hygiene rows. This sprint is where the access-control model is either right or wrong.

---

### S4 — SSE and the live flow table ◀ **VISUAL MVP**

**Branch:** `sprint-04-visual-mvp`
**Specs:** SPEC-1 §7.3; SPEC-2 §3, §4, §5

| Scope | Requirements |
|---|---|
| SSE hub, per-subscriber bounded queues, drop-don't-backpressure, `stream.gap` | API-022, SPEC-0 §7 |
| Server-side event filtering | SPEC-0 §7.1 |
| Web UI shell, routing, status bar, disconnected state | WUI-001, WUI-002, WUI-013 |
| API client and event stream modules | SPEC-2 §4 |
| Virtualized live flow table with the filter vocabulary and flag icons | WUI-003, PRF-004 |
| Static asset serving from the daemon | API-003 |

**Exit demo (v0.1 exit criterion 3):** open `http://127.0.0.1:8081`, browse in another tab, and watch flows arrive live. Filters narrow both the table and the subscription. Kill the daemon and the UI shows an unmistakable disconnected banner, not an empty table.

**This is the first sprint whose output you can look at.**

**Security focus:** SSE subscriber isolation; no token in URLs (SSE auth via header or a short-lived stream ticket, decided and documented this sprint).

---

### S5 — Extension proxy control

**Branch:** `sprint-05-extension-control`
**Specs:** SPEC-3 §2–§5

| Scope | Requirements |
|---|---|
| MV3 scaffolding, CRXJS build, minimal permissions | EXT-001, EXT-024 |
| Service worker state model, storage split, alarms heartbeat | SPEC-3 §3.1 |
| Proxy controller, fixed-server mode, bypass list, control-conflict detection | EXT-002, SPEC-3 §4.3 |
| **Fail-safe health monitor** | EXT-010, PXY-008 |
| Pairing flow | EXT-022 |
| Popup: toggle, status, profile placeholder, dev-toggle indicator | EXT-011 |
| Badge — global counts this sprint; per-tab arrives in S6 | EXT-012 (partial) |

**Exit demo (v0.1 exit criterion 4):** toggle the proxy on and off from the popup with no manual macOS settings change; the badge increments as traffic flows. **Kill the daemon and watch the extension clear Chrome's proxy configuration within two health-check intervals and show an error state**, with the browser still able to reach the internet.

**Note:** the badge is a global count until S6 provides attribution. This is a deliberate, stated partial.

**Security focus:** token never touches the filesystem from the extension; bypass list includes the control origin; fail-safe test is automated and gates the merge.

---

### S6 — Attribution and platform spikes ◀ **OI-1 / OI-2 resolved**

**Branch:** `sprint-06-attribution`
**Specs:** SPEC-0 §3.6; SPEC-1 §6.6; SPEC-3 §6

| Scope | Requirements |
|---|---|
| `webRequest` observation, batching, bounded buffer | SPEC-3 §6.1 |
| `POST /attribution`, join window, `flow.updated` backfill | SPEC-0 §3.6, §7.3 |
| Coverage metric in `GET /metrics` | API-028 |
| Per-tab badge counters; unattributed bucket | EXT-012 |
| **OI-1 verification:** empirically confirm PNA state for extension→loopback | API-005 |
| Fallback decision recorded in the sprint log | OI-1, OI-2 |

**Exit demo:** a 30-minute reference browsing session reports ≥95% attribution coverage, or the fallback is adopted and the sprint log records why. Badge counts are per-tab and correct across navigation. PNA finding is documented; if it blocks the HTTP channel, the Native Messaging fallback is scoped as an inserted sprint before S7.

**This sprint is a decision gate.** If OI-1 fails, stop and re-plan rather than proceeding.

---

### S7 — Rules engine and blocking

**Branch:** `sprint-07-rules-engine`
**Specs:** SPEC-1 §4.1–§4.3, §4.7; SPEC-0 §5.3, §5.4, §5.6

| Scope | Requirements |
|---|---|
| Matcher with every criterion, compile-at-load, PRF-002 ordering | MOD-010, MOD-011, PXY-025 |
| RuleSet partitioned by phase, priority ordering | MOD-012 |
| Evaluator phase machine, provenance emission | PXY-020, CAP-010–CAP-013 |
| `block` action, `stub`/`kill` modes | PXY-030, PXY-031 |
| `Sec-Fetch-Dest` derivation table, stub library | PXY-032, PXY-033 |
| YAML rule loading and hot reload of the rule file | MOD-004 (partial) |

**Exit demo (v0.1 exit criterion 2, and v0.2):** a host is blocked and the pages referencing it still render correctly. A stubbed tracker script lets `analytics.track()` succeed instead of throwing. Rules edited through the API take effect without restarting the proxy. Provenance for a blocked flow shows the exact rule and the derived stub type.

**Coverage note:** `engine/` must hit 90% here (REQ TST-002). This is the sprint where that bar is set.

---

### S8 — Provenance UI

**Branch:** `sprint-08-provenance-ui`
**Specs:** SPEC-2 §6; SPEC-3 §7

| Scope | Requirements |
|---|---|
| Flow detail panel: overview, request, response, body rendering | WUI-004 |
| **Provenance view** — every phase, outcome, and note code | CAP-013, DOC-003 |
| DevTools panel v1: per-tab flow list and provenance | EXT-013, EXT-014 |
| Jump-to-module links (targets land in S12) | EXT-014 |

**Exit demo:** for a blocked flow, the provenance view names the module, the rule, the action, and `short_circuited_by`. Every outcome in SPEC-0 §4.3 and every note code in §4.4 has a rendering, verified by a test that iterates the enum.

**Why here:** the debugging affordance must exist before the pipeline gets complicated enough to need it.

---

### S9 — Short-circuit actions and the buffering guard

**Branch:** `sprint-09-actions-buffering`
**Specs:** SPEC-1 §3.4, §4.4, §4.5

| Scope | Requirements |
|---|---|
| `map_local` with containment, missing-file error surfacing | PXY-034 |
| `redirect` — scheme, host, port, path, query | PXY-035 |
| `headers` — add/remove/replace, request and response, case-insensitive | PXY-036 |
| Buffering guard at `responseheaders`, `wants_body` short-circuit | PXY-021, PXY-022 |
| Time budget and `skipped_budget` | PXY-026 |
| Executor offload plumbing | PXY-024 |

**Exit demo:** a large response streams and its provenance says why; a `map_local` rule serves a local stub; a missing `map_local` file produces a visible error rather than silence; a rule attempting `../` path escape is rejected at load.

**Security focus:** path traversal containment (§2.5), SSRF-via-redirect documentation.

---

### S10 — Body rewriting

**Branch:** `sprint-10-body-rewriting`
**Specs:** SPEC-1 §3.6, §4.6; SPEC-0 §5.5

| Scope | Requirements |
|---|---|
| Transform registry with load-time parameter validation | MOD-013, MOD-014 |
| `strip_integrity_attributes`, applied unconditionally on rewritten documents | PXY-040 |
| `strip_csp`, both headers | PXY-042 |
| `inject_script` with nonce reuse before relaxation | PXY-041 |
| `regex_sub`, `replace_literal`, `json_patch`, `inject_style` | MOD-013 |
| `anticache` / `anticomp` dev toggles and their indicators | PXY-043, PXY-044, WUI-012 |
| Body diff view | CAP-014, WUI-014 |

**Exit demo (v0.3):** a script injected into a CSP-bearing page runs with no console errors, reusing the page's own nonce. A rewritten SRI-bearing script is not dropped by the browser. The diff view shows before and after. Dev toggles show their indicator in both the UI and the popup.

**Security focus:** injection content not assembled from response data; nonce handling; SRI/CSP changes always recorded and surfaced.

---

### S11 — Module system and profiles

**Branch:** `sprint-11-modules`
**Specs:** SPEC-1 §5; SPEC-0 §5.1, §5.2, §5.7, §8

| Scope | Requirements |
|---|---|
| Module directory format, manifest validation, API-version gating | MOD-001–MOD-003, MOD-026 |
| Loader with per-module error isolation | MOD-005 |
| Python tier: hooks, `ModuleContext`, module store | MOD-020–MOD-022 |
| Interleaved evaluation by priority | MOD-023 |
| Error isolation and quarantine after N failures | MOD-024, MOD-025 |
| Hot reload as an atomic snapshot swap | MOD-004 |
| Profiles: CRUD, activation, profile-scoped toggles | MOD-040–MOD-044 |
| Module and profile API routes | API-023, API-024 |

**Exit demo (v0.4):** a Python module loads, fires, and appears in provenance. A module with a syntax error disables only itself and reports its traceback. A module that raises on ten consecutive flows is quarantined without affecting the proxy or other modules. Profile switching takes effect immediately.

**Security focus:** the trusted-module boundary — verify documentation states it plainly; module asset path containment; strict manifest parsing.

---

### S12 — Authoring UI

**Branch:** `sprint-12-authoring-ui`
**Specs:** SPEC-2 §7, §8.1

| Scope | Requirements |
|---|---|
| Module library with inline load errors and quarantine reasons | WUI-005 |
| Monaco editor, schema-attached YAML validation, save-and-reload | WUI-006 |
| `POST /validate` wired to editor markers | API-027 |
| Rule builder emitting into the canonical YAML, round-trip safe | WUI-007 |
| Create-rule-from-flow, two clicks from the table | WUI-008, MCP-014 |
| Profiles UI | WUI-009 |

**Exit demo:** author a working module entirely in the browser — from noticing a bad request in the flow table, through create-rule-from-flow, to editing in Monaco, to enabling it — without touching a terminal.

---

### S13 — Sessions and redaction

**Branch:** `sprint-13-sessions`
**Specs:** SPEC-1 §6.3, §6.4; SPEC-0 §9; SPEC-2 §8.2

| Scope | Requirements |
|---|---|
| SQLite session schema, WAL, off-loop batched writer, overflow drops | CAP-020, CAP-023 |
| Session CRUD and browsing, shared table/detail components | CAP-021, WUI-010 |
| Redaction at write time for sessions, at serialize time for API | CAP-040–CAP-042, CAP-045 |
| Configurable patterns, effective config visible | CAP-044 |
| UI-only unmask on live flows | CAP-043 |
| HAR and native export | CAP-024 |
| Recording from the popup | CAP-025, EXT-023 |
| WebSocket capture and display | PXY-050, PXY-051, PXY-052 |

**Exit demo (v0.5 partial):** record a session, stop it, browse it. Verify with an external SQLite reader that **no unredacted `Cookie` or `Authorization` value exists in the file**. Unmask works on a live flow and is absent on a session flow.

**Security focus:** the redaction row of §2.5, in full. This is the sprint where a mistake writes secrets to disk.

---

### S14 — Dry run and MCP

**Branch:** `sprint-14-dryrun-mcp`
**Specs:** SPEC-1 §6.5, §11; SPEC-2 §8.3

| Scope | Requirements |
|---|---|
| Dry runner using the same `Evaluator` and `ModuleLoader` as live | CAP-030, CAP-031, CAP-032 |
| Aggregate summary and per-flow diffs | CAP-033 |
| Dry-run UI with collapsed unaffected flows and the code-execution warning | WUI-010 |
| MCP stdio server as an HTTP client of the control API | MCP-001, MCP-002 |
| All four tool families | MCP-010–MCP-013 |
| Guardrails: no enable on create, no unmask, provenance always, audit tagging | MCP-003, MCP-004, MCP-030–MCP-032 |
| Token-cost discipline in tool defaults | MCP-005 |
| MCP activity indicator in the UI | MCP-033 |

**Exit demo (v0.6):** with only MCP access, an agent records a session, reads provenance to find why a page broke, authors a module, dry-runs it until clean, and enables it. Confirm the agent could not enable it without a separate explicit call, and that no MCP response contained an unmasked secret.

---

### S15 — Extension completion

**Branch:** `sprint-15-extension-complete`
**Specs:** SPEC-3 §8–§10

| Scope | Requirements |
|---|---|
| In-page modification banner in a closed shadow root | EXT-020 |
| Per-host and global suppression | EXT-021 |
| Options page: pairing, connection, proxy mode, warnings, badge, attribution diagnostics | SPEC-3 §9 |
| PAC mode | EXT-003 |
| DevTools panel polish, unattributed bucket, redaction rendering | EXT-013, CAP-043 |
| Full error-code → UI mapping | SPEC-3 §10 |
| Playwright E2E for the extension | TST-006 |

**Exit demo:** visit a page whose CSP was relaxed and see a banner naming the responsible module; suppress it for that host and confirm the badge still reports the modification. Full Playwright suite green.

**Security focus:** banner isolation (closed shadow root, no page-content reads); injection-into-page row of §2.5.

---

### S16 — Packaging and hardening

**Branch:** `sprint-16-release`
**Specs:** SPEC-1 §8.3, §12, §13; all DOC requirements

| Scope | Requirements |
|---|---|
| launchd user agent, install/uninstall, log rotation | PXY-002, PXY-007 |
| Full CLI surface and `doctor --fix` | PXY-003, PXY-004 |
| Complete uninstall including CA removal, with a statement of what remains | DOC-005 |
| Benchmark harness and PRF-001/002 measurement | PRF-001–PRF-003 |
| Soak test for bounded memory | PRF-005 |
| Per-module cost in metrics | PRF-007 |
| Accessibility pass | WUI-015 |
| API contract tests against OpenAPI | TST-005 |
| Golden corpus regression suite | TST-004 |
| All documentation | DOC-001–DOC-006 |
| Full security review of the accumulated system | §2.5 in full |

**Exit demo (v1.0):** clean install on a fresh machine following only `docs/install.md`, ending in intercepted traffic visible in the UI and a working module. Benchmark numbers meet PRF-001/002 or the gap is documented with a plan.

---

## 5. Risk register

| Risk | Sprint | Mitigation | Trigger to re-plan |
|---|---|---|---|
| PNA blocks extension→loopback | S6 | Native Messaging fallback, API designed transport-agnostic (API-005) | Insert a Native Messaging sprint before S7 |
| Tab attribution below 95% | S6 | Two named fallbacks, confined to one module each side | Adopt fallback within S6; do not proceed to S8 unattributed |
| mitmproxy API churn on upgrade | any | Version pinned; changes confined to `addon/`; TST-007 gates upgrades | Treat as its own branch, never inside a feature sprint |
| Performance ceiling missed | S16 | Measured at S10 as an early warning, not first at S16 | If S10 shows >30% p50, insert a performance sprint before S11 |
| Event-loop stalls under load | S3, S4 | Loop-discipline test from S3; drop-don't-backpressure everywhere | Any sprint whose gate shows browsing slowdown |
| Coverage gate gamed by deletion | every | G4 is reviewed by hand, not automated | — |

---

## 6. Traceability

Every requirement in `pporlock_requirements-v1.md` appears in exactly one sprint's scope table. `docs/sprint-log.md` records, per sprint: requirements delivered, requirements deferred with reasons, gate results, decisions made, and the merge commit SHA.

A requirement that appears in no sprint is a planning bug. A sprint that closes without its requirement IDs demonstrated has not closed.
