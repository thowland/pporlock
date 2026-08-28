# SPEC-1 — Proxy Daemon and MCP Server

**Version:** 1.0
**Status:** Draft for development
**Date:** 2026-08-27
**Traces:** `pporlock_requirements-v1.md`
**Depends on:** SPEC-0 (Shared Contracts) — data model §3, provenance §4, schemas §5, API §6, events §7, module API §8, redaction §9
**Independent of:** SPEC-2, SPEC-3

---

## 1. Scope

This specification covers deliverables **D1 (proxy daemon)** and **D4 (MCP server)**: the mitmproxy addon and adapter, the rules and module engine, the capture subsystem, the control server, the CLI, launchd packaging, and the MCP stdio server.

It does not cover any browser-side code. The daemon's obligations to browser clients are entirely discharged through SPEC-0 §6 and §7.

**Design constraint that governs everything below:** the control server shares the proxy's asyncio event loop (SPEC-0-referenced REQ DD-3). Anything that blocks that loop stalls all browsing. Every section that introduces work states where that work runs.

---

## 2. Package structure

```
daemon/src/pporlock/
  __init__.py
  version.py
  config.py              # §11
  errors.py              # exception hierarchy, error codes

  addon/                 # THE ONLY mitmproxy-AWARE CODE
    __init__.py
    interceptor.py       # the addon class, hook methods
    normalize.py         # mitmproxy → SPEC-0 §3 dataclasses
    apply.py             # SPEC-0 §3.3 mutations → mitmproxy flow
    streaming.py         # buffering guard
    options.py           # mitmproxy option wiring, anticache/anticomp

  engine/                # PURE. NO mitmproxy IMPORT. mypy --strict.
    __init__.py
    models.py            # SPEC-0 §3.1–3.3 dataclasses
    provenance.py        # SPEC-0 §4
    ruleset.py           # RuleSet, Rule, compiled matchers
    matcher.py
    evaluator.py         # the phase machine
    transforms/
      __init__.py        # registry
      html.py            # strip_integrity_attributes, inject_script, inject_style
      csp.py             # strip_csp, nonce extraction
      text.py            # regex_sub, replace_literal
      json_ops.py        # json_patch
    stubs.py             # Sec-Fetch-Dest derivation, stub library loader
    modules/
      loader.py          # discovery, manifest validation, Python import
      registry.py        # ModuleRegistry, priority ordering, quarantine
      context.py         # ModuleContext (SPEC-0 §8.2)
      store.py           # module-scoped key/value persistence
    profiles.py
    exclusions.py

  capture/
    ring.py              # bounded in-memory buffer
    session.py           # SQLite writer/reader
    schema.sql
    redact.py            # SPEC-0 §9
    dryrun.py            # SPEC-0 §6.8
    attribution.py       # SPEC-0 §3.6
    export.py            # HAR and native export

  control/
    server.py            # asyncio HTTP server
    routes/              # one module per SPEC-0 §6 group
    auth.py              # token, origin policy, pairing
    events.py            # SSE hub
    static.py            # serves web/dist
    serialize.py         # detail levels (SPEC-0 §6.3)

  cli/
    main.py
    doctor.py
    certs.py
    launchd.py
```

### 2.1 Dependency pinning

`daemon/pyproject.toml` pins mitmproxy exactly (REQ PXY-006). The pin is recorded in one place and referenced by the doctor command and the release notes. Upgrades are gated on the integration suite (REQ TST-007) and are expected to require changes only in `addon/` (REQ DD-2).

### 2.2 The engine boundary

`engine/` must import nothing from `mitmproxy`, `asyncio`, or `control/`. This is enforced by a test:

```python
def test_engine_has_no_forbidden_imports():
    """REQ TST-001, DD-2. This test is load-bearing; do not weaken it."""
```

The consequence is that the entire rules and module system is unit-testable with no proxy process and no network (REQ TST-001).

---

## 3. The mitmproxy addon and adapter

### 3.1 Addon class

```python
class Interceptor:
    def __init__(self, app: DaemonApp) -> None: ...

    # lifecycle
    def load(self, loader) -> None: ...          # declare custom options
    def running(self) -> None: ...               # starts control server task
    def done(self) -> None: ...

    # interception hooks — thin. All logic delegates to engine/ and capture/.
    def tls_clienthello(self, data) -> None: ...
    def requestheaders(self, flow) -> None: ...
    def request(self, flow) -> None: ...
    def responseheaders(self, flow) -> None: ...
    def response(self, flow) -> None: ...
    def error(self, flow) -> None: ...
    def websocket_message(self, flow) -> None: ...
    def websocket_end(self, flow) -> None: ...
```

Each hook method is expected to be under twenty lines. Its job is: normalize, call the evaluator, apply the mutation, record. All decisions live in `engine/`.

### 3.2 Normalization

```python
# addon/normalize.py
def normalize_request(flow, *, flow_id: str, tab_id: int | None,
                      body: bytes | None) -> NormalizedRequest: ...
def normalize_response(flow, *, flow_id: str,
                       body: bytes | None, streamed: bool) -> NormalizedResponse: ...
def normalize_ws_message(flow, message) -> WebSocketMessage: ...
```

This is the version-churn containment layer (REQ DD-2). Everything above it is mitmproxy-shaped; everything below sees SPEC-0 §3 dataclasses only.

### 3.3 Application

