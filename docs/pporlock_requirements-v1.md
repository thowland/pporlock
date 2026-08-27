# pporlock — Requirements Specification

**Version:** 1.0
**Status:** Approved for development
**Date:** 2026-08-27
**Supersedes/extends:** `pporlock_approach-rev1.md` (design rationale remains authoritative where this document does not contradict it)

---

## 1. Overview

### 1.1 What pporlock is

pporlock is a single-user, single-machine HTTPS interception and modification system for Chrome. It comprises four deliverables:

| # | Deliverable | Description |
|---|---|---|
| D1 | **Proxy daemon** | `mitmproxy`-based MITM proxy with a rules/module engine, capture store, and control API. Runs as a macOS launchd user agent. |
| D2 | **Web UI** | React SPA served by the daemon's control server. Live flow inspection, module authoring (declarative + Python), profile management. |
| D3 | **Chrome extension** | MV3 extension: proxy on/off, profile switching, badge counts, DevTools flow panel, in-page modification warnings. |
| D4 | **MCP server** | Model Context Protocol interface exposing traffic introspection, module authoring, dry-run validation, and daemon control to an AI coding agent. |

### 1.2 What pporlock is not

- Not multi-tenant, not multi-user, not network-accessible. All listeners bind loopback only.
- Not an authenticated system. Loopback binding plus a per-install token is the entire access control model (§7.2).
- Not a system-wide proxy. Only Chrome, configured via the extension (§4.1).
- Not a security sandbox for module code. Modules are trusted (§6.4).

### 1.3 Design decisions carried forward from the approach document

These are settled and not open for re-litigation during implementation:

- **DD-1** Modification and inspection are co-equal. The pipeline is designed for buffered modification first, with streaming as a relaxation.
- **DD-2** The rules engine imports nothing from mitmproxy. A `normalize()` adapter is the only boundary crossing.
- **DD-3** The control server shares the proxy's asyncio event loop. No IPC, no locking. Consequence: nothing expensive may run on that loop.
- **DD-4** Blocking synthesizes a benign response by default; `flow.kill()` is opt-in.
- **DD-5** Rule-fire provenance is part of the engine's return value from day one, not retrofitted.
- **DD-6** The mitmproxy version is pinned; upgrades are deliberate work against the adapter layer.

### 1.4 Glossary

| Term | Meaning |
|---|---|
| **Flow** | One request/response pair (or a WebSocket connection and its frames). |
| **Rule** | A single declarative match+action unit. |
| **Module** | A named, versioned bundle: a manifest, zero or more declarative rules, and optionally one Python file. The unit of enable/disable, authoring, and sharing. |
| **Profile** | A named set of enabled modules. Exactly one profile is active at a time. |
| **Transform** | A named function that mutates a body or headers. Built-in (registry) or module-provided. |
| **Session** | An explicitly-started on-disk recording of flows, replayable and dry-runnable. |
| **Provenance** | The ordered record of which modules/rules matched and what each did to a given flow. |

### 1.5 Requirement conventions

Requirements are `[COMPONENT]-nnn`. Priority is one of:

- **M** (Must) — required for v1.0.
- **S** (Should) — required for v1.0 unless it threatens the schedule.
- **C** (Could) — post-v1.0 backlog, designed for but not built.

---

## 2. Delivery plan

### 2.1 v0.1 — Thin vertical slice (integration risk retirement)

The first milestone proves every process boundary at once, because the integration risks (CA trust, QUIC bypass, Private Network Access, `chrome.proxy` behaviour, SSE through the extension) are the ones most likely to invalidate the architecture, and each is cheap to discover early and expensive to discover late.

**v0.1 scope:**
- `mitmdump` + addon, CA installed, QUIC disabled, seed exclusion list.
- One hardcoded `block` rule with `Sec-Fetch-Dest`-derived stub synthesis.
- Control server on `127.0.0.1:8081` with `GET /flows`, `GET /events` (SSE), `GET/POST /state`.
- Web UI: a single live flow table, nothing else.
- Extension: popup with an on/off toggle that sets `chrome.proxy`, and a badge count.

**v0.1 exit criteria (all must hold):**
1. 30 minutes of ordinary browsing through the proxy with zero certificate warnings and zero broken sites.
2. The hardcoded rule blocks its host; pages referencing it still render correctly.
3. The web UI flow table updates live via SSE while browsing.
4. The extension popup turns the proxy on and off with no manual macOS network-settings change, and the badge count increments on block.
5. No measured page-load regression greater than the §9.1 budget on a reference page.

### 2.2 Subsequent milestones

