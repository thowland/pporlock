# SPEC-0 — Shared Contracts

**Version:** 1.0
**Status:** Draft for development
**Date:** 2026-08-27
**Traces:** `pporlock_requirements-v1.md`
**Consumed by:** SPEC-1 (Daemon + MCP), SPEC-2 (Web UI), SPEC-3 (Chrome Extension)

---

## 0. Purpose

This document is the single source of truth for everything that crosses a component boundary: the flow data model, the provenance model, the rule and module schemas, the control API surface, the SSE event shapes, and the module API stability contract.

The other three specifications reference this one by section number rather than restating it. When a shape here changes, all three change together.

**Rule for implementers:** if you need to invent a field, add it here first.

---

## 1. Repository layout

```
pporlock/
  daemon/                    # Python package, uv-managed
    pyproject.toml
    src/pporlock/
      addon/                 # mitmproxy-facing adapter (SPEC-1 §3)
      engine/                # pure rules + module engine, no mitmproxy imports
      capture/               # ring buffer, sessions, redaction
      control/               # asyncio HTTP control server
      cli/
    tests/
  mcp/                       # Python package, MCP stdio server (SPEC-1 §10)
    pyproject.toml
    src/pporlock_mcp/
  web/                       # React + Vite SPA (SPEC-2)
    package.json
    src/
  extension/                 # MV3, Vite + CRXJS + React (SPEC-3)
    package.json
    src/
  contracts/                 # THE ARTIFACTS DEFINED BY THIS DOCUMENT
    openapi.yaml
    schemas/
      module-manifest.schema.json
      rule.schema.json
      flow.schema.json
      provenance.schema.json
      events.schema.json
    generated/               # build output, gitignored
      types.ts               # TS types generated from the schemas
  stubs/                     # shipped script stub library (REQ PXY-033)
  docs/
  Makefile
```

### 1.1 Build contract

| Target | Command | Produces |
|---|---|---|
| Daemon | `make daemon` | installable Python package |
| Contracts | `make contracts` | validates schemas, generates `contracts/generated/types.ts` |
| Web UI | `make web` | static assets in `web/dist/`, copied into the daemon package at package time |
| Extension | `make extension` | unpacked MV3 bundle in `extension/dist/` |
| All | `make all` | contracts → daemon, web, extension |
| Test | `make test` | daemon pytest, contract validation, web/extension vitest |

`make contracts` MUST run before `make web` and `make extension`. TypeScript types are **generated**, never hand-written (REQ MOD-015).

### 1.2 Toolchain

| Component | Toolchain |
|---|---|
| Daemon, MCP | Python 3.12, `uv` with locked `pyproject.toml`, `pytest`, `ruff`, `mypy --strict` on `engine/` |
| Web UI | Node 20+, Vite, React 18, TypeScript, Monaco. State management, styling, and table library are **implementer's choice** (SPEC-2 §2.2) |
| Extension | Node 20+, Vite + CRXJS, React 18, TypeScript |
| Contracts | JSON Schema draft 2020-12; `json-schema-to-typescript` for generation |

mitmproxy is pinned exactly (REQ PXY-006). The pinned version is recorded in `daemon/pyproject.toml` and referenced in SPEC-1 §2.1.

---

## 2. Identifiers, units, and conventions

| Concept | Representation |
|---|---|
| Flow ID | `str`, ULID. Monotonic, sortable, generated at request start. |
| Timestamps | ISO 8601 with milliseconds, UTC, e.g. `2026-08-27T14:03:22.417Z`. Wire format is always string. |
| Durations | Milliseconds, `float`. |
| Sizes | Bytes, `int`. |
| Header names | Lowercased on the wire in all pporlock structures; original casing preserved only inside the mitmproxy adapter. |
| Header collections | List of `[name, value]` pairs, not a map — headers repeat. |
| Module name | Slug: `^[a-z0-9][a-z0-9-]{0,62}$`. Unique across the library. |
| Profile name | Same slug rule. `default` is reserved. |
| Rule ID | `"{module_name}:{rule_index}"` where `rule_index` is the zero-based position in the module's `rules` array. Stable within a module version. |
| Session ID | `str`, ULID. |
| Enums | Lowercase snake_case strings on the wire. Never integers. |

**Nullability:** absent and null are distinct. Absent means "not applicable to this flow"; null means "applicable but unknown".

---

## 3. Flow data model

### 3.1 Normalized request

This is what the rules engine and module code see. It contains no mitmproxy types (REQ DD-2, MOD-021).