```python
# addon/apply.py
def apply_request_mutation(flow, mut: RequestMutation, prov: ProvenanceBuilder) -> None: ...
def apply_response_mutation(flow, mut: ResponseMutation, prov: ProvenanceBuilder) -> None: ...
def apply_synthetic(flow, synth: SyntheticResponse) -> None: ...
```

Body assignment uses `flow.response.text` / `.content` so that decode and re-encode are handled by mitmproxy per `Content-Encoding` (REQ PXY-023). Application is the only place that touches mitmproxy mutation APIs.

### 3.4 Buffering guard

```python
# addon/streaming.py
@dataclass(frozen=True)
class BufferingDecision:
    buffer: bool
    reason: str | None          # "size" | "content_type" | None

def decide(headers, cfg: BufferingConfig,
           wants_body: bool) -> BufferingDecision: ...
```

Called from `responseheaders`, which is the only point at which the decision can be made (REQ PXY-021). When `decide()` returns `buffer=False`, the addon sets `flow.response.stream = True` and the evaluator is told body transforms are unavailable, producing `skipped_streamed` outcomes and a `response_streamed` note (REQ PXY-022, SPEC-0 §4.3/§4.4).

Defaults: 2 MiB size threshold; content-type allowlist `text/html`, `text/css`, `application/javascript`, `text/javascript`, `application/json`, `application/x-javascript`, plus charset variants. Configurable via `PUT /config`.

`wants_body` is computed by the evaluator from the loaded ruleset: if no enabled rule could possibly produce a body transform for this response, the guard streams regardless of type and size. This is the cheapest available optimization and should be implemented from the start.

### 3.5 Exclusion at ClientHello

```python
# engine/exclusions.py
class ExclusionList:
    @classmethod
    def load(cls, path: str, extra: list[str]) -> "ExclusionList": ...
    def should_exclude(self, sni: str | None, ip: str) -> tuple[bool, str | None]: ...
```

Matching is on SNI glob, with an IP/CIDR fallback for connections without SNI. On exclusion the addon sets `data.ignore_connection = True` and records a `kind: "passthrough"` flow record carrying host/SNI, timing, and byte counts but no content (REQ PXY-015).

The shipped seed list (REQ PXY-013) lives at `daemon/src/pporlock/data/exclusions-default.yaml` and covers, at minimum: Apple software update and OCSP endpoints, Chrome update and telemetry, Mozilla and Microsoft update endpoints, known certificate-pinning hosts, and a documented starter set of financial institution domains. The file carries a comment per entry explaining why it is there, because an unexplained exclusion is indistinguishable from a bug.

### 3.6 Development toggles

`anticache` and `anticomp` are exposed via `POST /state` and default off (REQ PXY-043). When either is active:

- Every flow processed while the toggle was on carries a `dev_toggle_active` note (SPEC-0 §4.4), so that a flow captured under `anticomp` is never mistaken for normal behaviour.
- `GET /state` reports the toggle, and the `state.changed` event fires so UI and extension can show the indicator (REQ PXY-044).

---

## 4. Rules engine

### 4.1 Ruleset

```python
# engine/ruleset.py
@dataclass(frozen=True)
class CompiledRule:
    rule_id: str                 # "module:index"
    module: str
    name: str
    enabled: bool
    phase: Phase                 # SPEC-0 §4.2
    action: Action
    matcher: CompiledMatcher     # regexes pre-compiled — REQ PXY-025
    params: object               # validated, action-specific dataclass

class RuleSet:
    @classmethod
    def build(cls, modules: Sequence[LoadedModule],
              profile: Profile) -> "RuleSet": ...

    short_circuit: tuple[CompiledRule, ...]   # ordered, first-match-wins
    request_headers: tuple[CompiledRule, ...] # ordered, all-match
    response_headers: tuple[CompiledRule, ...]
    response_body: tuple[CompiledRule, ...]
    passthrough: tuple[CompiledRule, ...]

    def wants_body(self, req: NormalizedRequest,
                   resp_headers) -> bool: ...   # feeds §3.4
```

Ordering across modules is by module `priority` ascending, then declaration order (SPEC-0 §5.4). The ruleset is rebuilt on module reload and profile activation, never mutated in place — a swap of an immutable object avoids any locking against in-flight flows (REQ MOD-004).

### 4.2 Matcher

```python
# engine/matcher.py
@dataclass(frozen=True)
class CompiledMatcher:
    host_glob: Pattern | None
    path_re: Pattern | None
    methods: frozenset[str] | None
    dests: frozenset[str] | None
    query_res: tuple[tuple[str, Pattern], ...]
    request_header_res: tuple[tuple[str, Pattern | None], ...]
    statuses: tuple[tuple[int, int], ...] | None
    content_type_re: Pattern | None

    def matches_request(self, req: NormalizedRequest) -> bool: ...
    def matches_response(self, req: NormalizedRequest,
                         resp: NormalizedResponse) -> bool: ...
```

Semantics are normative in SPEC-0 §5.3: all present criteria AND together; `path` is `re.search`; host globs are case-insensitive full-host matches; response-side criteria on a request-phase action are a **load-time** error, not a runtime one.

### 4.3 Evaluator

The evaluator is the phase machine. It is pure: given normalized inputs and a ruleset, it returns mutations and provenance and touches nothing else.

