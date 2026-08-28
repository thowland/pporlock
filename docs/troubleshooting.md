# Troubleshooting

**REQ DOC-003.** Organised around the characteristic failure of this class of
tool: **the page is subtly wrong**. Not broken with an error — wrong. A button
does nothing, a font falls back, a widget never appears, and nothing anywhere
says why.

That failure is why provenance exists, and the first half of this guide is a
single procedure for walking from the symptom to the cause. The second half is
the specific failures that are common enough to name.

---

## Part 1 — The page is subtly wrong

### Step 1. Is it pporlock at all?

Turn the extension toggle off, reload, and look again.

Do this first, always. It takes five seconds and it splits the problem in half.
If the page is still wrong with the proxy off, nothing below applies and you are
debugging the site.

### Step 2. Open the DevTools panel on the broken page

The panel is scoped to the tab you are inspecting, which is the only scope that
matches the question you actually have. Filter to **modified** and **blocked**.

- **Nothing there?** pporlock changed nothing on this page. Check the
  **unattributed** chip — if flows are landing there, Chrome is not telling the
  extension which tab they belong to (see *Per-tab counts are empty* below), and
  you should use the web UI's flow table instead, filtered by host.
- **Something there?** That is your suspect list. Go to step 3.

### Step 3. Read the provenance for the suspect flow

This is the screen the whole system is built around. Read it in this order:

**a) Notes first, above the timeline.** Warnings and errors are conditions no
rule asked for. They are never collapsed. In particular:

| Note | What it means for a subtly-wrong page |
|---|---|
| `response_streamed` | A body transform **did not run**. The response was already on the wire. This is the most common "my rule did nothing" cause |
| `transform_budget_exceeded` | A transform was cut off mid-flow. The body may be partially transformed |
| `csp_modified` | Content-Security-Policy was changed. If a script is now blocked *or* now running that should not be, start here |
| `sri_stripped` | Integrity attributes were removed. The browser is no longer verifying those subresources |
| `script_injected` | Something was added to the document that the site did not send |
| `map_local_missing` | A `map_local` rule pointed at a file that is not there — so it served the real thing, silently |
| `module_error` | A module raised. Its rules for this flow did not apply |
| `module_quarantined` | A module failed repeatedly and has been disabled entirely |
| `body_truncated` | What you are *looking at* is cut off. The wire was fine; the capture was not |

**b) Then `short_circuited_by`.** If a rule short-circuited the flow, the
provenance view says so on that rule, in those words. Everything after it in its
class never ran. "An earlier rule ate it" is the single most common confusion
when a rule set grows, which is why it is stated rather than left to be
inferred.

**c) Then the outcomes, phase by phase.** Non-`applied` outcomes are rendered as
prominently as applied ones on purpose — the interesting question is almost
always why something *didn't* happen:

| Outcome | Read it as |
|---|---|
| `no_change` | The rule ran and matched, and the transform found nothing to change. Your **pattern** is wrong, not your match |
| `skipped_streamed` | The body was not buffered. See *A body rule does nothing* below |
| `skipped_budget` | The per-flow time budget was exhausted before this rule |
| `skipped_short_circuit` | An earlier rule ended the flow |
| `skipped_disabled` | The module is off, or quarantined |
| `error` | The rule itself raised. The detail carries the exception |

**d) A rule you expected is not in the list at all.** It never matched. Open the
rule and compare its match criteria against the request as recorded — the flow
detail shows the request *as it went over the wire*, after mutations, which is
what the next rule actually saw.

### Step 4. Confirm by removing the cause

Disable the single module or rule you have identified, reload, and check the
page. If it is still wrong, you found *a* modification, not *the* cause — go
back to step 2 with it excluded.

---

## Part 2 — Named failures

### Some sites never appear in the flow table at all

**QUIC.** Chrome is talking HTTP/3 over UDP and the proxy never sees it. This
produces exactly the symptom of a partial, inexplicable capture.

Fix: `chrome://flags/#enable-quic` → **Disabled** → relaunch. `pporlock doctor`
checks this.