| Milestone | Content | Exit criterion |
|---|---|---|
| **v0.2** Rules engine | Full declarative rule schema, YAML load + hot reload, first-match/all-match semantics, transform registry, unit test suite, provenance capture. | Rules edited through the API take effect without restarting the proxy; provenance is visible per flow in the UI. |
| **v0.3** Rewriting | Buffering guard, CSP/SRI handling, `anticache`/`anticomp` dev toggles, stub library, `map_local`. | A script injected into a CSP-bearing page runs with no console errors; a rewritten SRI-bearing script is not dropped. |
| **v0.4** Modules & profiles | Module format, Python tier, hot reload, error isolation, profile switching from popup, module CRUD in the web UI with a code editor. | A Python module authored in the web UI loads, fires, and appears in provenance; a module that throws is quarantined without killing the proxy. |
| **v0.5** Capture & sessions | SQLite session recording, redaction, replay, dry-run engine. | A module can be dry-run against a recorded session and produce a diff without touching live traffic. |
| **v0.6** MCP | MCP server over the four tool families. | An AI agent, given only MCP access, observes traffic and authors a working module for a named site. |
| **v1.0** Hardening | DevTools panel, in-page warnings, launchd packaging, install/uninstall, docs. | Clean install on a fresh machine from documented steps. |

---

## 3. Proxy daemon (D1)

### 3.1 Process and lifecycle

| ID | P | Requirement |
|---|---|---|
| PXY-001 | M | The daemon SHALL run `mitmdump` in explicit (regular) proxy mode bound to `127.0.0.1:8080`. The port SHALL be configurable. |
| PXY-002 | M | The daemon SHALL be installable as a macOS launchd **user agent** (`~/Library/LaunchAgents/`) that starts at login and restarts on crash. |
| PXY-003 | M | A CLI (`pporlock`) SHALL provide at minimum: `start`, `stop`, `status`, `restart`, `install`, `uninstall`, `logs`, `doctor`. |
| PXY-004 | M | `pporlock doctor` SHALL check and report: CA present and trusted in the login keychain, ports free, Chrome QUIC state, config file validity, module load errors, and daemon reachability. |
| PXY-005 | M | The daemon SHALL run in the foreground when invoked as `pporlock run`, for development and debugging. |
| PXY-006 | M | The mitmproxy version SHALL be pinned exactly in the project dependency manifest. |
| PXY-007 | S | The daemon SHALL log to a rotating file under `~/Library/Logs/pporlock/` with configurable level, and SHALL never log request/response bodies at default level. |
| PXY-008 | M | On unclean shutdown the daemon SHALL NOT leave Chrome's proxy settings pointing at a dead listener; the extension SHALL detect an unreachable daemon and revert to direct connection (see EXT-010). |

### 3.2 Certificates and browser configuration

| ID | P | Requirement |
|---|---|---|
| PXY-010 | M | The daemon SHALL use mitmproxy's generated root CA at `~/.mitmproxy/mitmproxy-ca-cert.pem`. |
| PXY-011 | M | `pporlock install` SHALL install and mark that root as trusted in the macOS login keychain, prompting the user for authorization, and `pporlock uninstall` SHALL remove it. |
| PXY-012 | M | Documentation SHALL state that Chrome QUIC/HTTP-3 must be disabled (`chrome://flags` or policy), and `doctor` SHALL warn when it appears enabled. |
| PXY-013 | M | The system SHALL ship a seeded **exclusion list** applied at `tls_clienthello` via `data.ignore_connection = True`, covering at minimum: OS and browser update endpoints, browser telemetry, known certificate-pinning applications, and financial institutions. |
| PXY-014 | M | The exclusion list SHALL be user-editable through the control API and web UI, and changes SHALL take effect without a daemon restart. |
| PXY-015 | M | Excluded connections SHALL be recorded in the capture store as passthrough entries (host/SNI, timing, byte counts) so the user can see that traffic occurred without seeing its content. |
| PXY-016 | S | The web UI SHALL offer a one-click "exclude this host" action from any flow. |

### 3.3 Interception pipeline

| ID | P | Requirement |
|---|---|---|
| PXY-020 | M | Per-flow processing order SHALL be fixed and documented: (1) passthrough decision at `tls_clienthello`; (2) request-side short-circuit (`block`, `map_local`, `redirect`) first-match-wins; (3) request header actions, all-match, declaration order; (4) upstream request; (5) buffering decision at `responseheaders`; (6) response header actions, all-match, declaration order; (7) response body transforms, all-match, declaration order; (8) capture record written. |
| PXY-021 | M | The buffering decision SHALL be made in `responseheaders` and SHALL stream (not buffer) any response whose declared or observed size exceeds a configurable threshold (default 2 MiB) or whose content type is outside the configurable buffer allowlist (default: `text/html`, `text/css`, `application/javascript`, `text/javascript`, `application/json`, and charset variants thereof). |
| PXY-022 | M | When a response is streamed, body transforms SHALL be skipped, and the flow's provenance SHALL record that they were skipped and why. |
| PXY-023 | M | Body reads/writes SHALL use mitmproxy's decoded `.text`/`.content` accessors so that gzip/deflate/brotli are handled transparently and re-encoded on assignment. |
| PXY-024 | M | A transform declared as expensive, or one exceeding a wall-clock threshold, SHALL be executed via `run_in_executor` rather than on the proxy event loop. |
| PXY-025 | M | Regular expressions in rules SHALL be compiled once at rule-load time, never per flow. |
| PXY-026 | S | The daemon SHALL enforce a per-flow total transform time budget (default 250 ms); on exceeding it, remaining transforms are skipped, the flow is delivered unmodified from that point, and the event is recorded in provenance and surfaced in the UI. |

