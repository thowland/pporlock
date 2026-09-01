# Driving pporlock with an LLM

pporlock ships an MCP server so an agent can read what the proxy captured, read
the provenance that explains it, author modules, dry-run them, and operate the
daemon. This document is about getting good results out of that — how to connect
it, how to prompt it, what it can and cannot do, and how to review what it wrote.

The tool reference is **[`mcp/README.md`](../mcp/README.md)**: the tool table,
the caps, the guardrail-to-test mapping. This document does not repeat it.

One thing up front, because it colours everything else:

> **Module code is fully trusted. Dry run executes it. "I'll dry-run it first"
> is not a safety measure against code you have not read.**

The guardrails in this system stop an agent from *quietly* changing your
browsing. They do not stop code you enabled without reading. [Section
6](#6-reviewing-what-the-agent-wrote) is the part of this document that matters.

---

## 1. Setup

### Prerequisites

The MCP server is an ordinary HTTP client of the control API. It needs a daemon
that is installed, has run at least once (so `~/.pporlock/token` exists), and is
running when the agent calls a tool. It imports nothing from the `pporlock`
package and opens no database — if the daemon is down, every tool fails with
"cannot reach the pporlock daemon", not with stale data.

```bash
pporlock run        # leave it running
pporlock doctor     # 18 checks; fix anything failing before involving an agent
```

### Registering the server

The entry point is `pporlock-mcp` (`mcp/pyproject.toml` → `[project.scripts]`).
If it is on your `PATH`:

```jsonc
{
  "mcpServers": {
    "pporlock": {
      "command": "pporlock-mcp",
      "args": ["--base-url", "http://127.0.0.1:8081", "--state-dir", "~/.pporlock"]
    }
  }
}
```

From the repo, without installing:

```jsonc
{
  "mcpServers": {
    "pporlock": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/pporlock/mcp", "pporlock-mcp"]
    }
  }
}
```

The three flags are the whole CLI surface:

| Flag | Default | Effect |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8081` | Control API address. |
| `--state-dir` | `~/.pporlock` | Where the bearer token is read from. |
| `--read-only` | off | Registers **only** the introspection family. The authoring, validation and control tools are not advertised at all — the agent cannot see them, so it cannot try them. |

### The token, honestly

The MCP server **reads `~/.pporlock/token` directly**. It does not pair, and
there is no per-agent scoping: it runs as you, so it has exactly the access you
have. That is a deliberate asymmetry with the extension, which cannot read the
token file and must be paired with a short-lived code (REQ API-012) because it
runs inside a browser.

`PPORLOCK_TOKEN` overrides the file and `PPORLOCK_STATE_DIR` overrides where the
file is looked for. Both exist so the server can start in a test or a sandbox
without a daemon install. **Do not put the token in the MCP client config as an
env var** if that config is in a repo — the token file already works, and the
standing rule in this project is that the bearer token does not appear in URLs,
error bodies, or anything checked in.

An agent connected to this server can read every decrypted request you make
through the proxy, minus redacted values. Start with `--read-only` if you are
evaluating an agent you do not yet trust with your traffic; it is one flag and
it removes 19 of the 26 tools.

### The tools, by family

Read-only mode keeps the first family and drops the other three.

| Family | Tools |
|---|---|
| **Introspection** (7) | `list_flows`, `get_flow`, `get_provenance`, `flow_stats`, `list_websocket_messages`, `list_sessions`, `list_session_flows` |
| **Authoring** (6) | `list_modules`, `read_module`, `create_module`, `update_module`, `delete_module`, `suggest_rule_from_flow` |
| **Validation** (2) | `validate_module`, `dry_run` |
| **Control** (11) | `get_status`, `set_module_enabled`, `activate_profile`, `list_profiles`, `set_dev_toggle`, `start_recording`, `stop_recording`, `reload_modules`, `edit_exclusions`, `proxy_start`, `proxy_stop` |

Every schema is `additionalProperties: false`. An argument that is not in the
table in `mcp/README.md` is not an argument.

---

## 2. A system prompt you can paste

The server sends its own instructions on initialize, and a good MCP client shows
them to the model. This adds what the server cannot know: your intent, your
tolerance for changes to live browsing, and the review step at the end.

```text
You are working with pporlock, a local HTTPS interception proxy for Chrome on
this machine. Its MCP tools let you read captured traffic and author filter
modules. You have no other access to the browser.

THE LOOP. Follow it in order. Do not skip to authoring.

1. OBSERVE. get_status first — if the proxy is not running, nothing you do will
   have an effect and you should say so instead of proceeding. Then flow_stats
   to see the shape of the traffic, then list_flows or list_session_flows with
   filters to narrow. Only then get_flow on individual flows.
2. ATTRIBUTE. Before proposing any change, use get_provenance to establish what
   pporlock is already doing to the flow. "pporlock did not touch this" is a
   finding, and often the correct one. Read the provenance notes and the
   outcomes, not just whether a rule appears.
3. AUTHOR. suggest_rule_from_flow gives a starting point matched to a real
   observed request; it is not a finished module. Prefer declarative rules in
   module.yaml over Python. Write Python only for conditions a match block
   cannot express, and say in a comment why.
4. DRY RUN. validate_module (schema and syntax, installs nothing), then
   create_module, then dry_run against a recorded session with
   include_diffs=true. Read the diffs. Iterate until the result is what you
   intended, not merely non-empty.
5. ASK. Stop here. Show the human the full module source and the dry-run
   summary, and ask them to read it before it is enabled.
6. ENABLE only if they say to. set_module_enabled is the one call that changes
   live browsing.

TOKEN COST. Every tool states its cost in its description; respect it.
- list_flows and list_session_flows default to detail="summary", 50 flows
  (max 200). Summary has no bodies and collapses provenance to counts.
- get_flow defaults to detail="full" — headers and provenance, no large bodies.
  Ask for detail="bodies" on ONE flow at a time, never across a page.
- flow_stats aggregates up to 200 summary flows (max 1000) into counts. Use it
  before you list anything; it usually answers the question on its own.
- read_module truncates each file to 8000 chars; pass full=true only when you
  need the rest.
- dry_run evaluates 200 flows (max 500) and returns at most 20 per-flow
  results; include_diffs defaults to false and diff text is capped at 2000
  chars.
Filter server-side (host, path, method, status, content_type, dest, tab_id,
modified, blocked, module, note_code, since, until, q) rather than listing
broadly and filtering in your head.

THINGS THIS INTERFACE WILL REFUSE. Do not attempt them; you will lose turns.
- You cannot unmask redacted values. Secrets arrive as
  «redacted:sha1=abcd,len=42» and stay that way. The parameters unmask,
  unredact, reveal and unmask_field are refused before any request is sent.
  Unmasking exists only in the web UI, only on live flows, one value at a time.
  If a task needs a real secret value, say so and stop.
- You cannot enable a module by creating or updating it. create_module and
  update_module send {name, files} and nothing else. Enabling is always a
  separate set_module_enabled call.
- If a tool is not in the list you were given, it does not exist. The server
  may have been started with --read-only, in which case only the introspection
  tools exist and you should report what you found rather than trying to fix it.

TRUST. Module Python is fully trusted: no sandbox, no import allowlist. It runs
in the proxy process with the user's full privileges. dry_run executes it too,
so dry-running is not a safety step — it is a correctness step. Write module
code you would be willing to have read line by line, keep it minimal, and never
add a dependency, a file write, or a network call that the stated task does not
require. Call out in your summary anything that weakens page security:
strip_csp, strip_integrity_attributes, inject_script, map_local, redirect.

Worked example modules are installed under examples/modules/: adblock,
cookie-banners, css-tamper, fault-lab, header-lab, json-tamper, local-bundle,
ws-inspect. Read the closest one before writing a new module from scratch.
```

Trim it if your client charges for a long system prompt; the two paragraphs that
earn their keep every time are the cost list and the refusal list. Without the
cost list an agent will call `list_flows(detail="bodies", limit=200)` once and
spend the rest of the session out of context. Without the refusal list it will
spend three turns trying to talk its way past the unmask guard.

---

## 3. How to phrase a task

The agent sees three things: **flows** (what went over the wire, redacted),
**provenance** (what pporlock did to each flow and what it declined to do), and
**modules** (what is installed and enabled). It cannot see your screen, your
console, or the page. Every useful prompt gives it a way to get from something
it can see to the thing you care about.

**Vague:**

> The checkout page is broken, fix it.

This fails in a specific way. The agent has no host, no time window, and no
session, so it starts with an unfiltered `list_flows`, gets 50 summaries of
whatever is in the ring buffer, and guesses. It cannot tell "broken" from
"working" because it never sees the page. It will usually propose a module for
whatever looked most suspicious, which is not the same thing as the cause.

**Good:**

> I'm on `app.example.com/checkout`. The "Place order" button does nothing —
> no network request when I click it. I have modules `adblock` and
> `local-bundle` enabled. Record a session while I reproduce it, then tell me
> whether pporlock caused this. Don't write a module yet.

Why this works:

- **A host and a path** give it a `list_flows(host=...)` filter, so its first
  call is narrow and its 50-flow budget covers the page instead of the internet.
- **"no network request when I click"** is a symptom the agent can map onto
  something it can see: a blocked script, an `sri_stripped` note, a
  `response_streamed` note explaining why a body rule did not run.
- **Naming the enabled modules** lets it go straight to
  `list_flows(module="adblock")` and `get_provenance`, instead of inferring the
  rule set from its effects.
- **"Record a session"** means the evidence is durable and dry-runnable.
  Live-buffer flows age out; a session can be dry-run against later.
- **"Don't write a module yet"** stops it from skipping attribution. The most
  common agent failure here is proposing a fix for a problem it has not
  established pporlock caused.

The general shape: **give it an anchor it can filter on, a symptom it can map to
provenance, and an explicit stopping point.**

---

## 4. Worked scenarios

Argument names below are the real ones. See
[`docs/worked-example.md`](worked-example.md) for the same loop told end to end
through both the web UI and MCP.

### 4.1 "The page is subtly wrong — did pporlock do it?"

The characteristic failure this whole system exists for. The answer is often no,
and establishing that quickly is worth more than a fix.

| # | Call | What the agent should conclude |
|---|---|---|
| 1 | `get_status` | Proxy running, which profile is active, how many modules loaded, whether any failed to load or are quarantined. A quarantined module is frequently the entire answer. |
| 2 | `flow_stats(host="app.example.com")` | The notes histogram is the cheap signal. `csp_modified`, `sri_stripped`, `script_injected`, `map_local_missing`, `module_error` on this host each point at a different cause. All zeros and `modified: 0` means pporlock is not involved — stop and say so. |
| 3 | `list_flows(host="app.example.com", modified=true)` | Which flows were actually changed. If the symptom is a script not running, also `list_flows(host=..., dest="script", blocked=true)`. |
| 4 | `get_provenance(flow_id=...)` on the **document** flow | The full record: notes, `short_circuited_by`, and every rule's outcome. Read non-`applied` outcomes as carefully as applied ones — `skipped_streamed` means a body rule never ran, `no_change` means it ran and matched nothing. |
| 5 | `read_module(name="…")` | The rule named in the provenance entry. Confirm it matches more broadly than intended, which is the usual cause. |
| 6 | `set_module_enabled(name="…", enabled=false)`, then reload and re-check | Confirmation by removal. If the page is still wrong, this was *a* modification, not *the* cause. |

Do not let the agent skip step 6. `applied` and *caused your problem* are
different claims.

### 4.2 Block a tracker without breaking the page

The failure mode is blocking a script the page then waits on forever. The
defence is the stub: `block` with `mode: stub` serves a destination-appropriate
synthetic response rather than an error.

```
start_recording(name="tracker-repro")
   → reproduce in the browser →
stop_recording(session_id="…")

list_session_flows(session_id="…", dest="script")
   → find the tracker's flows; note the hosts and the Sec-Fetch-Dest values

suggest_rule_from_flow(flow_id="…", intent="block")
   → a candidate rule matched to that exact request. A starting point, not a
     module.

validate_module(files={"module.yaml": "…"})
   → schema and syntax only, nothing written, nothing enabled. Omit `name`;
     the daemon uses the manifest's own name.

create_module(name="block-vendor", files={"module.yaml": "…"})
   → created DISABLED. The response says so and names the next step.

dry_run(session_id="…", module_name="block-vendor", include_diffs=true)
   → how many flows it would have hit, and what it would have served
```

The judgement call is in the dry run. `mode: kill` drops the connection and a
page that expected a script gets a network error; `mode: stub` with `stub: auto`
derives a valid empty response from `Sec-Fetch-Dest`, which is why the page
usually survives. [`examples/modules/adblock`](../examples/modules/adblock) is
the reference for this — point the agent at it rather than letting it invent the
host-glob-versus-apex rule from scratch.

Blocking is **first-match-wins across all modules**, so a new blocker interacts
with existing ones. If the dry run shows fewer hits than expected, an earlier
module already short-circuited those flows; `get_provenance` on one of them
names it in `short_circuited_by`.

### 4.3 A JSON API needs a field changed for local testing

Turning on a feature flag the server will not send you.

```
list_flows(host="api.example.com", content_type="application/json", status=200)
get_flow(flow_id="…", detail="bodies")
```

One flow, `detail="bodies"`, deliberately — a page of bodies is how a context
window gets spent. Credential-shaped JSON keys (`token`, `secret`, `password`,
`session`, `auth`, …) come back masked; the agent sees the *shape* of the
response, which is what it needs to write a JSON Pointer.

```
create_module(name="flag-override", files={"module.yaml": "…"})
dry_run(session_id="…", files={"module.yaml": "…"}, include_diffs=true)
```

`dry_run` takes either `files` (an uninstalled candidate) or `module_name` (an
installed one). Passing `files` lets the agent iterate without touching the
module store at all. If it passes neither, the tool refuses.

[`examples/modules/json-tamper`](../examples/modules/json-tamper) is the model:
`json_patch` with RFC 6902 ops, matched on `host` + `path` + `content_type` +
`status`. Two things worth insisting on:

- **Match narrowly.** A body rule that matches widely makes the proxy buffer
  bodies it has no use for, on every site. That is a real cost, not a style
  point.
- **Read the diff, not the count.** `json_patch` on a non-JSON body reports
  `no_change` with an error note rather than failing the flow — a dry run that
  says "1 flow affected, no change" means the rule matched something that was
  not the JSON you meant.

---

## 5. What the agent cannot do, and why

Four properties you can rely on without auditing the transcript. Each is
enforced in code and covered by a named test (the mapping is in
[`mcp/README.md`](../mcp/README.md#guardrails)).

**It cannot unmask a redacted value.** `unmask`, `unredact`, `reveal` and
`unmask_field` are refused in `client.request` — before the HTTP request is
constructed — and again in `PporlockMCP.call_tool` before dispatch. No tool
schema names them, and every schema is `additionalProperties: false`. The
refusal is an error the agent reads, not a silently dropped parameter, because
an agent that is quietly ignored tries again. Unmasking is web-UI-only,
live-ring-only, per-value, audited (SPEC-0 §9.3); session data cannot be
unmasked by anyone.

*For review:* an agent transcript cannot contain a cookie, an `Authorization`
header, or a credential-shaped JSON value from your traffic. You do not have to
scan it for leaked secrets before pasting it somewhere.

**It cannot enable a module it created.** `create_module` and `update_module`
build a body of `{name, files}` and nothing else; `enabled` is absent from both
schemas, and a test drives them with `enabled=true` to prove the wire body is
unchanged. The responses restate that a separate `set_module_enabled` call is
required.

*For review:* there is no path from "the agent wrote a module" to "it is
intercepting your traffic" that does not pass through a call you can see and
approve. This is the guardrail that makes the rest of the workflow safe to
delegate.

**Every request is tagged and audited.** `X-Pporlock-Client: mcp` is set once in
`ControlClient._headers`, so no tool can forget it. The daemon requires it on
mutating requests, and the audit log's origin column is recorded rather than
inferred. The web UI shows an MCP activity indicator.

*For review:* the audit log is the ground truth for what the agent actually did,
independent of what it told you. Check it when the summary and the behaviour
disagree.

**`--read-only` removes the write families entirely.** `ToolRegistry.build`
filters by family, so the 19 non-introspection tools are not advertised, not
merely refused. An agent in read-only mode cannot write a module, cannot enable
one, cannot stop the proxy, and cannot edit the exclusion list.

*For review:* read-only is the right default for exploratory work — "explain
what is happening on this page" needs none of the write tools. Note that
`dry_run` is in the validation family, so read-only removes it too; that is
correct, because dry run executes module Python.

**What is not a guardrail:** module code itself. See the next section.

---

## 6. Reviewing what the agent wrote

**Module Python is fully trusted** (REQ MOD-030): no sandbox, no import
allowlist, no resource jail. It runs in the proxy process with your full user
privileges, and it sees every byte of your decrypted traffic. The only enforced
limits are error isolation, quarantine after repeated failures, and a per-flow
time budget — all of which contain *mistakes*, none of which contain intent.

**Dry run executes the Python hooks.** It must, or its result would not predict
live behaviour (REQ CAP-032). So dry run is a correctness check, not a safety
check, and dry-running an unread module is not safer than enabling it. Read
first, then dry-run.

This is not a warning about malicious agents specifically. The realistic failure
is duller: a competent agent writes a module that does what you asked and three
other things it thought were helpful.

### Checklist: `module.yaml`

| Look for | Why |
|---|---|
| A `match` block with no `host` | Applies to every site you browse. Occasionally intended; usually not. |
| `path` regex that is not anchored | `path` is `re.search`, not `fullmatch`. `"/api"` matches `/notapi/x`. |
| A `body` rule matching broadly on `content_type` | Forces the proxy to buffer responses on every matching site, on every page. A real latency cost. |
| `action: block` with `mode: kill` | Drops the connection rather than serving a stub. The page gets a network error. Prefer `stub`. |
| `map_local` with a `file` outside `assets/` | Paths are confined to the module's `assets/` with symlinks resolved before the containment check — but a rule that tries is a rule worth asking about. |
| `redirect` to a host you did not name | This sends your requests somewhere else. Read the target. There is no reason for an agent to introduce a host that was not in your task. |
| `priority` far from the default 100 | Changes ordering against your other modules. Should be justified in a comment. |
| Anything in `config:` you did not ask for | The Python tier reads it. A URL or a path here is worth following. |

### Checklist: `module.py`

Most legitimate modules are short and touch four things: `ctx.config`,
`ctx.matches`, `ctx.note`/`ctx.log`, and a returned `RequestMutation` or
`ResponseMutation`. Anything outside that shape deserves a sentence of
explanation.

- **Imports.** `socket`, `subprocess`, `os` beyond `os.environ` reads, `httpx`,
  `requests`, `urllib.request`, `smtplib` — a module has no reason to open a
  network connection or spawn a process. `pporlock.engine.models` for the
  mutation types is expected.
- **File I/O.** `open()` for writing, `pathlib.Path.write_*`, `shutil`. Module
  assets are read through `ctx.asset_bytes` / `ctx.asset_text` /
  `ctx.asset_path`, which are containment-checked. Direct filesystem access is
  not.
- **Dynamic execution.** `eval`, `exec`, `compile`, `__import__`,
  `base64.b64decode` of a literal, long hex or base64 strings. There is no
  benign reason for encoded payloads in a filter module.
- **Where request data goes.** The hook receives your decrypted traffic. Follow
  every use of `request` and `resp` that is not a match test or a mutation. Data
  reaching a file, a socket, or a log line at anything other than debug level is
  the thing to catch.
- **Module-level side effects.** Code at import time, outside `on_load`, runs
  when the module is loaded — including during dry run.
- **State.** `ctx.store_*` is the right place for counters; a module-level
  global silently resets on reload. See
  [`examples/modules/fault-lab`](../examples/modules/fault-lab).

### Transforms that weaken page security

Three transforms remove protections the site put there. Each emits a provenance
note and triggers the in-page banner on a document flow, so they are not
invisible — but they should be present because the task required them, and the
agent should have said so.

| Transform | What it removes | When it is legitimate |
|---|---|---|
| `strip_integrity_attributes` | Subresource integrity. The browser stops verifying those scripts. Note: `sri_stripped`. | You are substituting a local build — see [`examples/modules/local-bundle`](../examples/modules/local-bundle). Necessary, and paired with a `map_local` or `redirect` that explains it. |
| `strip_csp` | Content-Security-Policy. Injected and third-party script that the page forbade will now run. Note: `csp_modified`. | Rarely. Usually a symptom of an injection that CSP correctly blocked. Ask what needed it. |
| `inject_script` | Nothing — it *adds* code to the page, with the page's own origin and cookies. Note: `script_injected`. | When you asked for injected behaviour. Read the injected source with the same care as `module.py`; it runs in your logged-in session. |

`strip_csp` **and** `inject_script` in the same module is the combination to
look at hardest. It is exactly what a legitimate page-modification module needs,
and exactly what an exfiltration module needs.

### The review itself

1. `read_module(name="…", full=true)` — or open the directory under
   `~/.pporlock/modules/`. Read the whole thing; these files are short.
2. Check the rules against the two checklists above. Ask about anything the
   stated task does not explain.
3. Check the dry-run diffs against what you asked for, not against "did
   something change".
4. Then enable it — yourself, or by approving the `set_module_enabled` call.

> **Read a module before you enable it. Especially one an AI wrote for you.**

---

## 7. Troubleshooting the agent

**It loops on the same tool.** Almost always a filter that matches nothing.
Check the daemon side first with `pporlock doctor` — if QUIC is still enabled in
Chrome, whole hosts never appear in the flow table at all and no amount of
filtering will find them. Then tell the agent to call `flow_stats` with no
filter to see what hosts exist; it is usually filtering on a host that resolves
differently than it assumed.

**It cannot see the flow it needs.** Three common causes, in order of frequency:
the ring buffer has aged the flow out (record a session and reproduce); the host
is on the ClientHello exclusion list and was tunneled undecrypted (a
`passthrough_excluded` note, or no flow at all); or the request was HTTP/3.
Recording a session and pointing the agent at `list_session_flows` fixes the
first and makes the evidence dry-runnable.

**It invents a tool or an argument.** Every schema is
`additionalProperties: false`, so an invented argument is rejected rather than
ignored, and an unknown tool name returns an error that says whether the server
is in read-only mode. If it keeps happening, the tool list is not reaching the
model — check that your client forwards MCP server instructions, and paste the
family table from [section 1](#the-tools-by-family) into the system prompt.
`docs/worked-example.md` has some MCP calls written in shorthand rather than
with exact argument names; if an agent read that file, tell it the schemas in
the tool list win.

**Provenance says `no_change` and the agent does not understand why.**
`no_change` means the rule ran, matched, and the transform found nothing to
change — the **match is right and the pattern is wrong**. This is different from
the rule not appearing at all (never matched) and from `skipped_streamed` (the
body was already on the wire, so the transform never ran). Agents routinely
conflate the three and start editing the match block when the pattern is at
fault. The outcome table in
[`docs/troubleshooting.md`](troubleshooting.md#step-3-read-the-provenance-for-the-suspect-flow)
is worth pasting to the agent verbatim.

**It says a module is enabled and nothing is happening.** `get_status` reports
load errors and quarantine. A module that raises repeatedly is disabled
mid-flight with a `module_quarantined` note, and its rules stop applying without
anything changing in `list_modules`' enabled flag.

For everything that is a daemon problem rather than an agent problem, see
**[docs/troubleshooting.md](troubleshooting.md)**.

---

## See also

- [`mcp/README.md`](../mcp/README.md) — the tool table, the caps, the
  guardrail-to-test mapping
- [Module authoring](module-authoring.md) — rules, transforms, the `ctx` API,
  the trust model
- [Worked example](worked-example.md) — one problem end to end, via the UI and
  via MCP
- [Troubleshooting](troubleshooting.md) — reading provenance when a rule does
  not do what you meant
- [`examples/modules/`](../examples/modules) — eight commented modules; the
  fastest way to give an agent a correct pattern to follow
- `docs/spec-0-contracts.md` §4, §5, §8, §9 — provenance, rule schema, module
  API, redaction
