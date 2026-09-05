# September 5, 2026 Code Review Findings

**Review status:** Complete for the documented architecture and the highest-priority daemon paths, with emphasis on the MITM proxy and rules engine.

**Reviewed revision:** `c38a9a5` on `master`

**Change scope:** This file records review findings only. No implementation, configuration, generated contract, or test file was changed as part of this review.

---

## 0. Disposition — added on remediation (0.13.0)

Every finding was assessed against the code at `c38a9a5` and every one was
**valid**; none was a false positive and none was already mitigated elsewhere.
All fifteen are remediated in `0.13.0` on branch `review-sep-5-findings`.

Each has at least one test that fails against `c38a9a5` and passes after, in
`daemon/tests/unit/test_review_sep_5.py` — one class per finding, named for it.
That whole file was run against the pre-fix tree to confirm it fails; the
failures are listed per finding below.

| ID | Assessment | Where it is demonstrated |
|---|---|---|
| F-01 | Valid. Hook-only modules were invisible to body demand, offload, and the budget. | `TestF01PythonHooksParticipateInBufferingAndOffload` |
| F-02 | Valid. Reproduced in both declaration orders and across module priorities. | `TestF02HeaderOrderSurvivesApplication` |
| F-03 | Valid. A reload between `request` and `responseheaders` split one flow across two generations. | `TestF03AFlowKeepsTheGenerationItStartedOn` |
| F-04 | Valid. Reload emptied and refilled the live registry in place. | `TestF04ReloadPublishesAReplacement` |
| F-05 | Valid. Confirmed the exact accounting divergence the probe reported. | `TestF05WebSocketGrowthIsAccounted` |
| F-06 | Valid. Only the declared length was checked. | `TestF06ObservedSizeBoundsTheBody` |
| F-07 | Valid. `compile_rule` never called the registry validator. | `TestF07TransformsValidateAtLoadTime` |
| F-08 | Valid. `re.compile` per execution. | `TestF08RegexIsCompiledOnce` |
| F-09 | Valid, both halves — the matcher phase and the duplicated aggregate. | `TestF09TwoSidedHeaderRules` |
| F-10 | Valid. One context per module, shared by concurrent invocations. | `TestF10NotesBelongToOneInvocation` |
| F-11 | Valid. Neither setting reached runtime. | `TestF11ExecutorConfigurationReachesRuntime` |
| F-12 | Valid, both observations. | `TestF12ProvenanceReportsWhatActuallyHappened` |
| F-13 | Valid. SQLite on the caller's thread. | `TestF13StoreWritesLeaveTheCallingThread` |
| F-14 | Valid. | `TestF14BodyDemandMeansTheBodyIsNeeded` |
| F-15 | Valid. Most of the drift is closed by the fixes above; the rest is corrected in SPEC-0 §3.3/§8.3 and SPEC-1 §2/§3.4/§4.5/§5. | `make docs`, `make version-check` |

**One item is deliberately only partly closed.** F-04 asked for an old snapshot
to be retained until its in-flight users finish, and only then unloaded. The
replacement is now built into locals and published in one assignment, which
closes the empty-and-partial windows; `on_unload` still runs *before* the
replacement loads, because that is the guarantee `on_unload` exists to make — a
module holding a file or a connection releases it before its successor takes
over. A hook already executing can therefore still overlap its own module's
teardown. The window is now bounded by hook duration rather than by the whole
reload. Recorded in `docs/open-issues.md` as **OI-39**.

Two corrections to the report itself, neither material to a finding:

- F-12's Observation A said the evaluator's `changed` flag "does not inspect the
  input header state". It now does — and it threads that state across the whole
  phase, so a rule removing a header an *earlier rule* added is correctly a
  change while a second rule removing the same header is correctly not. Reading
  the message once per rule would have got the second case wrong.
- F-08 suggested storing compiled regexes on the compiled rule. A transform
  block is a plain dict by design and has no identity to hang one on, so the
  compiled form is a bounded process-wide cache keyed by pattern and flags, and
  the *validation* half moved to load time where the finding wanted it.

## 1. Executive summary

The project is unusually deliberate about architecture, contracts, provenance, and the performance constraints of running a control server beside a MITM proxy. The documentation gives a clear basis for reviewing behavior rather than merely reviewing style. The existing automated suite is also broad: 2,028 daemon unit tests and 55 MITM integration tests passed during this review, and Ruff plus Python bytecode compilation completed successfully.

The main risks are therefore not basic correctness failures caught by ordinary unit tests. They occur at boundaries between otherwise well-tested components:

- declarative rules and Python hooks;
- rule evaluation and deferred application to mitmproxy objects;
- request and response phases of one flow;
- hot reload and concurrent traffic;
- mutable capture records and ring-buffer accounting;
- documented configuration and the executor actually used at runtime.

Six findings are classified P1 because they can violate core proxy semantics, allow memory to escape configured bounds, or run user-supplied module work on the proxy event loop. Nine are classified P2 because they produce incorrect validation or provenance, introduce concurrency hazards, ignore performance controls, or create avoidable work.

