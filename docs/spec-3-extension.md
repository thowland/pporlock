# SPEC-3 — Chrome Extension

**Version:** 1.0
**Status:** Draft for development
**Date:** 2026-08-27
**Traces:** `pporlock_requirements-v1.md`
**Depends on:** SPEC-0 (Shared Contracts) — data model §3, tab attribution §3.6, provenance §4, API §6, events §7, redaction §9
**Independent of:** SPEC-1 internals, SPEC-2

---

## 1. Scope

Deliverable **D3**: a Manifest V3 Chrome extension providing proxy control, at-a-glance status, per-tab debugging, and in-page warnings.

The extension owns two things nothing else can do: **configuring Chrome's proxy** and **knowing which tab a request came from**. Everything else it displays is the daemon's data, reached through SPEC-0 §6 and §7.

**Safety obligation.** The extension is the only component that can leave the browser pointed at a dead proxy. §4.4 is therefore the most important section in this document.

---

## 2. Stack and structure

### 2.1 Toolchain

| Concern | Decision |
|---|---|
| Manifest | MV3 |
| Build | Vite + CRXJS |
| Language | TypeScript strict |
| UI | React 18 for popup, options, and DevTools panel |
| Types | Generated from `contracts/schemas/` (SPEC-0 §1.1). Never hand-written |
| Output | `make extension` → `extension/dist/`, loadable unpacked (REQ EXT-024) |

Chrome Web Store distribution is out of scope for v1.

### 2.2 Layout

```
extension/src/
  manifest.config.ts
  background/
    index.ts            # service worker entry
    proxy.ts            # §4 — chrome.proxy control
    health.ts           # §4.4 — daemon liveness and fail-safe revert
    attribution.ts      # §6 — webRequest → tab_id correlation
    counters.ts         # §5.2 — per-tab counters
    events.ts           # §3.3 — SSE client
    api.ts              # §3.2 — control API client
    pairing.ts          # §3.4
    state.ts            # single source of truth, persisted
  popup/                # §5.1
  devtools/
    devtools.ts         # panel registration
    panel/              # §7
  content/
    banner.ts           # §8 — in-page modification warning
  options/              # §9
  shared/               # types, formatting, filter helpers
```

### 2.3 Permissions (REQ EXT-001)

```jsonc
{
  "permissions": ["proxy", "storage", "tabs", "webRequest", "alarms"],
  "host_permissions": ["http://127.0.0.1:8081/*"],
  "content_scripts": [{ "matches": ["<all_urls>"], "js": ["content/banner.js"],
                        "run_at": "document_idle" }],
  "devtools_page": "devtools.html",
  "action": { "default_popup": "popup.html" }
}
```

Justification, because each is a real cost:

- `proxy` — the extension's core function.
- `webRequest` (observation only, no blocking) — tab attribution (§6). No `webRequestBlocking`; interception is the daemon's job.
- `tabs` — mapping tab IDs to URLs for the popup and panel.
- `alarms` — the health check heartbeat, which must survive service-worker suspension.
- `host_permissions` limited to the control origin. **No broad host permissions.**
- The content script matches `<all_urls>` because a modification warning must be able to appear on any page. It is inert unless the daemon reports warning-level notes for that document (§8), and it can be disabled entirely in options.

---

## 3. Background service worker

### 3.1 Lifecycle

MV3 service workers are terminated aggressively. The design consequence is that **no state lives only in the worker's memory**.

- Durable state (proxy on/off, token, paired status, active profile, settings) lives in `chrome.storage.local` and is read on every wake.
- Ephemeral state (per-tab counters, attribution buffer) lives in `chrome.storage.session`, which survives worker restarts but not browser restarts — the correct lifetime for both.
- A `chrome.alarms` heartbeat (30 s) wakes the worker for the health check (§4.4), because a suspended worker cannot notice that the daemon died.
- The SSE connection (§3.3) is re-established on every worker wake. Reconnection uses `Last-Event-ID`, and a `stream.gap` triggers a counter refetch rather than leaving badges wrong.

