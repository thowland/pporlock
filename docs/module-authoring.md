# Writing pporlock modules

**REQ DOC-002.**

A module is a directory. It has two tiers, and most modules only ever need the
first:

- **Declarative** — rules in `module.yaml`. No code. Covers blocking,
  redirecting, serving a local file, rewriting headers, and transforming bodies
  through a fixed registry of named transforms.
- **Python** — a `module.py` with hooks, for the cases where a rule cannot
  express the condition.

---

## ⚠ Read this before you enable anything

**Python modules are fully trusted.** There is no sandbox, no import allowlist,
and no resource jail (REQ MOD-030). A module's code runs **in the proxy process,
with your full user privileges**. It can read your files, open sockets, and see
every byte of your decrypted HTTPS traffic.

This is a deliberate decision, not an oversight. Meaningful sandboxing of Python
is not achievable at a cost proportionate to a single-user local tool, and a
sandbox that does not actually hold is worse than none — it invites you to trust
what you should be reading.

So the rule is simple and it has no exceptions:

> **Read a module before you enable it. Especially one an AI wrote for you.**

The system's only guardrails are containment of *mistakes*, not malice: errors
are isolated to the module that raised them, a module that keeps failing is
quarantined, and there is a per-flow time budget. None of those stop code that
means harm.

The same applies to **dry run**, which executes Python hooks by design so that
its result matches live behaviour (REQ CAP-032). Dry-running an unread module is
not safer than enabling it.

---

## 1. Layout

```
~/.pporlock/modules/my-module/
  module.yaml       required — the manifest and the rules
  module.py         optional — the Python tier
  assets/           optional — files map_local and ctx.asset_* can reach
```

The directory name **is** the module name and must match `name:` in the
manifest. Two names for one module means log lines, audit entries, and rule ids
that refer to something you cannot find on disk.

Names match `^[a-z0-9][a-z0-9-]{0,62}$`.

---

## 2. The manifest

```yaml
name: my-module            # required; must equal the directory name
pporlock_api: "1"          # required; refuses to load on a mismatch
version: "0.1.0"
description: What this module is for.
author: you
enabled: false             # default false — creating never enables
priority: 100              # lower runs earlier; default 100
config: {}                 # free-form; reaches your code as ctx.config
rules: []                  # see §3
```

Validation is strict: **an unknown top-level key is an error, not a warning**
(REQ MOD-014). A typo in a key name is otherwise a rule that silently never
runs, which is the failure this whole system exists to make impossible.

---

## 3. Declarative rules

```yaml
rules:
  - name: block-analytics          # required, unique within the module
    enabled: true
    match:
      host: "*.analytics-vendor.example"   # glob, case-insensitive, full host
      path: "^/collect"                    # regex — re.search, not fullmatch
      method: [GET, POST]
      dest: script                         # Sec-Fetch-Dest
      query: { tid: "^UA-" }               # key → regex
      request_headers: { referer: "^https://target\\." }
      status: [200, "300-399"]             # response-side only
      content_type: "text/html"            # response-side only
    action: block
    mode: stub                             # stub (default) | kill
```

**Match semantics.** Every criterion present must match. Absent criteria do not
constrain. `path` is `re.search`, so `"/collect"` matches `/api/collect` too —
anchor with `^` when you mean it. A response-side criterion on a request-phase
action is a load-time error rather than a rule that quietly never fires.

### Actions

| Action | Extra keys | Runs at |
|---|---|---|
| `passthrough` | — | ClientHello — tunnels without decrypting |
| `block` | `mode` (`stub`\|`kill`), `stub` | request, short-circuit |
| `map_local` | `file` (relative to `assets/`), `content_type`, `status` | request, short-circuit |
| `redirect` | `to: {scheme, host, port, path, query}` | request, short-circuit |
| `headers` | `request:` and/or `response:`, each with `add`/`remove`/`set` | request or response headers |
| `body` | `transform:` or `transforms:` | response body |

### The evaluation rule that trips everyone up

**REQ MOD-012.** Two different semantics, and knowing which you are in explains
most "my rule didn't run" reports:

- `block`, `map_local`, `redirect` — **first match wins**, across *all* enabled
  modules. The first one to match ends evaluation of that class. When one of
  yours loses, provenance names the rule that won under `short_circuited_by`.
- `headers`, `body` — **all matches apply**, in module `priority` order
  (ascending), then declaration order within a module.
- `passthrough` — decided at ClientHello. A match tunnels the connection and no
  other phase runs at all.

### Transforms

Body transforms are **named registry entries**, never expressions embedded in
YAML (REQ MOD-013). Every one has a validated parameter schema.