No new unauthenticated remote-control path, path traversal issue, unsafe YAML load, or obvious secret-comparison weakness was found in the security-sensitive code sampled. That is a limited statement, not a full security audit. The highest security-adjacent concern in this report is availability: several findings can stall the shared event loop or defeat memory limits.

## 2. Review method and confidence

The review followed the requested order:

1. Read the root [`CLAUDE.md`](../CLAUDE.md), including its architectural constraints, test expectations, and open-issue guidance.
2. Read the requirements and contracts, then the daemon specification, authoring material, API documentation, sprint history, and open issues.
3. Traced the MITM lifecycle from request normalization through rule evaluation, buffering, mutation application, capture, and module reload.
4. Examined the rules compiler, matcher, transform registry, Python module runtime, provenance builder, and capture ring.
5. Ran the existing unit and MITM integration suites, static linting, and compilation checks.
6. Ran small, disposable probes against suspicious boundary behavior. These probes were not added to the repository.

The report distinguishes existing project limitations from new findings. In particular, OI-12 and OI-21 already document the broader mitmproxy latency and single-core throughput ceiling. This report does not relabel those known limitations as new defects. The findings below concern behavior that can add stalls, memory growth, or semantic errors within the current architecture.

### Priority convention

- **P1 — high:** A core documented contract can be violated in ordinary supported use, or the problem can materially affect proxy correctness, availability, or bounded memory. These should be resolved before treating the affected requirement as delivered.
- **P2 — important:** The defect is real and can mislead users, create intermittent behavior, or waste significant work, but it has a narrower trigger or lower immediate operational impact.

## 3. Findings overview

| ID | Priority | Area | Summary |
|---|---:|---|---|
| F-01 | P1 | Python hooks / buffering | Python response hooks do not participate in body-demand or offload decisions, and request hooks are only charged after returning. |
| F-02 | P1 | Header rules | Deferred header mutations are regrouped by operation type, destroying all-match declaration order. |
| F-03 | P1 | Hot reload / flows | A flow does not retain its evaluator snapshot, so one flow can use different rule generations across phases. |
| F-04 | P1 | Module reload | The live module registry is unloaded and rebuilt in place while traffic can still consult it. |
| F-05 | P1 | Capture memory | Appending WebSocket messages bypasses ring byte accounting and eviction. |
| F-06 | P1 | Body buffering | Unknown-length response bodies have no observed-size enforcement path. |
| F-07 | P2 | Rule validation | Body transform kinds and parameters are not validated through `TransformRegistry` at rule-load time. |
| F-08 | P2 | Regex performance | `regex_sub` compiles its pattern on every flow rather than at rule load. |
| F-09 | P2 | Rule phases | A two-sided header rule can apply request mutations while response-only criteria are unavailable, and is duplicated in aggregate views. |
| F-10 | P2 | Hook concurrency | One mutable `ModuleContext` note/log buffer is shared by concurrent hook invocations. |
| F-11 | P2 | Executor configuration | `executor_threshold_bytes` and `executor_workers` are defined but not connected to runtime construction. |
| F-12 | P2 | Provenance | No-op header actions and failed short-circuit attempts can be reported as applied. |
| F-13 | P2 | Module storage | `ctx.store_set` and `ctx.store_delete` perform synchronous SQLite writes on the calling thread. |
| F-14 | P2 | Buffering efficiency | `strip_csp`, which changes only headers, makes matching responses eligible for body buffering. |
| F-15 | P2 | Documentation | Parts of the daemon design document describe files, validation, size enforcement, and reload behavior the implementation does not currently provide. |

## 4. Detailed findings

### F-01 — Python hooks are absent from body-demand, offload, and effective budget decisions

- **Priority:** P1
- **Primary requirements:** PXY-021, PXY-024, PXY-026, MOD-020, MOD-023, PRF-004, PRF-006
- **Primary code:** [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py), [`addon/interceptor.py`](../daemon/src/pporlock/addon/interceptor.py), [`engine/ruleset.py`](../daemon/src/pporlock/engine/ruleset.py)

#### Observation

`Evaluator.evaluate_request()` runs `on_request` hooks and then assigns `decision.wants_body` from `RuleSet.wants_body(request)`. `RuleSet.wants_body()` checks only matching declarative body rules. It has no representation of an enabled Python `on_response` hook.

At response time, `Interceptor._should_offload()` likewise looks only at transforms in matching declarative body rules. It never considers whether a Python response hook will receive a body or how expensive that hook may be. The evaluator may subsequently invoke Python hooks as part of response-body evaluation, but the earlier buffering and thread-selection decisions have already been made without them.

The request-side time budget is consumed only after `_run_python_hooks()` returns. This records elapsed time but cannot interrupt, offload, or protect the event loop from a slow request hook. A response hook can inherit worker execution when a declarative transform happens to trigger offload, but a hook-only module does not receive that protection.

#### Reproduction and evidence

A module layout documented as supported can contain `module.py` with `on_response` and an empty declarative `rules` list. For such a module:

1. request evaluation reports that no body is wanted;
2. `responseheaders` enables streaming;
3. normalization supplies `response.body=None` to the hook;
4. `_should_offload()` finds no declarative transforms and returns false;
5. the hook runs inline if it is invoked.