### 3.4 Action taxonomy

| ID | P | Requirement |
|---|---|---|
| PXY-030 | M | The engine SHALL implement six actions: `passthrough`, `block`, `map_local`, `redirect`, `headers`, `body`, with the hook bindings and semantics given in the approach document. |
| PXY-031 | M | `block` SHALL default to synthesizing a benign response rather than killing the connection. `flow.kill()` SHALL be available only via an explicit `mode: kill` on the rule. |
| PXY-032 | M | Stub synthesis with `stub: auto` SHALL derive the response from the request's `Sec-Fetch-Dest` header per the following table, falling back to `204` for unrecognized or absent values: |

| `Sec-Fetch-Dest` | Synthesized response |
|---|---|
| `script` | `200`, `application/javascript`, empty body (or named stub if given) |
| `image` | `200`, `image/gif`, 1×1 transparent GIF |
| `empty` (fetch/XHR) | `200`, `application/json`, `{}` |
| `iframe` | `200`, `text/html`, empty document |
| `document` | `403` with a short explanatory page identifying the blocking module |
| `style` | `200`, `text/css`, empty body |
| `font`, `media`, other | `204` |

| ID | P | Requirement |
|---|---|---|
| PXY-033 | M | A **stub library** of named script stubs (defining the globals common trackers expect) SHALL ship with the system, deliverable through `map_local` without special-casing. |
| PXY-034 | M | `map_local` SHALL serve from a local file, setting a content type derived from the file extension unless overridden, and SHALL fail loudly (recorded error + UI surface) rather than silently when the file is missing. |
| PXY-035 | M | `redirect` SHALL support independent rewriting of scheme, host, port, path, and query. |
| PXY-036 | M | `headers` SHALL support add, remove, and replace on both request and response, with case-insensitive header name matching. |

### 3.5 SRI, CSP, and cache interference

| ID | P | Requirement |
|---|---|---|
| PXY-040 | M | Whenever body rewriting is enabled for a document response, the system SHALL unconditionally strip `integrity` and `crossorigin` attributes from `<script>` and `<link>` tags in that document. |
| PXY-041 | M | When a module injects script into a page, the system SHALL first attempt to parse a `nonce-` value from the page's existing `Content-Security-Policy` and reuse it on the injected tag. Wholesale CSP relaxation SHALL be the fallback, not the default. |
| PXY-042 | M | When CSP is modified or removed, both `Content-Security-Policy` and `Content-Security-Policy-Report-Only` SHALL be handled, and the modification SHALL be recorded in provenance and surfaced to the user (see EXT-020). |
| PXY-043 | M | The `anticache` (strip `If-None-Match`/`If-Modified-Since`) and `anticomp` (strip `Accept-Encoding`) options SHALL be exposed as **development toggles** through the control API and web UI, both defaulting to off. |
| PXY-044 | M | The web UI and extension popup SHALL display a persistent indicator whenever any development toggle is active, because both alter traffic in ways that make production behaviour unreproducible. |

### 3.6 WebSockets

| ID | P | Requirement |
|---|---|---|
| PXY-050 | M | WebSocket connections and their frames SHALL be captured and displayed (direction, opcode, size, timestamp, payload subject to redaction and size cap). |
| PXY-051 | M | WebSocket frames SHALL NOT be modifiable in v1. The `websocket_message` hook is inspection-only. |
| PXY-052 | M | The rule schema SHALL reserve namespace for future WebSocket actions such that adding them is not a breaking change to existing modules. |
| PXY-053 | C | WebSocket frame modification via the module system. |

---

## 4. Traffic scope and Chrome integration

| ID | P | Requirement |
|---|---|---|
| SCP-001 | M | Only Chrome traffic is in scope. The daemon SHALL NOT modify macOS system-wide network proxy settings. |
| SCP-002 | M | Proxy configuration SHALL be applied by the extension via `chrome.proxy.settings.set()`. |
| SCP-003 | M | Traffic from non-Chrome clients that reaches the proxy port SHALL be handled correctly (it is a normal proxy) but is explicitly unsupported and untested. |
| SCP-004 | C | An opt-in system-wide mode for debugging non-browser clients. |

---

## 5. Rules and modules

### 5.1 Two-tier model

pporlock supports two authoring tiers inside one module format. The declarative tier covers the common cases and is safe to generate, diff, and validate; the Python tier is the escape hatch for anything requiring real logic. A single module MAY use either or both.

### 5.2 Module format

