# Tutorial: a Python module with state

**REQ DOC-003.** Build a module whose behaviour depends on what it has already
seen — the point at which declarative rules stop being enough. About thirty
minutes, and it assumes you have done
[the declarative tutorial](tutorial-declarative-module.md).

> **Module code is trusted and unsandboxed. There is no jail, no import
> allowlist, and no resource limit.** A module runs with your user's
> privileges and can do anything you can. **Dry run executes it too** — it is a
> rehearsal of the modification, not a sandbox. The guardrails are error
> isolation, failure quarantine, and a per-flow time budget; none of them is a
> security boundary. Read code before you enable it.

---

## When you actually need Python

Reach for the Python tier when the answer depends on something a `match:` block
cannot express:

- **State across flows** — "the same as last time", "only the first one".
- **Reading the body to decide** — parsing JSON and acting on a field.
- **Computation** — signing, decoding, deriving a value.
- **Conditional logic** — where a rule would need an `if` it does not have.

Not for things a rule already does. A `headers` rule is faster, cannot raise,
and is legible to someone who does not read Python. **Most modules should be
mostly declarative**, with Python only where it earns its place.

## What you will build

`pin` — a module that makes a flaky endpoint deterministic. The first response
it sees for a matching URL is remembered; every later request for that URL is
answered from memory without touching the network.

That is a genuinely useful debugging tool: when a backend is intermittently
wrong and you are trying to reproduce something *else*, pinning it removes one
variable. It also exercises most of the module API — both request and response
hooks, cross-flow state, synthesised responses, notes, and a registered
transform.

---

## Step 1 — the skeleton

```bash
mkdir -p ~/.pporlock/modules/pin
```

`~/.pporlock/modules/pin/module.yaml`:

```yaml
name: pin
version: "1.0.0"
pporlock_api: "1"
description: Pin an endpoint's first response and replay it.
author: me
enabled: false
priority: 55

config:
  pattern: "^/dest/json$"

rules: []
```

`config:` is arbitrary YAML the module reads as `ctx.config`. Putting the URL
pattern here rather than in the code means changing what you pin does not mean
editing Python — and a config value cannot introduce a syntax error that stops
the module loading.

`~/.pporlock/modules/pin/module.py`:

```python
"""Pin an endpoint's first response and replay it for later requests."""


def on_load(ctx):
    ctx.log("info", "pin ready", pattern=ctx.config.get("pattern"))
```

Reload. The module should load and report healthy with a single hook.

**Load it before it does anything.** A module that fails to import fails
loudly, and finding that out now — with four lines in the file — is much
cheaper than finding it out with eighty.

---

## Step 2 — capture the first response

Add to `module.py`:

```python
from pporlock.engine.models import ResponseMutation


def _key(request):
    # Host included: two sites can serve the same path and mean different
    # things, and a pin keyed on path alone would cross them over.
    return f"{request.host}{request.path}"


def on_response(request, response, ctx):
    if not ctx.matches(request, path=ctx.config.get("pattern", "^$")):
        return None

    key = _key(request)
    if ctx.store_get(key) is not None:
        return None                      # already pinned; step 3 serves it

    body = response.body
    if body is None:
        # Streamed, or a body we never buffered. Recording nothing is correct;
        # pinning a body we did not see would be a lie.
        return None

    ctx.store_set(key, {"status": response.status, "body": body.decode("utf-8", "replace")})
    ctx.log("info", "pinned", key=key, status=response.status)
    return None
```

Three things worth stopping on.

**`ctx.matches` takes the request positionally.** The context is per-module and
long-lived — it exists across every flow — so it cannot know which request you
mean. This is the single most common mistake when moving from the docs to real
code, and it used to be documented backwards.

**Returning `None` means "I propose no change".** It is not an error and not a
failure; it is the normal case. A hook that returns `None` most of the time is a
well-behaved hook.

**`response.body` can be `None`.** Large responses stream rather than buffer.
Every hook that reads a body needs this branch, and the honest thing to do is
nothing rather than guess.

Restart, enable, and hit the endpoint twice:

```bash
curl -s -x http://127.0.0.1:8080 http://127.0.0.1:8099/dest/json
curl -s -x http://127.0.0.1:8080 http://127.0.0.1:8099/dest/json
```

### Why `ctx.log` and not `ctx.note`