This is particularly misleading because the Python module tutorial presents hooks as a supported independent tier, and the daemon specification says that Python hooks on bodies over the configured threshold run in the executor.

#### Impact

- A hook-only module cannot reliably inspect or transform a response body.
- CPU-heavy or blocking hook code can stall every connection sharing the proxy event loop.
- The per-flow budget becomes accounting after the fact for request hooks rather than an enforcement point.
- Adding an unrelated declarative body transform can accidentally change whether Python hook work is buffered or offloaded, creating non-local performance behavior.
- The implementation does not preserve the documented equivalence between declarative and Python tiers.

This is also an availability concern. Modules are trusted code, but PRF-006 explicitly expects the time budget and quarantine to prevent one module or flow from permanently wedging the proxy.

#### Why the current tests can pass

Tests can verify Python hook mutation, declarative buffering, and declarative offload independently without exercising a Python-only response module through the full `request` → `responseheaders` → `response` lifecycle. Offload tests that include an expensive declarative transform also conceal the missing hook-specific decision.

#### Remediation direction

Represent enabled hook capabilities in the same immutable evaluation snapshot used for rules. The response body-demand decision should include Python hooks that require a body, and the offload decision should explicitly include Python body hooks using the documented threshold. Budget enforcement needs a design that protects the event loop rather than merely measuring time after synchronous code returns. Any fix should add end-to-end tests for a module containing only `on_response`, both below and above the threshold.

### F-02 — Header application destroys declaration order across matching rules

- **Priority:** P1
- **Primary requirements:** PXY-020, PXY-036, MOD-012
- **Primary code:** [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py), [`addon/apply.py`](../daemon/src/pporlock/addon/apply.py), [`engine/models.py`](../daemon/src/pporlock/engine/models.py)

#### Observation

The evaluator visits matching header rules in priority and declaration order, but it folds their effects into three aggregate collections: removals, sets, and additions. `_apply_header_ops()` later applies every removal, then every set, then every addition.

This means rule visitation order is preserved only within each operation category. Ordering between categories is lost. The comment in `_apply_header_ops()` explicitly makes operation type, rather than rule order, the final authority.

#### Reproduction and evidence

With two matching rules in this order:

1. add `X-Review: present`;
2. remove `X-Review`;

the final request still contains `X-Review`, because all removals are applied before all additions. Reversing the declarations can produce the same result even though all-match, applied-in-order semantics require the results to differ.

Equivalent conflicts exist for remove→set, set→remove, add→set, and repeated operations from different module priorities.

#### Impact

- A later cleanup or security-hardening rule can fail to remove a header added by an earlier rule.
- Module priority and declaration order become unreliable when rules use different header operation types.
- Provenance can show rules in the correct order while the wire result reflects a different order, making debugging especially difficult.
- The behavior contradicts the central MOD-012 semantics that the documentation repeatedly identifies as error-prone and important.

#### Why the current tests can pass

Tests that exercise add, set, and remove independently all pass. Tests with multiple rules of the same operation type also pass. The defect requires conflicting operations across separately declared matching rules and verification of the final mitmproxy header collection.

#### Remediation direction

Preserve an ordered mutation operation stream, or apply each rule's concrete mutation to a normalized header state as that rule is evaluated. Do not reconstruct semantic order from three lossy aggregate containers. Tests should cover every pair of conflicting operation categories in both declaration orders and across module priorities.

### F-03 — In-flight flows can switch evaluator generations during hot reload

- **Priority:** P1
- **Primary requirement:** MOD-004
- **Primary code:** [`addon/interceptor.py`](../daemon/src/pporlock/addon/interceptor.py), [`cli/runner.py`](../daemon/src/pporlock/cli/runner.py)

#### Observation

The request callback evaluates with `self.evaluator` and stashes the request, provenance builder, body-demand flag, and budget on the flow. It does not stash the evaluator or another immutable runtime snapshot.

Later, `responseheaders()` again dereferences `self.evaluator` for response header evaluation and buffering. `response()` does so again for offload classification and response body evaluation. If a reload replaces the interceptor's evaluator between those callbacks, one flow is evaluated by multiple generations.

#### Impact

- Request mutations can come from generation A while response mutations and Python hooks come from generation B.
- The body-demand flag from A can be combined with response-body rules from B, causing an incorrect stream/buffer decision.
- Provenance can name a module set that did not consistently govern the flow.
- A response can run a newly loaded hook without its corresponding request phase, or miss the response half of a module that handled its request.

This is exactly the race that the documented immutable-snapshot design is intended to prevent. An atomic assignment of the global evaluator is insufficient unless each flow retains the assigned object for all later phases.

#### Why the current tests can pass

Reload tests and flow tests can each pass in isolation. The failure requires a reload scheduled after request handling but before response headers or body processing. It is timing-dependent unless the test deliberately pauses a flow at that boundary.

#### Remediation direction

Stash a complete, immutable evaluator/runtime snapshot on each flow at its first evaluated phase and use only that snapshot thereafter. Add a deterministic test that begins a flow under generation A, swaps to B, and proves every remaining phase still uses A while a newly started flow uses B.

### F-04 — Module reload mutates the live registry instead of building a replacement snapshot

