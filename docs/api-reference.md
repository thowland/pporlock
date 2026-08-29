<!-- GENERATED FILE — do not edit.
     Source: contracts/openapi.yaml and contracts/schemas/.
     Regenerate with: make docs -->


# Control API reference

**REQ API-029, DOC-004.** Generated from `contracts/openapi.yaml`, which is the source of truth — this file is a rendering of it, not a second description.

Everything here is served on loopback only, and every route except `/state/health` and `/pair` requires `Authorization: Bearer <token>`. Mutating requests also require an `X-Pporlock-Client` header naming the caller (`ui`, `cli`, `extension`, `mcp`), which is what makes the audit log meaningful — and what makes unmask refusable for everyone but the UI.


**pporlock control API** 0.1.0


## Routes at a glance

| Method | Path | Summary |
|---|---|---|
| `GET` | `/state/health` | Liveness. The only unauthenticated route (SPEC-0 §6.1). |
| `GET` | `/state` | Daemon status, active profile, dev toggles, counters, module errors |
| `POST` | `/state` | Set development toggles, start/stop the proxy listener |
| `GET` | `/metrics` | Throughput, latency percentiles, per-module cost, attribution coverage |
| `GET` | `/audit` | Origin-tagged log of state changes (REQ MCP-031) |
| `GET` | `/flows` | Ring buffer, filtered and paginated |
| `DELETE` | `/flows` | Clear the ring buffer |
| `GET` | `/flows/{flow_id}` | One flow with full provenance |
| `POST` | `/flows/{flow_id}/suggest-rule` | Candidate rule matching this flow (REQ WUI-008, MCP-014) |
| `GET` | `/events` | Server-Sent Events stream of flows, module errors, and state changes |
| `GET` | `/modules` | List modules with load state, errors, quarantine, and stats |
| `POST` | `/modules` | Create a module |
| `GET` | `/modules/{name}` | Manifest, parsed rules, Python source, asset listing |
| `PUT` | `/modules/{name}` | Replace module files |
| `PATCH` | `/modules/{name}` | Set enabled or priority only |
| `DELETE` | `/modules/{name}` | Remove a module |
| `GET` | `/modules/{name}/report` | A module's own report, rendered by the module |
| `POST` | `/modules/reload` | Force reload of all modules |
| `POST` | `/validate` | Validate a candidate module without installing it (REQ API-027) |
| `GET` | `/profiles` | List profiles |
| `POST` | `/profiles` | Create a profile |
| `GET` | `/profiles/{name}` | One profile |
| `PUT` | `/profiles/{name}` | Replace a profile |
| `DELETE` | `/profiles/{name}` |  |
| `POST` | `/profiles/{name}/activate` | Activate a profile without restarting the daemon (REQ MOD-042) |
| `GET` | `/sessions` | List recorded sessions |
| `POST` | `/sessions` | Start recording. Opt-in, off by default (REQ CAP-020). |
| `GET` | `/sessions/{session_id}` | Session metadata |
| `PATCH` | `/sessions/{session_id}` | Rename a session (REQ CAP-021) |
| `DELETE` | `/sessions/{session_id}` | Delete a session and its database file |
| `GET` | `/sessions/{session_id}/export` | Export a session (REQ CAP-024) |
| `POST` | `/sessions/{session_id}/stop` | Stop recording |
| `GET` | `/sessions/{session_id}/flows` | Session flows, same filter vocabulary as /flows |
| `POST` | `/sessions/{session_id}/dryrun` | Evaluate candidate modules against a session without touching live traffic |
| `GET` | `/config` | Effective configuration, with defaults resolved |
| `PUT` | `/config` | Update buffering, capture, budget, redaction, and logging settings |
| `GET` | `/exclusions` | ClientHello exclusion list (REQ PXY-014) |
| `PUT` | `/exclusions` | Replace the exclusion list |
| `POST` | `/pair/begin` | Open a pairing window and return the code (REQ API-012) |
| `POST` | `/pair` | Redeem a pairing code for the bearer token (REQ API-012) |
| `POST` | `/attribution` | Batched (request key -> tab_id) associations (SPEC-0 §3.6) |
| `GET` | `/rules` | The rules currently in force, as loaded (REQ API-022) |
| `PUT` | `/rules` | Replace the rule set without restarting the proxy (REQ MOD-004) |