```python
# engine/evaluator.py
@dataclass
class RequestDecision:
    mutation: RequestMutation
    provenance: Provenance
    short_circuit: SyntheticResponse | None
    wants_body: bool

@dataclass
class ResponseDecision:
    mutation: ResponseMutation
    provenance: Provenance

class Evaluator:
    def __init__(self, ruleset: RuleSet, registry: ModuleRegistry,
                 budget: TimeBudget) -> None: ...

    def evaluate_clienthello(self, sni: str | None, ip: str) -> ClientHelloDecision: ...
    def evaluate_request(self, req: NormalizedRequest,
                         prov: ProvenanceBuilder) -> RequestDecision: ...
    def evaluate_response(self, req: NormalizedRequest, resp: NormalizedResponse,
                          prov: ProvenanceBuilder) -> ResponseDecision: ...
    def evaluate_websocket(self, msg: WebSocketMessage, req: NormalizedRequest,
                           prov: ProvenanceBuilder) -> None: ...
```

**Phase order** is fixed and matches SPEC-0 §4.2 and REQ PXY-020. The evaluator interleaves declarative rules and Python hooks of equivalent action class by module priority (REQ MOD-023); there is no separate "rules run, then Python runs" stage, because a module that both strips CSP declaratively and injects via Python must see a consistent ordering.

### 4.4 Time budget

```python
# engine/evaluator.py
class TimeBudget:
    def __init__(self, total_ms: float) -> None: ...
    def consume(self, ms: float) -> None: ...
    @property
    def exhausted(self) -> bool: ...
```

Default 250 ms per flow (REQ PXY-026). On exhaustion, remaining transforms are skipped with `skipped_budget` outcomes and a `transform_budget_exceeded` note naming the transform that was cut. The flow is delivered with whatever mutations were already applied — never dropped, never delayed further.

### 4.5 Executor offload

Transforms declare a cost class:

```python
@dataclass(frozen=True)
class TransformSpec:
    name: str
    fn: Callable
    schema: dict
    cost: str          # "cheap" | "expensive"
```

`expensive` transforms, and any Python hook on a body over a configurable threshold (default 256 KiB), run via `loop.run_in_executor` on a bounded thread pool (REQ PXY-024). `cheap` transforms run inline. The registry's built-ins are classified: `regex_sub` and `replace_literal` are cheap under the size threshold and expensive above it; HTML-parsing transforms are always expensive.

### 4.6 Transform registry

```python
# engine/transforms/__init__.py
class TransformRegistry:
    def register(self, spec: TransformSpec) -> None: ...
    def get(self, name: str) -> TransformSpec: ...
    def validate_params(self, name: str, params: dict) -> object: ...
```

Built-ins are enumerated normatively in SPEC-0 §5.5. Parameters validate at **load time** against the transform's schema, so a malformed transform is a module load error, not a runtime surprise (REQ MOD-014).

`inject_script` implements the nonce policy of REQ PXY-041: parse the document's existing CSP for a `script-src` nonce, reuse it on the injected tag, and only relax the policy if no nonce is present. It emits `script_injected` with `detail.nonce_reused`.

`strip_integrity_attributes` is applied unconditionally to any document whose body is being rewritten (REQ PXY-040), whether or not a rule requested it — the evaluator injects it as an implicit transform and records it in provenance like any other.

### 4.7 Stub synthesis

```python
# engine/stubs.py
class StubLibrary:
    @classmethod
    def load(cls, builtin_dir: str, module_assets: Mapping[str, str]) -> "StubLibrary": ...
    def auto_for(self, dest: str | None, req: NormalizedRequest) -> SyntheticResponse: ...
    def named(self, name: str) -> SyntheticResponse: ...
```

The `auto` derivation table is normative in REQ PXY-032 and implemented exactly once, here. The shipped stub library (REQ PXY-033) lives in `stubs/` at the repository root and is installed alongside the package; each stub is a small JavaScript file defining the globals its target expects, with a header comment naming the tracker it replaces.

`document` destinations get `403` with a short explanatory page naming the blocking module and rule, because a blocked top-level navigation must be visible to the user rather than looking like a network failure.

---

## 5. Module system

### 5.1 Loading

```python
# engine/modules/loader.py
@dataclass
class LoadedModule:
    name: str
    version: str
    api_version: str
    path: str
    manifest: Manifest
    rules: tuple[CompiledRule, ...]
    python: PythonModule | None
    state: str                    # "loaded" | "load_error" | "quarantined" | "disabled"
    error: ModuleError | None

class ModuleLoader:
    def discover(self, root: str) -> list[str]: ...
    def load_one(self, path: str) -> LoadedModule: ...
    def load_all(self, root: str) -> list[LoadedModule]: ...
```

Load sequence per module: read `module.yaml` → validate against the manifest JSON Schema (strict, unknown keys are errors) → check `pporlock_api` against the supported range (SPEC-0 §8.1) → compile rules and validate transform parameters → import `module.py` if present → call `on_load(ctx)`.

A failure at any step yields `state="load_error"` with a structured `ModuleError` carrying code, message, traceback, and source line where determinable. **Only that module is affected** (REQ MOD-005); the daemon starts, other modules load, and the error is reported through `GET /state`, `GET /modules`, and a `module.error` event.

### 5.2 Python import isolation

Each module's `module.py` is imported under a unique synthetic package name (`pporlock_module_<name>`) with the module directory on the path, so two modules may both have a helper called `utils` without colliding. Reload discards the old module object and re-imports; `on_unload(ctx)` is called on the outgoing instance first.