- **Priority:** P1
- **Primary requirements:** MOD-004, MOD-024, DD-3
- **Primary code:** [`engine/modules/registry.py`](../daemon/src/pporlock/engine/modules/registry.py), [`control/app.py`](../daemon/src/pporlock/control/app.py)

#### Observation

`ModuleRegistry.reload()` calls `on_unload` for every current module, unloads its Python module, replaces `_modules` and `_contexts` with empty dictionaries, then incrementally loads and initializes the new set on the same registry object.

The control path correctly offloads reload work from the asyncio event loop, but traffic can continue on the event loop and its body work can continue in executor threads. Those paths still hold and consult the same mutable registry instance. During reload they may observe the old modules after `on_unload`, no modules, a partially loaded set, or new contexts paired with an evaluator/ruleset from another moment.

Offloading the mutation avoids blocking the loop; it does not make the shared mutation safe.

#### Impact

- Hooks may disappear transiently during ordinary traffic.
- A hook can execute concurrently with its module's `on_unload` lifecycle method.
- Partial reload state can leak into provenance, quarantine accounting, and module lookup.
- Module code or context state may be accessed after lifecycle teardown.
- Results become timing-dependent and difficult to reproduce.

F-03 and F-04 are related but distinct. Stashing an evaluator per flow does not solve this issue if that evaluator still references a registry that is mutated in place.

#### Why the current tests can pass

Serial reload tests see the expected final registry. Serial hook tests see a stable registry. The defect requires traffic or worker execution to overlap the internal reload window.

#### Remediation direction

Build a complete replacement registry, contexts, loaded modules, and combined ruleset away from live traffic. Publish one immutable runtime snapshot atomically only after validation and `on_load` complete. Retain the old snapshot until its in-flight users finish; only then run its unload lifecycle. Stress tests should overlap reload with request hooks, response hooks, and worker-thread body transforms.

### F-05 — WebSocket message growth bypasses ring-buffer byte accounting

- **Priority:** P1
- **Primary requirements:** PXY-050, CAP-001, CAP-003, PRF-005
- **Primary code:** [`capture/sink.py`](../daemon/src/pporlock/capture/sink.py), [`capture/ring.py`](../daemon/src/pporlock/capture/ring.py), [`capture/records.py`](../daemon/src/pporlock/capture/records.py)

#### Observation

`CaptureSink.record_websocket_message()` retrieves a mutable `FlowRecord` already stored in the ring and appends directly to `record.ws_messages`. The ring updates its `_bytes` counter and performs eviction only in `add()` and `update()`. No ring operation follows the append.

Each individual payload is capped, but the number of frames retained on one long-lived connection is not bounded. Per-message truncation is therefore not equivalent to bounding the accumulated record.

#### Reproduction and evidence

A disposable probe added a WebSocket record, appended messages, and compared the ring's reported byte count with the record's current `size_bytes`. After growth, the record measured 2,312 bytes while `ring.stats.bytes` remained 512 bytes. The exact values depend on test payloads; the material result is that the accounting remained at the insertion size.

#### Impact

- A long-running, high-volume WebSocket can grow memory without triggering the configured `max_bytes` limit.
- Ring statistics under-report actual retained memory.
- Old records are not evicted when this hidden growth crosses the cap.
- Multi-day daemon uptime can violate PRF-005 even when ordinary HTTP bodies and flow counts remain within bounds.
- A page can trigger an availability problem without needing access to the control API.

#### Why the current tests can pass

Tests can verify frame capture, individual payload truncation, flow-count eviction, and byte eviction at initial insertion without checking byte-accounting changes after mutation of an existing record.

#### Remediation direction

Make record growth pass through an accounting-aware ring operation and decide an explicit retention policy for frames per connection. Re-accounting alone can cause the currently active socket record to evict itself once oversized, so the intended behavior should be specified: cap total frame bytes, retain the newest N frames, or store a bounded summary. A soak test should generate sustained WebSocket traffic and compare measured retained size with ring statistics and configured caps.

### F-06 — Chunked or otherwise unknown-length bodies are not bounded by observed size

- **Priority:** P1
- **Primary requirements:** PXY-021, PXY-022, PRF-004, PRF-005
- **Primary code:** [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py), [`addon/interceptor.py`](../daemon/src/pporlock/addon/interceptor.py)

#### Observation

`decide_buffering()` rejects a body when a declared `Content-Length` exceeds `max_buffer_bytes`. If the length is absent or unparsable, it remains `None`; a wanted and allowlisted content type is then buffered with no later observed-size guard in the inspected path.

PXY-021 explicitly requires streaming when the declared **or observed** size exceeds the threshold. The current decision point implements the declared half only.

#### Impact

- A chunked HTML, JSON, CSS, or JavaScript response of arbitrary size can be accumulated in memory for transformation.
- Concurrent large responses can create sharp memory spikes and event-loop pressure.
- A remote origin can trigger the problem through normal page traffic when a matching body rule is enabled.
- Configuration communicates a maximum that is not a maximum for a common HTTP transfer mode.

#### Design constraint

At `responseheaders`, the final observed size is inherently unknown. Satisfying the requirement therefore needs a streaming accumulation strategy that aborts buffering after the cap, a mitmproxy-supported size-limit mechanism, or a clarified behavior for unknown-length bodies. Treating `None` as safe cannot enforce the documented bound.