```python
@dataclass(frozen=True)
class NormalizedRequest:
    flow_id: str
    timestamp: str                       # ISO 8601
    scheme: str                          # "http" | "https"
    method: str                          # uppercased
    host: str                            # authority host, no port
    port: int
    path: str                            # path only, no query
    query: tuple[tuple[str, str], ...]   # ordered, repeats preserved
    url: str                             # full reconstructed URL
    http_version: str                    # "HTTP/1.1" | "HTTP/2"
    headers: tuple[tuple[str, str], ...] # lowercased names
    dest: str | None                     # Sec-Fetch-Dest, verbatim or None
    body: bytes | None                   # None if not buffered or absent
    body_truncated: bool
    tab_id: int | None                   # see §3.6
```

Helper accessors are defined on the dataclass and are the only supported way module code reads headers:

```python
def header(self, name: str) -> str | None: ...
def headers_all(self, name: str) -> list[str]: ...
def query_param(self, name: str) -> str | None: ...
@property
def content_type(self) -> str | None: ...   # media type only, no parameters
```

### 3.2 Normalized response

```python
@dataclass(frozen=True)
class NormalizedResponse:
    flow_id: str
    timestamp: str
    status: int
    reason: str
    http_version: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None                   # decoded; None if streamed
    body_truncated: bool
    streamed: bool                       # True when the buffering guard declined
    encoding: str | None                 # original Content-Encoding
```

Same helper accessors as §3.1, plus:

```python
@property
def text(self) -> str | None: ...        # decoded per charset, None if not text
```

### 3.3 Mutation objects

Module and rule code does not mutate the frozen normalized objects. It returns, or accumulates into, a mutable proposal:

```python
@dataclass
class RequestMutation:
    set_headers: dict[str, str]
    add_headers: list[tuple[str, str]]
    remove_headers: list[str]
    redirect: RedirectSpec | None
    short_circuit: SyntheticResponse | None
    body: bytes | None                   # None = unchanged

@dataclass
class ResponseMutation:
    set_headers: dict[str, str]
    add_headers: list[tuple[str, str]]
    remove_headers: list[str]
    status: int | None
    body: bytes | None                   # None = unchanged

@dataclass
class RedirectSpec:
    scheme: str | None
    host: str | None
    port: int | None
    path: str | None
    query: str | None

@dataclass
class SyntheticResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    origin: str                          # rule_id or module name that produced it
```

The adapter (SPEC-1 §3) is the only code that applies a mutation to a mitmproxy flow.

### 3.4 Flow record

The persisted and API-exposed representation. This is the shape in `GET /flows`, in session storage, in the SSE stream, and in MCP output.

```jsonc
{
  "flow_id": "01JB2K7Q9X4M8Z0V3T5R7W1Y2A",
  "kind": "http",                      // "http" | "websocket" | "passthrough"
  "started_at": "2026-08-27T14:03:22.417Z",
  "completed_at": "2026-08-27T14:03:22.694Z",
  "tab_id": 481,
  "request": {
    "method": "GET",
    "scheme": "https",
    "host": "cdn.example.com",
    "port": 443,
    "path": "/a/analytics.js",
    "query": [["v", "3"]],
    "url": "https://cdn.example.com/a/analytics.js?v=3",
    "http_version": "HTTP/2",
    "dest": "script",
    "headers": [["accept", "*/*"], ["cookie", "«redacted:sha1=a3f2,len=142»"]],
    "body_size": 0,
    "body": null,
    "body_truncated": false
  },
  "response": {
    "status": 200,
    "reason": "OK",
    "http_version": "HTTP/2",
    "headers": [["content-type", "application/javascript"]],
    "content_type": "application/javascript",
    "body_size": 48213,
    "body": "…",                        // base64 if binary, utf-8 string if text
    "body_encoding": "utf8",            // "utf8" | "base64"
    "body_truncated": false,
    "streamed": false
  },
  "timing": {
    "dns_ms": null,
    "connect_ms": 12.4,
    "request_ms": 1.1,
    "upstream_ms": 210.7,
    "response_ms": 3.2,
    "pporlock_ms": 4.8,                 // total time spent in our pipeline
    "total_ms": 277.0
  },
  "modified": true,
  "blocked": false,
  "provenance": { /* §4 */ },
  "redacted": true
}
```

**Field notes.**

- `body` is omitted entirely from list responses (`GET /flows`); it appears only in detail responses (`GET /flows/{id}`). See §6.3.
- `body_encoding` disambiguates: text bodies are sent as UTF-8 strings for readability, binary as base64.
- `modified` is true when any header or body mutation was applied. `blocked` is true when the
  client was **denied** the response it asked for — a `block` rule, by stub or by kill.
  It is deliberately *not* "short-circuited": `map_local` and `redirect` end request
  evaluation early too (REQ MOD-012) and both return a response the browser uses, so
  reporting them as blocked misdescribes a flow that succeeded. `short_circuit` names
  which of the three ended evaluation, and is null when none did (OI-26).
- `redacted` reports whether redaction was applied to this representation (REQ CAP-040).

### 3.5 WebSocket flows

`kind: "websocket"` records carry the handshake as `request`/`response` plus:

```jsonc
{
  "websocket": {
    "closed": false,
    "close_code": null,
    "message_count": 42,
    "messages": [
      {
        "index": 0,
        "timestamp": "2026-08-27T14:03:25.001Z",
        "direction": "outbound",       // "outbound" (client→server) | "inbound"
        "opcode": "text",              // "text" | "binary"
        "size": 218,
        "payload": "…",                // subject to size cap and redaction
        "payload_encoding": "utf8",
        "truncated": false
      }
    ]
  }
}
```

WebSocket frames are inspection-only in v1 (REQ PXY-051). `messages` is omitted from list responses.

### 3.6 Tab attribution

Tab attribution (REQ OI-2) resolves as follows. This is a contract because all three clients depend on it.

**Primary mechanism.** The extension's service worker observes `chrome.webRequest.onBeforeRequest` and maintains a correlation map keyed on `(method, url, approximate_timestamp)`, POSTing batched `(key → tab_id)` associations to `POST /attribution` (§6.10). The daemon joins these against flows in the ring buffer within a bounded time window (default 5 s) and backfills `tab_id`.

**Consequences that all specs must honor:**
- `tab_id` is `null` on first observation and MAY be backfilled later. Clients MUST tolerate a flow arriving with `tab_id: null` and being updated (see the `flow.updated` event, §7.3).
- Attribution is best-effort. Badge counts (REQ EXT-012) and DevTools per-tab filtering (REQ EXT-013) degrade to "unattributed" rather than failing.

**Fallback mechanism.** If the correlation proves unreliable in the v0.1 spike, the daemon injects a request header `x-pporlock-tab` via a PAC-script-scoped path, or the extension supplies attribution through `chrome.debugger`. The switch cost is confined to the extension's attribution module (SPEC-3 §6) and the daemon's `POST /attribution` handler; no other interface changes.

**Decision criterion:** if fewer than 95% of flows in a 30-minute reference browsing session are attributed, the primary mechanism is rejected.

---

## 4. Provenance model

Provenance is a structural, always-present output of the rules engine (REQ CAP-010). It is not logging. Every flow in every consumer carries it (REQ CAP-013).

### 4.1 Shape

```jsonc
{
  "profile": "ad-blocking",
  "evaluated_modules": ["block-vendors", "strip-sri", "inject-debug"],
  "entries": [
    {
      "seq": 0,
      "phase": "clienthello",           // §4.2
      "module": "block-vendors",
      "rule_id": "block-vendors:2",
      "rule_name": "block-analytics-vendor",
      "action": "block",
      "outcome": "applied",             // §4.3
      "detail": {
        "stub": "auto",
        "derived_from_dest": "script",
        "synthesized_status": 200,
        "synthesized_content_type": "application/javascript"
      },
      "duration_ms": 0.3
    }
  ],
  "notes": [ /* §4.4 */ ],
  "total_ms": 4.8,
  "short_circuited_by": "block-vendors:2"
}
```

### 4.2 Phases

Ordered, matching the fixed pipeline of REQ PXY-020:

| Phase | Meaning |
|---|---|
| `clienthello` | Passthrough / exclusion decision |
| `request_short_circuit` | `block`, `map_local`, `redirect` — first-match-wins |
| `request_headers` | Request header actions — all-match |
| `buffering_decision` | Stream-or-buffer guard at `responseheaders` |
| `response_headers` | Response header actions — all-match |
| `response_body` | Body transforms — all-match |
| `websocket` | WebSocket frame observation |

### 4.3 Outcomes

| Outcome | Meaning |
|---|---|
| `applied` | The action ran and changed the flow. |
| `no_change` | The action ran and was a no-op (e.g. a `remove_headers` for a header that was absent). |
| `skipped_streamed` | Skipped because the response was streamed (REQ PXY-022). |
| `skipped_budget` | Skipped because the per-flow time budget was exhausted (REQ PXY-026). |
| `skipped_short_circuit` | Not reached because an earlier rule short-circuited the flow. |
| `skipped_disabled` | The owning module is disabled or quarantined. |
| `error` | The action raised. `detail.error` carries type, message, and traceback. |

### 4.4 Notes — the silent-breakage record

`notes` records behaviour-changing conditions that are not rule actions (REQ CAP-012). Each note is:

```jsonc
{ "code": "csp_modified", "severity": "warning", "module": "relax-csp", "message": "…", "detail": {} }
```

Codes, all of which MUST be rendered by every client:

| Code | Severity | Emitted when |
|---|---|---|
| `response_streamed` | info | Buffering guard declined. `detail.reason` ∈ `size` \| `content_type`. |
| `transform_budget_exceeded` | warning | Per-flow budget hit; names the transform that was cut. |
| `module_quarantined` | error | A module was disabled mid-flight after N failures (REQ MOD-025). |
| `map_local_missing` | error | `map_local` target file absent (REQ PXY-034). |
| `csp_modified` | warning | CSP header rewritten or removed (REQ PXY-042). |
| `sri_stripped` | warning | `integrity`/`crossorigin` attributes removed (REQ PXY-040). |
| `script_injected` | warning | A script tag was injected; `detail.nonce_reused` bool (REQ PXY-041). |
| `dev_toggle_active` | warning | `anticache` or `anticomp` was in effect for this flow (REQ PXY-043/044). |
| `body_truncated` | info | Body exceeded the capture cap. |
| `module_error` | error | A Python hook raised (REQ MOD-024). |
| `passthrough_excluded` | info | Connection was tunneled undecrypted (REQ PXY-015). |
| `attribution_missing` | info | No tab could be associated. |

The `severity` values (`info`, `warning`, `error`) drive UI treatment uniformly across SPEC-2 and SPEC-3. `warning` and `error` notes on a document flow are what trigger the in-page banner (REQ EXT-020).

---

## 5. Rule and module schemas

Canonical JSON Schema lives in `contracts/schemas/`. What follows is the normative description; the schema files are generated from and validated against these definitions.

### 5.1 Module directory

```
~/.pporlock/modules/<module-name>/
  module.yaml        # manifest + declarative rules (required)
  module.py          # Python tier (optional)
  assets/            # stubs, map_local targets (optional)
```

### 5.2 Manifest

```yaml
name: block-vendors                 # slug, must equal directory name
version: 1.2.0                      # semver
pporlock_api: "1"                   # module API major version (§8)
description: Suppresses common analytics vendors
author: th
enabled: true
priority: 100                       # lower runs earlier; default 100
rules: []                           # §5.3
config: {}                          # free-form defaults, passed to ctx.config
settings: []                        # user-settable fields; §5.2.1
```

Validation is strict: unknown top-level keys are an error, not a warning (REQ MOD-014). A manifest declaring an unsupported `pporlock_api` refuses to load with a clear message (REQ MOD-026).

#### 5.2.1 Settings

`config` is the author's. `settings` declares which parts of it a *user* may change from the module library, without editing the file:

```yaml
settings:
  - key: identity                   # the ctx.config key this field sets
    label: Identify as
    type: enum                      # string | text | boolean | integer | enum | string_list
    description: Which crawler to present.
    default: googlebot
    options:                        # enum only; a bare string is value and label
      - { value: googlebot, label: Googlebot }
      - { value: claudebot, label: ClaudeBot }
  - key: hosts
    type: string_list
    default: ["*"]
  - key: repeats
    type: integer
    min: 1                          # integer only
    max: 9
```

Deliberately **not** JSON Schema. The expressive half of JSON Schema is unrenderable as a form, and a declaration that can say more than the UI can show is a declaration whose author will be surprised. Six types, no nesting, no conditionals.

- A malformed declaration is a **load error** (`module_invalid_settings`), reported the same way a bad rule is, including by `POST /validate` before anything is installed. This includes a `default` that its own field would reject.
- **Where values live.** A value the user sets is written to the module-state sidecar, never back into the manifest — the same rule that governs `enabled` (OI-8). The daemon does not rewrite a file it does not own.
- **What `ctx.config` holds** is the merge: each field's declared default, overlaid by the manifest's own `config` block, overlaid by what the user has set. A key the manifest states wins over the field's `default`, because that is what the module ships with; `GET /modules/{name}` therefore serves each field's `default` as the *effective* one, so a client can send only what the user actually changed.
- An override for a key the module no longer declares is ignored rather than passed through. The module has been rewritten since, and handing its code a value it never asked for is how a stale toggle survives a rename and quietly does nothing.
- **There is no secret type.** Values are stored and served in clear; a module needing a credential takes it from the environment. The absence of the type is the documentation of that.

Adding `settings` is a minor module-API change under §8.1: a module that declares none behaves exactly as it did before.

### 5.3 Rule

```yaml
- name: block-analytics-vendor       # required, unique within module
  enabled: true                      # default true
  match:
    host: "*.analytics-vendor.example"   # glob, case-insensitive
    path: "^/collect"                    # regex, compiled at load (REQ PXY-025)
    method: [GET, POST]                  # string or list
    dest: script                         # Sec-Fetch-Dest value or list
    query:
      tid: "^UA-"                        # key → regex
    request_headers:
      referer: "^https://target\\."      # key → regex; presence if value omitted
    status: [200, "300-399"]             # response-side only
    content_type: "text/html"            # response-side only; media type or regex
  action: block                          # §5.4
  # action-specific keys follow
```

**Match semantics.** All present criteria must match (AND). Absent criteria do not constrain. `host` globs match the full host, case-insensitively. `path` regexes are `re.search`, not `re.fullmatch` — anchor explicitly when you mean it. Response-side criteria (`status`, `content_type`) on a request-phase action are a load-time error.

### 5.4 Actions