```ts
interface BackgroundState {
  proxyEnabled: boolean;
  daemonReachable: boolean;
  paired: boolean;
  token: string | null;
  activeProfile: string | null;
  devToggles: { anticache: boolean; anticomp: boolean };
  moduleHealth: { errors: number; quarantined: number };
  recordingSession: string | null;
  lastError: ExtError | null;
}
```

### 3.2 API client

A subset of SPEC-0 §6, only what the extension needs:

```ts
interface ExtApi {
  health(): Promise<Health>;                                  // unauthenticated
  pair(code: string): Promise<{ token: string }>;
  getState(): Promise<State>;
  setDevToggle(patch: Partial<DevToggles>): Promise<State>;
  listProfiles(): Promise<Profile[]>;
  activateProfile(name: string): Promise<State>;
  listFlows(f: FlowFilter, o: PageOpts): Promise<FlowPage>;
  getFlow(id: string, detail?: DetailLevel): Promise<FlowRecord>;
  startRecording(name: string): Promise<SessionMeta>;
  stopRecording(id: string): Promise<SessionMeta>;
  putExclusions(e: Exclusions): Promise<Exclusions>;
  submitAttribution(entries: AttributionEntry[]): Promise<{ accepted: number }>;
}
```

Mutating calls send `Authorization: Bearer <token>` and `X-Pporlock-Client: extension` (SPEC-0 §6.1). **The extension never unmasks** — the unmask endpoint is not in this interface (REQ CAP-043).

### 3.3 Event stream

Subscribes to `GET /events` with a server-side filter (SPEC-0 §7.1), consuming:

| Event | Use |
|---|---|
| `flow.completed` | Increment per-tab counters (§5.2); feed the DevTools panel |
| `flow.updated` | Tab-ID backfill — **re-attribute the counter** if the flow was previously unattributed |
| `state.changed` | Update profile, dev toggles, recording indicator |
| `module.error`, `module.quarantined` | Module health badge |
| `stream.gap` | Refetch counters for open tabs |

Counter deltas are derived from `flow.completed`, never pushed separately (SPEC-0 §7.3) — one source of truth shared with the web UI.

### 3.4 Pairing (REQ EXT-022, API-012)

The extension has no filesystem access and must never be given any. Pairing:

1. User runs `pporlock pair` or clicks Pair in the web UI, opening a 120-second window and displaying a code.
2. The extension's options page (or a first-run popup) accepts the code and calls `POST /pair` from its `chrome-extension://` origin.
3. The daemon returns the bearer token and records the extension ID as the only accepted extension origin thereafter.
4. The token is stored in `chrome.storage.local`.

Unpaired state is explicit in the popup with a link to the instructions. A `401` on any call clears `paired` and returns to that state rather than retrying silently.

---

## 4. Proxy control

### 4.1 Configuration

```ts
interface ProxyController {
  enable(mode: "fixed" | "pac"): Promise<void>;
  disable(): Promise<void>;
  current(): Promise<ProxyState>;
  isControllable(): Promise<boolean>;   // levelOfControl check
}
```

Fixed-server mode (REQ EXT-002):

```ts
{
  mode: "fixed_servers",
  rules: {
    singleProxy: { scheme: "http", host: "127.0.0.1", port: 8080 },
    bypassList: ["127.0.0.1", "localhost", "<local>", "[::1]"]
  }
}
```

The bypass list **must** include the control port's host, or the extension's own API calls route through the proxy.

Scope is `regular`, set via `chrome.proxy.settings.set()`. Only Chrome traffic is affected; the daemon never touches macOS system proxy settings (REQ SCP-001/002).

### 4.2 PAC mode (REQ EXT-003)

An optional PAC script for browser-side per-host scoping, generated from a host include/exclude list held in options. Useful when the user wants only certain sites proxied at all. Off by default; fixed-server is the normal path.

### 4.3 Control conflicts

`chrome.proxy.settings.get()` reports `levelOfControl`. When another extension or a policy controls the proxy, pporlock cannot set it. The extension detects this, refuses to show an enabled state it does not own, and says plainly in the popup which condition applies (`controlled_by_other_extensions` or `controlled_by_policy`).

