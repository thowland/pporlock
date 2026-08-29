# SPEC-2 — Web UI

**Version:** 1.0
**Status:** Draft for development
**Date:** 2026-08-27
**Traces:** `pporlock_requirements-v1.md`
**Depends on:** SPEC-0 (Shared Contracts) — data model §3, provenance §4, schemas §5, API §6, events §7, redaction §9
**Independent of:** SPEC-1 internals, SPEC-3

---

## 1. Scope

Deliverable **D2**: a React + Vite single-page application, built to static assets and served by the daemon's control server at `http://127.0.0.1:8081` (REQ WUI-001).

The UI is the authoring and analysis surface. The extension (SPEC-3) is the control-and-glance surface; anything requiring a keyboard and screen area belongs here.

This specification treats the daemon as a black box reachable only through SPEC-0 §6 and §7. No knowledge of SPEC-1 is required to build it.

---

## 2. Stack

### 2.1 Fixed

| Concern | Decision |
|---|---|
| Framework | React 18, TypeScript strict |
| Build | Vite; `make web` emits `web/dist/`, packaged into the daemon (SPEC-0 §1.1) |
| Editor | **Monaco** — required (REQ WUI-006) |
| Types | Generated from `contracts/schemas/` into `contracts/generated/types.ts`. Never hand-written (SPEC-0 §1.1) |
| Routing | Hash or history routing with SPA fallback; the server serves `index.html` for unknown paths |

### 2.2 Implementer's choice

State management, CSS approach, component library, and table/virtualization library are **deliberately unspecified**. Pick them at implementation time and record the choice in `web/README.md`.

Two constraints bound the choice, and they are requirements rather than preferences:

- **The flow table must be virtualized.** It holds thousands of rows fed by a live event stream (REQ WUI-003, PRF-004). A naive render will drop frames on a busy page load.
- **Server state and local UI state must be distinguishable.** SSE pushes updates into the same cache that REST queries populate; a design that cannot express "this row was updated by an event" will produce stale views.

### 2.3 Build constraints

- No network access at runtime beyond the daemon origin. The page is served from loopback and must work offline.
- Monaco is bundled locally, never CDN-loaded. Load it lazily so the flow view does not pay for the editor.
- No analytics, no telemetry, no external fonts.

---

## 3. Application shell

### 3.1 Navigation

Seven top-level views:

| Route | View | Requirement |
|---|---|---|
| `/traffic` | Live traffic (default) | WUI-003, WUI-004 |
| `/modules` | Module library | WUI-005 |
| `/modules/:name` | Module editor | WUI-006, WUI-007 |
| `/profiles` | Profiles | WUI-009 |
| `/sessions` | Sessions and dry run | WUI-010 |
| `/settings` | Settings | WUI-011 |
| `/audit` | Audit log | MCP-031 |

### 3.2 Persistent status bar

Always visible, at the top. This is where the system tells the truth about its own state, and it exists because most of this tool's failure modes are invisible in the page being debugged.

| Element | Source | Behavior |
|---|---|---|
| Connection state | `GET /state/health`, SSE liveness | Three states: connected, reconnecting, **disconnected**. Disconnected is unmistakable — a banner, not a subtle dot (REQ WUI-013) |
| Proxy on/off | `state.proxy.running` | Toggle |
| Active profile | `state.active_profile` | Dropdown, switches via `POST /profiles/{name}/activate` |
| **Dev toggle indicator** | `state.dev_toggles` | Prominent, high-contrast warning whenever `anticache` or `anticomp` is on (REQ WUI-012, PXY-044). Names which toggles and offers one-click off |
| Module health | `state.modules` | Badge when any module is in `load_error` or `quarantined`; click goes to that module |
| Recording indicator | `state.capture.recording_session` | Red dot with elapsed time and flow count when recording; click stops |
| MCP activity | `state.clients.mcp_connected` | Indicator when an MCP client is connected, with a popover listing what it changed this session (REQ MCP-033) |

### 3.3 Disconnected behavior (REQ WUI-013)

When the daemon is unreachable the UI must not render an empty table that looks like quiet traffic. It shows an explicit disconnected state naming the probable causes (daemon stopped, port changed, token invalid) with a retry control and the exact CLI command to check (`pporlock status`).