| `action` | Additional keys | Phase |
|---|---|---|
| `passthrough` | — | `clienthello` |
| `block` | `mode` (`stub` \| `kill`, default `stub`), `stub` (`auto` \| named stub \| inline spec) | `request_short_circuit` |
| `map_local` | `file` (path, relative to module `assets/`), `content_type` (optional override), `status` (default 200) | `request_short_circuit` |
| `redirect` | `to` (RedirectSpec fields: `scheme`, `host`, `port`, `path`, `query`) | `request_short_circuit` |
| `headers` | `request: {add, remove, set}` and/or `response: {add, remove, set}` | `request_headers` / `response_headers` |
| `body` | `transform` (single) or `transforms` (list) — §5.5 | `response_body` |

**Evaluation semantics (REQ MOD-012), restated because it is the most error-prone part of the model:**

- `block`, `map_local`, `redirect` — **first match wins** across all enabled modules. Evaluation of this class stops at the first match.
- `headers`, `body` — **all matches apply**, ordered by module `priority` ascending, then declaration order within the module.
- `passthrough` — evaluated at ClientHello; a match tunnels the connection and no other phase runs.

### 5.5 Transform registry

Built-in transforms (REQ MOD-013), each with a validated parameter schema:

| Transform | Parameters | Notes |
|---|---|---|
| `strip_integrity_attributes` | — | Removes `integrity` and `crossorigin` from `<script>`/`<link>`. Emits `sri_stripped`. |
| `strip_csp` | `report_only` (bool, default true — also strip the report-only header) | Emits `csp_modified`. |
| `inject_script` | `src` \| `inline`, `position` (`head_start`\|`head_end`\|`body_end`), `reuse_nonce` (default true) | Emits `script_injected`. |
| `inject_style` | `href` \| `inline`, `position` | |
| `regex_sub` | `pattern`, `repl`, `count` (default 0 = all), `flags` | Pattern compiled at load. |
| `replace_literal` | `find`, `replace`, `count` | |
| `json_patch` | `ops` (RFC 6902 operation list) | Fails as `no_change` with an `error` note if body is not valid JSON. |

Transforms are named registry entries, never expressions embedded in YAML (REQ MOD-013). Modules extend the registry from the Python tier via `ctx.register_transform` (§8.2).

### 5.6 Stub specification

```yaml
stub: auto                          # derive from Sec-Fetch-Dest (REQ PXY-032)
# or
stub: gtm                           # named stub from the shipped library
# or
stub:
  status: 200
  content_type: application/javascript
  body: "window.analytics={track(){}};"
```

The `auto` derivation table is normative in REQ PXY-032 and is implemented once, in the daemon.

### 5.7 Profile

```yaml
name: ad-blocking
description: Everyday browsing
modules: [block-vendors, strip-sri]   # ordered set; ordering does not override priority
dev_toggles:                          # optional, REQ MOD-044
  anticache: false
  anticomp: false
exclusions_add: []                    # profile-scoped additions to the exclusion list
```

Stored at `~/.pporlock/profiles/<name>.yaml`. `default` always exists and is not deletable (REQ MOD-041).

---

## 6. Control API

Canonical OpenAPI at `contracts/openapi.yaml` (REQ API-029). Base URL `http://127.0.0.1:8081`.

### 6.1 Authentication and origin policy

- All mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) require `Authorization: Bearer <token>` (REQ API-011).
- All requests require an `Origin` header matching either the server's own origin or the paired `chrome-extension://<id>` origin; others are rejected `403` (REQ API-004, API-013).
- Mutating requests must carry `Content-Type: application/json` **and** `X-Pporlock-Client: <ui|extension|mcp|cli>`, which is a non-simple header and therefore forces a preflight, defeating form-based CSRF (REQ API-013).
- `GET /state/health` is the sole unauthenticated endpoint, returning only `{"ok": true, "version": "…"}`, so clients can detect a live daemon before pairing.

### 6.2 Errors

Uniform error body on every non-2xx:

```jsonc
{ "error": { "code": "module_load_failed", "message": "…", "detail": {}, "trace": "…" } }
```

`trace` is present only for `500`-class errors and only when the daemon runs with debug logging.

### 6.3 Representation levels

Flow endpoints take a `?detail=` parameter with three levels, because body payloads dominate response size:

| Level | Contains |
|---|---|
| `summary` (default for lists) | Everything in §3.4 except `request.body`, `response.body`, `websocket.messages`, and `provenance.entries` (a `provenance.summary` count object replaces them) |
| `full` (default for `GET /flows/{id}`) | Everything except bodies over the cap |
| `bodies` | `full` plus bodies up to the configured cap |

MCP tools default to `summary` and must opt in to `bodies` (REQ MCP-005).