### 4.4 Fail-safe (REQ EXT-010, PXY-008)

**This is the section that prevents the worst failure in the system: the daemon dies and the browser silently loses all network access.**

```ts
interface HealthMonitor {
  start(): void;
  stop(): void;
  check(): Promise<boolean>;
  readonly consecutiveFailures: number;
}
```

Rules:

1. While the proxy is enabled, poll `GET /state/health` every 10 seconds, driven by a `chrome.alarms` heartbeat so a suspended worker still checks.
2. On **two consecutive failures**, immediately call `ProxyController.disable()`, clearing Chrome's proxy configuration.
3. Set an unmistakable error state: red badge, error text in the popup, and a notification. The user must understand that pporlock turned itself off and why — not discover it by finding the internet broken.
4. Do **not** auto-re-enable when the daemon returns. Re-enabling is a deliberate user action, because a daemon that crashed once may crash again mid-page-load.
5. The health check itself must never route through the proxy (§4.1 bypass list), or a dead proxy makes the check fail in a way indistinguishable from a dead daemon.
6. `chrome.runtime.onSuspend` is best-effort only and is not relied upon; the heartbeat is the mechanism.

The daemon's complementary obligation is to fail closed — connection refused rather than a hang — so the check resolves fast (SPEC-1 §8.3).

---

## 5. Popup and badge

### 5.1 Popup (REQ EXT-011)

Compact, single screen, no scrolling in the normal case:

| Element | Behavior |
|---|---|
| **Proxy toggle** | On/off. Disabled with an explanation when unpaired, when the daemon is unreachable, or when another extension controls the proxy (§4.3) |
| **Daemon status** | Connected / unreachable / unpaired, with the reason |
| **Profile selector** | Lists profiles, activates on change (`POST /profiles/{name}/activate`) |
| **Current-tab summary** | Requests, blocked, modified, warnings — for the active tab |
| **Bypass this host** | One click; adds the current tab's host to the exclusion list, confirms, and offers undo |
| **Dev toggle indicator** | Prominent warning when `anticache` or `anticomp` is on, with one-click off (REQ PXY-044) |
| **Module health** | Warning when any module is in `load_error` or `quarantined`, linking into the web UI |
| **Recording** | Start/stop with elapsed time and flow count (REQ EXT-023, CAP-025) |
| **Open web UI** | Opens `http://127.0.0.1:8081` in a new tab |
| **Open DevTools panel** | Instruction, since an extension cannot open DevTools programmatically |

### 5.2 Badge (REQ EXT-012)

Per-tab, updated from `flow.completed` events and reset on navigation:

| State | Badge |
|---|---|
| Proxy off | Empty badge, muted icon |
| Proxy on, nothing acted on | Empty badge, active icon |
| Blocked and/or modified counts | Count text, neutral color |
| Any `warning`-severity note on this tab | Amber badge |
| Any `error`-severity note, module error, or quarantine | Red badge |
| Dev toggle active | Distinct color, applied to all tabs — a global condition, not a per-tab one |
| Daemon unreachable / fail-safe fired | Red badge with an error glyph |

Badge state must be readable without color alone where possible; the icon changes shape between off, on, and error states.

Counters are keyed by tab and stored in `chrome.storage.session`. Unattributed flows are counted separately and shown in the popup as an "unattributed" figure rather than being silently dropped or misattributed (SPEC-0 §3.6).

---

## 6. Tab attribution

Implements SPEC-0 §3.6. This is the extension's second irreplaceable function and one of the two v0.1 spikes (OI-2).

### 6.1 Primary mechanism

```ts
interface AttributionEntry {
  method: string;
  url: string;
  ts: string;        // ISO 8601
  tabId: number;
  frameId: number;
  type: string;      // chrome.webRequest ResourceType
}

interface Attributor {
  start(): void;
  stop(): void;
  readonly pending: number;
  readonly submitted: number;
}
```

