# The golden corpus — REQ TST-004

> **REQ TST-004** — "A golden-file corpus of recorded sessions SHALL be maintained
> and used to regression-test module behaviour and dry-run output."
> **SPEC-1 §13** — "Recorded sessions in `daemon/tests/corpus/`, replayed to
> regression-test module behaviour and dry-run diffs."

Each file under `cases/` is one recorded flow, the rules that ran on it, and the
**provenance the engine produced** — stored verbatim. The suite replays every
case through the real `Evaluator` (and, for the dry-run case, the real
`DryRunner`) and compares.

The unit tests prove that any one rule works. The corpus does something the unit
tests cannot: it makes a *change in behaviour* visible. An edit to the evaluator,
the matcher, a transform, the stub table or the cost model that alters what a
real flow produces shows up here as a named field of a named provenance entry
changing value, on the commit that caused it — rather than as a page that renders
subtly wrong three sprints later.

Driven by `tests/unit/test_corpus.py`. Runs in the default `pytest` run; it is
fast (no I/O beyond reading these files and one temporary directory for the dry
run).

---

## Regenerating the goldens

```bash
cd daemon
PPORLOCK_REGEN_CORPUS=1 uv run pytest tests/unit/test_corpus.py
git diff tests/corpus/
```

**Regenerating is how you ACCEPT a behaviour change. It is never how you make a
red test green.**

A red corpus test means the engine now produces something different from what it
produced when the case was recorded. That is either a bug you have just
introduced, or a change you meant to make. Those two look identical to the test
and are told apart only by reading the diff. So:

1. Read the failure. It names the case, the JSON path of the first differing
   field, and both values.
2. Decide whether the new value is correct — by the requirements, not by whether
   it is what your branch happens to produce.
3. If it is correct: regenerate, **read `git diff tests/corpus/`**, and say in
   the commit message which requirement made the change correct.
4. If it is not correct: fix the code. Do not regenerate.

Regenerating without reading the diff converts this suite from a regression test
into a record of whatever the code did last, which is worth less than nothing:
it is a green check mark that means nothing.

---

## Case format

One JSON object per file in `cases/`. The file stem must equal `id`.

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Case identifier; must equal the filename stem. |
| `description` | yes | What behaviour this case pins, and why it matters. Printed on failure. |
| `requirements` | yes | Requirement IDs this case exercises. |
| `kind` | no | `"engine"` (default) or `"dryrun"`. |
| `profile` | no | Profile name for the provenance record. Default `"default"`. |
| `module` | no | Module name the rules compile under. Default `"corpus"`; it becomes the `rule_id` prefix. |
| `priority` | no | Module priority for ordering. Default `100`. |
| `asset_root` | no | Path relative to `tests/corpus/` used as the evaluator's asset root, for `map_local`. |
| `budget_ms` | no | Per-flow `TimeBudget` ceiling. Omit for no budget; `0.0` is the deterministic form of "already exhausted". |
| `rules` | yes | Raw rules, in the `rules.yaml` shape, compiled by `RuleSet.from_rules`. |
| `request` | engine | A `NormalizedRequest` (see below). |
| `response` | no | A `NormalizedResponse`, or `null` for a flow that never got one. |
| `buffering` | no | `{"content_type": ..., "content_length": ...}`. Present means the case also drives `decide_buffering`. |
| `expected_provenance` | engine | The golden: `Provenance.to_dict()`. |
| `flows` | dryrun | The recorded flows to replay, each `{flow_id, request, response}`. |
| `dry_run` | dryrun | The `POST /dryrun` request body (SPEC-0 §6.8): `modules`, `use_installed`, `include_diffs`, `limit`. |
| `expected_dry_run` | dryrun | The golden: the full `DryRunner.run` result. |

`request` and `response` are the `pporlock.engine.models` shapes with JSON-safe
substitutions: `headers` and `query` are lists of `[name, value]` pairs, and
`body` is a UTF-8 string (or `null`). Everything else is the field name from
`NormalizedRequest` / `NormalizedResponse` verbatim, and every field has the
dataclass default when omitted.

### Timing fields

`duration_ms`, `total_ms`, `avg_ms`, `p95_ms` and `_duration_ms` are
`perf_counter` deltas and their aggregates. They differ on every run, on every
machine, so they are normalised to the string `"<timing>"` on both sides before
comparison. They are normalised rather than deleted deliberately: a golden that
*stops carrying* a timing field is still a real change and still shows up as a
diff.