### 6.4 State

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/state` | Daemon status, active profile, dev toggles, counters, module load errors, connected MCP clients |
| `POST` | `/state` | Set dev toggles, start/stop the proxy listener |
| `GET` | `/state/health` | Unauthenticated liveness |

`GET /state` response:

```jsonc
{
  "version": "0.1.0",
  "mitmproxy_version": "11.0.0",
  "proxy": { "running": true, "listen": "127.0.0.1:8080", "uptime_s": 4821 },
  "active_profile": "ad-blocking",
  "dev_toggles": { "anticache": false, "anticomp": false },
  "modules": { "loaded": 7, "enabled": 4, "quarantined": 1, "errors": [ /* §6.6 */ ] },
  "capture": { "ring_flows": 1841, "ring_bytes": 91234567, "recording_session": null },
  "counters": { "flows_total": 20114, "blocked": 3122, "modified": 894, "passthrough": 77 },
  "clients": { "mcp_connected": 1, "mcp_read_only": false }
}
```

### 6.5 Flows

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/flows` | Ring buffer, filtered and paginated |
| `GET` | `/flows/{flow_id}` | Single flow |
| `DELETE` | `/flows` | Clear the ring buffer |
| `POST` | `/flows/{flow_id}/suggest-rule` | Candidate rule for this flow (REQ WUI-008, MCP-014) |

`GET /flows` query parameters — the canonical filter vocabulary, identical in the UI, the DevTools panel, and MCP (REQ CAP-004):

`host`, `path`, `method`, `status`, `content_type`, `dest`, `tab_id`, `modified` (bool), `blocked` (bool), `module` (fired), `note_code`, `since`, `until`, `q` (substring over URL), `limit` (default 100, max 1000), `cursor`, `detail`.

Paginated responses:

```jsonc
{ "flows": [ /* … */ ], "next_cursor": "…", "total_estimate": 1841 }
```

### 6.6 Modules

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/modules` | List with status |
| `GET` | `/modules/{name}` | Manifest, parsed rules, Python source, assets listing |
| `POST` | `/modules` | Create (body: `{name, files: {"module.yaml": "...", "module.py": "..."}}`) |
| `PUT` | `/modules/{name}` | Replace files |
| `PATCH` | `/modules/{name}` | Set `enabled` or `priority` only |
| `DELETE` | `/modules/{name}` | Remove |
| `POST` | `/modules/reload` | Force reload of all modules |
| `POST` | `/modules/{name}/export` | Archive |
| `POST` | `/modules/import` | Archive |

Module status object:

```jsonc
{
  "name": "block-vendors",
  "version": "1.2.0",
  "enabled": true,
  "priority": 100,
  "state": "loaded",         // "loaded" | "disabled" | "quarantined" | "load_error"
  "has_python": true,
  "rule_count": 12,
  "error": null,             // {code, message, trace, line} when state is load_error
  "quarantine": null,        // {reason, failures, since} when quarantined
  "stats": { "flows_matched": 8123, "flows_modified": 190, "errors": 0, "avg_ms": 0.4 }
}
```

**Creating or updating a module never enables it** (REQ MCP-030). `enabled` transitions require `PATCH`.

### 6.7 Profiles

| Method | Path |
|---|---|
| `GET` | `/profiles` |
| `GET` | `/profiles/{name}` |
| `POST` | `/profiles` |
| `PUT` | `/profiles/{name}` |
| `DELETE` | `/profiles/{name}` |
| `POST` | `/profiles/{name}/activate` |

### 6.8 Sessions and dry run

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/sessions` | List |
| `POST` | `/sessions` | Start recording (`{name}`) |
| `POST` | `/sessions/{id}/stop` | Stop recording |
| `GET` | `/sessions/{id}` | Metadata |
| `GET` | `/sessions/{id}/flows` | Same filter vocabulary as §6.5 |
| `DELETE` | `/sessions/{id}` | Delete |
| `POST` | `/sessions/{id}/dryrun` | Evaluate candidate modules against the session |
| `GET` | `/sessions/{id}/export?format=har\|pporlock` | Export |

Dry run request:

```jsonc
{
  "modules": [ { "name": "candidate", "files": {"module.yaml": "…", "module.py": "…"} } ],
  "use_installed": ["strip-sri"],
  "profile": null,
  "limit": 500,
  "include_diffs": true
}
```

Dry run response (REQ CAP-030, CAP-033):

```jsonc
{
  "summary": { "flows_evaluated": 500, "matched": 63, "modified": 41,
               "blocked": 22, "errors": 0, "avg_ms": 0.9, "p95_ms": 3.1 },
  "results": [
    {
      "flow_id": "…",
      "url": "…",
      "provenance": { /* §4 */ },
      "diff": {
        "headers": [ { "op": "remove", "name": "content-security-policy" } ],
        "body": { "kind": "unified", "text": "@@ -1,4 +1,4 @@\n…", "truncated": false }
      }
    }
  ]
}
```

