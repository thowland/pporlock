# Local Intercepting Proxy and Companion Extension — Approach Document

**Status:** Draft for development
**Date:** August 27, 2026
**Scope:** Single-user, single-machine. No multi-tenancy, no remote access, no authentication beyond loopback binding.

## Purpose and scope

The system is a local HTTPS interception proxy, built on mitmproxy, paired with a Chrome extension that controls proxy state and reads captured traffic over a separate loopback channel. Inspection and modification are treated as equal first-class functions rather than modification being bolted onto a viewer, because the two make different demands on the pipeline: inspection tolerates streaming and sampling, while modification requires that the full body be buffered and re-encoded before the browser sees any of it. Designing for modification first and then relaxing constraints for large or uninteresting responses gives a cleaner result than the reverse, so the rules engine is the center of the design and the capture ring buffer is a consumer hanging off it.

The intended uses are advertisement and tracker suppression, blackholing of specific hosts and behaviors, substitution of remote assets with local ones, and inspection of the request and response traffic that the browser's own developer tools either hide or present after the fact.

## Component layout

Four processes or process-parts, with the boundaries chosen so that the mitmproxy version churn is contained in one place:

1. **The proxy core** — `mitmdump` running our addon set, listening on `127.0.0.1:8080` in regular (explicit) proxy mode. This owns TLS termination, certificate generation from our local root, and the hook lifecycle.
2. **The rules engine** — a plain Python module, imported by the addon, with no mitmproxy imports in it. It takes a normalized description of a request or response and returns a list of actions. Keeping it free of mitmproxy types means it is unit-testable without spinning up a proxy, and it survives an addon API change with only the adapter layer rewritten.
3. **The control server** — an asyncio HTTP server started from the addon's `running()` hook on `127.0.0.1:8081`, sharing the proxy's event loop and its in-memory state directly. Serves the rule set, accepts rule edits, exposes the capture ring buffer, and streams live flow events over Server-Sent Events.
4. **The Chrome extension** — a service worker that toggles `chrome.proxy` settings, a popup for on/off and quick rules, and a devtools panel that reads the control server.

The control server sharing the proxy's event loop is worth stating explicitly as a decision: it removes any need for inter-process communication or locking, since the hooks and the API handlers run on the same loop and touch the same objects. The cost is that a slow API handler will stall traffic, which constrains us to keeping anything expensive off the loop.

## Certificate and browser configuration

Standard mitmproxy setup, recorded here so the checkpoint is unambiguous. On first run mitmproxy generates a root CA at `~/.mitmproxy/mitmproxy-ca-cert.pem`; that root is installed into the macOS keychain (or the platform equivalent) and marked as trusted, after which the per-domain leaf certificates that mitmproxy mints on the fly validate cleanly. Chrome exempts locally-installed roots from Certificate Transparency enforcement and from its static key-pinning set, so the well-known sites that would otherwise refuse a MITM will pass.

Two configuration items belong here rather than being discovered later:

- QUIC must be disabled in Chrome, since HTTP/3 runs over UDP and will not traverse a TCP `CONNECT` proxy. Without this, a portion of traffic silently bypasses the proxy and the rules appear intermittently broken.
- An exclusion list should be established before any serious use, via mitmproxy's `ignore_hosts` option or the `tls_clienthello` hook setting `data.ignore_connection = True`. Browser update and telemetry endpoints, anything doing certificate pinning at the application layer, and financial sites all belong on it. Excluding at the ClientHello means the connection is tunneled without decryption, so there is no failure to handle downstream.

## The rewrite and blocking model

This is the part worth getting right early, since the rest of the system is largely plumbing around it.

### Action taxonomy

Six action types cover the cases we care about, ordered by how early they can short-circuit the pipeline:

| Action | Hook | Effect |
|---|---|---|
| `passthrough` | `tls_clienthello` | Connection tunneled undecrypted; no further processing possible |
| `block` | `request` | Synthesized response returned; request never leaves the machine |
| `map_local` | `request` | Request satisfied from a local file |
| `redirect` | `request` | Host, path, or scheme rewritten before the request goes out |
| `headers` | `request` / `response` | Named headers added, removed, or substituted |
| `body` | `response` | Response body decoded, transformed, and re-encoded |

### Blocking behavior and the shape of the null response

Killing the connection outright (`flow.kill()`) is the wrong default for most blocking, because a page's JavaScript frequently reacts to a failed fetch by retrying, by throwing into a handler that breaks unrelated rendering, or by displaying an anti-adblock notice. A better default is to synthesize a benign response whose content type matches what the browser asked for, which we can determine from the `Sec-Fetch-Dest` request header rather than guessing from the URL extension:

- `script` → `200` with `application/javascript` and an empty body, or a stub that defines the globals the page expects
- `image` → `200` with a 1×1 transparent GIF, or `204`
- `empty` (fetch/XHR) → `200` with `application/json` and `{}` or `[]`
- `iframe` → `200` with `text/html` and an empty document
- `document` → `403` or a small explanatory page, since a top-level navigation should be visible to the user
- anything else → `204`

The stub-script case is where most of the value sits for tracker suppression, because a page that calls `analytics.track()` on a script that failed to load will throw, whereas one that loads a stub defining `analytics = { track: () => {} }` proceeds normally. Building a small library of these stubs is a natural extension once the mechanism works, and `map_local` gives us the delivery path for them without special-casing.

### Response body rewriting

The mechanism has three parts, and each has a failure mode that needs an explicit answer.

**Buffering.** mitmproxy makes the decoded body available as `flow.response.content` (bytes) and `flow.response.text` (str), transparently handling gzip, deflate, and brotli, and re-encoding on assignment according to the `Content-Encoding` header. This only works if the response is buffered, and the decision to buffer or stream must be made in `responseheaders`, before the body arrives. The guard there should stream anything above a size threshold or outside a small allowlist of content types (`text/html`, `text/css`, `application/javascript`, `application/json`, and their variants), since running a regex over a 200MB video is both pointless and enough to stall the event loop for every other connection.

**Cache interference.** During rewrite development the browser will send conditional requests and receive `304 Not Modified` with no body, which makes rules appear not to fire. mitmproxy's `anticache` option strips `If-None-Match` and `If-Modified-Since` so full bodies come back every time. The companion option `anticomp` strips `Accept-Encoding` so bodies arrive uncompressed, which is useful while debugging but should be off in normal use since it inflates transfer volume. Both belong on a development toggle exposed through the control API.

**Subresource Integrity and Content-Security-Policy.** Any modification to a script or stylesheet carrying an `integrity` attribute will fail its hash check and be dropped by the browser, and any script we inject will be refused if the page's CSP does not admit it. Both are handled at the response header and HTML rewrite stage, and both must be handled unconditionally whenever body rewriting is enabled for a document, since the breakage is silent from the proxy's perspective — the proxy sees a successful response, and only the browser console shows the failure. The two mitigations are stripping `integrity` and `crossorigin` attributes from `<script>` and `<link>` tags in rewritten HTML, and rewriting the `Content-Security-Policy` header. For injection specifically, parsing the existing CSP for a `nonce-` value and reusing that nonce on our injected tag is cleaner than relaxing the policy wholesale, since it leaves the page's own protections intact.

### Rule matching and evaluation order

A rule matches on host (glob), path (regex), method, and resource destination from `Sec-Fetch-Dest`, with response-side rules additionally able to match on status and content type. Two different evaluation semantics apply depending on the action class, and conflating them causes trouble later:

- Blocking and short-circuit actions (`block`, `map_local`, `redirect`) are **first match wins**. Evaluation stops at the first matching rule.
- Header and body actions are **all matches apply, in declaration order**. A rule that strips CSP and a rule that injects a script both need to run.

Within a single flow the order is fixed: passthrough decision at ClientHello, then request-side short-circuit, then request headers, then the request goes out; on the way back, response headers (which is where CSP and cache-control changes land), then response body.

A sketch of the rule format, as YAML so it can be hand-edited and hot-reloaded:

```yaml
rules:
  - name: block-analytics-vendor
    match:
      host: "*.analytics-vendor.example"
    action: block
    stub: auto            # infer from Sec-Fetch-Dest

  - name: stub-tag-manager
    match:
      host: "www.googletagmanager.com"
      path: "^/gtm\\.js"
    action: map_local
    file: "./stubs/gtm-stub.js"

  - name: relax-csp-on-target-site
    match:
      host: "app.example.com"
      dest: document
    action: headers
    response:
      remove: ["content-security-policy", "content-security-policy-report-only"]

  - name: strip-sri
    match:
      dest: document
      content_type: "text/html"
    action: body
    transform: strip_integrity_attributes
```

Transforms are named functions in a registry rather than arbitrary expressions in the YAML, which keeps the config declarative and keeps anything requiring real logic in Python where it can be tested.

## Addon skeleton