The distinction between "no flows because nothing is happening" and "no flows because we are not connected" must never require inference.

---

## 4. Data layer

### 4.1 API client

Generated types plus a thin typed client covering SPEC-0 §6. One module, `src/api/`, and nothing else in the app issues fetches.

```ts
interface ApiClient {
  health(): Promise<Health>;
  getState(): Promise<State>;
  setState(patch: StatePatch): Promise<State>;

  listFlows(filter: FlowFilter, opts: PageOpts): Promise<FlowPage>;
  getFlow(id: string, detail?: DetailLevel): Promise<FlowRecord>;
  unmask(id: string, fieldPath: string): Promise<string>;
  clearFlows(): Promise<void>;
  suggestRule(id: string, intent: RuleIntent): Promise<string>;

  listModules(): Promise<ModuleStatus[]>;
  getModule(name: string): Promise<ModuleDetail>;
  createModule(name: string, files: ModuleFiles): Promise<ModuleStatus>;
  updateModule(name: string, files: ModuleFiles): Promise<ModuleStatus>;
  patchModule(name: string, patch: {enabled?: boolean; priority?: number}): Promise<ModuleStatus>;
  deleteModule(name: string): Promise<void>;
  reloadModules(): Promise<ReloadResult>;
  validateModule(files: ModuleFiles): Promise<ValidationResult>;

  listProfiles(): Promise<Profile[]>;
  saveProfile(p: Profile): Promise<Profile>;
  activateProfile(name: string): Promise<State>;
  deleteProfile(name: string): Promise<void>;

  listSessions(): Promise<SessionMeta[]>;
  startRecording(name: string): Promise<SessionMeta>;
  stopRecording(id: string): Promise<SessionMeta>;
  listSessionFlows(id: string, filter: FlowFilter, opts: PageOpts): Promise<FlowPage>;
  dryRun(id: string, req: DryRunRequest): Promise<DryRunResult>;
  exportSession(id: string, format: "har" | "pporlock"): Promise<Blob>;
  deleteSession(id: string): Promise<void>;

  getConfig(): Promise<Config>;
  putConfig(c: Config): Promise<Config>;
  getExclusions(): Promise<Exclusions>;
  putExclusions(e: Exclusions): Promise<Exclusions>;
  getMetrics(): Promise<Metrics>;
  getAudit(opts: PageOpts): Promise<AuditPage>;
}
```

Every mutating call sends `Authorization: Bearer <token>` and `X-Pporlock-Client: ui` (SPEC-0 §6.1).

### 4.2 Token acquisition

The web UI is served from the same origin as the API, so it obtains the token from a bootstrap endpoint on first load rather than pairing. If the token is missing or rejected, the UI shows a first-run screen with the `pporlock pair` instruction rather than failing silently.

### 4.3 Event stream

```ts
interface EventStream {
  connect(filter: EventFilter): void;
  disconnect(): void;
  on<T extends EventType>(type: T, handler: (e: EventOf<T>) => void): () => void;
  readonly state: "connecting" | "open" | "reconnecting" | "closed";
}
```

Handles SPEC-0 §7 in full:

- `EventSource` with automatic reconnect and `Last-Event-ID`.
- On `stream.gap`, refetch the current view rather than silently missing flows.
- `flow.updated` merges into the existing row — notably `tab_id` backfill (SPEC-0 §3.6), which arrives after `flow.completed`. **Rows must be keyed on `flow_id` and tolerate late field updates.**
- Backpressure: the UI buffers incoming `flow.completed` events and flushes on animation frame. A busy page load delivers hundreds of events per second and per-event React updates will not keep up.

---

## 5. Traffic view

### 5.1 Flow table (REQ WUI-003)

Virtualized. Columns, all sortable, with a persisted column set:

| Column | Notes |
|---|---|
| Time | Relative by default, absolute on hover |
| Method | |
| Host | |
| Path | Truncated left-to-right so the filename stays visible |
| Status | Colored by class; synthesized responses visually distinct from upstream ones |
| Type | Media type only |
| Size | Response body |
| Duration | With a `pporlock_ms` sub-bar so proxy overhead is visible per flow |
| Flags | Icon set: **blocked**, **modified**, **streamed**, **passthrough**, **has warning notes**, **has error notes**, **unattributed** |

