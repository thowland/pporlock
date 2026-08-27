# pporlock — Project Instructions

**pporlock** is a single-user, single-machine HTTPS interception and modification system for Chrome: a mitmproxy-based daemon, a React web UI for authoring filter modules, a Chrome extension, and an MCP server for AI-assisted module authoring.

---

## Read this first on any restart

Documentation is layered. Load only what the task needs — the specs are written to be read independently.

| Document | When to read it |
|---|---|
| `docs/pporlock_approach-rev1.md` | Design rationale and the *why* behind decisions. Background, not normative. |
| `docs/pporlock_requirements-v1.md` | **Normative.** ~150 numbered requirements (`PXY-nnn`, `MOD-nnn`, `CAP-nnn`, `API-nnn`, `WUI-nnn`, `EXT-nnn`, `MCP-nnn`, `PRF-nnn`, `TST-nnn`, `DOC-nnn`). Cite these IDs in commits and tests. |
| `docs/spec-0-contracts.md` | **Always, for any implementation work.** Data model, provenance, rule/module schemas, control API, SSE events, module API stability contract, redaction format. |
| `docs/spec-1-daemon.md` | Daemon or MCP work. Pipeline, rules engine, module system, capture, control server, CLI, launchd. |
| `docs/spec-2-web-ui.md` | Web UI work. Independent of SPEC-1 internals. |
| `docs/spec-3-extension.md` | Extension work. Independent of SPEC-1 internals. |
| `docs/implementation-plan.md` | **Always.** Sprint order, scope tables, close gates, security checklist. |
| `docs/sprint-log.md` | Current state: which sprint, what's delivered, what's deferred, decisions made. |

**Context discipline:** a web UI task loads SPEC-0 + SPEC-2. An extension task loads SPEC-0 + SPEC-3. Only daemon work needs SPEC-1. Do not load all four.

**Precedence:** requirements > SPEC-0 > component spec > approach doc. If they conflict, that is a bug — raise it, do not silently pick one.

---

## Current state

Check `docs/sprint-log.md` and `git log --oneline --graph --decorate -20`. Sprint completion is tagged `sprint-NN-complete`.

---

## Repository layout

```
daemon/      Python 3.12, uv    — mitmproxy addon, engine, capture, control server, CLI
mcp/         Python 3.12, uv    — MCP stdio server (HTTP client of the control API)
web/         React + Vite + TS  — web UI, served by the daemon at 127.0.0.1:8081
extension/   MV3 + CRXJS + TS   — Chrome extension
contracts/   schemas + OpenAPI  — SOURCE OF TRUTH for cross-component shapes
stubs/                          — shipped script stub library
tests/fixtures/origin/          — local fixture HTTP/HTTPS origin server
docs/
```

### Load-bearing structural rules

These are not style preferences. Breaking one breaks the architecture.

1. **`daemon/src/pporlock/engine/` imports nothing from `mitmproxy`, `asyncio`, or `control/`.** There is a test asserting this. Do not weaken it. It is what makes the rules engine unit-testable without a proxy (REQ TST-001, DD-2).
2. **`addon/normalize.py` is the only mitmproxy→pporlock boundary.** All mitmproxy version churn is absorbed there.
3. **TypeScript types are generated from `contracts/schemas/`.** Never hand-write a type that describes a wire shape. Run `make contracts` after schema changes.
4. **The control server shares the proxy's event loop.** Anything doing I/O offloads to the executor. There is a test asserting no inline-classified route performs I/O.
5. **Provenance is a structural return value of the engine, not logging.** Every flow carries it, everywhere (REQ CAP-010).
6. **Listeners bind loopback only, asserted in code.** A non-loopback config value is rejected at startup.

---

## Commands

```bash
make setup       # install toolchains, generate contracts, install hooks
make contracts   # validate schemas, regenerate contracts/generated/types.ts
make all         # contracts -> daemon, web, extension

make test        # ALL tests, all components
make coverage    # per-component thresholds
make lint        # ruff, mypy, eslint, tsc
make security    # bandit, pip-audit, npm audit, gitleaks
make gate        # coverage + test + lint + security  <- run before every merge

make e2e         # Playwright
make fixtures    # fixture origin server standalone
make bench       # PRF-001/002 harness
```

---

## Sprint procedure

### Branch

One branch per sprint, `sprint-NN-slug`, cut from `master`:

```bash
git checkout master && git pull
git checkout -b sprint-07-rules-engine
```

Commit granularly. Reference requirement IDs in commit messages. **Never squash** — granular history is a deliberate requirement.

### Close gates — ALL must pass before merge

Do not merge, and do not declare a sprint complete, until every one of these holds. "We'll fix it next sprint" is not available.

| # | Gate | How |
|---|---|---|
| **G1** | Sprint requirements met | Every requirement ID in the sprint's scope table (`docs/implementation-plan.md` §4) is implemented and demonstrated. The sprint's exit demo runs. |
| **G2** | Coverage ≥ 80% per component | `make coverage`. Per-component, not repo-wide: `daemon` ≥ 80%, `daemon/src/pporlock/engine` ≥ **90%**, `mcp` ≥ 80%, `web` ≥ 80%, `extension` ≥ 80%. A component with any code this sprint is not exempt. |
| **G3** | **All** tests pass, including earlier sprints' | `make test`. A regression in Sprint 3's tests blocks Sprint 9's merge. Fix the regression; do not delete the test. |
| **G4** | Obsolete tests removed, coverage still met | Deleting a test that no longer describes real behavior is correct. Deleting a test to raise a coverage number is not. If removal drops a component below threshold, write new tests before closing. |
| **G5** | Lint clean | `make lint`. Individually-justified inline suppressions with a stated reason are acceptable. Blanket file- or directory-level disables are not. |
| **G6** | Security gate | `make security` clean at high severity, **plus** the hand-reviewed checklist in `docs/implementation-plan.md` §2.5 for every area this sprint touched. |
| **G7** | Merge preserving history | `git merge --no-ff` into `master`. Never squash. |

### Merge

```bash
make gate
# ... G1 exit demo, G4 review ...
git checkout master
git merge --no-ff sprint-07-rules-engine -m "Merge sprint 07: rules engine and blocking"
git tag -a sprint-07-complete -m "Sprint 07 complete: rules engine, blocking, stub synthesis"
git branch -d sprint-07-rules-engine
```

Then update `docs/sprint-log.md`: requirements delivered, requirements deferred **with reasons**, gate results, decisions made, merge SHA.

---

## Security standards

This system terminates TLS, holds session cookies in memory, runs unsandboxed user code, and can rewrite any page. Treat every sprint as security-relevant.

`make security` runs bandit, pip-audit, npm audit, eslint-plugin-security, and gitleaks. It catches mechanical issues only. **The hand-reviewed checklist in `docs/implementation-plan.md` §2.5 is the real gate** — it maps this system's actual risks (loopback binding, token handling, origin/CSRF policy, `map_local` path traversal, SSRF via `redirect`, redaction correctness, the trusted-module boundary, injection into rewritten pages, schema strictness, logging hygiene, the extension fail-safe) to OWASP categories and to the sprints that touch them.

Standing rules:

- Never log request or response bodies at default level. Log headers only with redaction applied.
- Never put the bearer token in a URL, an error body, or the audit log.
- Session data is redacted **at write time**. A session file on disk must never contain an unredacted secret.
- The MCP interface has no unmask capability and cannot enable a module it just created.
- Module code is deliberately trusted and unsandboxed. Every authoring surface must say so.
- `map_local` and module asset paths are confined to the module's `assets/` directory, symlinks resolved before the containment check.

---

## Conventions

- **Requirement IDs** in commit messages, test names, and docstrings for anything traceable.
- **Python:** ruff format, ruff check, `mypy --strict` on `engine/`. Dataclasses for wire shapes, frozen where the spec says frozen.
- **TypeScript:** strict, `noUncheckedIndexedAccess`. Generated types only for wire shapes.
- **Tests:** name the requirement. `test_first_match_wins_for_short_circuit_actions  # REQ MOD-012`.
- **New cross-component field?** Add it to `contracts/` first, then regenerate, then use it. Never invent a wire field in a component.

---

## Known decision points

Two items were spikes resolved in Sprint 6; check `docs/sprint-log.md` for the outcomes before assuming either:

- **OI-1** — Private Network Access for extension→loopback. If it blocked the HTTP control channel, the system uses Native Messaging and SPEC-0 §6 is transported differently.
- **OI-2** — tab attribution mechanism and its measured coverage. Everything consuming `tab_id` must still tolerate `null`.