| ID | P | Requirement |
|---|---|---|
| MOD-001 | M | A module SHALL be a directory containing `module.yaml` (manifest + declarative rules), an optional `module.py` (Python tier), and optional asset files (stubs, local-mapped files). |
| MOD-002 | M | The manifest SHALL carry at minimum: `name` (unique, slug), `version` (semver), `description`, `author`, `enabled`, `priority` (integer, controls ordering across modules), and `pporlock_api` (the module API version it targets). |
| MOD-003 | M | Modules SHALL live under a configurable module root, default `~/.pporlock/modules/`, one directory per module. |
| MOD-004 | M | The module set SHALL hot-reload on file change and on explicit API request, without dropping in-flight flows and without restarting the daemon. |
| MOD-005 | M | Module load failure (syntax error, manifest schema violation, unresolvable asset) SHALL disable only that module, SHALL be reported through the control API with the full error and traceback, and SHALL be surfaced in the web UI and extension. It SHALL NOT prevent the daemon from starting or other modules from loading. |
| MOD-006 | S | The system SHALL support module export/import as a single archive, so a module can be shared or checked into version control. |

### 5.3 Declarative tier

| ID | P | Requirement |
|---|---|---|
| MOD-010 | M | Rules SHALL match on any combination of: `host` (glob), `path` (regex), `method`, `dest` (`Sec-Fetch-Dest`), `query` (key/value or regex), and request header presence/value. |
| MOD-011 | M | Response-side rules SHALL additionally match on `status` (value or range) and `content_type`. |
| MOD-012 | M | Evaluation semantics SHALL be: short-circuit actions (`block`, `map_local`, `redirect`) are **first-match-wins** across all enabled modules; `headers` and `body` actions are **all-match, applied in order**. Ordering across modules is by module `priority`, then by declaration order within a module. |
| MOD-013 | M | Body transforms in the declarative tier SHALL be named entries in a registry, not arbitrary expressions in YAML. The registry SHALL include at minimum: `strip_integrity_attributes`, `strip_csp`, `inject_script`, `inject_style`, `regex_sub`, `json_patch`, `replace_literal`. |
| MOD-014 | M | Registry transforms SHALL accept typed, schema-validated parameters, and the manifest SHALL be rejected at load time if parameters do not validate. |
| MOD-015 | M | The rule schema SHALL be published as a JSON Schema, used by the web UI editor, the MCP validation tool, and the loader alike, so all three agree on what is valid. |

### 5.4 Python tier

| ID | P | Requirement |
|---|---|---|
| MOD-020 | M | A module's `module.py` MAY define any of: `on_request(req, ctx)`, `on_response(req, resp, ctx)`, `on_websocket_message(msg, ctx)` (read-only), `on_load(ctx)`, `on_unload(ctx)`. |
| MOD-021 | M | Hook functions SHALL receive only pporlock's own normalized dataclasses. mitmproxy types SHALL NOT be exposed to module code. |
| MOD-022 | M | `ctx` SHALL provide at minimum: matching helpers, structured logging scoped to the module, module-scoped persistent key/value storage, module asset path resolution, and the current profile name. |
| MOD-023 | M | Python hooks SHALL run in the same pipeline order as declarative rules of equivalent action class, interleaved by module priority. |
| MOD-024 | M | An exception raised by a Python hook SHALL be caught, logged with traceback, attributed to the module in provenance, and SHALL NOT affect the flow's delivery or other modules. |
| MOD-025 | M | A module whose hooks raise on N consecutive flows (default N=10, configurable) SHALL be automatically quarantined (disabled with a surfaced reason) rather than continuing to fail. |
| MOD-026 | S | The module API SHALL be versioned; a module declaring an unsupported `pporlock_api` SHALL refuse to load with a clear message rather than failing at runtime. |

### 5.5 Trust model

| ID | P | Requirement |
|---|---|---|
| MOD-030 | M | Python modules are **fully trusted**. There is no import allowlist, no sandbox, and no resource jail. This is a deliberate decision for a single-user machine. |
| MOD-031 | M | The documentation SHALL state plainly that module code runs with the user's full privileges in the proxy process, and that AI-generated modules should be read before enabling. |
| MOD-032 | M | The only enforced guardrails SHALL be error isolation (MOD-024), failure quarantine (MOD-025), and the per-flow time budget (PXY-026). |
| MOD-033 | M | The MCP interface SHALL be able to write and update module files but SHALL NOT enable a newly created module without an explicit separate call; see MCP-031. |

### 5.6 Profiles

| ID | P | Requirement |
|---|---|---|
| MOD-040 | M | Modules SHALL form a flat library. **Profiles** are named sets selecting which modules are active. |
| MOD-041 | M | Exactly one profile SHALL be active at a time. A built-in `default` profile SHALL always exist and SHALL NOT be deletable. |
| MOD-042 | M | Profile switching SHALL take effect immediately without a daemon restart, and SHALL be available from the extension popup and the web UI. |
| MOD-043 | M | Profiles SHALL be creatable, renamable, duplicable, and deletable through the control API and web UI. |
| MOD-044 | S | A profile SHALL be able to carry its own development-toggle state and exclusion-list additions, so switching to a "debugging site X" profile applies the whole working context at once. |

---

## 6. Capture, provenance, and sessions

### 6.1 Live ring buffer

