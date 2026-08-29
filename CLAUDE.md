# pporlock — Project Instructions

**pporlock** is a single-user, single-machine HTTPS interception and modification system for Chrome: a mitmproxy-based daemon, a React web UI for authoring filter modules, a Chrome extension, and an MCP server for AI-assisted module authoring.

**The project is feature-complete and in fit-and-finish.** Sprints 0–18 are merged and tagged (`sprint-NN-complete`). Read *Where things stand* before planning work — the remaining items are known, listed, and small.

---

## Read this first on any restart

Documentation is layered. Load only what the task needs; the specs are written to be read independently.

**Context discipline:** a web UI task loads SPEC-0 + SPEC-2. An extension task loads SPEC-0 + SPEC-3. Only daemon work needs SPEC-1. Do not load all four.

### Normative — the contract

| Document | When |
|---|---|
| `docs/pporlock_requirements-v1.md` | ~176 numbered requirements (`PXY-`, `MOD-`, `CAP-`, `API-`, `WUI-`, `EXT-`, `MCP-`, `PRF-`, `TST-`, `DOC-`, `SCP-`). Cite these IDs in commits and tests. |
| `docs/spec-0-contracts.md` | **Always, for implementation.** Data model, provenance, rule/module schemas, control API, SSE events, module API (§8), redaction (§9). |
| `contracts/` | **Source of truth** for every cross-component shape. OpenAPI + JSON Schemas. Outranks any prose describing them. |
| `docs/spec-1-daemon.md` | Daemon or MCP work. |
| `docs/spec-2-web-ui.md` | Web UI work. |
| `docs/spec-3-extension.md` | Extension work. |

**Precedence:** requirements > SPEC-0 > component spec > approach doc. Where a spec disagrees with working code, *that is a finding* — raise it, do not silently pick one. This has happened four times and every instance was a real bug in one side or the other.

### Where things stand

| Document | What it tells you |
|---|---|
| `docs/open-issues.md` | **Read before starting anything.** 24 issues — 17 closed (one partly), 7 open. The closed ones record decisions you would otherwise re-litigate, and several record a bug's *shape* rather than just its fix. |
| `docs/sprint-log.md` | What each sprint delivered, deferred, and why. Every bug found, with the shape of the mistake. |
| `docs/implementation-plan.md` | Sprint history and §2.5, the hand-reviewed security checklist — still the real security gate. |

### Versioning

Semver, one source: the `VERSION` file at the repository root. Everything else
is generated from it and `make version-check` fails the gate on drift.

| Change | Bump |
|---|---|
| A significant merge — a behaviour fix, a new capability, a contract change | `make bump-minor` |
| A bundle of small ones — docs, tests, tidying | `make bump-patch` |

**Bump on the branch, before the merge.** The number exists so a running system
can be identified: "which version are you on" is the first question of every
diagnosis, and it is worthless if the answer has been 0.1.0 since Sprint 0 —
which it was, through eighteen sprints, until OI-25.

`make version-sync` propagates; `make version` prints it. Never edit a
`pyproject.toml`, `package.json` or the extension manifest version by hand.
Python reads its version from installed package metadata, so there is no
literal to update.

A prerelease (`0.3.0-rc.1`) is fine in `VERSION`. Chrome cannot store one, so
the manifest gets the numeric core and the full string goes in `version_name` —
handled automatically.

### Generated — never hand-edit

| File | Regenerate with |
|---|---|
| `contracts/generated/types.ts` | `make contracts` |
| every version field (`pyproject.toml`, `package.json`, the extension manifest) | `make version-sync` |
| `docs/api-reference.md` | `make docs` |
| `docs/rule-schema.md` | `make docs` |

The pre-commit hook and `make gate` both reject stale or hand-edited copies.

### User-facing documentation

`README.md`, `docs/install.md`, `docs/module-authoring.md`, `docs/module-cookbook.md`, `docs/troubleshooting.md`, `docs/worked-example.md`, `docs/llm-with-mcp.md`, `examples/README.md`.

`examples/modules/` holds eight working modules. **They are tested** (`daemon/tests/unit/test_examples.py`) and are the closest thing to a public API conformance suite: a change that breaks a module written the documented way breaks there.

---

## Repository layout