---

## What the corpus covers

`test_the_corpus_covers_the_outcome_and_note_taxonomy` asserts the spread below
still exists, so deleting a case does not quietly shrink the coverage this
directory is here to provide.

| Case | Pins |
|---|---|
| `01-passthrough-no-rule-matches` | Provenance is structural and present even when nothing fired (REQ CAP-013). |
| `02-block-stub-synthesised-for-a-script` | `block`/stub, `Sec-Fetch-Dest` stub derivation, `short_circuited_by` (REQ PXY-032). |
| `03-block-kill-mode` | `block`/kill stays distinguishable from `block`/stub (REQ PXY-031). |
| `04-first-short-circuit-rule-wins` | First match wins across short-circuit actions; the second rule must not appear (REQ MOD-012). |
| `05-map-local-serves-a-module-asset` | `map_local` success, byte count and guessed content type (REQ PXY-033). |
| `06-map-local-missing-file-fails-loudly` | `error` outcome + `MAP_LOCAL_MISSING`; a missing file must not look like a non-match (REQ PXY-034). |
| `07-strip-csp-runs-in-the-header-phase` | `strip_csp` is recorded in `response_headers`, not `response_body`, and raises `CSP_MODIFIED` (REQ PXY-042). |
| `08-a-headers-rule-that-removes-csp-says-so` | `CSP_MODIFIED` is attached to the act, not to the transform (REQ PXY-036). |
| `09-inject-script-strips-sri-implicitly` | `SCRIPT_INJECTED`, plus the implicit SRI strip entry and `SRI_STRIPPED` that must follow any body rewrite (REQ PXY-040/041). |
| `10-a-transform-that-matches-nothing-is-no-change` | `no_change` — "matched and did nothing" stays distinct from "did not match". |
| `11-a-streamed-response-skips-body-rules` | `RESPONSE_STREAMED` and `skipped_streamed` together (REQ PXY-021/022). |
| `12-an-exhausted-budget-skips-the-rest` | `skipped_budget` + `TRANSFORM_BUDGET_EXCEEDED` (REQ PXY-026). |
| `13-a-failing-transform-is-attributed-not-fatal` | `error` outcome + `MODULE_ERROR`, attributed to its rule, flow unharmed (REQ MOD-024). |
| `14-request-headers-still-run-on-a-blocked-request` | Header rules run after a short circuit, and in that order (REQ PXY-020). |
| `15-a-headers-rule-with-nothing-to-do` | An inert headers rule records `no_change`. |
| `16-dry-run-diff-for-a-candidate-module` | Dry-run unified body diff, header ops and aggregate tallies over an uninstalled module (REQ CAP-030…033). |

### Outcomes not covered, and why

`Outcome.SKIPPED_SHORT_CIRCUIT` is declared in `engine/provenance.py` but **no
code path in `src/` emits it** (verified by grep across `src/`, `contracts/` and
`web/`). Header rules deliberately still run on a short-circuited request
(case 14), and a short-circuited flow never reaches the response phases, so
nothing is currently in a position to record it. There is no corpus case for it
because there is nothing to record. If a path ever emits it, add a case here.

`Outcome.SKIPPED_DISABLED`, `NoteCode.MODULE_QUARANTINED`,
`NoteCode.DEV_TOGGLE_ACTIVE`, `NoteCode.BODY_TRUNCATED`,
`NoteCode.PASSTHROUGH_EXCLUDED`, `NoteCode.ATTRIBUTION_MISSING` and
`NoteCode.MODULE_DEPRECATION` are produced outside the evaluator — by the module
registry, the addon and the capture layer — and are covered by their own unit
tests rather than by replaying a flow through the engine.

---

## Adding a case

1. Write `cases/NN-what-it-pins.json` with everything except the golden. Leave
   `expected_provenance` (or `expected_dry_run`) as `null`.
2. `PPORLOCK_REGEN_CORPUS=1 uv run pytest tests/unit/test_corpus.py`
3. **Read the generated golden.** It is now the specification of this behaviour;
   if it does not say what you expected, you have found something.
4. Commit the case and the golden together, citing the requirement IDs.

Keep cases small and single-purpose. A case that pins twenty entries at once
fails for twenty reasons and diagnoses none of them.

`assets/` holds files the `map_local` cases serve. Their byte counts appear in
the goldens, so editing one is a behaviour change that must be regenerated.