| ID | P | Requirement |
|---|---|---|
| CAP-001 | M | The daemon SHALL maintain a bounded in-memory ring buffer of recent flows, sized by both entry count and total bytes (defaults: 2,000 flows / 256 MiB), both configurable. |
| CAP-002 | M | Ring buffer entries SHALL include: timestamp, method, URL, host, `Sec-Fetch-Dest`, request and response headers, status, content type, sizes, timing breakdown, the initiating Chrome tab ID where obtainable, and provenance. |
| CAP-003 | M | Bodies SHALL be retained in the ring buffer only up to a configurable per-body cap (default 512 KiB), with truncation flagged. |
| CAP-004 | M | The ring buffer SHALL be filterable by host, path, status, content type, method, `dest`, tab, and "was modified". |

### 6.2 Provenance — the primary debugging affordance

Silent breakage is the characteristic failure of this class of tool: the proxy considers a flow successful, the page is subtly wrong, and the cause is three rules deep. Provenance is therefore a first-class output of the engine, not a logging feature.

| ID | P | Requirement |
|---|---|---|
| CAP-010 | M | The rules engine's return value SHALL include a provenance record. This SHALL be structural, present from the first implementation, and never optional. |
| CAP-011 | M | Provenance SHALL record, in evaluation order, every module and rule that was **evaluated and matched**, the action it produced, and whether that action was applied, skipped, or errored — with the reason for skip or error. |
| CAP-012 | M | Provenance SHALL explicitly record non-actions that change behaviour: response streamed (and why), transform skipped by time budget, module quarantined, `map_local` file missing, CSP/SRI modified, development toggle active. |
| CAP-013 | M | Every flow in the ring buffer, in a session, in the web UI, in the DevTools panel, and in MCP output SHALL carry its provenance. |
| CAP-014 | S | The UI SHALL offer a body diff (before/after) for any flow whose body was transformed, subject to the body size cap. |

### 6.3 Session recording

| ID | P | Requirement |
|---|---|---|
| CAP-020 | M | The user SHALL be able to explicitly start and stop a named **session** recording, which persists flows to SQLite under `~/.pporlock/sessions/`. Recording is opt-in and off by default. |
| CAP-021 | M | Sessions SHALL be listable, renamable, deletable, and openable for browsing in the web UI. |
| CAP-022 | M | Session flows SHALL be replayable through the current module set as a **dry run** (see CAP-030). |
| CAP-023 | M | Session storage SHALL enforce a configurable per-session size cap and a configurable body cap, with clear indication when truncation occurred. |
| CAP-024 | S | Sessions SHALL be exportable to HAR, and to a pporlock-native format that preserves provenance (which HAR cannot represent). |
| CAP-025 | S | Recording SHALL be startable from the extension popup, since the moment you notice a problem is when you want to capture it. |

### 6.4 Dry run

| ID | P | Requirement |
|---|---|---|
| CAP-030 | M | The system SHALL support evaluating a candidate module (or a whole profile) against a recorded session **without affecting live traffic**, returning per-flow: which rules would fire, what the resulting headers/body would be, and a diff against the recorded original. |
| CAP-031 | M | Dry run SHALL be available through the control API, the web UI, and the MCP interface, using the same code path in all three cases. |
| CAP-032 | M | Dry run SHALL execute Python-tier hooks. This means dry run runs untrusted-to-the-agent code; it is acceptable under MOD-030 but SHALL be documented. |
| CAP-033 | S | Dry run SHALL report an aggregate summary: flows matched, flows modified, flows errored, estimated per-flow cost. |

### 6.5 Redaction

| ID | P | Requirement |
|---|---|---|
| CAP-040 | M | Redaction SHALL be **on by default** for data written to sessions and for data returned through the MCP interface. |
| CAP-041 | M | Redaction SHALL mask at minimum: `Cookie`, `Set-Cookie`, `Authorization`, `Proxy-Authorization`, `X-Api-Key`, and any header matching a configurable pattern list; plus JSON body fields whose keys match a configurable pattern list (default covers `password`, `token`, `secret`, `api_key`, `access_token`, `refresh_token`, `session`, `auth`). |
| CAP-042 | M | Masking SHALL preserve enough shape to be useful — length and a stable per-value hash prefix — so the user can tell whether two requests carried the same token without seeing it. |
| CAP-043 | M | The web UI SHALL allow unmasking a specific value locally, on explicit user action, from the live ring buffer. Unmasking SHALL NOT be available through the MCP interface. |
| CAP-044 | M | The redaction pattern lists SHALL be user-configurable, and the effective configuration SHALL be visible in the UI. |
| CAP-045 | S | Redaction SHALL be applied at write time for sessions (not at read time), so a session file on disk never contains the secret. |

---

## 7. Control server and API

### 7.1 Server

