# Tutorial: your first declarative module

**REQ DOC-003.** Build a working module from an empty directory, one rule at a
time, checking after each that it actually ran. About twenty minutes.

This is a *tutorial* — it teaches by building one thing. When you want the full
list of what a rule can do, that is
[module-authoring.md](module-authoring.md); when you want a solution to a
specific problem, that is the [cookbook](module-cookbook.md).

> **Module code is trusted and unsandboxed, and dry run executes it too.**
> Everything here runs with your user's privileges. This tutorial has no Python
> in it, but the tier next door does — see
> [the Python tutorial](tutorial-python-module.md).

---

## What you will build

A module called `tutorial` that does three things to one site:

1. adds a header so you can prove interception is working at all,
2. rewrites text in the page body,
3. serves a file from the module's own `assets/` instead of the network.

Those three cover the shapes almost every real module is made of: **observe**,
**modify**, **replace**.

## Why we aim it at a fixture, not a real site

The repository ships an origin server for exactly this:

```bash
make fixtures      # serves http://127.0.0.1:8099
```

Point a tutorial at a real site and it rots the first time that site redesigns.
More importantly, you cannot tell "my rule is wrong" from "the site changed"
while you are still learning what a rule does. The fixture is stable, offline,
and yours.

Leave it running in its own terminal. In another:

```bash
pporlock run
```

Open `http://127.0.0.1:8081/` — the flow table is where you will check your
work.

---

## Step 1 — an empty module that loads

A module is a directory with a manifest. Create it in your state directory:

```bash
mkdir -p ~/.pporlock/modules/tutorial
```

`~/.pporlock/modules/tutorial/module.yaml`:

```yaml
name: tutorial
version: "1.0.0"
pporlock_api: "1"
description: Learning the declarative tier.
author: me
enabled: false
priority: 50

rules: []
```

Four fields deserve a word:

- **`name` must match the directory.** The loader keys on the directory and the
  manifest names itself; a mismatch is caught at load rather than becoming a
  module you cannot enable by name.
- **`pporlock_api: "1"`** is the module API generation. It is how a future
  breaking change can refuse to load a module written against the old shape,
  rather than misbehaving.
- **`enabled: false`** is deliberate. Enablement is *your* state, not the
  author's, and it lives in a sidecar the daemon owns
  (`~/.pporlock/module-state.json`). Once a module has been seen, editing
  `enabled:` in the manifest does nothing — the API is where you turn it on.
- **`priority: 50`** decides ordering against other modules. Lower runs earlier.
  The shipped examples use 10–80, so 50 sits in the middle of them.

Reload and look:

```bash
curl -s -X POST http://127.0.0.1:8081/modules/reload \
  -H "Authorization: Bearer $(cat ~/.pporlock/token)" \
  -H "X-Pporlock-Client: cli" | head -c 200
```

Or just open the Modules page in the web UI. Either way the module should appear
as loaded, healthy, and off.

**If it does not appear, stop here.** A module that fails to load reports why —
read that before adding rules. Debugging a manifest is much easier with no rules
in it.

---

## Step 2 — a header, to prove anything is happening

The first rule of any new module should be one whose effect is unmistakable.
Add to `rules:`:

```yaml
rules:
  - name: mark-responses
    action: headers
    match:
      host: "127.0.0.1"
    response:
      set:
        X-Tutorial: "hello"
```

Reload, then **enable it** — through the API, not the manifest:

```bash
curl -s -X PATCH http://127.0.0.1:8081/modules/tutorial \
  -H "Authorization: Bearer $(cat ~/.pporlock/token)" \
  -H "Content-Type: application/json" \
  -H "X-Pporlock-Client: cli" \
  -d '{"enabled": true}'
```

Now make a request through the proxy:

```bash
curl -s -D- -o /dev/null -x http://127.0.0.1:8080 http://127.0.0.1:8099/ | grep -i x-tutorial
```

You should see `X-Tutorial: hello`.

### If you don't

Work down this list, in order — it is ordered by how often each is the answer:

1. **Is the module enabled?** Check the Modules page. A loaded module and an
   enabled one look similar and behave completely differently.
2. **Did the request go through the proxy?** `-x http://127.0.0.1:8080`. A
   request that bypassed the proxy is not a rule problem.
3. **Does the flow appear in the flow table at all?** If not, it was excluded —
   see the exclusions list — or it never reached the daemon.
4. **Does the flow show `MOD`?** If the row is there and unflagged, your `match`
   did not match. That is a matching problem, not a rule problem, and the next
   section is about matching.

This ladder is worth internalising. Nearly every "my module doesn't work" is one
of these four, and they need completely different fixes.

---

## Step 3 — matching, which is where the time actually goes

Widen and narrow the rule until you understand what it selects. Try:

```yaml
    match:
      host: "127.0.0.1"
      path: "^/$"
```

`host` is a glob (`*.example.com`), `path` is a **regex**. Mixing them up is the
single most common mistake: `path: "/api/*"` is a regex meaning "`/api` followed
by any number of `/`", which is almost certainly not what you meant.

Now try adding:

```yaml
      content_type: "text/html"
```