Dry run executes Python hooks (REQ CAP-032) and shares its code path with live evaluation (REQ CAP-031).

### 6.9 Configuration and exclusions

| Method | Path | Purpose |
|---|---|---|
| `GET`/`PUT` | `/exclusions` | ClientHello exclusion list |
| `GET`/`PUT` | `/config` | Buffering thresholds, ring caps, redaction patterns, ports, log level |
| `POST` | `/validate` | Validate a candidate module without installing (REQ API-027) |
| `GET` | `/metrics` | Throughput, latency percentiles, per-module cost (REQ API-028) |
| `GET` | `/audit` | Origin-tagged log of enable/activate/toggle events (REQ MCP-031) |

### 6.10 Extension support

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pair` | First-run pairing; issues the token to the extension (REQ API-012) |
| `POST` | `/attribution` | Batched `(request key → tab_id)` associations (§3.6) |

`POST /pair` is available only within a short window opened by `pporlock pair` or by a button in the web UI, and only from a `chrome-extension://` origin. The daemon records the paired extension ID and thereafter accepts that origin only.

---

## 7. Event stream

### 7.1 Transport

`GET /events` is a Server-Sent Events stream (REQ API-022). Query parameters filter the stream server-side: `tab_id`, `kinds` (comma-separated event types), and the flow filter vocabulary of §6.5.

Each SSE message uses the `event:` field for the type and a JSON `data:` payload. Every event carries `seq` (monotonic per connection) and `ts`.

### 7.2 Reconnection

Clients send `Last-Event-ID`; the server replays from the ring buffer where possible and otherwise emits a `stream.gap` event so the client knows to refetch rather than silently miss flows.

### 7.3 Event types

| Event | Payload | Emitted when |
|---|---|---|
| `flow.started` | `{flow_id, tab_id, method, url, dest}` | Request observed |
| `flow.completed` | Flow record at `summary` detail | Flow finished, provenance final |
| `flow.updated` | `{flow_id, changed: {...}}` | Backfill, notably `tab_id` (§3.6) |
| `websocket.message` | `{flow_id, message}` | Frame observed |
| `state.changed` | Partial `GET /state` body | Profile switch, toggle change, proxy start/stop |
| `module.error` | `{module, code, message, trace, flow_id?}` | Load failure or hook exception |
| `module.quarantined` | `{module, reason, failures}` | Auto-quarantine fired |
| `session.changed` | `{session_id, state}` | Recording started/stopped |
| `stream.gap` | `{from_seq, to_seq}` | Events were dropped |

Counter deltas for badge rendering are derived by the extension from `flow.completed`, not pushed separately — one source of truth.

---

## 8. Module API stability contract

Modules are user- and agent-authored code that must survive daemon upgrades (REQ MOD-026). This section is the contract.

### 8.1 Versioning

- The module API has a **major version only**, declared as `pporlock_api: "1"` in the manifest.
- Within a major version, the daemon guarantees: existing manifest keys keep their meaning, existing rule actions and match criteria keep their semantics, existing registry transforms keep their parameter names and behaviour, and the `ctx` surface in §8.2 is additive-only.
- New optional manifest keys, new match criteria, new actions, new transforms, and new `ctx` members are **minor changes** and do not bump the major version.
- Removing or repurposing anything is a **major change**. The daemon supports the current major version and the one before it; a module declaring an unsupported version refuses to load with a message naming the required daemon version.
- Deprecation path: a member marked deprecated in major version N emits a `module_deprecation` note on first use, continues working for all of N, and may be removed in N+1.

### 8.2 The `ctx` surface (v1)

Everything module code may rely on. Anything not listed here is private and may change without notice.

```python
class ModuleContext:
    # identity
    name: str
    version: str
    config: dict                        # the effective config: field defaults,
                                        # then the manifest `config` block,
                                        # then what the user set (§5.2.1).
                                        # Replaced in place when a setting
                                        # changes, so read it per flow rather
                                        # than caching it in on_load.
    profile: str                        # active profile name

    # matching helpers
    #
    # `request` is positional and required: the context is per-module and
    # long-lived, not per-flow, so it does not know which request you mean.
    def matches(self, request, *, host: str | None = None, path: str | None = None,
                method: str | None = None, dest: str | None = None,
                content_type: str | None = None,
                response=None) -> bool: ...

    # logging — module-scoped, structured, surfaces in the UI
    def log(self, level: str, message: str, **fields) -> None: ...

    # provenance note. `severity` defaults to "warning" and so follows the
    # message; a code outside the taxonomy becomes MODULE_ERROR carrying the
    # code you asked for, rather than raising.
    def note(self, code: str, message: str, severity: str = "warning", **detail) -> None: ...

    # module-scoped persistent key/value storage (REQ MOD-022)
    def store_get(self, key: str, default=None): ...
    def store_set(self, key: str, value) -> None: ...
    def store_delete(self, key: str) -> None: ...

    # asset resolution — confined to the module's assets/ directory, with
    # containment checked after symlink resolution
    def asset_path(self, relative: str) -> pathlib.Path: ...
    def asset_bytes(self, relative: str) -> bytes: ...
    def asset_text(self, relative: str) -> str: ...

    # registry extension. `cost` is what the scheduler needs in order to decide
    # what may run inline on the event loop, and defaults to "expensive"
    # because nothing is known about a module's transform.
    def register_transform(self, name: str, fn, cost: str = "expensive") -> None: ...

    # response construction
    def synthesize(self, *, status: int = 200, content_type: str | None = None,
                   body: bytes | str = b"") -> SyntheticResponse: ...

    # the same Sec-Fetch-Dest derivation the `block` action uses; `request` is
    # needed for the Accept-header fallback on insecure contexts
    def stub_for(self, dest: str | None, request) -> SyntheticResponse: ...
```