### A host appears as `TUNNEL` with no content

That host is **excluded**, so the connection was tunnelled without decryption
(REQ PXY-015). This is deliberate and the default exclusion list ships with 33
entries, each commented with why — certificate-pinned hosts, OS update
endpoints, and anything where interception breaks the client rather than
informing you.

The flow is still visible; only its content is not. To intercept it anyway,
remove the entry from the exclusion list — and read the comment first, because
it says what will break.

### A body rule does nothing, and provenance says `skipped_streamed`

The response was **streamed**, not buffered, so there was no body in memory to
transform.

pporlock only buffers a response when a rule could actually use the body, and
only for configured content types under a size cap. That is the cheapest
optimisation available and it applies to the large majority of flows. The
consequences:

- Your rule must be a **body** rule, and it must **match this flow**. A rule
  that matches nothing causes no buffering.
- The response's content type must be in `buffering.content_types`.
- The response must be under `buffering.max_body_bytes`.

Check the buffering decision phase in provenance — it records which of these
made the call.

### A response header rule does nothing on a large or slow response

Header mutations are applied at `responseheaders`, before the body streams,
precisely because mitmproxy has already sent the headers by the time the
response hook runs. If a header rule shows `applied` but the header is not on
the wire, that is a bug and worth reporting — it was one, once.

### Certificate warnings in Chrome

The CA is not trusted. Run `pporlock install`, then `pporlock doctor` and check
`ca_trusted`.

If it says trusted and Chrome still warns: Chrome caches certificate decisions
per-profile, and a *fresh* profile does not inherit login-keychain trust the way
you might expect. Restart Chrome. If you are testing in a temporary profile,
that profile needs the trust too.

### Per-tab counts are empty, or flows show no tab

Chrome only reports a request to an extension that already has host access to
it. With loopback-only permissions, attribution coverage is **zero** — this was
measured, not assumed.

Fix: extension options page → **Per-tab attribution** → *enable*. This requests
`<all_urls>`, which is a real grant and is why it is not taken at install.

Without it, counts are browser-wide and flows carry no tab. Everything else
works normally.

### "pporlock turned the proxy off"

The fail-safe. The daemon stopped responding to health checks, so the extension
returned Chrome to a direct connection rather than leaving you unable to browse.

This is working as intended: a dead proxy that Chrome is still pointed at means
no internet at all. Start the daemon with `pporlock run` and turn the toggle
back on.

The notice does not auto-clear, because "it fixed itself" is not something you
should have to guess at.

### The proxy toggle is disabled and will not turn on

The popup says why rather than sitting greyed out. The three causes:

- **The daemon is not running** — `pporlock run`
- **Not paired** — `pporlock pair`
- **Another extension or an enterprise policy holds Chrome's proxy** — pporlock
  will not fight another extension for it. Disable the other one, or ask
  whoever manages the machine about the policy

### A module stopped working and shows as quarantined

A module that raises repeatedly is disabled after a threshold of failures, and
a `module_quarantined` note is attached to the flow where it happened. This is
error isolation, not a judgement: one bad module must not take down the
pipeline.

Read the module's load error or last exception in the module library, fix it,
and re-enable — enabling clears the quarantine.

### Everything is slow

Check the per-flow timing in provenance (`total_ms`). If a single transform
dominates, that is your answer. If the budget note appears often, the budget is
doing its job and the rule set is too expensive for the traffic.

### A value shows as `redacted · 48 bytes · #a1b2`

That is a masked secret, not a bug. The fingerprint lets you tell whether two
requests carried the *same* value without revealing either.

Reveal is available in the **web UI only**, on **live** flows only, one value at
a time. It is unavailable for recorded sessions and categorically unavailable
through MCP — a recorded session never contained the real value in the first
place, because redaction happens at write time.

---

## When none of this helps

Collect, in this order:

1. `pporlock doctor` output
2. The flow's provenance — the detail panel has the whole structure
3. `pporlock version`

The provenance record is the thing to lead with. It is the structured answer to
"what did this system do", and it exists so that this conversation does not have
to start with guesswork.