- `chrome.webRequest.onBeforeRequest` (observe only, no blocking) records `(method, url, ts) → tabId`.
- Entries are buffered and POSTed to `/attribution` in batches (every 500 ms or 50 entries, whichever first) so the daemon is not hit per request.
- The daemon joins within a 5-second window and emits `flow.updated` for backfilled flows (SPEC-0 §7.3).
- The buffer is bounded; on overflow the oldest entries are dropped and a counter increments. Attribution is best-effort and must never be a source of memory growth or latency.

### 6.2 Measurement and the decision criterion

The extension reports its submitted count and the daemon reports coverage in `GET /metrics`. The OI-2 criterion is fixed: **if fewer than 95% of flows in a 30-minute reference browsing session are attributed, the primary mechanism is rejected** and the fallback is adopted.

### 6.3 Fallback

Per SPEC-0 §3.6, the fallback is either a PAC-scoped injected `x-pporlock-tab` header or `chrome.debugger`-based attribution. The switch is confined to `background/attribution.ts` and the daemon's `/attribution` route; no other interface changes, and neither the popup, the panel, nor the web UI is affected.

`chrome.debugger` carries a visible "DevTools is debugging this browser" bar and is therefore a last resort, not a preference.

### 6.4 Degradation

Every consumer of `tab_id` must work when it is `null`: the badge counts unattributed flows separately, the DevTools panel offers an "unattributed" bucket, and per-tab filtering degrades to a warning rather than an empty list (REQ EXT-012, EXT-013).

---

## 7. DevTools panel

Registered via `devtools.html`; panel title "pporlock" (REQ EXT-013). This is the doc's designated primary debugging affordance and is not optional.

### 7.1 Content

Scoped to `chrome.devtools.inspectedWindow.tabId` by default:

- **Live flow list** for this tab, fed by the SSE stream filtered server-side on `tab_id`, with the same columns and flag icon set as SPEC-2 §5.1 so the two views are learnable as one.
- **Filters** from the SPEC-0 §6.5 vocabulary, with quick chips for **modified**, **blocked**, **has warnings**, **errors**.
- **Flow detail** on selection: overview, headers, body, and — the reason the panel exists — **provenance**.
- An **unattributed** toggle showing flows the daemon could not associate with a tab, so attribution gaps are visible rather than mysterious.

### 7.2 Provenance rendering

The panel renders SPEC-0 §4 to the same standard as SPEC-2 §6.3, within a narrower viewport:

- Notes first, styled by severity, never collapsed for `warning` and `error`.
- Ordered timeline by phase, showing module, rule, action, outcome, and duration.
- Non-`applied` outcomes visually distinct with their reason always visible.
- `short_circuited_by` called out explicitly.

Given the space constraint, the panel may collapse `applied` entries by default — but never `error`, `skipped_budget`, `skipped_streamed`, or any note above `info`.

### 7.3 Jump to module (REQ EXT-014)

Every module and rule reference links to the web UI at `/modules/:name`, opened in a new tab, deep-linked to the rule where possible. The panel does not embed an editor; authoring belongs in SPEC-2.

### 7.4 Panel lifecycle

The panel is created per inspected tab and may outlive service-worker restarts. It holds no authoritative state: on open, and after any `stream.gap`, it refetches from `GET /flows?tab_id=…` rather than assuming its buffer is complete.

### 7.5 Redaction

Masked values render in the SPEC-0 §9.1 format with distinct styling. **The panel offers no unmask control** — unmasking is web-UI only (REQ CAP-043).

---

## 8. In-page modification warnings

Implements REQ EXT-020. The justification is specific: stripping SRI, relaxing CSP, and injecting scripts weaken a page's own protections, and a tool that does so invisibly is a tool that will eventually surprise its user badly.

### 8.1 Trigger

The service worker watches `flow.completed` for the tab's **document** flow. If its provenance carries any note of severity `warning` or `error` — in practice `sri_stripped`, `csp_modified`, `script_injected`, `module_error`, `transform_budget_exceeded`, `dev_toggle_active` (SPEC-0 §4.4) — it messages the content script for that tab.

### 8.2 Banner