`ctx.note` writes into provenance, and its first argument is a **closed
vocabulary** — `csp_modified`, `sri_stripped`, `script_injected`,
`module_error`, and a dozen others. Every client renders notes from that list
and has a completeness test over it, so there is nowhere for a code you invent
to be described.

An unrecognised code does not raise — one typo must not take down the body
phase for every other module — it degrades to a **`module_error`** note
carrying the code you asked for. Which means `ctx.note("pinned", ...)` produces
something that reads, to anyone else looking at the flow, as your module
failing. Use `ctx.log` for "here is what I did" and reserve `ctx.note` for the
taxonomy's codes, which describe things a *user* needs to be warned about.

Whether modules should be able to extend the taxonomy is a genuinely open
design question — `docs/open-issues.md` OI-15.

Now hit the endpoint twice and open the second flow's provenance: the `pin`
entry appears on both, and your log line only on the first — which tells you
the state survived between flows.

---

## Step 3 — serve the pin

Now the interesting half. Add:

```python
from pporlock.engine.models import RequestMutation


def on_request(request, ctx):
    if not ctx.matches(request, path=ctx.config.get("pattern", "^$")):
        return None

    pinned = ctx.store_get(_key(request))
    if pinned is None:
        return None                      # nothing pinned yet; let it through

    ctx.log("info", "served pinned response", key=_key(request))
    return RequestMutation(
        short_circuit=ctx.synthesize(
            status=pinned["status"],
            content_type="application/json",
            body=pinned["body"].encode(),
        )
    )
```

Hit it a third time. The request never reaches the origin — stop the fixture
server and confirm it still answers.

That last check is the one worth doing. It is the difference between "my module
returned something" and "my module replaced the network", and only one of them
survives the origin going away.

### What the flow table shows — and what it does not

A 200, and **no flag at all**.

This surprises people, so it is worth being exact. The `short_circuit` field is
set from the *declarative* path: a `map_local` or `block` rule records which
rule ended evaluation. A Python hook returning a synthesised response takes a
different route, so the flow carries `short_circuit: null` and shows no `LOC`
badge — even though your module replaced the network just as completely.

The evidence is in provenance rather than the flags column: the `pin` module
appears with an `on_request` entry marked `applied`. That is enough to trace it,
but it is less legible than the declarative equivalent, and it is a real gap
rather than a design decision — recorded as OI-27.

The practical consequence for you: **do not use the flags column to check
whether your hook fired.** Open the provenance, or log.

---

## Step 4 — a transform the declarative tier can use

Registering a transform lets a *rule* call your Python. This is how a module
stays mostly declarative while still doing something rules cannot:

```python
def _redact_numbers(text, params):
    import re
    return re.sub(r"\d", params.get("with", "#"), text)


def on_load(ctx):
    ctx.log("info", "pin ready", pattern=ctx.config.get("pattern"))
    ctx.register_transform("redact_numbers", _redact_numbers, cost="expensive")
```

Then any rule in this module can use it:

```yaml
rules:
  - name: redact-digits
    action: body
    match:
      content_type: "application/json"
    transform:
      kind: redact_numbers
      with: "*"
```

**Cost defaults to `expensive`, and leaving it there is usually right.** The
scheduler uses it to decide what may run inline on the proxy's event loop
versus what gets offloaded. A transform declared cheap that is not is how one
module makes every page slow — and there is no parameter validation for a
module transform, so yours is responsible for its own arguments.

### The two tiers compose

Declarative transforms run **first**, then `on_response` hooks — and the hooks
see the body as the transforms left it. So a rule and a hook editing the same
body build on each other rather than racing.

This was not always true. The hooks used to run against the *original* response
and the transform result was written over the hook's afterwards, so a hook's
body edit silently vanished whenever any body rule also matched — while
provenance recorded the hook as `applied`. It was found by writing
`examples/modules/cookie-banners`, which is much of why that example library
exists.

---

## Step 5 — failing well

Make your hook raise on purpose:

```python
def on_response(request, response, ctx):
    raise RuntimeError("boom")
```

Then watch what happens. The flow completes. The error is attributed to your
module in provenance, with a `module_error` note. Nothing else on the page is
affected.