| ID | P | Requirement |
|---|---|---|
| API-001 | M | The control server SHALL be an asyncio HTTP server started from the addon's `running()` hook, bound to `127.0.0.1:8081` (configurable), sharing the proxy's event loop. |
| API-002 | M | Any handler whose work is not trivially fast SHALL offload to a thread pool, because blocking this loop stalls all browsing. |
| API-003 | M | The server SHALL serve the web UI's static build assets. |
| API-004 | M | The server SHALL return `Access-Control-Allow-Origin` for the extension's `chrome-extension://` origin and for its own origin, and SHALL reject all others. |
| API-005 | M | Private Network Access enforcement state for extension→loopback requests SHALL be verified empirically during v0.1, not assumed. If PNA or any successor policy blocks the channel, **Native Messaging over stdio** is the designated fallback and the API surface SHALL be designed to be transportable to it (request/response + event stream, no HTTP-specific semantics in the contract). |

### 7.2 Access control

| ID | P | Requirement |
|---|---|---|
| API-010 | M | Loopback binding is the primary control. The server SHALL NOT bind any non-loopback interface under any configuration. |
| API-011 | M | A per-install bearer token SHALL be generated at first run, stored at `~/.pporlock/token` with `0600` permissions, and required on all mutating endpoints. This defends against other local processes and against browser pages on the machine reaching the API, not against a remote attacker. |
| API-012 | M | The extension SHALL obtain the token through a first-run pairing step, not by reading the filesystem. |
| API-013 | M | The server SHALL reject requests carrying an `Origin` it does not recognize, and SHALL require a non-simple content type or custom header on mutating requests, to defeat cross-origin form-based CSRF from ordinary web pages. |

### 7.3 Endpoints

| ID | P | Requirement |
|---|---|---|
| API-020 | M | `GET /state`, `POST /state` — daemon status, active profile, development toggles, module load errors, counters. |
| API-021 | M | `GET /flows` — ring buffer with filtering and pagination; `GET /flows/{id}` — full detail including bodies and provenance. |
| API-022 | M | `GET /events` — SSE stream of live flow events, module errors, and state changes. The stream SHALL support filtering so the extension can subscribe per-tab. |
| API-023 | M | `GET/POST/PUT/DELETE /modules`, `/modules/{name}` — module CRUD including file contents; `POST /modules/reload`. |
| API-024 | M | `GET/POST/PUT/DELETE /profiles`, `POST /profiles/{name}/activate`. |
| API-025 | M | `GET/PUT /exclusions` — the ClientHello exclusion list. |
| API-026 | M | `GET/POST /sessions`, `GET /sessions/{id}/flows`, `POST /sessions/{id}/dryrun`, `DELETE /sessions/{id}`. |
| API-027 | M | `POST /validate` — validate a module manifest and/or Python source without installing it. |
| API-028 | S | `GET /metrics` — throughput, latency percentiles, per-module cost. |
| API-029 | M | The API SHALL be documented as an OpenAPI specification kept in the repository and current with the implementation. |

---

## 8. Web UI (D2)

| ID | P | Requirement |
|---|---|---|
| WUI-001 | M | The web UI SHALL be a React + Vite SPA, built to static assets and served by the control server on `127.0.0.1:8081`. |
| WUI-002 | M | The build SHALL be reproducible from the repository with a single documented command, and built assets SHALL be produced by the project's build pipeline (not committed pre-built, unless the release process says otherwise). |
| WUI-003 | M | **Live traffic view:** a virtualized flow table fed by SSE, with the filters of CAP-004, showing at minimum method, host, path, status, type, size, duration, and a modification indicator. |
| WUI-004 | M | **Flow detail:** request and response headers, bodies (pretty-printed for JSON/HTML/JS), timing, and the full provenance chain with each module/rule linked to its source. |
| WUI-005 | M | **Module library:** list, enable/disable, priority ordering, load-error display, create, duplicate, delete, import, export. |
| WUI-006 | M | **Module editor:** a code editor (Monaco) for `module.yaml` and `module.py`, with JSON-Schema-driven validation and inline errors for the manifest, Python syntax checking, and save-and-reload in one action. |
| WUI-007 | M | **Rule builder:** a form-based path to authoring declarative rules for users who do not want to hand-write YAML, producing the same YAML the editor shows. |
| WUI-008 | M | **"Create rule from this flow":** from any captured flow, generate a pre-populated candidate rule (block, map_local, redirect, or header edit) matching that flow. This is the shortest path from observing a problem to fixing it. |
| WUI-009 | M | **Profiles:** create, edit membership, activate, duplicate, delete. |
| WUI-010 | M | **Sessions:** start/stop recording, browse recorded sessions, run a dry-run of a module or profile against a session and view the resulting diffs. |
| WUI-011 | M | **Settings:** exclusion list editor, development toggles, redaction patterns, buffering thresholds, ports, log level. |
| WUI-012 | M | Development toggles SHALL be visually prominent whenever active (PXY-044). |
| WUI-013 | M | The UI SHALL degrade clearly when the daemon is unreachable: an unmistakable disconnected state, not a silently empty table. |
| WUI-014 | S | Body diff view for transformed flows (CAP-014). |
| WUI-015 | S | The UI SHALL be usable at 1280×800 and SHALL meet WCAG 2.1 AA for contrast and keyboard navigation. |

---

