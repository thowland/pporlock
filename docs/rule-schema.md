<!-- GENERATED FILE — do not edit.
     Source: contracts/openapi.yaml and contracts/schemas/.
     Regenerate with: make docs -->


# Rule and manifest schema reference

**REQ MOD-015, DOC-004.** Generated from `contracts/schemas/`. The same JSON Schema validates the module loader, the web UI editor, and the MCP `validate_module` tool, so all three agree on what a valid rule is — which is the point of publishing it rather than describing it.

For *how* to use these, read [the module cookbook](module-cookbook.md). This is the field list.


## Module manifest (`module.yaml`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Slug. Must equal the module directory name and be unique in the library. |
| `version` | `string` | yes | Semver |
| `pporlock_api` | `string` | yes | Module API major version the module targets (SPEC-0 §8.1). An unsupported value refuses to load with a clear message (REQ MOD-026). |
| `description` | `string` |  |  |
| `author` | `string` |  |  |
| `enabled` | `boolean` |  | Creating or updating a module never enables it (REQ MCP-030); enabling is a separate explicit action. |
| `priority` | `integer` |  | Lower runs earlier. Orders rules across modules (SPEC-0 §5.4). |
| `rules` | array of `rule.schema` |  |  |
| `config` | `object` |  | Free-form; the author's defaults. Merged under any declared `settings` the user has changed, then passed to ctx.config (SPEC-0 §8.2). |
| `settings` | array of `object` |  | Fields a user may change from the module library without editing this file. |

Validation is strict: an unknown top-level key is an error, not a warning (REQ MOD-014). A typo in a key name is otherwise a setting that silently never applies.


## Rule

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | `string` | yes | Unique within the module |
| `enabled` | `boolean` |  |  |
| `match` | `object` |  | All present criteria must match. Absent criteria do not constrain (SPEC-0 §5.3). |
| `action` | `passthrough` \| `block` \| `map_local` \| `redirect` \| `headers` \| `body` | yes | The action namespace beginning `ws_` is RESERVED for future WebSocket actions (REQ PXY-052) and no rule may use it. |

## `match`

| Criterion | Type | Notes |
|---|---|---|
| `host` | `string` | Glob, case-insensitive, matched against the full host |
| `path` | `string` | Regex, re.search not fullmatch — anchor explicitly when you mean it |
| `method` | `string_or_list` |  |
| `dest` | `string_or_list` | Sec-Fetch-Dest value(s) |
| `query` | `object` | key -> regex |
| `request_headers` | `object` | key -> regex; null or omitted value means presence only |
| `status` |  | Response-side only. Integer, or a "300-399" range string, or a list of either. |
| `content_type` | `string` | Response-side only. Media type or regex. |

All present criteria must match; absent criteria do not constrain. `path` is `re.search`, not `fullmatch` — anchor explicitly when you mean it.


## Actions

Every action: `passthrough`, `block`, `map_local`, `redirect`, `headers`, `body`.

The action namespace beginning `ws_` is RESERVED for future WebSocket actions (REQ PXY-052) and no rule may use it. WebSocket frames are inspection-only in v1 (REQ PXY-051); holding the prefix now is what makes adding ws_send, ws_drop or ws_rewrite later an addition rather than a collision with a name a module had meanwhile invented for itself. The daemon rejects a `ws_`-prefixed action with an explicit "reserved" error rather than a generic "unknown action", so the reason is legible.

| Action | Additional fields |
|---|---|
| `block` | `mode`, `stub` |
| `map_local` | `file` **required**, `content_type`, `status` |
| `redirect` | `to` **required** |
| `headers` | `request`, `response` |
| `body` | `transform`, `transforms` |

## Transforms

Built-in transform kinds: `strip_integrity_attributes`, `strip_csp`, `inject_script`, `inject_style`, `regex_sub`, `replace_literal`, `json_patch`. Modules add their own with `ctx.register_transform`.

| Kind | Parameters |
|---|---|
| `strip_csp` | `report_only` (default `true`) |
| `inject_script` | `src`, `inline`, `position`, `reuse_nonce` (default `true`) |
| `inject_style` | `href`, `inline`, `position` |
| `regex_sub` | `pattern`, `repl`, `count` (default `0`), `flags` |
| `replace_literal` | `find`, `replace`, `count` (default `0`) |
| `json_patch` | `ops` |

Transforms are named registry entries, never expressions embedded in YAML (REQ MOD-013).


---

Source: [`contracts/schemas/rule.schema.json`](../contracts/schemas/rule.schema.json) and [`module-manifest.schema.json`](../contracts/schemas/module-manifest.schema.json).