```
daemon/            Python 3.12, uv    — mitmproxy addon, engine, capture, control server, CLI, bench/
mcp/               Python 3.12, uv    — MCP stdio server (HTTP client of the control API)
web/               React + Vite + TS  — web UI (served by the daemon), Playwright suites in e2e/
extension/         MV3 + CRXJS + TS   — Chrome extension
contracts/         schemas + OpenAPI  — SOURCE OF TRUTH for cross-component shapes
examples/modules/                     — the shipped example module library, tested
stubs/                                — shipped script stub library
testfixtures/origin/                  — local fixture HTTP origin server
docs/
```

---

## Load-bearing structural rules

Not style preferences. Breaking one breaks the architecture, and each has a test.

1. **`daemon/src/pporlock/engine/` imports nothing from `mitmproxy`, `asyncio`, or sibling packages.** An AST test asserts it. It is what makes the rules engine testable without a proxy (REQ TST-001, DD-2).
2. **`addon/normalize.py` is the only mitmproxy→pporlock boundary.** All version churn is absorbed there.
3. **TypeScript types for wire shapes are generated from `contracts/schemas/`.** Never hand-write one.
4. **The control server shares the proxy's event loop.** Anything doing I/O goes through `offload()` and is listed in `OFFLOAD_ROUTES`. A test asserts every route is classified.
5. **Provenance is a structural return value of the engine, not logging.** Every flow carries it (REQ CAP-010).
6. **Listeners bind loopback only, asserted in code.** A non-loopback config value is rejected at startup.
7. **Never `assert` a runtime invariant in `src/`.** `python -O` strips them and bandit flags them. Raise.
8. **User state never goes in a user's file.** Module enablement and the active profile live in `state_dir` sidecars. The daemon does not rewrite a manifest to record a toggle.

---

## Commands

```bash
make setup       # toolchains, contracts, git hooks
make contracts   # validate schemas, regenerate types AND the generated docs
make docs        # regenerate docs/api-reference.md and docs/rule-schema.md
make all         # contracts -> daemon, web, extension
make examples    # install the example modules (disabled, never overwriting)

make gate        # coverage + test + lint + security. Run before every merge.
make test / coverage / lint / security

make e2e         # Playwright — web headless, extension headed (MV3 requires it)
make fixtures    # fixture origin standalone
make bench       # PRF-001/002 harness
make version     # print it   /  version-sync, version-check, bump-minor, bump-patch
make bench-saturation  # concurrency/throughput vs mitmproxy's own ceiling (OI-21)
```

**Current baseline:** daemon 1940, web 494, extension 260, mcp 134, E2E 28. Coverage: daemon 93%, `engine/` 96.8%, web 94.5%, extension 93%, mcp 98.9%.

If a number drops, something was deleted. Find out what.

---

## How to work now

Sprints are over. Work is issue-driven, but the gates did not relax.

**One branch per piece of work**, cut from `master`, merged `--no-ff`. **Never squash** — granular history is a deliberate requirement.

```bash
git checkout master && git pull
git checkout -b fix-oi-13-unused-outcome
# ... work ...
make gate
git checkout master && git merge --no-ff fix-oi-13-unused-outcome
```

Before merging, all of these still hold:

| Gate | Meaning |
|---|---|
| **G1** | The requirement or issue is demonstrated, not merely implemented. |
| **G2** | Coverage thresholds hold per component (`engine/` ≥ 90%, everything else ≥ 80%). |
| **G3** | **All** tests pass, including every earlier one. A regression blocks the merge; fix it, do not delete the test. |
| **G4** | A test removed because it no longer describes real behaviour is correct. A test removed to raise a number is not. Name any you remove and its replacement. |
| **G5** | Lint clean. Individually justified inline suppressions are fine; blanket disables are not. |
| **G6** | `make security` clean, **plus** the hand-reviewed checklist in `docs/implementation-plan.md` §2.5 for every area touched. |
| **G7** | `git merge --no-ff`. |

Then update `docs/open-issues.md` (close it, or record what changed) and `docs/sprint-log.md` if it is substantial.

---

## What this project learned the expensive way

Four of its worst bugs survived a fully green test suite. Every one was found by *using* the system rather than testing it. These are not anecdotes — they are why the gates above are not sufficient on their own.