#### Why the current tests can pass

Boundary tests with explicit `Content-Length` validate correctly, as do type allowlist tests. The gap appears only when the length is omitted or invalid and the delivered bytes exceed the threshold.

#### Remediation direction

Add an observed-byte enforcement path and provenance for the transition from intended buffering to size-based streaming/skipping. Integration coverage should include chunked responses just below and just above the cap, multiple concurrent oversized responses, and misleading or malformed length metadata.

### F-07 — Transform registry validation is disconnected from rule compilation

- **Priority:** P2
- **Primary requirements:** MOD-014, MOD-015
- **Primary code:** [`engine/ruleset.py`](../daemon/src/pporlock/engine/ruleset.py), [`engine/transforms/__init__.py`](../daemon/src/pporlock/engine/transforms/__init__.py), [`rule-schema.md`](rule-schema.md)

#### Observation

`compile_rule()` performs shallow action checks through `_validate_params()`. For a body rule it verifies only that `transform` or `transforms` is present. It does not call `TransformRegistry.validate()` for each transform. Search of the runtime source found the registry validation definitions but no rule-load call site connecting them to compilation.

The static JSON Schema can validate part of this structure in some entry points, but the loader/compiler path is supposed to share the same authoritative validation. Custom registry transforms also require runtime registry knowledge that a static top-level check cannot supply by itself.

#### Reproduction and evidence

A disposable probe compiled a body rule containing:

```yaml
transform:
  kind: definitely_unknown
  bogus: 1
```

Compilation accepted the rule. The failure is deferred until traffic matches and execution attempts to resolve the transform.

#### Impact

- A typo can load successfully and fail only on a live request.
- The editor, validation API, and loader can disagree about validity.
- A module author may believe a rule is active when its transform can never execute.
- Runtime error and quarantine behavior is forced to handle what should be a deterministic load error.

#### Remediation direction

Compile body rules with the active transform registry and validate every transform kind and parameter object at snapshot construction. The loader, dry-run/validation endpoint, and editor contract tests should share fixtures containing unknown kinds, unknown parameters, wrong types, and invalid regex flags.

### F-08 — `regex_sub` recompiles its regular expression for every matching flow

- **Priority:** P2
- **Primary requirements:** PXY-025, PRF-004
- **Primary code:** [`engine/transforms/text.py`](../daemon/src/pporlock/engine/transforms/text.py), [`engine/transforms/__init__.py`](../daemon/src/pporlock/engine/transforms/__init__.py)

#### Observation

`regex_sub()` calls `re.compile()` each time the transform executes. Match criteria are compiled at rule load, but transform regexes are not. The specification's transform section states that transform parameters validate at load time, and PXY-025 says regular expressions in rules are compiled once at rule-load time.

#### Impact

- Every matching body pays repeated parsing and compilation overhead.
- Invalid transform patterns become per-flow runtime failures rather than module load errors.
- High-subresource pages amplify small avoidable costs on a latency-sensitive path.
- Python's internal regex cache may soften repeated identical patterns, but it is an implementation cache with finite capacity, not the project's promised compiled representation.

#### Remediation direction

Compile and store regex transform state as part of the immutable compiled rule/transform representation. Benchmark both repeated identical patterns and rule sets large enough to exceed the interpreter cache. Validation and compilation should be one operation so an invalid pattern prevents snapshot publication.

### F-09 — Two-sided header rules have ambiguous matching phases and duplicate aggregate representation

- **Priority:** P2
- **Primary requirements:** PXY-020, MOD-011, MOD-012
- **Primary code:** [`engine/ruleset.py`](../daemon/src/pporlock/engine/ruleset.py), [`engine/matcher.py`](../daemon/src/pporlock/engine/matcher.py)

#### Observation

A header rule may contain both `request` and `response` mutation blocks. `phase_for()` classifies such a rule as response-phase, which permits response-only criteria such as `status` and `content_type` during matcher compilation. The `RuleSet` constructor nevertheless places the same `CompiledRule` in both `request_headers` and `response_headers`.

During request evaluation, `matches_request()` cannot evaluate response status. The reviewed behavior allows the request half to match based on the available request criteria, so a response condition does not constrain the request mutation.

The same dual insertion also makes `RuleSet.__len__()` and `all_rules` count/return one declared rule twice, even though `self.rules` correctly retains a single compiled rule.

#### Reproduction and evidence

A disposable probe created one two-sided header rule with `status: 500`. The request mutation was applied before any response existed. The rule set contained one declared compiled rule, but `len(ruleset)` returned 2 and `all_rules` contained two occurrences.

#### Impact

- A request header can be sent even when the eventual response does not meet the rule's stated response condition.
- Users cannot infer request-side behavior from the rule as written.
- API counts or serialized rule views based on `all_rules` can duplicate entries.
- Any later combine/reload logic that uses an aggregate partition view risks multiplying rules.

#### Remediation direction

Define this case explicitly. The cleanest options are to reject response-only criteria on any rule with request mutations, or split a two-sided declaration into separate compiled phase operations with explicit matcher semantics and one stable declaration identity. Use `self.rules`, not reconstructed phase partitions, for unique aggregate enumeration.