The flags column is the density payoff — it is how a user scans a hundred flows for the one that went wrong.

### 5.2 Filters

The SPEC-0 §6.5 filter vocabulary exactly, no more and no less, so a filter set is transferable between the UI, the DevTools panel, and MCP. Presented as a filter bar plus quick chips for the common cases: **modified only**, **blocked only**, **has warnings**, **errors only**, **this tab**.

Filters are pushed to the server for both the REST query and the SSE subscription, so a narrow filter reduces event volume rather than merely hiding rows.

### 5.3 Live controls

Pause/resume (buffer events while paused, show a count of held rows, flush on resume), clear buffer, and a follow-tail toggle that disengages when the user scrolls up.

---

## 6. Flow detail (REQ WUI-004)

Opens as a side panel, not a route change, so the table context is retained.

### 6.1 Tabs

| Tab | Contents |
|---|---|
| **Overview** | Method, URL, status, timing breakdown, sizes, `dest`, tab, flags |
| **Request** | Headers table, body pretty-printed by type |
| **Response** | Headers table, body pretty-printed by type |
| **Provenance** | §6.3 — the reason this view exists |
| **Diff** | §6.4, present only when the body was transformed |
| **WebSocket** | Frame list when `kind: "websocket"` |

### 6.2 Body rendering

JSON pretty-printed and collapsible; HTML, CSS, and JavaScript syntax-highlighted (reuse Monaco in read-only mode, lazily); binary as a hex preview with size and hash. Truncated bodies are labelled with the original size, never silently shortened.

### 6.3 Provenance view — the primary debugging affordance

This is the most important screen in the application (REQ CAP-013, DOC-003). It renders SPEC-0 §4 in full:

- **Ordered timeline by phase** (SPEC-0 §4.2), each phase labelled, showing every module and rule that matched.
- Each entry shows module, rule name, action, **outcome** (SPEC-0 §4.3), duration, and the action-specific `detail` block expanded.
- Non-`applied` outcomes are visually distinct and always show their reason. `skipped_streamed`, `skipped_budget`, `skipped_disabled`, and `error` must be as prominent as successful applications — the whole point is to explain why something *didn't* happen.
- **Notes** (SPEC-0 §4.4) are rendered at the top, above the timeline, styled by severity. `error` and `warning` notes are never collapsed by default.
- Every module name links to `/modules/:name`; every rule links to its line in the module editor.
- `short_circuited_by` is called out explicitly, since "an earlier rule ate it" is the single most common confusion.

### 6.4 Diff view (REQ WUI-014, CAP-014)

Side-by-side or unified body diff for transformed flows, using Monaco's diff editor. Subject to the body size cap, with an explicit message when the diff is unavailable because the body was truncated or streamed.

### 6.5 Redaction handling (SPEC-0 §9)

Masked values render in the `«redacted:sha1=…,len=…»` format with distinct styling and an inline unmask control. Unmasking calls `GET /flows/{id}?unmask=…`, applies to one value at a time, and is available only for live ring-buffer flows — for session flows the control is absent with a tooltip explaining that session data is redacted at write time (REQ CAP-043, CAP-045).

Two masked values sharing a hash prefix are visually linked, so "is this the same token" is answerable without unmasking.

### 6.6 Actions from a flow

- **Create rule from this flow** (REQ WUI-008) — §7.4.
- **Exclude this host** (REQ PXY-016) — one click, appends to the exclusion list, confirms.
- **Copy as cURL**, **copy URL**, **copy flow ID**.

---

## 7. Module library and editor

### 7.1 Library (REQ WUI-005)

List with: name, version, enabled toggle, priority (drag-to-reorder writing back `priority`), state badge (`loaded` / `disabled` / `quarantined` / `load_error`), rule count, whether it has a Python tier, and live stats (flows matched, modified, errors, avg ms).

Load errors and quarantine reasons render **inline in the list**, expanded, with the traceback — not behind a click. A module that failed to load is the thing the user came to this page to find.

Actions: create, duplicate, delete, import, export (REQ MOD-006).