Modules are fully trusted (REQ MOD-030). There is no import allowlist and no sandbox. The loader does not attempt to restrict what module code can do, and the documentation says so plainly (REQ MOD-031).

### 5.3 Hot reload

```python
# engine/modules/registry.py
class ModuleRegistry:
    def current(self) -> RegistrySnapshot: ...      # immutable
    def reload(self, reason: str) -> ReloadResult: ...
    def set_enabled(self, name: str, enabled: bool) -> None: ...
    def set_priority(self, name: str, priority: int) -> None: ...
    def quarantine(self, name: str, reason: str) -> None: ...
    def record_failure(self, name: str) -> None: ...
    def record_success(self, name: str) -> None: ...
```

Reload is a **snapshot swap**: a new registry snapshot and ruleset are built off the event loop (in the executor, since it involves filesystem reads and Python imports), then swapped atomically. In-flight flows continue against the snapshot they started with (REQ MOD-004). This is why the snapshot is immutable and why the evaluator holds a reference rather than consulting a mutable registry.

File watching uses a debounced watcher (250 ms) on the module root. Explicit reload via `POST /modules/reload` uses the same path.

### 5.4 Error isolation and quarantine

Every Python hook invocation is wrapped:

- An exception is caught, converted to a `module_error` note attributed to the module and flow, logged with traceback, emitted as a `module.error` event, and counted (REQ MOD-024).
- N consecutive failures (default 10, configurable) trigger `quarantine()`: the module is disabled, a `module.quarantined` note and event are emitted, and the reason is surfaced in `GET /modules` (REQ MOD-025).
- A successful invocation resets the consecutive counter.

Quarantine is sticky until the module is edited (which triggers reload) or explicitly re-enabled via `PATCH /modules/{name}`.

### 5.5 Module context and store

`ModuleContext` implements exactly SPEC-0 §8.2 and nothing more; anything additional is private and must not be reachable from module code. The module store is SQLite at `~/.pporlock/module-store.db`, one table keyed by `(module, key)`, values JSON. Writes are queued to the executor; `store_set` is fire-and-forget from the module's perspective, `store_get` reads a write-through in-memory cache so it never touches disk on the event loop.

### 5.6 Profiles

```python
# engine/profiles.py
class ProfileManager:
    def list(self) -> list[Profile]: ...
    def get(self, name: str) -> Profile: ...
    def save(self, profile: Profile) -> None: ...
    def delete(self, name: str) -> None: ...     # refuses "default"
    def activate(self, name: str) -> ActivationResult: ...
    @property
    def active(self) -> Profile: ...
```

Activation rebuilds the ruleset from the profile's module set, applies profile-scoped dev toggles and exclusion additions (REQ MOD-044), emits `state.changed`, and writes an audit entry. It never restarts the daemon (REQ MOD-042).

---

## 6. Capture subsystem

### 6.1 Ring buffer

```python
# capture/ring.py
class RingBuffer:
    def __init__(self, max_flows: int, max_bytes: int, max_body_bytes: int) -> None: ...
    def add(self, record: FlowRecord) -> None: ...
    def update(self, flow_id: str, **changes) -> FlowRecord | None: ...
    def get(self, flow_id: str) -> FlowRecord | None: ...
    def query(self, f: FlowFilter, limit: int, cursor: str | None) -> QueryResult: ...
    def clear(self) -> None: ...
    @property
    def stats(self) -> RingStats: ...
```

Bounded by both flow count (default 2,000) and total bytes (default 256 MiB), evicting oldest first on either bound (REQ CAP-001). Bodies are capped per-body at 512 KiB with truncation flagged (REQ CAP-003) and a `body_truncated` note.

Memory boundedness is verified by a soak test (REQ PRF-005).

### 6.2 Filter vocabulary

```python
@dataclass(frozen=True)
class FlowFilter:
    host: str | None
    path: str | None
    method: str | None
    status: str | None
    content_type: str | None
    dest: str | None
    tab_id: int | None
    modified: bool | None
    blocked: bool | None
    module: str | None
    note_code: str | None
    since: str | None
    until: str | None
    q: str | None
```

One implementation serves `GET /flows`, `GET /sessions/{id}/flows`, the SSE stream filter, and the MCP listing tools — identical vocabulary everywhere (SPEC-0 §6.5).

### 6.3 Session storage

SQLite, one database file per session, at `~/.pporlock/sessions/<session_id>.db` (REQ CAP-020).

```sql
-- capture/schema.sql (abbreviated; the file is normative)
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
  -- schema_version, session_name, started_at, stopped_at, pporlock_version,
  -- profile, redaction_config

CREATE TABLE flows (
  flow_id       TEXT PRIMARY KEY,
  seq           INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  completed_at  TEXT,
  tab_id        INTEGER,
  method        TEXT, scheme TEXT, host TEXT, port INTEGER,
  path          TEXT, query TEXT, url TEXT, dest TEXT,
  status        INTEGER, content_type TEXT,
  req_headers   TEXT NOT NULL,     -- JSON
  resp_headers  TEXT,              -- JSON
  req_body      BLOB, req_body_encoding TEXT, req_body_truncated INTEGER,
  resp_body     BLOB, resp_body_encoding TEXT, resp_body_truncated INTEGER,
  timing        TEXT,              -- JSON
  provenance    TEXT NOT NULL,     -- JSON, SPEC-0 §4
  modified      INTEGER, blocked INTEGER, streamed INTEGER
);
CREATE INDEX flows_host   ON flows(host);
CREATE INDEX flows_seq    ON flows(seq);
CREATE INDEX flows_status ON flows(status);
CREATE INDEX flows_tab    ON flows(tab_id);

CREATE TABLE ws_messages (
  flow_id TEXT NOT NULL, idx INTEGER NOT NULL, ts TEXT NOT NULL,
  direction TEXT, opcode TEXT, size INTEGER,
  payload BLOB, payload_encoding TEXT, truncated INTEGER,
  PRIMARY KEY (flow_id, idx)
);

CREATE TABLE notes (
  flow_id TEXT NOT NULL, code TEXT NOT NULL, severity TEXT NOT NULL,
  module TEXT, message TEXT, detail TEXT
);
CREATE INDEX notes_code ON notes(code);
```