### F-10 — Module notes and logs are shared mutable state across hook invocations

- **Priority:** P2
- **Primary requirements:** MOD-024, CAP-010, CAP-012, CAP-013
- **Primary code:** [`engine/modules/context.py`](../daemon/src/pporlock/engine/modules/context.py), [`engine/modules/registry.py`](../daemon/src/pporlock/engine/modules/registry.py), [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py)

#### Observation

The registry creates one `ModuleContext` per loaded module. That context owns mutable `_notes` and `_log` lists. Hook evaluation reads `context.notes` and then calls `context.drain()` after each hook.

Response-body evaluations can run concurrently in the default executor. Two invocations of the same module therefore share the same lists without per-flow ownership or synchronization. Basic individual list operations are protected from interpreter memory corruption, but the multi-step protocol—append, snapshot, drain—is not atomic and does not preserve attribution.

#### Example interleaving

1. Flow A's hook appends note A.
2. Flow B's hook appends note B.
3. Flow A snapshots the context and receives both notes.
4. Flow A drains the lists.
5. Flow B snapshots an empty context.

The result is a note attributed to the wrong flow and a missing note on the correct flow. Logs have the same ownership problem even if their current consumer path differs.

#### Impact

- Provenance can become nondeterministic under concurrency.
- Diagnostics can be lost or attached to the wrong captured request.
- A security-relevant module note can appear on an unrelated flow.
- Failures will be rare in low-concurrency tests and most visible under real page loads.

#### Remediation direction

Make notes and logs invocation-scoped. A per-hook child context can share immutable configuration, assets, registry access, and persistent store while owning its own output buffers. A concurrency test should synchronize two hook calls at controlled barriers and verify exact per-flow attribution.

### F-11 — Executor threshold and worker-count configuration are not wired into runtime behavior

- **Priority:** P2
- **Primary requirements:** PXY-024, PRF-004
- **Primary code:** [`config.py`](../daemon/src/pporlock/config.py), [`cli/runner.py`](../daemon/src/pporlock/cli/runner.py), [`addon/interceptor.py`](../daemon/src/pporlock/addon/interceptor.py), [`control/app.py`](../daemon/src/pporlock/control/app.py)

#### Observation

Configuration defines `budget.executor_threshold_bytes` and `budget.executor_workers`. The daemon specification publishes both. Runtime evaluator construction passes buffering configuration but does not pass `executor_threshold_bytes`, so the evaluator keeps its constructor default regardless of configuration.

No `ThreadPoolExecutor` construction or `loop.set_default_executor()` was found in daemon source. Calls use `run_in_executor(None, ...)`, which delegates to asyncio's process-wide default executor and ignores `executor_workers`.

#### Impact

- Changing either documented setting can have no effect.
- Operators cannot tune the point at which work leaves the event loop.
- Operators cannot bound or reserve worker capacity as documented.
- Control-plane filesystem work and data-plane body work share the implicit default pool, so expensive transforms can delay reload/config work and vice versa.
- Tests that instantiate `Evaluator` directly with an explicit threshold can pass while the real daemon ignores the setting.

#### Remediation direction

Construct explicit bounded executor resources from configuration and define whether proxy transforms and control-plane I/O share or separate pools. Pass the configured threshold into the published runtime snapshot. Add startup tests that vary both settings and observe actual offload selection and maximum concurrency.

### F-12 — Provenance can report actions as applied when no wire change occurred

- **Priority:** P2
- **Primary requirements:** CAP-010, CAP-012, CAP-013; related MOD-012 behavior
- **Primary code:** [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py), [`addon/apply.py`](../daemon/src/pporlock/addon/apply.py), [`capture/sink.py`](../daemon/src/pporlock/capture/sink.py)

#### Observation A: no-op headers

`_apply_header_rule()` sets its local `changed` flag whenever an operation is declared. It does not inspect the input header state. Removing an absent header or setting a header to its existing value is therefore recorded as `applied` by the evaluator, even though `_apply_header_ops()` correctly returns false when the mitmproxy object did not change.

This is more than presentation drift: the capture layer derives modification state from applied provenance, so a no-op can make the UI's “was modified” filter include an unmodified flow.

#### Observation B: failed `map_local`

`evaluate_request()` calls `builder.short_circuit(rule.rule_id)` unconditionally after `_apply_short_circuit()`. If `map_local` matches but its file is missing, evaluation records the documented failure note and allows the upstream request to continue, yet the provenance still names that rule as the flow's short circuit.

A disposable probe confirmed that the actual decision contained no short-circuit response while provenance still held the rule ID.

#### Impact

- Users can be told a flow was modified or short-circuited when it was not.
- Filters and counters derived from provenance become inaccurate.
- Troubleshooting decisions based on the flow detail view can point at the wrong rule.
- Provenance stops being a reliable structural account of actual behavior, weakening one of the project's strongest design features.

#### Remediation direction

Separate “matched,” “attempted,” and “changed/applied” outcomes. Base final applied state on the concrete before/after mutation or on the adapter's application result, while retaining explicit failure outcomes such as `map_local_missing`. Only assign `short_circuit_action` when a synthetic response, redirect, block response, or kill decision was actually produced.