**Settings.** A module that declares `settings:` in its manifest (SPEC-0 §5.2.1) gets a gear on its row, opening a form rendered *from the declaration* — the UI knows six field types and nothing about any particular module. A module that declares nothing has no gear: one on every row would open an empty dialog for most of them, and that teaches people the control does nothing.

The form sends nothing until **Save**. A PATCH per keystroke would send whatever a half-typed field held to a module that is modifying live traffic. On save it sends only the fields that differ from their effective default — sending everything would freeze today's defaults into the user's state, so a later version of the module that improved one would never reach anyone who had opened the dialog once. **Reset to defaults** is the empty override map, which is the same mechanism.

A refused save keeps the form open with the daemon's message: the daemon writes nothing when it refuses, so closing would discard edits the user has the only copy of. The form states that settings are read by unsandboxed module code (REQ MOD-031) — a surface that changes what module code does is an authoring surface.

### 7.2 Editor (REQ WUI-006)

Monaco, two files in a tab pair:

- **`module.yaml`** — YAML mode with the module manifest JSON Schema attached (SPEC-0 §5.2/§5.3), giving completion, hover documentation, and inline validation for rule structure, action names, match criteria, and transform parameters. The schema is the same artifact the daemon validates against, so the editor cannot disagree with the loader.
- **`module.py`** — Python mode with syntax highlighting. Syntax errors surface from `POST /validate`, since there is no in-browser Python parser.

Controls: **Save**, **Save and reload** (single action, REQ WUI-006), **Validate** (calls `POST /validate`, installs nothing), and **Dry run** (jumps to §8.3 pre-populated with this module).

Validation results render as Monaco markers at the reported line, with the daemon's error `code` and message.

Unsaved-changes protection on navigation.

### 7.3 Rule builder (REQ WUI-007)

A form path for users who do not want to hand-write YAML. Fields mirror SPEC-0 §5.3 exactly: match criteria on one side, action and action-specific parameters on the other.

**Requirement:** the builder emits YAML into the same `module.yaml` the editor shows, and the two views stay synchronized. It is a generator for the canonical format, not a parallel representation. Round-tripping through the builder must not reformat or reorder unrelated rules.

The builder shows a live preview of the emitted YAML.

### 7.4 Create rule from flow (REQ WUI-008)

From any flow, an intent picker (**block**, **map local**, **redirect**, **edit headers**) calls `POST /flows/{id}/suggest-rule` and opens the rule builder pre-populated with match criteria derived from that flow — host, path, method, `dest`.

The user then chooses a destination: an existing module, or a new one. The resulting module is created **disabled**, matching the daemon's rule that creation never enables (REQ MCP-030); the UI presents enabling as a separate, deliberate step.

This is the shortest path from "that request broke my page" to "I have a rule for it," and it should be reachable in two clicks from the flow table.

---

## 8. Profiles and sessions

### 8.1 Profiles (REQ WUI-009)

List, create, duplicate, rename, delete (`default` is not deletable), and activate. Membership editing is a two-pane include/exclude against the module library, showing effective ordering by priority so the user can see what will run in what order.

Profile-scoped dev toggles and exclusion additions (REQ MOD-044) are edited here, with a warning that activating a profile carrying `anticomp` will change traffic behaviour.

### 8.2 Sessions (REQ WUI-010)

- Start/stop recording with a name; the status bar carries the live indicator (§3.2).
- Session list: name, started, stopped, flow count, size on disk, the profile that was active, whether flows were dropped by writer overflow.
- Browse a session's flows using the same table and detail components as the live view — one implementation, differing only in data source and the absence of unmasking.
- Export to HAR or native format (REQ CAP-024), with a note that HAR cannot represent provenance.
- Delete, with confirmation showing size reclaimed.

### 8.3 Dry run (REQ WUI-010, CAP-030)