Bodies are stored in-row as BLOBs, capped at the configured per-body size (REQ CAP-023). A session is one file, so copying, deleting, or handing it to a dry run is a single filesystem operation.

**Writer discipline.** The session writer runs on a dedicated thread with a bounded queue, WAL journaling, and batched commits (default every 100 flows or 500 ms). The event loop enqueues and returns; it never touches SQLite. If the queue fills, flows are dropped with a counter increment and a warning rather than backpressuring the proxy — recording must never slow browsing.

```python
# capture/session.py
class SessionWriter:
    def start(self, name: str) -> Session: ...
    def enqueue(self, record: FlowRecord) -> None: ...   # non-blocking
    def stop(self) -> Session: ...
    @property
    def dropped(self) -> int: ...

class SessionReader:
    def open(self, session_id: str) -> Session: ...
    def query(self, f: FlowFilter, limit: int, cursor: str | None) -> QueryResult: ...
    def iter_all(self) -> Iterator[FlowRecord]: ...
```

Schema version is recorded in `meta`; the reader refuses a newer version with a clear message and migrates older versions forward where a migration exists (OI-5 resolves here).

### 6.4 Redaction

```python
# capture/redact.py
class Redactor:
    def __init__(self, cfg: RedactionConfig) -> None: ...
    def redact_headers(self, headers) -> tuple[list, bool]: ...
    def redact_json_body(self, body: bytes) -> tuple[bytes, bool]: ...
    def redact_record(self, record: FlowRecord) -> FlowRecord: ...
    def mask(self, value: str) -> str: ...      # SPEC-0 §9.1 format
```

Redaction is applied **at write time** for sessions, so a session file on disk never contains the secret (REQ CAP-045), and **at serialization time** for API and MCP responses. The live ring buffer holds unredacted values, which is what makes UI-side unmasking possible (REQ CAP-043) and is the reason unmasking is unavailable for sessions.

Unmasking path: `GET /flows/{id}?unmask=<field_path>` returns the single requested value, requires the bearer token, writes an audit entry, and is rejected outright when the request originates from the MCP client (REQ MCP-003).

### 6.5 Dry run

```python
# capture/dryrun.py
@dataclass
class DryRunRequest:
    candidate_modules: list[CandidateModule]    # name + file contents, uninstalled
    use_installed: list[str]
    profile: str | None
    limit: int
    include_diffs: bool

class DryRunner:
    def run(self, session_id: str, req: DryRunRequest) -> DryRunResult: ...
```

Implementation constraints:

- Candidate modules are materialized into a temporary directory and loaded through the **normal** `ModuleLoader`, so a module that dry-runs cleanly loads cleanly (REQ CAP-031).
- Evaluation uses the **same** `Evaluator` as live traffic. There is no second implementation. This is the point of the pure engine.
- The whole run executes in the executor, never on the event loop, and reports progress via SSE for long runs.
- Python hooks execute (REQ CAP-032). The documentation states this, because dry-running an agent-authored module runs that agent's code.
- Diffs: header diffs as an op list; body diffs as unified diff for text, and a byte-length-and-hash summary for binary.

### 6.6 Attribution

```python
# capture/attribution.py
class AttributionIndex:
    def submit(self, entries: list[AttributionEntry]) -> int: ...
    def resolve(self, req: NormalizedRequest) -> int | None: ...
    def backfill(self, ring: RingBuffer) -> list[str]: ...   # returns updated flow_ids
    @property
    def coverage(self) -> float: ...
```

Implements SPEC-0 §3.6: the extension POSTs batched `(method, url, ts) → tab_id` associations; the index joins them against flows within a 5 s window; backfilled flows emit `flow.updated`. `coverage` is exposed in `GET /metrics` and is the measurement against which the OI-2 decision criterion (95% attribution over a 30-minute reference session) is evaluated.

The fallback mechanisms named in SPEC-0 §3.6 are confined to this module plus the `POST /attribution` route.

---

## 7. Control server

### 7.1 Server

```python
# control/server.py
class ControlServer:
    def __init__(self, app: DaemonApp, cfg: ControlConfig) -> None: ...
    async def start(self) -> None: ...     # called from Interceptor.running()
    async def stop(self) -> None: ...
```

Started from the addon's `running()` hook as an asyncio task on the proxy's loop (REQ API-001). Binds `127.0.0.1:8081` and refuses to bind any non-loopback interface under any configuration (REQ API-010) — this is asserted in code, not merely defaulted.