---

## state

### `GET /state/health`

Liveness. The only unauthenticated route (SPEC-0 §6.1).

Kept cheap and failing closed, because the extension polls it to decide
whether to clear Chrome's proxy configuration (REQ EXT-010, PXY-008).

| Status | Meaning |
|---|---|
| `200` | Alive |

### `GET /state`

Daemon status, active profile, dev toggles, counters, module errors

| Status | Meaning |
|---|---|
| `200` | Current state |
| `401` |  |

### `POST /state`

Set development toggles, start/stop the proxy listener

**Request body:** `StatePatch`

| Status | Meaning |
|---|---|
| `200` | Updated state |
| `401` |  |
| `403` |  |

### `GET /metrics`

Throughput, latency percentiles, per-module cost, attribution coverage

| Status | Meaning |
|---|---|
| `200` | Metrics |

### `GET /audit`

Origin-tagged log of state changes (REQ MCP-031)

| Parameter | In | Type | Notes |
|---|---|---|---|
| `limit` | query | `integer` |  |
| `cursor` | query | `string` |  |

| Status | Meaning |
|---|---|
| `200` | Audit entries |


---

## flows

### `GET /flows`

Ring buffer, filtered and paginated

| Parameter | In | Type | Notes |
|---|---|---|---|
| `host` | query | `string` |  |
| `path` | query | `string` |  |
| `method` | query | `string` |  |
| `status` | query | `string` |  |
| `content_type` | query | `string` |  |
| `dest` | query | `string` |  |
| `tab_id` | query | `integer` |  |
| `modified` | query | `boolean` |  |
| `blocked` | query | `boolean` |  |
| `module` | query | `string` |  |
| `note_code` | query | `string` |  |
| `since` | query | `string` |  |
| `until` | query | `string` |  |
| `q` | query | `string` | Substring over URL |
| `limit` | query | `integer` |  |
| `cursor` | query | `string` |  |
| `detail` | query | `summary` \| `full` \| `bodies` | Representation level (SPEC-0 §6.3). Bodies dominate response size. |

| Status | Meaning |
|---|---|
| `200` | A page of flows |

### `DELETE /flows`

Clear the ring buffer

| Status | Meaning |
|---|---|
| `204` | Cleared |

### `GET /flows/{flow_id}`

One flow with full provenance

| Parameter | In | Type | Notes |
|---|---|---|---|
| `flow_id` | path | `string` |  **required** |
| `detail` | query | `summary` \| `full` \| `bodies` | Representation level (SPEC-0 §6.3). Bodies dominate response size. |
| `unmask` | query | `string` | Reveal one masked value, by field path. Live ring-buffer flows only, requires the bearer token, and is audited. |

| Status | Meaning |
|---|---|
| `200` | The flow |
| `404` |  |

### `POST /flows/{flow_id}/suggest-rule`

Candidate rule matching this flow (REQ WUI-008, MCP-014)

| Parameter | In | Type | Notes |
|---|---|---|---|
| `flow_id` | path | `string` |  **required** |

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Candidate rule |
| `400` | `intent` was missing or not one of the four supported values. |


---

## events

### `GET /events`

Server-Sent Events stream of flows, module errors, and state changes

Filtered server-side so a narrow client filter reduces event volume
rather than merely hiding rows. Clients send Last-Event-ID on reconnect;
where replay is impossible the server emits stream.gap (SPEC-0 §7.2).

| Parameter | In | Type | Notes |
|---|---|---|---|
| `tab_id` | query | `integer` |  |
| `kinds` | query | `string` | Comma-separated event types |

| Status | Meaning |
|---|---|
| `200` | SSE stream |


---

## modules

### `GET /modules`

List modules with load state, errors, quarantine, and stats

| Status | Meaning |
|---|---|
| `200` | Modules |

### `POST /modules`

Create a module

Creating a module never enables it (REQ MCP-030).