The dry-run screen takes a session plus a candidate module (from the editor's buffer, or an installed module, or a whole profile) and renders `POST /sessions/{id}/dryrun`:

- **Summary band**: flows evaluated, matched, modified, blocked, errored, avg and p95 ms (REQ CAP-033).
- **Result list**: one row per affected flow, with its provenance and diff, using the same provenance and diff components as §6.3/§6.4.
- **Unaffected flows are collapsed** by default but countable, because "my rule matched nothing" is the most common dry-run outcome and must be obvious.
- Long runs stream progress from the event stream.

A prominent, permanent note states that dry run **executes the candidate module's Python code** (REQ CAP-032). This matters most when the candidate was authored by an AI agent.

---

## 9. Settings (REQ WUI-011)

| Section | Contents |
|---|---|
| Exclusion list | Editable list with per-entry comments preserved; add/remove; a warning that removing a pinning or financial host may break that site or leak sensitive traffic into the capture buffer |
| Development toggles | `anticache`, `anticomp`, each with an explanation of what it changes and why it must be off for normal use; the status-bar indicator is not optional (REQ WUI-012) |
| Redaction | Header pattern list and JSON key pattern list, both editable, with the effective configuration displayed (REQ CAP-044) and an example of the mask format |
| Buffering | Size threshold and content-type allowlist, with an explanation that anything outside them is streamed and therefore not transformable |
| Capture | Ring buffer flow and byte caps, per-body cap, session size cap |
| Budget | Per-flow transform budget, executor threshold and worker count |
| Ports and logging | Proxy port, control port (both loopback-locked), log level |
| Metrics | Throughput, latency percentiles, per-module cost (REQ PRF-007), attribution coverage |

Every setting shows its default and marks non-default values, so "what did I change" is answerable.

---

## 10. Audit view

Renders `GET /audit` (REQ MCP-031): timestamp, actor origin (`ui` / `extension` / `mcp` / `cli`), and the change. Filterable by origin, which is how "what did the MCP client do" gets answered.

---

## 11. Accessibility and layout (REQ WUI-015)

- Usable at 1280×800; the flow table and detail panel must both be workable at that width.
- WCAG 2.1 AA contrast, including for the status-bar warning states, which must not rely on color alone — dev-toggle and error indicators carry an icon and text.
- Full keyboard navigation: table row navigation, detail panel tabs, filter focus, editor save.
- Reduced-motion respected for the live table's insertion animations.
- Dark and light themes both supported, with the flow-flag icon set legible in each.

---

## 12. Performance

| Constraint | Requirement |
|---|---|
| Flow table | Virtualized; sustained 200+ events/sec during a page load without dropped frames (REQ PRF-004) |
| Event handling | Buffer and flush on animation frame; never one render per event |
| Monaco | Lazily loaded; the traffic view must not pay its bundle cost |
| Memory | The UI holds at most the server's ring buffer size; older rows are dropped from the client cache, not accumulated |
| Initial load | Interactive within 1 s on loopback |

---

## 13. Test plan

| Layer | Coverage |
|---|---|
| Unit | Filter serialization round-trip against SPEC-0 §6.5; provenance rendering for every outcome and note code in SPEC-0 §4.3/§4.4; mask format detection and rendering; rule-builder YAML emission round-trip |
| Component | Flow table virtualization under synthetic event load; detail panel tab behaviour; editor validation marker placement |
| Contract | API client typed against `contracts/generated/types.ts`; a test asserts the client covers every route in `contracts/openapi.yaml` |
| Integration | Against a running daemon: live flow arrival via SSE, `flow.updated` backfill merging, `stream.gap` refetch, module create/validate/dry-run/enable loop |
| Degradation | Daemon killed mid-session produces the disconnected banner, not an empty table (REQ WUI-013) |
| Accessibility | Automated contrast and keyboard-navigation checks on every view |

---

## 14. Build order

| Step | Deliverable | Gate |
|---|---|---|
| 1 | Shell, API client, event stream, status bar | Connects to the daemon, shows state, survives disconnect |
| 2 | Flow table with SSE | **v0.1 exit criterion 3**: live flow table while browsing |
| 3 | Flow detail with provenance view | Provenance renders every outcome and note code |
| 4 | Module library + Monaco editor + validation | v0.4 |
| 5 | Rule builder + create-rule-from-flow | v0.4 |
| 6 | Profiles | v0.4 |
| 7 | Sessions browser + dry run | v0.5 |
| 8 | Settings, metrics, audit | v1.0 |
| 9 | Accessibility and performance pass | v1.0 |