A module transform registered through `register_transform` has **no parameter schema**. Built-in transforms validate their parameters at rule-compile time; a module's does not, and is responsible for its own argument handling.

### 8.3 Hook signatures (v1)

```python
def on_load(ctx: ModuleContext) -> None: ...
def on_unload(ctx: ModuleContext) -> None: ...

# Called after a declared setting changes (§5.2.1), for a module that derives
# something from its config at load time. Optional: a module that reads
# ctx.config per flow needs nothing here. The module is NOT reloaded on a
# settings change — a reload would re-run on_load and discard whatever the
# module has accumulated.
def on_config(ctx: ModuleContext) -> None: ...

def on_request(req: NormalizedRequest, ctx: ModuleContext) -> RequestMutation | None: ...
def on_response(req: NormalizedRequest, resp: NormalizedResponse,
                ctx: ModuleContext) -> ResponseMutation | None: ...
def on_websocket_message(msg: WebSocketMessage, req: NormalizedRequest,
                         ctx: ModuleContext) -> None: ...   # read-only, REQ MOD-021/051
```

Returning `None` means no mutation. Raising is caught, logged, attributed, and does not affect flow delivery (REQ MOD-024).

`on_websocket_message` is **read-only**: any value it returns is ignored. Frames are inspection-only in v1 (REQ PXY-051), and a hook whose return value were quietly dropped while provenance reported a change would be worse than one that cannot change anything at all.

Mutations are `pporlock.engine.models.RequestMutation` and `ResponseMutation`. Both carry `set_headers`, `add_headers`, `remove_headers` and `body`; `RequestMutation` also carries `redirect` and `short_circuit` (a `SyntheticResponse`, as returned by `ctx.synthesize` or `ctx.stub_for`), and `ResponseMutation` also carries `status`.

A returned mutation is folded into the same mutation the declarative rules contribute to, rather than applied in a separate pass. That is what makes ordering between the two tiers meaningful (REQ MOD-023).

### 8.4 Trust

Module code is **fully trusted** (REQ MOD-030). No import allowlist, no sandbox, no resource jail. The enforced guardrails are exactly three: error isolation (§8.3), failure quarantine after N consecutive raises, and the per-flow transform time budget. Every authoring surface — the web UI editor, the MCP authoring tools, and the module documentation — must state this (REQ MOD-031).

---

## 9. Redaction contract

Redaction is applied at write time for sessions and at serialization time for API and MCP responses (REQ CAP-040, CAP-045).

### 9.1 Masked value format

A masked value is the literal string:

```
«redacted:sha1=<first 4 hex of SHA-1 of the value>,len=<original byte length>»
```

This preserves enough shape to compare two values without revealing either (REQ CAP-042). The format is fixed so all three clients can detect and render masked values consistently.

### 9.2 Default patterns

**Headers** (case-insensitive, exact or pattern): `cookie`, `set-cookie`, `authorization`, `proxy-authorization`, `x-api-key`, `x-auth-token`, plus any header matching the configurable pattern list.

**JSON body keys** (case-insensitive substring): `password`, `token`, `secret`, `api_key`, `apikey`, `access_token`, `refresh_token`, `session`, `auth`, `credential`.

Both lists are user-configurable via `PUT /config` and the effective configuration is visible in the UI (REQ CAP-044).

### 9.3 Unmasking

Unmasking is available only from the **live ring buffer**, only through the web UI, only on explicit per-value user action, via `GET /flows/{id}?unmask=<field_path>` with a bearer token. It is unavailable for session data and categorically unavailable through MCP (REQ CAP-043, MCP-003).

---

## 10. Cross-references

| Concern | Owning spec |
|---|---|
| Pipeline, engine, capture, control server, MCP server, CLI, launchd | SPEC-1 |
| Web UI screens, editor, dry-run UX, settings | SPEC-2 |
| Extension service worker, popup, badge, DevTools panel, in-page banner, attribution | SPEC-3 |
| Everything above | this document |
