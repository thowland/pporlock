# The module cookbook

A working reference for writing pporlock modules, organised around the things
people actually want to do. It assumes you have read
[module authoring](module-authoring.md) for the trust model and the basic shape;
this is the depth behind it.

Every example here is either taken from a module in
[`examples/modules/`](../examples/modules/) or is exercised by
`daemon/tests/unit/test_examples.py`. Nothing in this file is aspirational.

---

## Contents

- [Before anything else](#before-anything-else)
- [The mental model](#the-mental-model)
- [Matching: getting the right flows and no others](#matching)
- [Recipes](#recipes)
  - [Block a tracker](#block-a-tracker)
  - [Hide a cookie banner](#hide-a-cookie-banner)
  - [Restyle a site](#restyle-a-site)
  - [Serve a local build](#serve-a-local-build)
  - [Rewrite headers](#rewrite-headers)
  - [Edit a JSON API response](#edit-a-json-api-response)
  - [Send a privacy signal a site will actually notice](#send-a-privacy-signal-a-site-will-actually-notice)
  - [Classify captured data by what it is, not how it travels](#classify-captured-data-by-what-it-is-not-how-it-travels)
  - [Inject a script that survives CSP](#inject-a-script-that-survives-csp)
  - [Rewrite HTML with a regex, carefully](#rewrite-html-with-a-regex-carefully)
  - [Inject faults on purpose](#inject-faults-on-purpose)
  - [Watch WebSocket frames](#watch-websocket-frames)
- [The Python tier](#the-python-tier)
- [Ordering, priority, and composition](#ordering)
- [Performance](#performance)
- [Debugging a module that does nothing](#debugging)
- [Anti-patterns](#anti-patterns)

---

## Before anything else

**Module code is fully trusted.** No sandbox, no import allowlist, no resource
jail. `module.py` runs in the proxy process with your full user privileges, and
sees every byte of your decrypted traffic. Dry run executes it too, by design —
so dry-running an unread module is not safer than enabling it.

Read a module before you enable it. Especially one an AI wrote for you.

---

## The mental model

A flow moves through phases. A rule runs in exactly one of them, decided by its
action, and that decides what it can see and what it can change.

| Phase | Rules that run | Sees | Can change |
|---|---|---|---|
| `clienthello` | `passthrough` | SNI, IP | whether to decrypt at all |
| `request_short_circuit` | `block`, `map_local`, `redirect` | request | ends the flow, or redirects it |
| `request_headers` | `headers` with `request:` | request | request headers |
| `buffering_decision` | — | request, response headers | whether the body is held |
| `response_headers` | `headers` with `response:`, and `strip_csp` | response headers | response headers |
| `response_body` | `body` | response body | the body |
| `websocket` | — | frames | nothing (inspection-only in v1) |

Two consequences worth internalising:

**A response header rule runs before the body streams.** That is why it works at
all — by the time a body transform runs, the headers have already gone. This is
also why `strip_csp` is written as a body transform but applied in the header
phase: it operates on headers, and the header phase is the last moment a header
change can reach the wire.

**A body rule needs a buffered body.** pporlock buffers only when a body rule
could actually use it, for configured content types, under a size cap.
Otherwise the response streams and your rule reports `skipped_streamed`. A rule
that matches nothing causes no buffering, which is why a badly-scoped rule is
expensive: it makes the proxy hold bodies it has no use for.

---

## Matching

All present criteria must match. Absent criteria do not constrain.

```yaml
match:
  host: "*.example.com"        # glob, case-insensitive, full host
  path: "^/api/v[12]/"         # regex — re.search, NOT fullmatch
  method: [GET, POST]          # string or list
  dest: script                 # Sec-Fetch-Dest; string or list
  query: { tid: "^UA-" }       # key -> regex
  request_headers:
    referer: "^https://target\\."
    x-debug: null              # null means "present, any value"
  status: [200, "300-399"]     # response-side only
  content_type: "application/json"   # response-side only
```

### The three that catch everyone

**`path` is `re.search`.** `"/collect"` matches `/api/collect` and
`/not-collect-this`. Anchor when you mean it: `"^/collect$"`. And `.` is a
wildcard — `"app.js"` matches `appXjs`. Escape it: `"^/static/app\\.js$"`.

**`host` globs the whole host.** `"*.example.com"` matches `www.example.com`
but **not** `example.com`. List both when you mean both. `"*"` matches
everything, which is occasionally right and usually a mistake.

**Response-side criteria on a request-phase action are a load error**, not a
rule that quietly never fires. `status` and `content_type` on a `block` rule
refuse to load, and say so.

### `dest` is the cheapest way to be precise

`Sec-Fetch-Dest` tells you what the browser will do with the response —
`document`, `script`, `style`, `image`, `font`, `empty` (fetch/XHR). It is the
difference between blocking a tracker's script and blocking the page a user
navigated to.

One caveat: browsers send `Sec-Fetch-Dest` only on secure contexts. pporlock
falls back to inferring from `Accept` on plain HTTP, which is good but not
perfect. Do not rely on `dest` alone for something destructive on an http:// origin.

---

## Recipes

### Block a tracker

From [`examples/modules/adblock`](../examples/modules/adblock/module.yaml).

```yaml
- name: block-google-analytics
  action: block
  mode: stub          # stub (default) | kill
  stub: auto
  match:
    host: "www.google-analytics.com"
```

`stub: auto` derives the response from `Sec-Fetch-Dest`: an empty script for a
script, a 1×1 for an image, empty JSON for a fetch. This matters more than it
sounds. A blocked script answered with an HTML error page breaks the page
*differently* than it was already broken, and you will spend the afternoon
debugging the wrong thing.

`mode: kill` drops the connection instead of answering. Reserve it for
long-poll and streaming endpoints where a 200 with an empty body makes the
client retry forever. Anything the page `await`s should get a stub.

**Blocking is first-match-wins across every enabled module.** When your rule
loses, provenance names the winner under `short_circuited_by`. Give a blocker a
low `priority` so it runs early.

Scope path-based blocks by `dest`:

```yaml
- name: block-beacon-paths
  action: block
  match:
    path: "^/(collect|beacon|telemetry)(/|$)"
    dest: [script, image, empty]   # never a document navigation
```

`/collect` on a first-party host may be the application's own telemetry, and
blocking a document navigation to it is how you get a blank page.

### Hide a cookie banner

From [`examples/modules/cookie-banners`](../examples/modules/cookie-banners/).

```yaml
- name: hide-consent-overlays
  action: body
  match:
    content_type: "text/html"
    dest: document
  transform:
    kind: inject_style
    position: head_end
    inline: |
      #onetrust-consent-sdk, #CybotCookiebotDialog, #usercentrics-root,
      [class*="cookie-banner"] { display: none !important; }
      html, body { overflow: auto !important; position: static !important; }
```

The second line is the one people forget. Consent overlays routinely set
`overflow: hidden` on the document to trap you behind them. Hide the overlay
without restoring scrolling and you have made the page worse than the banner
did.

Prefer `inline:` over `href:` here: an external stylesheet is another request,
and on a page with a strict CSP it is another thing to be blocked.

CSS cannot beat an inline style the banner's own script has already applied, so
that module pairs the rule with a small `on_response` hook — see
[The Python tier](#the-python-tier).

### Restyle a site

From [`examples/modules/css-tamper`](../examples/modules/css-tamper/).

Two rules, not one:

```yaml
- name: link-user-stylesheet
  action: body
  match: { content_type: "text/html", dest: document }
  transform:
    kind: inject_style
    href: "/__pporlock__/user.css"

- name: serve-user-stylesheet
  action: map_local
  file: user.css                       # relative to the module's assets/
  content_type: "text/css; charset=utf-8"
  match:
    path: "^/__pporlock__/user\\.css$"
```

The link is same-origin, so it arrives as an ordinary request and `map_local`
answers it from disk. Worth the extra rule: a stylesheet the browser fetches
can be cached, inspected in DevTools, and edited without touching the page. An
inline blob can be none of those.

### Serve a local build

From [`examples/modules/local-bundle`](../examples/modules/local-bundle/).

```yaml
- name: drop-integrity-attributes
  action: body
  match: { content_type: "text/html", dest: document }
  transform: { kind: strip_integrity_attributes }

- name: serve-local-build
  action: map_local
  file: app.js
  content_type: "application/javascript; charset=utf-8"
  match:
    path: "^/static/app\\.js$"
```

**Both rules, always.** Without the SRI strip the browser fetches your file,
hashes it, finds it does not match the `integrity` attribute, and refuses to run
it — with a console error that says nothing about pporlock. This pairing is the
single most common "why didn't my module work".

`file:` is confined to the module's `assets/` directory, with containment
checked **after** symlink resolution. A symlink pointing out of `assets/` is
refused, so copy your build in rather than linking to it.

If the file is missing, the rule emits `map_local_missing` and serves the real
thing. Findable — but only once you already suspect it.

### Rewrite headers

From [`examples/modules/header-lab`](../examples/modules/header-lab/module.yaml).

```yaml
- name: relax-cors-for-local-dev
  action: headers
  match: { host: "api.staging.example.com" }
  response:
    set:
      access-control-allow-origin: "http://localhost:5173"
    remove:
      - vary
```

`set` replaces every occurrence; `add` appends another. For a header that must
appear exactly once — `Access-Control-Allow-Origin` above all — `set` is the
only correct choice. Two of them and the browser rejects both.

A rule declaring only `request:` runs in the request phase; one declaring
`response:` runs in the response phase. This is not cosmetic: it decides whether
response-side match criteria are legal on the rule.

Removing a security header is a modification like any other, and pporlock says
so — removing `Content-Security-Policy` by *any* means emits `csp_modified` and
raises the in-page banner, not only when you use `strip_csp`.

### Edit a JSON API response

From [`examples/modules/json-tamper`](../examples/modules/json-tamper/module.yaml).

```yaml
- name: force-feature-flags
  action: body
  match:
    host: "api.example.com"
    path: "^/v1/flags$"
    content_type: "application/json"
    status: 200
  transform:
    kind: json_patch
    ops:
      - { op: add, path: "/newCheckout", value: true }
      - { op: replace, path: "/ads", value: [] }
      - { op: remove, path: "/nested/tracker" }
```

RFC 6902, with `add`, `remove` and `replace` implemented. `move`, `copy` and
`test` are deliberately absent rather than half-implemented.

Two behaviours worth knowing: `add` on an existing key replaces it, so it works
whether or not the server already sends the field. And a path that is not there
is **not an error** — a rule describes a shape the body may or may not have. A
non-JSON body is left alone and reported as `no_change` with a note, rather than
failing the flow.

### Inject a script that survives CSP

```yaml
- name: add-debug-shim
  action: body
  match: { content_type: "text/html", dest: document }
  transform:
    kind: inject_script
    inline: "window.__DEBUG__ = true;"
    position: head_start     # head_start | head_end | body_end
    reuse_nonce: true        # default
```

`reuse_nonce` is why this works on a page with a CSP nonce. Leave it on. With it
off, the browser blocks your script and the transform still reports `applied` —
because it *did* apply; the browser just refused the result. That gap between
"applied" and "had the effect you wanted" is exactly why you read provenance
*and* then check the page.

`position` matters: `head_start` runs before the page's own scripts (use it to
patch globals), `body_end` runs after the DOM exists (use it to touch elements).

If the page's CSP blocks you outright and no nonce exists, `strip_csp` is the
lever — and it is a real reduction in that page's protection, which is why it
raises a banner.

### Send a privacy signal a site will actually notice

A header alone is usually not enough, and the reason generalises well beyond
this example.

Global Privacy Control is specified as **two** signals: a `Sec-GPC: 1` request
header, and a `navigator.globalPrivacyControl` DOM property. Sites overwhelmingly
test the second — globalprivacycontrol.org's own checker is literally
`!!navigator.globalPrivacyControl`. A proxy can set headers all day and the page
will still report the signal as absent, because a proxy cannot set a DOM
property. Do both:

```yaml
  - name: send-gpc
    action: headers
    match: { host: "*" }
    request:
      set:
        Sec-GPC: "1"
        DNT: "1"

  - name: expose-gpc-to-javascript
    action: body
    match:
      content_type: "text/html"
    transform:
      kind: inject_script
      position: head_start          # before any page script can read it
      reuse_nonce: true             # honour the page's CSP, do not strip it
      inline: |
        (function () {
          try {
            if (!('globalPrivacyControl' in navigator)) {
              Object.defineProperty(navigator, 'globalPrivacyControl', {
                value: true, configurable: true, enumerable: true
              });
            }
          } catch (e) { /* never break a page over a privacy signal */ }
        })();
```

`head_start` matters: the property has to exist before the page's own script
reads it. `reuse_nonce` matters more — reaching for `strip_csp` to make an
injection work turns off a real protection on a page you are about to keep
using.

**The general rule: check how the thing you are trying to influence is actually
detected.** Anything with both a transport form and a JavaScript form —
privacy signals, feature detection, client hints — needs the tier that matches
the check, and a header-only module looks broken while working perfectly.

### Classify captured data by what it is, not how it travels

A companion trap, from the audit half of the same module. Tallying `Set-Cookie`
and sorting names into "essential" and "advertising" invites this:

```yaml
essential_prefixes:
  - "__Secure-"      # WRONG
  - "__Host-"        # WRONG
```

`__Secure-` and `__Host-` are cookie **security prefixes**. They constrain how a
cookie may be set — Secure-only, path-locked — and say nothing about its
purpose. Treating them as evidence of "essential" classified Google's
`__Secure-3PSIDCC` and `__Secure-3PSIDTS` as essential, when the `3P` in those
names means *third-party*: they are precisely the cross-site cookies the audit
existed to surface. The count went from 2 advertising to 9 once it was fixed.

Two habits fall out of it:

- **Default to `unclassified`, never to benign.** An audit that guesses
  "essential" launders its own finding, and the guess is invisible in the
  output.
- **Classify at render, not at capture.** Recompute the category when the report
  is built, so editing the lists and reloading corrects the whole history
  instead of only the entries that happen to be seen again.

### Rewrite HTML with a regex, carefully

```yaml
- name: swap-an-endpoint
  action: body
  match: { content_type: "text/html" }
  transform:
    kind: regex_sub
    pattern: "https://api\\.prod\\.example\\.com"
    repl: "https://api.local.test"
    count: 0          # 0 = every occurrence
    flags: "i"
```

Patterns compile at load, so a bad one fails when you save rather than on
traffic.

Reach for `replace_literal` when you mean a literal — it is faster and cannot
surprise you:

```yaml
transform: { kind: replace_literal, find: "PROD_MODE", replace: "DEV_MODE" }
```

Do not parse HTML with a regex. Use it for URLs, tokens and flags — things with
no nesting. For structural changes, inject a script and let the browser's own
parser do it.

### Inject faults on purpose

From [`examples/modules/fault-lab`](../examples/modules/fault-lab/module.py).
This one needs Python: "every third request" is not something a declarative rule
can express.

```python
from pporlock.engine.models import RequestMutation

def on_request(request, ctx):
    if not ctx.matches(request, host=ctx.config.get("host", "*")):
        return None
    seen = int(ctx.store_get("seen", 0)) + 1
    ctx.store_set("seen", seen)
    if seen % int(ctx.config["every"]) != 0:
        return None
    ctx.note("module_error", f"injected 503 into request {seen}", severity="warning")
    return RequestMutation(
        short_circuit=ctx.synthesize(status=503, content_type="application/json",
                                     body=b'{"error":"injected"}')
    )
```

Deterministic on purpose. Random failure finds bugs but cannot be replayed, so a
failure you saw once becomes a failure you argue about.

The counter lives in `ctx.store`, not a module-level variable, so it survives a
reload — otherwise editing the file silently resets the experiment.

### Watch WebSocket frames

From [`examples/modules/ws-inspect`](../examples/modules/ws-inspect/module.py).

```python
def on_websocket_message(message, request, ctx):
    if message.opcode != "text":
        return
    try:
        text = message.payload.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return
    if "unauthorized" in text.lower():
        ctx.note("module_error", "socket said unauthorized",
                 severity="warning", index=message.index)
```

**Read-only.** Frames are inspection-only in v1, and whatever this hook returns
is ignored — deliberately, because a hook whose return value were quietly
dropped while provenance claimed a change would be worse than one that cannot
change anything at all.

Guard the decode. Treating a binary frame as UTF-8 to search it for words is how
a working socket becomes an exception on every frame.

---

### Pin a flaky endpoint

When a backend is intermittently wrong and you are trying to reproduce
something *else*, pinning it removes a variable. Remember the first response,
replay it for every later request:

```python
def on_response(request, response, ctx):
    if not ctx.matches(request, path="^/api/flags$"):
        return None
    if ctx.store_get("flags") is not None or response.body is None:
        return None
    ctx.store_set("flags", response.body)
    return None


def on_request(request, ctx):
    if not ctx.matches(request, path="^/api/flags$"):
        return None
    pinned = ctx.store_get("flags")
    if pinned is None:
        return None
    return RequestMutation(
        short_circuit=ctx.synthesize(status=200, content_type="application/json", body=pinned)
    )
```

`ctx.store_*` is **persistent** and per-module (REQ MOD-022) — SQLite behind a
write-through cache, so the pin outlives a daemon restart. If you want it gone,
`ctx.store_delete` it; restarting will not do it for you.
Built step by step in
[the Python tutorial](tutorial-python-module.md).

---

## The Python tier

Add `module.py` when a rule cannot express the condition. Hooks interleave with
declarative rules by module priority (REQ MOD-023) — the Python tier is not a
separate pass bolted on afterwards.

```python
def on_load(ctx): ...
def on_unload(ctx): ...
def on_request(request, ctx) -> RequestMutation | None: ...
def on_response(request, response, ctx) -> ResponseMutation | None: ...
def on_websocket_message(message, request, ctx) -> None: ...
```

Return `None` for no change. Raising is caught, attributed to your module, and
does not affect the flow; repeated raises quarantine the module.

### The mutation objects

```python
from pporlock.engine.models import RequestMutation, ResponseMutation

RequestMutation(
    set_headers={"x-thing": "1"},      # replaces every occurrence
    add_headers=[("x-thing", "2")],    # appends another
    remove_headers=["cookie"],
    body=b"...",
    redirect=RedirectSpec(host="localhost", port=5173),
    short_circuit=ctx.synthesize(...),  # ends the flow here
)

ResponseMutation(set_headers=..., add_headers=..., remove_headers=...,
                 status=503, body=b"...")
```

### The `ctx` surface

```python
ctx.name, ctx.version, ctx.config, ctx.profile

ctx.matches(request, host="*.example.com", path="^/api/", method="POST",
            dest="script", content_type="json", response=response)  # -> bool

ctx.log("info", "message", **fields)
ctx.note("csp_modified", "message", severity="warning", **detail)

ctx.store_get(key, default); ctx.store_set(key, value); ctx.store_delete(key)

ctx.asset_path("f.js")  # -> Path, confined to assets/
ctx.asset_bytes("f.js"); ctx.asset_text("f.js")

ctx.register_transform("name", fn, cost="expensive")
ctx.synthesize(status=200, content_type=None, body=b"")
ctx.stub_for(request.dest, request)
```

`ctx.matches` takes the **request positionally**. The context is per-module and
long-lived; it does not know which flow you mean.

`ctx.note`'s severity comes last and defaults to `"warning"`. A code outside the
taxonomy becomes `module_error` carrying the code you asked for, rather than
raising — one typo must not take down the body phase.

### Registering a transform

```python
def uppercase_keys(text, params):
    return text.upper()

def on_load(ctx):
    ctx.register_transform("shout", uppercase_keys, cost="expensive")
```

Then use `transform: { kind: shout }` in any rule in that module.

**Cost defaults to `expensive` on purpose.** The scheduler uses it to decide what
may run inline on the event loop. A module transform assumed cheap is how one
module makes every page slow. There is no parameter validation for a
module-registered transform — yours is responsible for its own arguments.

---

## Ordering

Two different semantics, and knowing which you are in explains most surprises:

- **`block`, `map_local`, `redirect` — first match wins**, across all enabled
  modules. Evaluation of that class stops.
- **`headers`, `body` — all matches apply**, in module `priority` ascending,
  then declaration order within a module.

Lower priority runs earlier. The example library uses 10 (adblock) through 80
(ws-inspect) and a test asserts none of them collide, because they are meant to
be enabled together.

### Composition between the tiers

Declarative transforms run first, then `on_response` hooks — and the hooks see
the body **as the transforms left it**. So a rule and a hook editing the same
body compose rather than race.

This was not always true. Until recently the hooks ran against the original
response and the transform result was written over the hook's afterwards, so a
hook's body edit vanished whenever any body rule also matched — while provenance
recorded the hook as `applied`. `examples/modules/cookie-banners` found it, and
`TestTheTwoTiersCompose` now pins it.

---

## Performance

The engine's own decision path is about **0.004 ms per flow**, so a rule set is
essentially free. What costs is buffering.

| Choice | Cost |
|---|---|
| A rule that matches nothing | nothing — no buffering |
| A `body` rule matching broadly | the proxy holds every matching body in memory |
| `regex_sub` on large HTML | the expensive one; offloaded off the event loop |
| A module-registered transform | assumed expensive unless you say otherwise |

Scope `body` rules by `content_type` **and** `host` or `path`. A body rule
matching `content_type: text/html` alone buffers every page you load.

Each flow has a time budget. Exceed it and later transforms report
`skipped_budget` with a `transform_budget_exceeded` note — the work is cut, not
queued.

---

## Debugging

A module that appears to do nothing, in the order worth checking.

**1. Did it load?** The module library shows load errors inline, and
`pporlock run` prints failures at startup by name. A module with a syntax error
loads as `load_error` and is listed — it never silently disappears.

**2. Is it enabled?** Creating never enables. Neither does editing.

**3. Does the profile include it?** A non-default profile narrows which modules
contribute.

**4. Read the provenance.** Open the flow in the web UI or the DevTools panel.

| What you see | What it means |
|---|---|
| Rule absent entirely | It never matched. Compare the match against the request *as recorded* — that is what the next rule saw. |
| `no_change` | It matched and the transform found nothing to change. Your **pattern** is wrong, not your match. |
| `skipped_streamed` | The body was not buffered. See the buffering rules above. |
| `skipped_short_circuit` | An earlier `block`/`map_local`/`redirect` ended the flow. `short_circuited_by` names it. |
| `skipped_budget` | The time budget ran out first. |
| `error` | Your rule or hook raised; the detail carries the exception. |
| `applied`, but the page is unchanged | The change reached the wire and the browser rejected it. Classic causes: an injected script without the page's nonce, or a served file failing SRI. |

**5. Do not use the flags column to check whether a *hook* fired.** A
declarative `map_local` shows `LOC`; a Python hook that synthesises a response
does the same job and shows **no flag at all**, because `short_circuit` is set
from the declarative path only (OI-27). The hook's entry is in provenance —
that is where to look. This has misled people into thinking a working hook
never ran.

**6. Dry run it** against captured flows before enabling — remembering that dry
run executes your Python.

---

## Anti-patterns

**`match: { host: "*" }` on a body rule.** Buffers every page on every site. Use
it only in a module you enable for one task and turn off after.

**Parsing HTML with `regex_sub`.** Works until the markup changes. Inject a
script and use the browser's parser.

**Module-level mutable state.** Reset on every reload, so editing the file
silently resets it. Use `ctx.store`.

**`strip_csp` as a first move.** It turns off a real protection on a page you are
going to keep using. Try `reuse_nonce` first; reach for `strip_csp` when you
have established the nonce path cannot work.

**A bare `except:` in a hook.** Errors are already isolated and attributed.
Swallowing them converts a legible `module_error` note into a module that
silently does nothing — the failure this whole system exists to prevent.

**Inventing a note code.** `ctx.note`'s first argument is a closed vocabulary.
An unrecognised code does not raise — it degrades to a **`module_error`** note
carrying the code you asked for, so `ctx.note("pinned", …)` publishes something
that reads, to anyone else looking at that flow, as your module failing. Use
`ctx.log` for "here is what I did"; reserve `ctx.note` for the taxonomy's codes,
which describe things a *user* must be warned about. Whether modules should be
able to extend the taxonomy is open — OI-15.

**Trusting `applied`.** It means the engine did what the rule said. Whether that
achieved what you meant is a separate question, and only the page can answer it.

---

## See also

- **[Tutorial: your first declarative module](tutorial-declarative-module.md)** —
  build one from an empty directory, checking after each step that it ran.
- **[Tutorial: a Python module with state](tutorial-python-module.md)** — the
  point at which declarative rules stop being enough.

- [Module authoring](module-authoring.md) — the shorter introduction and the trust model
- [Troubleshooting](troubleshooting.md) — when the page is subtly wrong
- [Worked example](worked-example.md) — one problem end to end
- [`examples/modules/`](../examples/modules/) — the eight modules this file draws on
- [Rule and manifest schema reference](rule-schema.md) — every field, generated from the JSON Schema
- [Control API reference](api-reference.md) — every route, generated from the OpenAPI spec
- `docs/spec-0-contracts.md` §5 and §8 — the normative rule schema and module API