**Loop discipline (REQ API-002).** Handlers are classified. Handlers that only read in-memory state (`/state`, `/flows`, `/events`) run inline. Handlers that touch the filesystem, SQLite, or module import (`/modules` writes, `/sessions`, `/dryrun`, `/validate`, `/config` writes) offload to the executor. A test asserts that no route registered as "inline" performs I/O.

### 7.2 Auth and pairing

```python
# control/auth.py
class TokenStore:
    def ensure(self) -> str: ...           # generates at first run, 0600
    def verify(self, presented: str) -> bool: ...

class PairingWindow:
    def open(self, ttl_s: int = 120) -> str: ...   # returns a pairing code
    def redeem(self, code: str, origin: str) -> str: ...  # returns token, records ext id
```

Token at `~/.pporlock/token`, mode 0600 (REQ API-011). The extension never reads the filesystem; it redeems a pairing code through `POST /pair` within a short window opened by `pporlock pair` or a web UI button (REQ API-012, SPEC-0 §6.10). The paired extension ID is persisted and thereafter is the only accepted `chrome-extension://` origin.

Origin and content-type policy is enforced by middleware per SPEC-0 §6.1 (REQ API-004, API-013). `GET /state/health` is the only unauthenticated route.

### 7.3 SSE hub

```python
# control/events.py
class EventHub:
    def publish(self, event: Event) -> None: ...          # non-blocking
    async def subscribe(self, f: EventFilter,
                        last_event_id: str | None) -> AsyncIterator[Event]: ...
    @property
    def subscriber_count(self) -> int: ...
```

Per-subscriber bounded queues. A slow subscriber is dropped from the queue's tail and receives a `stream.gap` event rather than backpressuring the publisher (SPEC-0 §7.2/§7.3) — a stalled DevTools panel must never slow the proxy. Server-side filtering per SPEC-0 §7.1 keeps the extension's per-tab subscription cheap.

### 7.4 Routes

One module per SPEC-0 §6 group: `state.py`, `flows.py`, `modules.py`, `profiles.py`, `sessions.py`, `config.py`, `exclusions.py`, `validate.py`, `metrics.py`, `audit.py`, `pair.py`, `attribution.py`, `events.py`.

Serialization applies detail levels (SPEC-0 §6.3) and redaction (§6.4) uniformly in `control/serialize.py`; no route serializes a flow itself.

### 7.5 Static assets

`control/static.py` serves the built web UI from the packaged `web/dist` (REQ API-003), with SPA fallback to `index.html` and no-cache headers on `index.html` so a rebuilt UI is picked up without a hard refresh.

### 7.6 Audit log

Every enable/disable, profile activation, dev-toggle change, config change, unmask, and module create/update/delete is recorded with timestamp, actor origin (`ui` | `extension` | `mcp` | `cli`, from `X-Pporlock-Client`), and the change (REQ MCP-031). Stored in SQLite at `~/.pporlock/audit.db`, exposed at `GET /audit`.

---

## 8. CLI

```
pporlock run                    # foreground daemon (REQ PXY-005)
pporlock start|stop|restart     # launchd control
pporlock status
pporlock install [--no-ca]      # launchd agent + CA trust
pporlock uninstall [--purge]    # --purge also removes modules and sessions
pporlock doctor [--fix]
pporlock pair                   # opens the pairing window
pporlock logs [-f]
pporlock modules list|enable|disable|validate <path>
pporlock profile list|activate <name>
pporlock session start|stop|list|export <id>
pporlock dryrun <session-id> <module-path>
```

### 8.1 Doctor

```python
# cli/doctor.py
@dataclass
class Check:
    id: str
    title: str
    run: Callable[[], CheckResult]
    fix: Callable[[], None] | None

CHECKS = [
    "ca_present", "ca_trusted", "port_8080_free", "port_8081_free",
    "chrome_installed", "chrome_quic_disabled", "config_valid",
    "modules_load_clean", "daemon_reachable", "extension_paired",
    "launchd_installed", "token_permissions", "disk_space",
]
```

Each check reports pass/warn/fail with a remediation string, and `--fix` runs the fix where one exists (REQ PXY-004). `chrome_quic_disabled` is a warning, not a failure, since it cannot be reliably enforced from outside Chrome (REQ PXY-012).

### 8.2 Certificate management

```python
# cli/certs.py
def ca_path() -> str: ...                  # ~/.mitmproxy/mitmproxy-ca-cert.pem
def is_present() -> bool: ...
def is_trusted() -> bool: ...              # queries the login keychain
def install_trust() -> None: ...           # prompts; login keychain, not System
def remove_trust() -> None: ...
```

Installs into the **login** keychain, not the System keychain, so no administrator privileges are required and the blast radius is the user account (REQ PXY-011). Uninstall removes trust and the certificate (REQ DOC-005).

### 8.3 launchd

```python
# cli/launchd.py
LABEL = "com.pporlock.daemon"
PLIST = "~/Library/LaunchAgents/com.pporlock.daemon.plist"

def install(auto_start: bool = True) -> None: ...
def uninstall() -> None: ...
def status() -> ServiceStatus: ...
```

User agent (not a system daemon), `RunAtLoad` true, `KeepAlive` with a crash-restart throttle, stdout/stderr to `~/Library/Logs/pporlock/` (REQ PXY-002, PXY-007). Logs rotate by size with a retained-file count, and bodies are never logged at default level.