```python
from mitmproxy import http, ctx
import asyncio

from rules import RuleSet, Decision   # no mitmproxy imports inside

class Interceptor:
    def __init__(self):
        self.rules = RuleSet.load("rules.yaml")
        self.captures = []             # ring buffer, bounded

    def running(self):
        asyncio.create_task(start_control_server(self))

    def tls_clienthello(self, data):
        if self.rules.should_passthrough(data.client_hello.sni):
            data.ignore_connection = True

    def request(self, flow: http.HTTPFlow):
        d = self.rules.evaluate_request(normalize(flow))
        if d.action == "block":
            flow.response = synthesize(d, flow)
        elif d.action == "map_local":
            flow.response = from_file(d.file, flow)
        elif d.action == "redirect":
            apply_redirect(flow, d)
        apply_header_actions(flow.request, d.request_headers)

    def responseheaders(self, flow: http.HTTPFlow):
        if not self.rules.wants_body(flow.response.headers):
            flow.response.stream = True     # decided here or never

    def response(self, flow: http.HTTPFlow):
        d = self.rules.evaluate_response(normalize(flow))
        apply_header_actions(flow.response, d.response_headers)
        if not flow.response.stream and d.body_transforms:
            text = flow.response.text
            for t in d.body_transforms:
                text = t(text, flow)
            flow.response.text = text       # re-encodes automatically
        self.record(flow)

addons = [Interceptor()]
```

The `normalize()` boundary is the important line in that sketch. Everything above it is mitmproxy-shaped and expected to change between releases; everything the rules engine sees is our own dataclass.

## Control channel and extension

The control server exposes a small surface: `GET/PUT /rules`, `POST /rules/reload`, `GET /state` and `POST /state` for the development toggles, `GET /flows` for the ring buffer with filtering, and `GET /events` as an SSE stream for live flow notifications. It returns `Access-Control-Allow-Origin` for the extension's `chrome-extension://` origin, which is sufficient because we control both ends.

Two browser-platform items constrain this. Loopback origins are treated as potentially trustworthy, so the extension talking plain HTTP to `127.0.0.1` is not blocked as mixed content. Private Network Access enforcement has been tightening on requests into localhost, and while the extension-to-loopback path is the most permissive case, the current enforcement state should be verified at build time rather than assumed. If it becomes an obstacle, Native Messaging over stdio is the fallback that sidesteps ports, CORS, and PNA entirely, at the cost of a native host manifest and a framed message protocol.

The extension itself needs `proxy`, `storage`, and host permissions for the loopback origin. Proxy control is `chrome.proxy.settings.set()` with either a fixed-server configuration pointing at `127.0.0.1:8080` or a PAC script when we want per-host scoping decided browser-side.

## Development sequence

Each phase has a checkpoint that should be demonstrable before moving on, since the failure modes compound and diagnosing a rewrite problem on top of an unresolved certificate or QUIC problem wastes time.

1. **Baseline interception.** `mitmdump` with no addon, root CA installed, QUIC disabled, exclusion list seeded. Checkpoint: thirty minutes of ordinary browsing with no certificate warnings and no broken sites.
2. **Rules engine and blocking.** Rules engine module with unit tests, addon wired to `request`, synthesized responses with `Sec-Fetch-Dest`-derived types. Checkpoint: a host is blocked, and the pages that reference it still render correctly.
3. **Header and body rewriting.** The buffering guard, CSP and SRI handling, transform registry, `anticache` development toggle. Checkpoint: a script injected into a CSP-bearing page runs without console errors.
4. **Control server and capture.** Bounded ring buffer, HTTP API, SSE stream. Checkpoint: rules edited through the API take effect without restarting the proxy.
5. **Extension.** Toggle, popup, devtools panel. Checkpoint: proxy on and off from the popup with no manual system-settings change.

## Risks and open items

The performance ceiling is the one most likely to bite in practice. Every buffered response costs a decompression, a transformation, and a recompression on a single event loop, so a page pulling two hundred subresources through a proxy doing regex work on each will feel slower than direct browsing. The mitigations are the content-type and size guard, compiling regexes once at rule load, and moving any expensive transform to a thread pool via `run_in_executor` rather than letting it block the loop. This should be measured at phase three rather than assumed.

The mitmproxy addon API has shifted across major versions, including in the areas we depend on most (streaming control, TLS hooks, option names). The version should be pinned in the project and upgrades treated as deliberate work against the `normalize()` adapter layer.

WebSocket traffic is handled through a different hook (`websocket_message`) and is not covered by the request and response rule model above. Whether to extend the rules engine to cover it or treat it as inspection-only is an open question, and depends on whether the sites we care about carry tracking over WebSocket.

Finally, silent breakage is the characteristic failure of this class of tool: the proxy considers a flow successful, the page is subtly wrong, and the cause is three rules deep. Recording which rules fired against each flow in the capture ring buffer, and surfacing that in the devtools panel, is not a nice-to-have but the primary debugging affordance for the whole system, and it should be built into the rules engine's return value from the start rather than retrofitted.