### F-13 — Module store writes perform synchronous SQLite I/O on the hook's calling thread

- **Priority:** P2
- **Primary requirements:** MOD-022, DD-3; analogous to API-002 loop discipline
- **Primary code:** [`engine/modules/context.py`](../daemon/src/pporlock/engine/modules/context.py)

#### Observation

`ModuleStore.set()` updates its cache and immediately opens SQLite and executes an upsert. `delete()` likewise performs an immediate database operation. A request hook normally runs inline on the proxy event loop, so a call to `ctx.store_set()` or `ctx.store_delete()` performs filesystem-backed SQLite work on that loop.

The authoring documentation describes the store as SQLite-backed and suitable for persistent module state. Nothing in the hook API warns that a normal write blocks all browsing, and the broader daemon design states that filesystem and SQLite work must leave the shared loop.

#### Impact

- First connection setup, filesystem latency, locking, journal work, or storage pressure can stall unrelated proxy flows.
- A module that writes on every request turns a convenience API into a systemic performance bottleneck.
- Because errors are swallowed as best-effort persistence, users may see both unexplained stalls and silent durability loss.
- Concurrent response hooks can also contend on independent SQLite connections.

#### Remediation direction

Keep the synchronous cache update if immediate read-after-write semantics are required, but move persistence to a bounded, non-blocking queue with explicit coalescing and shutdown flush behavior. Define failure visibility rather than silently discarding all SQLite errors. Add a slow-storage simulation proving request handling remains responsive.

### F-14 — `strip_csp` causes body buffering even though it only mutates headers

- **Priority:** P2
- **Primary requirements:** PXY-020, PXY-021, PRF-004
- **Primary code:** [`engine/ruleset.py`](../daemon/src/pporlock/engine/ruleset.py), [`engine/evaluator.py`](../daemon/src/pporlock/engine/evaluator.py), [`engine/transforms/headers.py`](../daemon/src/pporlock/engine/transforms/headers.py)

#### Observation

The schema models `strip_csp` among body transforms, but the implementation applies it during response-header evaluation and skips it during body mutation. `RuleSet.wants_body()` nevertheless treats any matching body rule as body demand without distinguishing whether its transforms actually consume a body.

Consequently, a response matching a rule whose only transform is `strip_csp` is eligible for buffering, subject to type and size checks, even though no body bytes are needed.

#### Impact

- Common HTML responses can be buffered unnecessarily.
- Time-to-first-byte and memory use increase for a header-only action.
- The body buffering provenance suggests transformation demand that does not exist.
- This compounds F-06 for unknown-length responses.

#### Remediation direction

Classify transforms by phase and body requirement in the compiled representation. Body demand should be true only when at least one applicable operation consumes or rewrites the body. Longer term, representing `strip_csp` as a response-header action would make its phase explicit and remove special cases from body evaluation.

### F-15 — Daemon documentation overstates several implemented guarantees

- **Priority:** P2
- **Primary requirements:** Documentation quality; MOD-004, MOD-014, PXY-021, PXY-024, PXY-025 are affected
- **Primary documentation:** [`spec-1-daemon.md`](spec-1-daemon.md), [`rule-schema.md`](rule-schema.md), [`sprint-log.md`](sprint-log.md)

#### Observation

The documentation is precise enough to act as a contract, but several statements describe the intended architecture rather than the current implementation:

- `spec-1-daemon.md` says reload builds a new immutable registry snapshot and atomically swaps it, while the current registry is rebuilt in place.
- It says in-flight flows continue on the snapshot they started with, while flows do not retain their evaluator.
- It says any Python hook on a body over the threshold runs on a bounded pool, while hooks are absent from the offload decision and no configured bounded pool is installed.
- It says transform parameters validate at load time, while rule compilation does not call the transform registry validator.
- It describes regexes as precompiled, while `regex_sub` compiles per execution.
- It says bodies are bounded by declared or observed size, but the current path checks only declared length.
- It describes an `addon/streaming.py` component that is not present; streaming logic currently lives in the evaluator/interceptor path.
- It sets a maintainability expectation that individual mitmproxy hook methods remain very small, while the interceptor has accumulated substantial cross-phase policy and state handling.

The sprint log also marks several of these requirements delivered. That makes the drift operationally important: future reviewers and contributors may assume coverage exists and build on it.

#### Impact

- Architectural reviews based on documentation alone reach incorrect conclusions.
- Contributors may preserve nonexistent invariants or add tests at the wrong boundary.
- Requirement status and actual behavior diverge.
- The central interceptor continues to collect responsibilities, increasing the likelihood of more cross-phase defects.

#### Remediation direction

After implementation decisions are made, update the generated-source documents rather than their generated outputs, and reconcile requirement status in the sprint/open-issue records. Add architecture conformance tests for the most important claims: per-flow snapshot identity, transform load validation, configured executor use, and observed body bounds. Until then, this review file should be treated as a qualification on the affected “delivered” statements.

## 5. Cross-cutting analysis

### 5.1 The dominant design problem is loss of semantic information between phases

Several defects share one cause: a rich decision is collapsed too early and later code tries to reconstruct its meaning.