**Request body:** `ModuleWrite`

| Status | Meaning |
|---|---|
| `201` | Created, disabled |
| `400` |  |

### `GET /modules/{name}`

Manifest, parsed rules, Python source, asset listing

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | Module detail |
| `404` |  |

### `PUT /modules/{name}`

Replace module files

Updating a module never enables it (REQ MCP-030).

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

**Request body:** `ModuleWrite`

| Status | Meaning |
|---|---|
| `200` | Updated |

### `PATCH /modules/{name}`

Set enabled or priority only

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Updated |

### `DELETE /modules/{name}`

Remove a module

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `204` | Deleted |

### `GET /modules/{name}/report`

A module's own report, rendered by the module

Modules that accumulate something worth reading — an audit, a tally, a diff — render it themselves through an `on_report` hook, and the daemon serves the result here (OI-29). Served from the control origin rather than through the proxy, because a report about browsing is most wanted when you are not browsing, and a URL that only resolves inside intercepted traffic cannot be linked to from the UI.

The body is module-authored, so it is returned under a `sandbox` Content-Security-Policy — a unique opaque origin with no script and no same-origin access to the control API. Module code is trusted and could reach the token by other means; this refuses to add a convenient path rather than claiming to be a boundary.

404 when the module does not exist or declares no `on_report`. 502 when the module raises, or returns a content type outside text/html, text/plain, text/csv and application/json.

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | The report, as the module rendered it |
| `404` | No such module |
| `502` | The module raised |

### `POST /modules/reload`

Force reload of all modules

| Status | Meaning |
|---|---|
| `200` | Reload result |

### `POST /validate`

Validate a candidate module without installing it (REQ API-027)

**Request body:** `ModuleWrite`

| Status | Meaning |
|---|---|
| `200` | Validation result |


---

## profiles

### `GET /profiles`

List profiles

| Status | Meaning |
|---|---|
| `200` | Profiles |

### `POST /profiles`

Create a profile

**Request body:** `profile.schema`

| Status | Meaning |
|---|---|
| `201` | Created |

### `GET /profiles/{name}`

One profile

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | Profile |

### `PUT /profiles/{name}`

Replace a profile

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

**Request body:** `profile.schema`

| Status | Meaning |
|---|---|
| `200` | Updated |

### `DELETE /profiles/{name}`

Refuses to delete 'default' (REQ MOD-041).

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `204` | Deleted |
| `409` |  |

### `POST /profiles/{name}/activate`

Activate a profile without restarting the daemon (REQ MOD-042)

| Parameter | In | Type | Notes |
|---|---|---|---|
| `name` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | New state |


---

## sessions

### `GET /sessions`

List recorded sessions

| Status | Meaning |
|---|---|
| `200` | Sessions |

### `POST /sessions`

Start recording. Opt-in, off by default (REQ CAP-020).

**Request body:** `object`

| Status | Meaning |
|---|---|
| `201` | Recording started |

### `GET /sessions/{session_id}`

Session metadata

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | Session metadata |

### `PATCH /sessions/{session_id}`

Rename a session (REQ CAP-021)

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Updated session metadata |

### `DELETE /sessions/{session_id}`

Delete a session and its database file

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `204` | Deleted |

### `GET /sessions/{session_id}/export`

Export a session (REQ CAP-024)

Exports carry redacted values, never raw ones. A session never held the
real value: redaction is applied at write time (REQ CAP-045), so there
is nothing here for an export to leak.

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |
| `format` | query | `har` \| `pporlock` |  |

| Status | Meaning |
|---|---|
| `200` | The exported session |

### `POST /sessions/{session_id}/stop`

Stop recording

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |

| Status | Meaning |
|---|---|
| `200` | Recording stopped |

### `GET /sessions/{session_id}/flows`

Session flows, same filter vocabulary as /flows

SPEC-0 §6.8 says "the same filter vocabulary as §6.5", and this listed
four of the seventeen. The prose is normative (CLAUDE.md's precedence
rule), so the document is widened to match rather than the other way
round — and a contract test written against the narrow version would
have been right for the wrong reason (docs/open-issues.md OI-5).