and confirm it still fires for the page. Then change it to `application/json`
and confirm it stops. **Watching a rule stop matching is how you learn what a
matcher does** — much faster than reading about it.

> **A trap worth meeting early.** `dest: document` looks like the precise way to
> say "only real page loads". It is, in a browser, on HTTPS. `Sec-Fetch-Dest` is
> only sent on secure contexts, so on a plain `http://` origin — and in every
> `curl` you have run so far — pporlock has to infer it from `Accept`, and a
> rule requiring `dest` silently matches nothing. It will look like your rule is
> broken. The shipped `css-tamper` module carries a comment explaining exactly
> this, because it bit its author.

---

## Step 4 — changing the body

Add a second rule:

```yaml
  - name: rename-the-fixture
    action: body
    match:
      host: "127.0.0.1"
      content_type: "text/html"
    transform:
      kind: replace_literal
      find: "pporlock fixture origin"
      replace: "Tutorial says hello"
```

Reload — you do not need to re-enable; enablement survives a reload, and a
restart.

```bash
curl -s -x http://127.0.0.1:8080 http://127.0.0.1:8099/ | grep -i tutorial
```

Two things to notice.

**Both rules fired.** `headers` and `body` are *all-match* actions: every
matching rule applies, ordered by module priority and then declaration order.
That is different from the next step, and the difference explains most
surprises.

**A streamed response cannot be transformed.** Large bodies stream through
rather than being buffered, and a body rule against one is recorded as skipped
rather than silently doing nothing. If you see `STR` on a row, that is why.
Provenance will say `skipped_streamed`.

---

## Step 5 — replacing a response entirely

Now the third shape. Create an asset:

```bash
mkdir -p ~/.pporlock/modules/tutorial/assets
echo 'body { background: #fee; }' > ~/.pporlock/modules/tutorial/assets/tutorial.css
```

And a rule to serve it:

```yaml
  - name: serve-local-css
    action: map_local
    file: tutorial.css
    content_type: "text/css; charset=utf-8"
    match:
      path: "^/tutorial\\.css$"
```

```bash
curl -s -x http://127.0.0.1:8080 http://127.0.0.1:8099/tutorial.css
```

`file:` resolves inside **this module's `assets/`** and nowhere else. Symlinks
are resolved before the containment check, so a link pointing out of the
directory is refused rather than followed. This is not configurable, and it is
the one place the trusted-module model still draws a line.

### First match wins here — and only here

`map_local` belongs to the short-circuit class, with `block` and `redirect`:

| Class | Actions | Semantics |
|---|---|---|
| Short-circuit | `block`, `map_local`, `redirect` | **First match wins**, across all enabled modules. Evaluation of the class stops. |
| Accumulating | `headers`, `body` | **All matches apply**, by priority then declaration order. |

If two modules both want to `map_local` the same path, priority decides, and the
loser records nothing at all — not an error, just no entry. Knowing which class
you are in is most of understanding why a rule did not fire.

### A served file is not a blocked one

In the flow table this row shows **`LOC`**, not `BLK`, and its status is 200.
That distinction is newer than the feature: `map_local` used to be reported as
blocked, because all three short-circuit actions set the same flag. A module
that was working perfectly looked like it was breaking the page. If you are
reading an older screenshot, that is what you are seeing.

---

## Step 6 — read the provenance

Click the flow in the web UI and open its provenance. Every rule that considered
this flow is there, with an outcome — `applied`, `skipped_streamed`,
`no_match` — and a duration.

This is the part most worth your attention. Provenance is a **structural return
value of the engine**, not logging: every flow carries it, and it is generated
whether or not anything matched. When a rule does not do what you expect, the
answer is almost always visible here, and almost never requires adding a print
statement.

---

## Step 7 — dry run before you trust it

```bash
# Against the flows currently in the ring buffer:
pporlock dryrun live ~/.pporlock/modules/tutorial

# Or against a recorded session:
pporlock dryrun <session-id> ~/.pporlock/modules/tutorial
```

It takes a session id — or the literal `live` for whatever is in the ring
buffer — and the **path to one module's directory**. So the question it answers
is "what would *this* module do to traffic I have already seen", which is
sharper than "what would all my rules do".

**It executes module code.** Python hooks run, transforms run, and anything a
module does outside the response — writing a file, making a request — happens
for real. It is a rehearsal of the modification, not a sandbox.

---

## Where to go next

- **[The cookbook](module-cookbook.md)** — twelve recipes for specific problems,
  plus matching, ordering, performance and anti-patterns.
- **[The Python tutorial](tutorial-python-module.md)** — when declarative rules
  run out, which is sooner than you would like and later than you fear.
- **[module-authoring.md](module-authoring.md)** — the reference.
- **`examples/modules/`** — eight working modules, all tested. `header-lab` and
  `json-tamper` are the closest to what you just built.

## What to take away

- **Enablement is your state, not the manifest's.** Toggle through the API.
- **`host` is a glob; `path` is a regex.** Most matching bugs are this.
- **Short-circuit actions are first-match-wins; header and body rules all
  apply.** Most ordering surprises are this.
- **Check the flow table after every change.** A module that loads, is enabled,
  and matches nothing looks exactly like one that is working.