**Clean-shutdown obligation (REQ PXY-008):** the daemon has no way to reach into Chrome on its way down, so this is discharged jointly — the daemon exposes `GET /state/health`, and the extension health-checks and reverts on failure (SPEC-3 §4.4). The daemon's part is to make `/state/health` cheap and to fail closed (connection refused) rather than hanging when shutting down.

---

## 9. Configuration

`~/.pporlock/config.yaml`, validated against a schema, with precedence: **CLI flags > environment (`PPORLOCK_*`) > config file > profile-scoped overrides > defaults**.

```yaml
proxy:
  listen_host: 127.0.0.1
  listen_port: 8080
control:
  listen_host: 127.0.0.1
  listen_port: 8081
buffering:
  max_body_bytes: 2097152
  content_types: [text/html, text/css, application/javascript, text/javascript, application/json]
budget:
  per_flow_ms: 250
  executor_threshold_bytes: 262144
  executor_workers: 4
capture:
  ring_max_flows: 2000
  ring_max_bytes: 268435456
  max_body_bytes: 524288
  session_max_bytes: 5368709120
modules:
  root: ~/.pporlock/modules
  quarantine_after_failures: 10
  watch: true
redaction:
  enabled: true
  header_patterns: [cookie, set-cookie, authorization, proxy-authorization, x-api-key, x-auth-token]
  json_key_patterns: [password, token, secret, api_key, apikey, access_token, refresh_token, session, auth, credential]
logging:
  level: info
  dir: ~/Library/Logs/pporlock
```

`listen_host` is validated to be a loopback address; any other value is rejected at startup (REQ API-010).

---

## 10. Errors

```python
# errors.py
class PporlockError(Exception):
    code: str                  # appears verbatim in SPEC-0 §6.2 error bodies

class ConfigError(PporlockError): ...
class ModuleLoadError(PporlockError): ...
class ModuleRuntimeError(PporlockError): ...
class RuleValidationError(PporlockError): ...      # carries module, rule index, field
class TransformError(PporlockError): ...
class SessionError(PporlockError): ...
class AuthError(PporlockError): ...
class PairingError(PporlockError): ...
```

Every error carries a stable `code`, because the UI, the DevTools panel, and MCP all branch on it.

---

## 11. MCP server

### 11.1 Architecture

A **separate process** (`mcp/`), speaking MCP over stdio to its client and HTTP to the control API on `127.0.0.1:8081` (REQ MCP-001). It imports nothing from the daemon package except the generated types; it is an ordinary API client.

This means the MCP server works against an already-running daemon, requires no import coupling, and can be started and stopped freely by the MCP client. It requires the bearer token, read from `~/.pporlock/token` (the MCP server runs as the user, unlike the extension).

```python
# mcp/src/pporlock_mcp/server.py
class PporlockMCP:
    def __init__(self, base_url: str, token: str, read_only: bool) -> None: ...
    async def serve_stdio(self) -> None: ...
```

`--read-only` disables the authoring and control tool families entirely (REQ MCP-032).

### 11.2 Tools

**Introspection (REQ MCP-010)** — always available:

| Tool | Arguments | Returns |
|---|---|---|
| `list_flows` | filter vocabulary (SPEC-0 §6.5), `limit` (default 50, max 200), `cursor`, `detail` (default `summary`) | Flow list |
| `get_flow` | `flow_id`, `detail` (default `full`) | One flow with provenance |
| `get_provenance` | `flow_id` | Provenance only — the cheapest way to answer "why did this break" |
| `flow_stats` | the full §6.5 filter vocabulary, plus `sample` (how many flows to aggregate) | Aggregate counts, notes histogram |
| `list_websocket_messages` | `flow_id`, `limit` | Frames |
| `list_sessions` | — | Sessions |
| `list_session_flows` | `session_id`, filter, `limit`, `cursor` | Flow list |

**Authoring (REQ MCP-011)** — suppressed in read-only mode:

| Tool | Arguments | Returns |
|---|---|---|
| `list_modules` | — | Modules with state, errors, quarantine, stats |
| `read_module` | `name`, `full` | Manifest, rules, Python source. Truncated unless `full` |
| `create_module` | `name`, `files` | Created module status. **Does not enable it.** |
| `update_module` | `name`, `files` | Updated status. **Does not enable it.** |
| `delete_module` | `name` | — |
| `suggest_rule_from_flow` | `flow_id`, `intent` (`block`\|`map_local`\|`redirect`\|`headers`) | Candidate rule YAML (REQ MCP-014) |

**Validation (REQ MCP-012)** — suppressed in read-only mode:

| Tool | Arguments | Returns |
|---|---|---|
| `validate_module` | `files`, optional `name` | Schema and syntax errors with line numbers; installs nothing. Omit `name` and the manifest's own is used |
| `dry_run` | `session_id` (required; `live` replays the ring), `files` or `module_name`, optional `name`, `profile`, `limit`, `include_diffs` | SPEC-0 §6.8 dry-run result |

**Control (REQ MCP-013)** — suppressed in read-only mode:

| Tool | Arguments |
|---|---|
| `get_status` | — |
| `set_module_enabled` | `name`, `enabled` |
| `activate_profile` | `name` |
| `list_profiles` | — |
| `set_dev_toggle` | `anticache` and/or `anticomp` |
| `start_recording` | `name` |
| `stop_recording` | `session_id` |
| `reload_modules` | — |
| `edit_exclusions` | `add`, `remove`, `comment` |
| `proxy_start` / `proxy_stop` | — |