- Ordered header operations become three unordered-by-rule collections (F-02).
- A runtime generation becomes a handful of stashed values rather than one snapshot identity (F-03).
- “This operation uses headers but is declared as a body transform” becomes undifferentiated body demand (F-14).
- A rule match becomes “applied” before the adapter knows whether state changed (F-12).

The most durable correction is to carry explicit immutable operations and runtime identity across the adapter boundary. Adding more booleans to the current flow metadata would likely make the state machine harder to reason about.

### 5.2 Offloading and bounding must be end-to-end properties

The project has the right primitives—content-type guards, size settings, cost classes, time budgets, executor calls, and a dual-bound ring—but several are enforced only at one point:

- declared size but not observed size (F-06);
- declarative transforms but not Python hooks (F-01);
- record insertion but not later WebSocket growth (F-05);
- configured values in data models but defaults at runtime (F-11);
- control-handler I/O classification but synchronous SQLite exposed to request hooks (F-13).

Performance tests should therefore verify the whole lifecycle and the externally visible bound, not merely the helper that makes an initial decision.

### 5.3 Immutable snapshots need ownership, not only immutable tuples

`RuleSet` is structurally immutable enough for ordinary reads, but the operational snapshot includes more than rule tuples: module objects, contexts, lifecycle state, transform registrations, configuration, and executor policy. F-03, F-04, and F-10 show that immutable rule partitions cannot provide snapshot safety while adjacent objects remain shared and mutable.

A useful runtime ownership model would make it clear:

- which snapshot owns each loaded Python module and transform registration;
- which flows retain that snapshot;
- when an old snapshot is no longer referenced;
- when `on_unload` may safely run;
- which state is intentionally shared across generations, such as persistent store contents or long-term module statistics;
- which state is invocation-local, such as notes and logs.

### 5.4 Green unit tests do not currently exercise enough adversarial interleavings

The passing suite is meaningful and should be preserved. The uncovered behavior is concentrated in composed paths. The highest-value additions would be scenario tests that deliberately combine:

- a Python-only module with streaming and executor thresholds;
- conflicting header operations from multiple priorities;
- a paused flow with an atomic reload;
- concurrent hook invocations that emit notes;
- a long-lived WebSocket with byte-bound assertions;
- chunked responses that cross the buffer cap;
- runtime startup with non-default executor configuration.

These are not requests for more low-level tests of already covered helpers. They are tests of whether the documented system-level guarantee survives the handoff between helpers.

## 6. Security-oriented observations

Security was a secondary focus, as requested. The sampled control and loading paths showed several good practices:

- control binding is constrained to loopback;
- bearer-token comparison uses a constant-time comparison;
- browser-origin policy is explicit;
- YAML loading uses safe loading;
- module asset resolution performs resolved-path containment checks;
- Python modules are treated as trusted local code rather than presented as a sandbox.

No claim is made that these observations cover every endpoint or dependency. The actionable security-adjacent findings are availability and integrity issues already described:

- unbounded memory through WebSocket retention (F-05);
- unbounded buffering for unknown-length bodies (F-06);
- event-loop stalls through hooks or synchronous storage (F-01, F-13);
- incorrect cross-flow provenance attribution (F-10);
- header order differing from declared policy (F-02).

These can be triggered by ordinary network traffic once the relevant local rules or modules are enabled, so they deserve attention even under the project's trusted-local-control threat model.

## 7. Suggested sequencing for future work

No fixes were made during this review. If the project chooses to address the findings, the following order minimizes rework:

1. **Define and implement the runtime snapshot ownership model** (F-03, F-04, F-10). Other rules-engine fixes should attach to that model.
2. **Preserve exact rule operations through application** (F-02, F-09, F-12, F-14).
3. **Make body demand, observed-size bounds, hook offload, and executor configuration one coherent policy** (F-01, F-06, F-11, F-13).
4. **Repair capture accounting and establish an explicit WebSocket retention policy** (F-05).
5. **Connect validation and compilation to the active transform registry** (F-07, F-08).
6. **Add adversarial lifecycle and concurrency tests, then reconcile the documentation and requirement status** (F-15).

The first step is intentionally architectural. Fixing reload by adding locks around the existing mutable registry, or fixing hook notes with a lock around `drain()`, could suppress individual races while retaining unclear ownership and creating new contention on the proxy path.

## 8. Verification record

The following checks were completed against the reviewed revision before this report was added:

- 2,028 daemon unit tests passed.
- 55 MITM integration tests passed.
- Ruff passed.
- Python bytecode compilation passed.
- Focused probes reproduced:
  - acceptance of an unknown transform kind;
  - an absent-header removal recorded as applied;
  - duplication of a two-sided header rule in `len()` and `all_rules`;
  - add-then-remove leaving the header present;
  - a `status: 500` two-sided rule mutating the request before the response existed;
  - missing `map_local` provenance naming a short circuit when no short-circuit response existed;
  - WebSocket record growth without corresponding ring byte growth.

The probes were diagnostic only and left no repository files behind. Passing tests and static checks should not be interpreted as contradicting the findings; the reproduced issues occur in combinations and lifecycle transitions that the current suite does not assert.