## 9. Chrome extension (D3)

| ID | P | Requirement |
|---|---|---|
| EXT-001 | M | Manifest V3, with permissions limited to `proxy`, `storage`, `tabs`, `webRequest` (observation only if needed for tab attribution), and host permissions for the loopback control origin. No broad host permissions beyond what the DevTools panel and content script require. |
| EXT-002 | M | A service worker SHALL apply and clear proxy configuration via `chrome.proxy.settings.set()`, using a fixed-server configuration pointing at `127.0.0.1:8080`, with a bypass list for loopback and the control port. |
| EXT-003 | S | A PAC-script mode SHALL be supported for browser-side per-host scoping, as an alternative to fixed-server. |
| EXT-010 | M | The extension SHALL health-check the daemon and, on finding it unreachable while the proxy is enabled, SHALL clear the proxy configuration and show an unmistakable error state — the browser must never be left pointed at a dead proxy. |
| EXT-011 | M | **Popup:** proxy on/off, active profile selector, current-tab summary (requests, blocked, modified), a one-click "bypass this host" action, and a link to the web UI. |
| EXT-012 | M | **Badge:** per-tab count of blocked and modified requests on the toolbar icon, with a distinct badge state when the proxy is off and when a development toggle is active. |
| EXT-013 | M | **DevTools panel:** per-tab live flow list with the provenance of each flow — which modules and rules fired and what they did. This is the doc's designated primary debugging affordance and is not optional. |
| EXT-014 | M | The DevTools panel SHALL allow jumping from a flow to the responsible module in the web UI. |
| EXT-020 | M | **In-page warnings:** when a module rewrote the current document — SRI stripped, CSP relaxed or removed, script injected — a content script SHALL display a dismissible banner naming the module and the modification, because these changes weaken the page's own protections and must not be invisible. |
| EXT-021 | M | The in-page warning SHALL be suppressible per-host and globally, with the suppression state visible in settings. |
| EXT-022 | M | First-run pairing SHALL obtain the control API token (API-012) with a clear, documented flow. |
| EXT-023 | S | Recording start/stop from the popup (CAP-025). |
| EXT-024 | M | The extension SHALL be loadable unpacked for development; Chrome Web Store distribution is out of scope for v1. |

---

## 10. MCP interface (D4)

### 10.1 Purpose

The MCP server exists so an AI coding agent can close the loop: observe live or recorded traffic, understand why a page behaves as it does, author a module, dry-run it against a session, and — after the user's approval — enable it.

| ID | P | Requirement |
|---|---|---|
| MCP-001 | M | The MCP server SHALL be a stdio MCP server shipped with pporlock, connecting to the control API. |
| MCP-002 | M | It SHALL be startable by an MCP client (e.g. Claude Code) with no additional configuration beyond the daemon being installed. |
| MCP-003 | M | All MCP responses SHALL be redacted per CAP-040/041. The interface SHALL have no unmask capability. |
| MCP-004 | M | All MCP responses containing flow data SHALL include provenance. |
| MCP-005 | M | Tool descriptions SHALL be explicit about token cost, and every listing tool SHALL default to a bounded page size with summary-level fields, expanding to bodies only on explicit request. |

### 10.2 Tool families

| ID | P | Requirement |
|---|---|---|
| MCP-010 | M | **Introspection (read):** list/filter flows from the ring buffer or a session; get a flow's full detail; get provenance for a flow; get aggregate stats for a host or page load; list WebSocket frames. |
| MCP-011 | M | **Authoring (write):** create a module; read a module's files; update a module's manifest, rules, or Python; delete a module; list modules and their load errors. |
| MCP-012 | M | **Validation:** validate a candidate module against the schema and Python syntax without installing it; dry-run a candidate module against a named session and return per-flow diffs and an aggregate summary. |
| MCP-013 | M | **Daemon control:** start/stop the proxy, get status, set development toggles, activate a profile, enable/disable a module, edit the exclusion list, start/stop session recording, reload modules. |
| MCP-014 | S | A `suggest_rule_from_flow` tool that returns a candidate rule for a given flow, mirroring WUI-008. |

### 10.3 Guardrails

| ID | P | Requirement |
|---|---|---|
| MCP-030 | M | Creating or updating a module SHALL NOT enable it. Enabling is a separate, explicit tool call. |
| MCP-031 | M | Enabling a module, activating a profile, and setting development toggles SHALL each be individually recorded in an audit log visible in the web UI, with timestamp and origin (`ui`, `extension`, `mcp`, `cli`). |
| MCP-032 | M | The MCP server SHALL be able to run in a read-only mode in which the authoring and control families are unavailable. |
| MCP-033 | S | The web UI SHALL show a live indicator when an MCP client is connected and what it has changed in this session. |

---

## 11. Performance and reliability