```ts
interface BannerPayload {
  flowId: string;
  notes: Array<{ code: string; severity: string; module: string; message: string }>;
  profile: string;
}
```

- Rendered in a **closed shadow root** with a high `z-index` and an all-properties reset, so page CSS cannot hide or restyle it and the banner cannot leak styles into the page.
- Names each modification and the module responsible — "`strip-sri` removed integrity attributes", "`relax-csp` removed Content-Security-Policy".
- Actions: **Dismiss**, **Don't warn for this host**, **Open details** (opens the DevTools panel instruction, or the web UI flow detail).
- Injects nothing else into the page, listens to no page events, and never reads page content.

### 8.3 Suppression (REQ EXT-021)

Per-host and global suppression, stored in `chrome.storage.local`, with the current suppression list visible and clearable in options. A suppressed host still shows the warning in the badge and panel — suppression silences the banner, not the fact.

---

## 9. Options page

| Section | Contents |
|---|---|
| Pairing | Status, pair with a code, unpair |
| Connection | Control server host and port (loopback-locked), health-check interval |
| Proxy | Fixed-server or PAC mode; PAC include/exclude host lists |
| Warnings | Banner on/off, suppression list with clear-all |
| Badge | What to count (blocked, modified, warnings), reset-on-navigation behavior |
| Attribution | Diagnostics: submitted count, daemon-reported coverage, and the 95% criterion (§6.2) |
| Diagnostics | Recent extension errors, last fail-safe event with timestamp and reason |

---

## 10. Error handling

```ts
type ExtErrorCode =
  | "daemon_unreachable"      // popup + badge, triggers fail-safe (§4.4)
  | "unpaired"                // popup prompts pairing
  | "token_rejected"          // clears paired state, prompts re-pair
  | "proxy_not_controllable"  // §4.3, names the controlling entity
  | "proxy_set_failed"
  | "attribution_overflow"    // options diagnostics only; not user-facing
  | "sse_disconnected";       // transient; visible only if sustained
```

Every code maps to a specific popup message and badge state. No silent failures: the extension's whole purpose is being the thing that tells you the state of a system you cannot otherwise see.

---

## 11. Test plan

| Layer | Coverage |
|---|---|
| Unit | Proxy config generation including the bypass list; badge state machine for every condition in §5.2; attribution buffering, batching, and overflow; note-severity → banner-trigger mapping |
| Fail-safe | **Daemon killed while proxy enabled → proxy configuration cleared within two health-check intervals, error state shown, no auto-re-enable.** This test is mandatory and gates release (REQ EXT-010) |
| Service worker | State survives worker suspension and restart; alarms fire the health check while suspended; SSE reconnects with `Last-Event-ID`; `stream.gap` triggers counter refetch |
| Contract | Types generated from `contracts/`; a test asserts the client covers only routes present in `contracts/openapi.yaml` |
| Integration (Playwright, REQ TST-006) | Load unpacked, pair, toggle proxy, browse a fixture site, assert badge counts, open the DevTools panel, assert provenance rendering, assert the banner appears on a CSP-modified document |
| Attribution measurement | The §6.2 30-minute reference session, reporting coverage against the 95% criterion |
| Degradation | Every `tab_id: null` path renders correctly |

---

## 12. Build order

| Step | Deliverable | Gate |
|---|---|---|
| 1 | Service worker skeleton, state persistence, API client, health monitor | Health check runs while suspended |
| 2 | Proxy controller + popup toggle + badge | **v0.1 exit criterion 4**: proxy on/off from the popup, badge increments on block |
| 3 | **Fail-safe (§4.4)** | Mandatory before any extended use — build this immediately after step 2, not later |
| 4 | Pairing flow | Token obtained without filesystem access |
| 5 | Attribution + measurement | OI-2 decision criterion met or fallback adopted (v0.1) |
| 6 | SSE client, per-tab counters | Badge accurate under load |
| 7 | DevTools panel with provenance | v0.2–v0.4 |
| 8 | In-page banner + suppression | v1.0 |
| 9 | Options page, diagnostics | v1.0 |