### 11.3 Guardrails

- `create_module` and `update_module` never enable. `set_module_enabled` is a separate, explicit call (REQ MCP-030). Tool descriptions say so.
- All flow-bearing responses are redacted; there is no unmask tool and the server refuses to pass an `unmask` parameter through (REQ MCP-003).
- All flow-bearing responses include provenance (REQ MCP-004).
- Every mutating call sends `X-Pporlock-Client: mcp`, landing in the audit log (REQ MCP-031) and in the web UI's MCP activity indicator (REQ MCP-033).
- Token budget discipline (REQ MCP-005): every listing tool defaults to `summary` detail and a bounded page size; bodies require explicit `detail: "bodies"`. Tool descriptions state the approximate cost of each level.

### 11.4 The intended loop

The tool set is shaped around one workflow, and it should be documented as such (REQ DOC-006):

1. `start_recording` → user reproduces the problem → `stop_recording`
2. `list_session_flows` + `get_provenance` to find what broke and why
3. `suggest_rule_from_flow` or hand-authored YAML → `validate_module`
4. `create_module` (disabled) → `dry_run` against the session → inspect diffs
5. Iterate on 3–4 until the dry run is clean
6. `set_module_enabled` — the one step that touches live browsing

---

## 12. Performance

| Requirement | Target | How it is met |
|---|---|---|
| PRF-001 | <15% p50, <30% p95 added page-load latency | Buffering guard, `wants_body` short-circuit, pre-compiled regexes, executor offload |
| PRF-002 | <2 ms p95 per non-matching flow | Matcher rejects on the cheapest criterion first; ruleset is pre-partitioned by phase so a request touches only the short-circuit and request-header lists |
| PRF-003 | Benchmark harness in-repo | `daemon/bench/` with a fixed local reference workload; `make bench` |
| PRF-004 | 200+ subresource page without stalling | Nothing expensive on the loop; SSE and session writes both non-blocking |
| PRF-005 | Bounded memory over days | Ring caps enforced on both axes; soak test |
| PRF-006 | Nothing wedges the proxy | Time budget + quarantine + drop-don't-backpressure everywhere |
| PRF-007 | Per-module cost visible | Per-module timing accumulated in provenance, aggregated into `GET /metrics` |

Matcher ordering for PRF-002: evaluate `methods` and `dests` (set membership) before `host_glob`, and `host_glob` before `path_re`, since regex is the expensive one.

---

## 13. Test plan

| Layer | Coverage |
|---|---|
| `engine/` unit | Every match criterion; both evaluation semantics; phase ordering; priority interleaving; provenance correctness for every outcome and note code; time budget exhaustion; transform parameter validation; stub derivation for every `Sec-Fetch-Dest` value. No proxy, no network (REQ TST-001). ≥85% coverage (REQ TST-002). |
| `engine/` import test | The forbidden-import assertion of §2.2. |
| Module loader unit | Manifest validation, API version gating, load-error isolation, quarantine after N failures, hot-reload snapshot swap, name collision between two modules' helpers. |
| Redaction unit | Every default pattern; mask format exactness (SPEC-0 §9.1); write-time application for sessions; MCP unmask refusal. |
| Capture unit | Ring eviction on both bounds; body truncation; filter vocabulary; session round-trip; writer queue overflow drops rather than blocks. |
| Integration | Full pipeline against a local test origin: all six actions; buffering guard both ways; SRI stripping; CSP nonce reuse; anticache/anticomp; provenance end-to-end (REQ TST-003). |
| Golden corpus | Recorded sessions in `daemon/tests/corpus/`, replayed to regression-test module behaviour and dry-run diffs (REQ TST-004). |
| API contract | Every route validated against `contracts/openapi.yaml` (REQ TST-005). |
| Loop discipline | Assert no inline-classified route performs I/O; assert session writes and SSE publishes never block. |
| Soak | Multi-day daemon uptime with bounded memory (REQ PRF-005). |
| Benchmark | PRF-001/002 numbers against the fixed workload (REQ PRF-003). |

---

## 14. Build order

| Step | Deliverable | Gate |
|---|---|---|
| 1 | `engine/models.py`, `provenance.py`, contracts validation | Types match SPEC-0 §3/§4 exactly |
| 2 | `engine/matcher.py`, `ruleset.py`, `evaluator.py` + unit tests | Both evaluation semantics correct with no proxy running |
| 3 | `addon/` adapter + `mitmdump` wiring | v0.1 exit criterion 1: 30 min clean browsing |
| 4 | `engine/stubs.py`, `block` action | v0.1 exit criterion 2: host blocked, pages still render |
| 5 | `capture/ring.py`, `control/server.py`, `control/events.py` | v0.1 exit criterion 3: live flows over SSE |
| 6 | `control/auth.py`, `pair.py`, `attribution.py` | OI-1 and OI-2 spikes resolved; §3.6 decision criterion measured |
| 7 | Full ruleset, transforms, hot reload | v0.2 |
| 8 | Buffering guard, CSP/SRI, dev toggles, stub library | v0.3 |
| 9 | Module system, Python tier, profiles | v0.4 |
| 10 | Sessions, redaction, dry run | v0.5 |
| 11 | MCP server | v0.6 |
| 12 | CLI, doctor, launchd, packaging | v1.0 |