| ID | P | Requirement |
|---|---|---|
| PRF-001 | M | On a reference page load with a representative profile enabled, added latency attributable to pporlock SHALL be under 15% of direct-connection page load time at the 50th percentile and under 30% at the 95th. This SHALL be **measured at v0.3**, not assumed. |
| PRF-002 | M | Per-flow overhead for a flow matching no rules SHALL be under 2 ms at the 95th percentile. |
| PRF-003 | M | A benchmark harness SHALL exist in the repository, runnable on demand, producing the PRF-001/002 numbers against a fixed reference workload. |
| PRF-004 | M | The system SHALL sustain a page pulling 200+ subresources without the event loop stalling perceptibly, via the content-type and size guards, pre-compiled regexes, and executor offload. |
| PRF-005 | M | Memory SHALL be bounded by the ring buffer caps (CAP-001) and SHALL not grow without limit over a multi-day daemon uptime. A long-running soak test SHALL verify this. |
| PRF-006 | M | No single module, rule, or flow SHALL be able to permanently wedge the proxy. The time budget (PXY-026) and quarantine (MOD-025) are the enforcement points. |
| PRF-007 | S | Per-module cost SHALL be measured and displayed, so an expensive module is identifiable rather than merely suspected. |

---

## 12. Testing and quality

| ID | P | Requirement |
|---|---|---|
| TST-001 | M | The rules engine SHALL be unit-testable with no mitmproxy process and no network. This is the load-bearing consequence of DD-2 and is a hard requirement, not an aspiration. |
| TST-002 | M | Unit test coverage of the rules engine, module loader, and redaction SHALL be maintained above 85%. |
| TST-003 | M | Integration tests SHALL exercise the full proxy pipeline against a local test origin server, covering each of the six actions, the buffering guard, SRI/CSP handling, and provenance correctness. |
| TST-004 | M | A golden-file corpus of recorded sessions SHALL be maintained and used to regression-test module behaviour and dry-run output. |
| TST-005 | M | The control API SHALL be contract-tested against its OpenAPI specification. |
| TST-006 | S | End-to-end tests SHALL drive Chrome with the extension loaded (Playwright or equivalent), covering proxy toggle, badge counts, and the DevTools panel. |
| TST-007 | M | A mitmproxy upgrade SHALL be gated on the integration suite passing, and the adapter layer (`normalize()` and the addon) is the only place expected to require changes. |

---

## 13. Documentation and packaging

| ID | P | Requirement |
|---|---|---|
| DOC-001 | M | Install guide covering CA trust, QUIC disable, extension loading, and first-run pairing, with a `doctor`-driven verification step. |
| DOC-002 | M | Module authoring guide covering both tiers, the transform registry, the `ctx` API, and the trust model warning (MOD-031). |
| DOC-003 | M | A troubleshooting guide organized around the characteristic failure — "the page is subtly wrong" — walking from provenance to cause. |
| DOC-004 | M | The OpenAPI spec (API-029) and the rule JSON Schema (MOD-015) SHALL be published as part of the documentation. |
| DOC-005 | M | Uninstall SHALL be documented and complete: launchd agent removed, CA untrusted and removed, Chrome proxy settings cleared, with an explicit statement of what is left behind (modules, sessions) and where. |
| DOC-006 | S | A worked example: authoring a module for a real site, from capture through dry run to enable, using both the web UI and the MCP path. |

---

## 14. Open items

These are not blockers for the v0.1 slice but need resolution before the milestone that depends on them.

| # | Item | Needed by |
|---|---|---|
| OI-1 | Private Network Access enforcement state for extension→loopback. Determines whether the HTTP control channel survives or Native Messaging is required (API-005). | v0.1 |
| OI-2 | Reliable attribution of a flow to a Chrome tab ID. `chrome.webRequest` observation, a proxy-injected header, and heuristic correlation are the candidates; badge counts (EXT-012) and the DevTools panel (EXT-013) both depend on solving it. | v0.1 |
| OI-3 | Reference workload definition for PRF-001. Needs to be a fixed, repeatable page set. | v0.3 |
| OI-4 | Whether module priority ordering is sufficient, or whether explicit inter-module dependency declarations are needed. Defer until several real modules exist. | v0.4 |
| OI-5 | Session file format versioning and migration policy. | v0.5 |
| OI-6 | Whether the MCP server should also expose resources (as opposed to tools only) for flows and modules. | v0.6 |
| OI-7 | Whether tracking traffic carried over WebSocket is common enough on target sites to promote MOD/PXY-053 out of the backlog. | post-v1.0 |

---

## 15. Requirement summary

| Area | Must | Should | Could |
|---|---|---|---|
| Proxy daemon (PXY) | 28 | 5 | 1 |
| Scope (SCP) | 3 | 0 | 1 |
| Modules (MOD) | 27 | 5 | 1 |
| Capture (CAP) | 22 | 6 | 0 |
| Control API (API) | 15 | 1 | 0 |
| Web UI (WUI) | 13 | 2 | 0 |
| Extension (EXT) | 12 | 2 | 0 |
| MCP (MCP) | 12 | 3 | 0 |
| Performance (PRF) | 6 | 1 | 0 |
| Testing (TST) | 6 | 1 | 0 |
| Documentation (DOC) | 5 | 1 | 0 |