1. **A unit test cannot tell you the daemon builds what you wrote.** Two sprints shipped a complete, fully-tested module system that `cli/runner.py` never constructed, so none of it ran. A unit test builds the objects it exercises and so cannot notice their absence.
   → Anything that must run in a real daemon gets a case in `daemon/tests/unit/test_runner.py::TestStartupWiring`, and you verify it against a daemon started by `pporlock run`.

2. **A test that stubs your own client agrees with whatever your client believed.** `GET /modules` returns a bare array; the web client expected an envelope, and the module library threw on first contact with a real daemon. `PUT /exclusions` reads `body["entries"]` — a bare array there would have silently deleted all 33 shipped exclusions.
   → Wire shapes are pinned in `web/src/api/wire-shapes.test.ts`, which stubs `fetch`, not the client. Check the daemon before writing a type.

3. **A published contract that nobody codes against drifts.** SPEC-0 §8 described a module API that did not exist, in six places, two of them not implementable as written. A module written faithfully from it raised `TypeError` on its first flow — and the error blamed the author.
   → `examples/modules/` exists partly to keep the public API honest.

4. **Scanners find mechanical problems; the checklist finds real ones.** Query-string secrets were written to disk unredacted while the header path was masked — found by walking §2.5 by hand, not by bandit.

**Corollaries that are now practice:** a guard you have not watched fail is not a guard (sabotage it once, then restore it). An exit demo is not a formality. An allowlist needs a test that fails when the allowlist becomes unnecessary.

---

## Security standards

This system terminates TLS, holds session cookies in memory, runs unsandboxed user code, and can rewrite any page. Treat every change as security-relevant.

`make security` runs bandit, pip-audit, npm audit, eslint-plugin-security, and gitleaks — mechanical issues only. **The hand-reviewed checklist in `docs/implementation-plan.md` §2.5 is the real gate.**

Standing rules:

- Never log request or response bodies at default level. Headers only, redacted.
- Never put the bearer token in a URL, an error body, or the audit log.
- Session data is redacted **at write time**. A session file on disk must never contain an unredacted secret — headers, JSON body keys, **or query strings**.
- Unmask is live-ring-only, web-UI-only, one value at a time. The MCP interface has no unmask capability and cannot enable a module it just created.
- Module code is deliberately trusted and unsandboxed, and **dry run executes it too**. Every authoring surface must say so.
- `map_local` and module asset paths are confined to the module's `assets/`, symlinks resolved before the containment check.
- `daemon/tests/conftest.py` refuses `shutil.rmtree` outside a temp directory. A test once deleted the real `~/.pporlock`. Do not work around it — if you hit it, your path is wrong.

---

## Conventions

- **Requirement IDs** in commit messages, test names, and docstrings for anything traceable.
- **Python:** ruff format, ruff check, `mypy --strict`. Dataclasses for wire shapes, frozen where the spec says frozen.
- **TypeScript:** strict, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`. Generated types for wire shapes only.
- **Tests:** name the requirement — `test_first_match_wins_for_short_circuit_actions  # REQ MOD-012` — and say *why* the behaviour matters, not just what it is.
- **Comments explain why.** The codebase's comments carry the reasoning behind non-obvious choices. Match that; do not narrate what the code already says.
- **New cross-component field?** `contracts/` first, regenerate, then use it. Never invent a wire field in a component.

---

## Known decision points

- **OI-1** (Sprint 6) — Private Network Access for extension→loopback. Resolved; HTTP control channel works.
- **OI-2** (Sprint 6) — tab attribution needs the optional `<all_urls>` grant; coverage is 0% without it. **Everything consuming `tab_id` must tolerate `null`**, and any feature keyed on it needs a fallback — the in-page banner needed one.

Open issues live in `docs/open-issues.md`. Currently open: OI-6, OI-10 (remaining half), OI-12, OI-13, OI-15, OI-20, OI-21.

**Not implemented:** MOD-006 (module export/import archive, Should). PXY-053 (WebSocket frame modification) is correctly out of scope — PXY-051 says frames shall not be modifiable in v1, and PXY-052 reserved the `ws_` namespace so adding it later is additive.

**Never run:** Sprint 16's exit demo on a fresh machine — a clean install following only `docs/install.md`. Everything is verified on a machine that already has the toolchain, the CA, and a Chrome profile. Given the four bugs above, this is the highest-value outstanding item in the project.