Now do it repeatedly. After enough consecutive failures the module is
**quarantined** — disabled at runtime, reported, and skipped until you fix and
reload it. One broken module must not be able to take down a browsing session.

That is the whole safety story, and it is worth being precise about what it is
not: it is *containment of mistakes*, not a security boundary. A module that
wants to read your files can. The trust model is that you read module code
before enabling it — which is why the MCP interface, where an agent writes
modules, deliberately **cannot enable the module it just created**.

### The time budget

Each flow carries a budget. A hook that overruns is recorded as such and the
flow proceeds. Do not sleep, do not make network calls, and do not do anything
unbounded — a hook runs in the path of a page load, and the person waiting is
you.

---

## Step 6 — the ctx surface, in full

```python
ctx.name, ctx.version, ctx.config, ctx.profile

ctx.matches(request, host="*.example.com", path="^/api/", method="POST",
            dest="script", content_type="json", response=response)  # -> bool

ctx.log("info", "message", **fields)
ctx.note("csp_modified", "message", severity="warning", **detail)

ctx.store_get(key, default); ctx.store_set(key, value); ctx.store_delete(key)

ctx.asset_path("f.js")        # -> Path, confined to this module's assets/
ctx.asset_bytes("f.js"); ctx.asset_text("f.js")

ctx.register_transform("name", fn, cost="expensive")
ctx.synthesize(status=200, content_type=None, body=b"")
ctx.stub_for(request.dest, request)
```

Two signatures that are not what you would guess:

- **`ctx.note(code, message, severity="warning", **detail)`** — severity comes
  last. A code outside the taxonomy degrades to `module_error` carrying the code
  you asked for, rather than raising: one typo must not take down the body
  phase for every other module.
- **`ctx.stub_for(dest, request)`** — the request is required, because the
  Accept-header fallback needs it when `Sec-Fetch-Dest` is absent.

**`ctx.store_*` is persistent and per-module** (REQ MOD-022). It is SQLite
backed by `~/.pporlock/module-store.db`, with a write-through in-memory cache so
a read never touches disk on the proxy's event loop. It survives a daemon
restart and is not shared between modules.

That is worth knowing before you rely on it either way. For `pin` it means a
pinned response outlives the restart you were counting on to clear it — use
`ctx.store_delete` or a versioned key if you want a shorter lifetime. Note also
that deleting a module does **not** delete its store.

---

## The hooks

| Hook | Signature | Returns |
|---|---|---|
| `on_load` | `(ctx)` | — |
| `on_unload` | `(ctx)` | — |
| `on_request` | `(request, ctx)` | `RequestMutation` or `None` |
| `on_response` | `(request, response, ctx)` | `ResponseMutation` or `None` |
| `on_websocket_message` | `(message, request, ctx)` | ignored — frames are read-only in v1 |
| `on_report` | `(ctx)` | `{"content_type": ..., "body": ...}`, a plain string, or `None` |

`on_report` is not a flow hook. It is called on demand from the control API, so
it is outside the per-flow time budget and may walk everything the module has
accumulated. The daemon serves the result at `GET /modules/<name>/report` and
the module library links to it — which is how a module that tallies something
makes it findable. Content types are limited to `text/html`, `text/plain`,
`text/csv` and `application/json`, and the response is served under a `sandbox`
CSP because the body is yours and the control origin is not.

`on_websocket_message`'s return value being ignored is deliberate, not an
oversight: PXY-051 says frames are not modifiable in v1. It is worth knowing
because a hook that returns a mutation there will look like it is doing
something.

---

## Where to go next

- **[The cookbook](module-cookbook.md)** — including a Python tier section and
  the anti-patterns.
- **`examples/modules/cookie-banners`** — the closest shipped module to this
  one, and the one that found the composition bug.
- **`examples/modules/fault-lab`** — deliberate fault injection, and the
  clearest example of a module whose whole job is to break things carefully.
- **[SPEC-0 §8](spec-0-contracts.md)** — the module API stability contract.

## What to take away

- **Use Python only where a rule cannot do it.** Mostly-declarative modules are
  faster, safer, and easier for someone else to read.
- **`ctx.matches(request, ...)` — request first, positionally.**
- **`response.body` can be `None`.** Handle it, do not guess.
- **Returning `None` is the normal case**, not a failure.
- **A raising hook is contained; a malicious one is not.** Read before you
  enable.