| Transform | Parameters | Notes |
|---|---|---|
| `strip_integrity_attributes` | — | Drops `integrity`/`crossorigin`. Emits `sri_stripped` |
| `strip_csp` | `report_only` (default true) | Emits `csp_modified` |
| `inject_script` | `src` \| `inline`, `position`, `reuse_nonce` (default true) | Emits `script_injected` |
| `inject_style` | `href` \| `inline`, `position` | |
| `regex_sub` | `pattern`, `repl`, `count`, `flags` | Compiled at load |
| `replace_literal` | `find`, `replace`, `count` | |
| `json_patch` | `ops` (RFC 6902) | `no_change` plus an error note if the body is not JSON |

`reuse_nonce` matters: on a page with a CSP nonce, an injected script that does
not carry the page's nonce is blocked by the browser, and the symptom is a
transform that reports `applied` while nothing happens.

### Body rules only run on buffered responses

A body transform needs a body in memory. pporlock buffers a response only when a
rule could actually use it, the content type is configured for buffering, and it
is under the size cap. Otherwise the response streams and your rule reports
`skipped_streamed`.

This is not a bug to work around — it is why ordinary browsing stays fast.

---

## 4. The Python tier

Add `module.py` when a rule cannot express the condition. Define any of:

```python
def on_load(ctx): ...
def on_unload(ctx): ...
def on_request(ctx, request): ...
def on_response(ctx, request, response): ...
def on_websocket_message(ctx, message): ...
```

Hooks are interleaved with declarative rules by module priority (REQ MOD-023) —
the Python tier is not a separate pass bolted on after the rules.

A hook that raises does not take down the pipeline: the error is isolated to
that module, attached to the flow as a `module_error` note, and after repeated
failures the module is quarantined and stops being called. Quarantine clears
when you re-enable it.

### The `ctx` object

**Matching** — so you are not reimplementing globbing:

```python
ctx.matches(request, host="*.example.com", path="^/api/", method="POST",
            dest="script", content_type="json", response=response)  # -> bool
```

**Reporting** — a module that changes something a page depends on should say so,
for the same reason the built-in transforms do:

```python
ctx.log("info", "saw a login response", user_id=uid)
ctx.note("script_injected", "added the debug shim", severity="warning", where="head_end")
```

`ctx.note` takes a code from the provenance vocabulary. An unrecognised code is
recorded as `module_error` with your requested code in the detail, rather than
being dropped.

**Storage** — durable, per-module, SQLite-backed. Reads are served from a
write-through cache so a `get` never touches disk on the proxy's event loop:

```python
count = ctx.store_get("seen", 0)
ctx.store_set("seen", count + 1)
ctx.store_delete("seen")
```

**Assets** — resolved inside your `assets/` directory, with containment checked
**after** symlink resolution, so a symlink pointing out of the directory is
caught rather than followed:

```python
ctx.asset_path("shim.js")     # -> Path
ctx.asset_bytes("shim.js")    # -> bytes
ctx.asset_text("shim.js")     # -> str
```

**Responses** — build one rather than assembling headers by hand:

```python
return ctx.synthesize(status=200, content_type="application/json", body=b"{}")
return ctx.stub_for(request.dest, request)   # the same table `block` uses
```

`stub_for` gives a `dest`-appropriate empty response: an empty script for a
script, a 1×1 for an image, and so on. A blocked script that gets an HTML error
page back breaks the page differently than it was already broken.

**Config** — `ctx.config` is the `config:` block from your manifest, and
`ctx.profile` is the active profile name.

### Registering a transform

```python
def on_load(ctx):
    ctx.register_transform("uppercase-json-keys", my_fn, cost="expensive")
```

**Cost defaults to `expensive` on purpose.** The scheduler uses cost to decide
what may run inline on the event loop and what must be offloaded. An unknown
transform assumed cheap is how one module makes every page slow.

---

## 5. The workflow

1. Notice something in the flow table.
2. **Create rule from flow** — two clicks, match pre-filled from that request.
3. Edit in the browser. Validation errors appear as you type.
4. **Dry run** against captured flows. Read the diff. (This executes your Python
   — see the warning at the top.)
5. Enable it. Creating never enables; that is always a separate, explicit step
   (REQ MOD-003), and it is the same rule whether you are clicking or an agent
   is calling MCP.
6. Reload the page and read the provenance to confirm it did what you meant.

Step 6 is not optional politeness. `applied` and *did what you meant* are
different claims, and only one of them the system can make for you.

---

## See also

- **[Tutorial: your first declarative module](tutorial-declarative-module.md)** —
  if you are starting from nothing, start there; this page is the reference.
- **[Tutorial: a Python module with state](tutorial-python-module.md)**

- [Troubleshooting](troubleshooting.md) — reading provenance when a rule does
  not do what you expected
- `docs/spec-0-contracts.md` §5 and §8 — the normative rule schema and module API
