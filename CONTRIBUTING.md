# Contributing

pporlock is feature-complete and in fit-and-finish. Most useful contributions
are small: a bug with a reproduction, a documentation error, a module for
`examples/`.

**It is macOS-only, and that is a design position rather than a gap.** It
installs a launchd agent, trusts its CA through the system keychain, and drives
Chrome. A port to another platform is not a small change and is not currently
planned.

---

## Reporting something

Open an issue and include **`pporlock version`**. "Which version are you on" is
the first question of every diagnosis, and it was worthless here for eighteen
sprints because the number never moved — see `docs/open-issues.md` OI-25. The
issue template asks for it first for that reason.

If a module did something you did not expect, the **provenance** for the flow is
the thing to paste. It records every rule that was considered, what it did, and
what it did not do and why. It is far more useful than a description of the
symptom.

---

## Before you open a pull request

```bash
make setup     # toolchains, contracts, git hooks
make gate      # coverage + tests + lint + security
```

`make gate` is the whole automated bar, and CI runs exactly that — not a
hand-copied approximation of it, so there is one definition of green.

Four things the gate cannot check, which are still expected:

| | |
|---|---|
| **Demonstrate it** | A requirement is done when it has been *seen working*, not when a test passes. Four of this project's worst bugs survived a fully green suite; `docs/sprint-log.md` names them. |
| **Do not delete a test to move a number** | Removing a test that no longer describes real behaviour is correct. Say which, and what replaced it. |
| **Never squash** | One branch per piece of work, merged `--no-ff`. Granular history is a deliberate requirement, not a preference. |
| **Walk §2.5** | `docs/implementation-plan.md` §2.5 is the hand-reviewed security checklist, and it is the real gate. The scanners find mechanical problems; the checklist found query-string secrets being written to disk unredacted. |

Cite requirement IDs (`PXY-`, `MOD-`, `CAP-`, `API-`, `WUI-`, `EXT-`, `MCP-`,
`PRF-`, `TST-`, `DOC-`, `SCP-`) in commit messages and test names for anything
traceable. `docs/pporlock_requirements-v1.md` is the list.

---

## Things that are not bugs

`SECURITY.md` states the trust model. Two consequences come up often enough to
repeat here:

- **Module code is unsandboxed by design**, and dry run executes it too. That is
  the model, not a vulnerability.
- **The proxy terminates TLS and holds session cookies in memory.** It is a
  single-user tool for a machine you own.

Please read `SECURITY.md` before reporting either as a finding.

---

## Where things are

| | |
|---|---|
| `CLAUDE.md` | The working agreement — layout, conventions, and the load-bearing structural rules. Written for an agent, accurate for a human. |
| `contracts/` | **Source of truth** for every cross-component shape. Change it there first, regenerate, then use it. |
| `docs/open-issues.md` | Read before starting. The closed entries record decisions you would otherwise re-litigate. |
| `docs/module-authoring.md` | Writing a module. `examples/modules/` is the working library, and it is tested. |

Generated files — `contracts/generated/types.ts`, `docs/api-reference.md`,
`docs/rule-schema.md`, and every version field — are never hand-edited. `make
contracts`, `make docs` and `make version-sync` produce them, and the pre-commit
hook rejects a stale or edited copy.
