# A worked example: fixing a page that a CDN's SRI breaks

**REQ DOC-006.** One real problem, start to finish, twice — once through the web
UI and once through MCP. The point is not the specific site; it is the shape of
the loop, and the fact that both paths end at the same guardrail.

---

## The problem

You are working on a local build of a site. It loads its main bundle from a CDN
with a subresource-integrity hash:

```html
<script src="https://cdn.example.com/app.v3.js"
        integrity="sha384-oqVuAfXRKap7fdgcCY5uykM6+R9GqQ8K/uxy9rx7HNQlGYl1kPzQho1wx4JwY8wC"
        crossorigin="anonymous"></script>
```

You want to point that at your local build. The page loads, and the script never
runs — **with no error you can act on**. Chrome's console says the integrity
check failed, which is true and unhelpful: you know you changed the file.

This is the characteristic failure. The page is subtly wrong and the cause is
one layer down from where you are looking.

---

## Path A — the web UI

### 1. See it

With the proxy on, reload the page and open the flow table at
`http://127.0.0.1:8081/`. Filter by host `cdn.example.com`.

The request for `app.v3.js` is there, `200`, unmodified. Nothing pporlock did
caused this — which is itself the first useful fact, and it took one look.

### 2. Create a rule from the flow

Select the flow for the **document** — not the script. The integrity attribute
lives in the HTML, so that is what has to change.

**Create rule from flow.** The match arrives pre-filled from the request:

```yaml
match:
  host: "app.example.com"
  path: "^/$"
```

Two clicks, and the tedious, error-prone part is done.

### 3. Choose the action

The rule wants to change the document body, so: action `body`, transform
`strip_integrity_attributes`.

```yaml
- name: drop-sri-on-the-shell
  match:
    host: "app.example.com"
    path: "^/$"
  action: body
  transform:
    kind: strip_integrity_attributes
```

Note what you did **not** write: a regex against HTML. Transforms are named
registry entries precisely so that "strip integrity attributes" is a thing the
system understands rather than a pattern you got nearly right.

### 4. Point the script at your build

A second rule, and this one is a short-circuit action:

```yaml
- name: serve-my-bundle
  match:
    host: "cdn.example.com"
    path: "^/app\\.v3\\.js$"
  action: map_local
  file: app.js          # resolved inside this module's assets/
  content_type: application/javascript
```

Put your build at `~/.pporlock/modules/local-bundle/assets/app.js`.

### 5. Dry run

**Dry run** against the flows you already captured. Read the diff: the
`integrity` and `crossorigin` attributes disappear from the shell, and the CDN
request is served from your file.

If `map_local` shows `map_local_missing`, the path is wrong — and it says so,
rather than silently serving the real bundle, which is what makes this failure
findable at all.

### 6. Enable it

Enabling is a separate, explicit act. Creating a module never enables it
(REQ MOD-003), whether you are clicking or an agent is calling.

### 7. Confirm

Reload. Open the DevTools panel on the page and read the provenance:

- the document flow shows `strip_integrity_attributes` → **applied**, with an
  `sri_stripped` note
- the CDN flow shows `map_local` → **applied**
- and a banner appears at the top of the page saying pporlock removed
  subresource-integrity attributes

That banner is not decoration. You have just turned off a security control on a
page you are going to keep looking at, and in a week you will not remember.

**`applied` and *did what you meant* are different claims.** Only one of them
the system can make for you — which is why step 7 exists.

---

## Path B — the same thing through MCP

An agent with only MCP access does the same loop, and hits the same walls.

```
list_flows(host="cdn.example.com", limit=50)
```

Summaries, capped, because a tool that returns everything is a tool that burns
the context you needed for the actual problem (REQ MCP-005).

```
get_provenance(flow_id="...")
```

Provenance only — no headers, no bodies. Enough to answer "what did pporlock do
to this", which is the question.

```
suggest_rule_from_flow(flow_id="...")
create_module(name="local-bundle", files={"module.yaml": "...", ...})
```

`create_module` sends `{name, files}` and nothing else. There is no `enabled`
parameter in its schema, and a test drives it with `enabled=True` to prove the
wire body is unchanged. **The module is created disabled.**

```
validate_module(name="local-bundle")
dry_run(module="local-bundle", include_diffs=true)
```

Dry run **executes Python hooks**, by design, so that its result matches live
behaviour (REQ CAP-032). For an agent-authored module that means the agent's own
code runs on your machine before you have read it. This is stated rather than
hidden, and it is the reason the trust warning leads the authoring guide.

```
set_module_enabled(name="local-bundle", enabled=true)
```

A separate call, made deliberately. This is the guardrail: an agent cannot go
from "I wrote a module" to "it is intercepting your traffic" in one step.

### What the agent cannot do

- **Unmask anything.** `unmask`, `unredact`, `reveal`, and `unmask_field` are
  refused in the HTTP client, before the network, and again at the tool layer.
  No tool schema names them, and every schema is `additionalProperties: false`.
  Reveal is web-UI-only, live-ring-only, one value at a time (SPEC-0 §9.3).
- **Act unaudited.** Every request carries `X-Pporlock-Client: mcp`, set once in
  the header builder so no tool can forget.
- **Enable what it just created.** See above. This is the one that matters.

---

## What the example is actually about

The loop is: **notice → attribute → change → dry run → enable → confirm.**

Three properties make it work, and all three are deliberate:

1. **Attribution is structural.** Provenance is a return value of the engine,
   carried by every flow, not a log line you hope was written (REQ CAP-010).
   That is why step 1 could rule pporlock out in one look.
2. **A modification announces itself.** The banner, the note, the badge. A tool
   that weakens a page's protections invisibly will eventually surprise you
   badly, and most likely while you are debugging something else.
3. **Creating is not enabling.** Identical on both paths, because the risk is
   identical — and on the MCP path the author is something that does not have to
   live with the consequences.

---

## See also

- [Module authoring](module-authoring.md) — the full rule and `ctx` reference,
  and the trust model warning
- [Troubleshooting](troubleshooting.md) — when step 7 says it did not do what
  you meant