`tab_id` is the one exception: attribution is a property of the live
browser session, and a recorded session's tab ids refer to tabs that no
longer exist.

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |
| `host` | query | `string` |  |
| `path` | query | `string` |  |
| `method` | query | `string` |  |
| `status` | query | `string` |  |
| `content_type` | query | `string` |  |
| `dest` | query | `string` |  |
| `modified` | query | `boolean` |  |
| `blocked` | query | `boolean` |  |
| `module` | query | `string` |  |
| `note_code` | query | `string` |  |
| `since` | query | `string` |  |
| `until` | query | `string` |  |
| `q` | query | `string` | Substring over URL |
| `limit` | query | `integer` |  |
| `cursor` | query | `string` |  |
| `detail` | query | `summary` \| `full` \| `bodies` | Representation level (SPEC-0 §6.3). Bodies dominate response size. |

| Status | Meaning |
|---|---|
| `200` | A page of flows |

### `POST /sessions/{session_id}/dryrun`

Evaluate candidate modules against a session without touching live traffic

Executes the candidate module's Python hooks (REQ CAP-032). Uses the same
Evaluator and ModuleLoader as live traffic — there is no second
implementation (REQ CAP-031).

| Parameter | In | Type | Notes |
|---|---|---|---|
| `session_id` | path | `string` |  **required** |

**Request body:** `DryRunRequest`

| Status | Meaning |
|---|---|
| `200` | Dry run result |


---

## config

### `GET /config`

Effective configuration, with defaults resolved

| Status | Meaning |
|---|---|
| `200` | Effective configuration |

### `PUT /config`

Update buffering, capture, budget, redaction, and logging settings

listen_host values are validated to be loopback and rejected otherwise (REQ API-010).

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Updated configuration |

### `GET /exclusions`

ClientHello exclusion list (REQ PXY-014)

| Status | Meaning |
|---|---|
| `200` | Exclusions |

### `PUT /exclusions`

Replace the exclusion list

Takes effect without a daemon restart (REQ PXY-014). Removing a
certificate-pinning or financial host may break that site or draw its
traffic into the capture buffer.

**Request body:** `Exclusions`

| Status | Meaning |
|---|---|
| `200` | Updated |


---

## extension

### `POST /pair/begin`

Open a pairing window and return the code (REQ API-012)

Authenticated, unlike `/pair` itself: only something that can already
read the token — the CLI or the web UI — may open a window. That is
what makes the code safe to read aloud. It is worthless without a human
having just asked for it, and it expires.

`pporlock pair` is this route.

| Status | Meaning |
|---|---|
| `200` | A pairing window is open |

### `POST /pair`

Redeem a pairing code for the bearer token (REQ API-012)

Available only within a short window opened by `pporlock pair` or a web
UI button, and only from a chrome-extension:// origin. The extension
never reads the filesystem.

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Token issued |
| `403` |  |

### `POST /attribution`

Batched (request key -> tab_id) associations (SPEC-0 §3.6)

Best-effort. Joined against the ring buffer within a bounded window;
matched flows emit flow.updated. Attribution never blocks a flow.

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | Accepted count |


---

## rules

### `GET /rules`

The rules currently in force, as loaded (REQ API-022)

The rules the running proxy is evaluating, which is the union of
`rules.yaml` and every active module's rules. Disabled rules are not
included: this is what is in force, not what is on disk.

| Status | Meaning |
|---|---|
| `200` | The active rule set |

### `PUT /rules`

Replace the rule set without restarting the proxy (REQ MOD-004)

The new set is compiled before it is swapped in, so a rule that does not
compile leaves the running rules untouched rather than emptying them —
a 400 here means nothing changed.

The swap replaces an immutable snapshot, so a flow already in flight
finishes against the rules it started with.

**Request body:** `object`

| Status | Meaning |
|---|---|
| `200` | The rule set now in force |
| `400` | A rule did not compile. The running rules are unchanged. |


---

See also: [SPEC-0 §6](spec-0-contracts.md) for the normative prose, and [`contracts/openapi.yaml`](../contracts/openapi.yaml) for the machine-readable source.
