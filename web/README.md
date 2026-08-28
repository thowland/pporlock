# pporlock web UI

The authoring and analysis surface (deliverable **D2**, SPEC-2). React + Vite +
TypeScript, built to static assets and served by the daemon's control server at
`http://127.0.0.1:8081`.

SPEC-2 §2.2 leaves state management, CSS, component library and table
virtualization to the implementer, and requires the choice to be recorded here
with its reasoning. That is what this file is for.

---

## Commands

```bash
npm run dev           # vite dev server (proxies nothing — point it at a running daemon)
npm run build         # tsc --noEmit && vite build  ->  web/dist/
npm run typecheck     # tsc --noEmit
npm run lint          # eslint src e2e --max-warnings 0
npm run format        # prettier --write
npm test              # vitest run
npm run coverage      # vitest run --coverage   (gate G2: >= 80% per metric)
```

`make web` from the repository root runs the build; `make gate` runs the whole
close-gate set. Playwright specs live in `e2e/` and are driven by `make e2e`.

---

## Implementer's choices (SPEC-2 §2.2)

### State management — React built-ins, no library

There is no Redux, Zustand, or React Query here. Three reasons, in order of
weight:

1. **Server state and local UI state must be distinguishable** (SPEC-2 §2.2).
   That constraint is satisfied by *where* state lives, not by which library
   holds it. Server state lives in exactly two hooks — `useDaemonState` and
   `useFlows` — and every component below them receives it as props. If a value
   did not come through one of those hooks, it is local UI state. A cache
   library would blur that line rather than sharpen it, because it would put
   locally derived values in the same store as fetched ones.

2. **There is one client and one server, on loopback.** The problems a query
   cache solves — request deduplication across an unknown component tree, stale
   time, background refetch, offline retry — are problems of a distributed
   application. Here the daemon is a process on the same machine, and the live
   data arrives by SSE rather than by polling. A cache keyed on query arguments
   would sit *between* the event stream and the view, which is precisely the
   design SPEC-2 §4.3 warns produces stale rows.

3. **The event stream is the hard part, and it wants direct control.** SSE
   pushes hundreds of `flow.completed` events per second during a page load.
   `useFlows` buffers them and flushes on an animation frame (SPEC-2 §12), keys
   rows on `flow_id`, and merges late `flow.updated` field backfills such as
   `tab_id` (SPEC-0 §3.6). Every one of those is a deliberate deviation from
   what a cache library does by default.

The rule that keeps this honest: **`src/api/` is the only place that issues
fetches** (SPEC-2 §4.1). Components take an `ApiClient` and call named methods.
That single chokepoint is what makes every component testable against a fake
client, and it is why there are no network mocks scattered through the tests.

If this grows a second data consumer that is not a hook — a worker, say, or a
second window — revisit the decision. It is a scale judgement, not a principle.

### Routing — hash routing, hand-rolled

`src/lib/router.ts`, about seventy lines, no dependency.

Hash rather than history because the daemon serves this UI from a static
directory, and a hash route needs no SPA fallback rule at all — one less thing
that can differ between `vite dev` and the packaged build. The route vocabulary
is a closed discriminated union, and an unknown hash falls back to the traffic
view rather than rendering nothing: a blank tool looks like a broken daemon, and
this UI exists to make broken things visible.

### CSS — one hand-written stylesheet, custom properties, no framework

`src/styles/app.css`. No Tailwind, no CSS-in-JS, no component library.

- **No runtime CSS.** SPEC-2 §2.3 forbids network access beyond the daemon
  origin and any external font. A single stylesheet with no build-time class
  generation is the shortest path to a page that is provably self-contained.
- **Themes are custom properties.** Dark by default because this tool sits
  beside devtools; `prefers-color-scheme: light` redefines the same tokens.
  Every colour in the app resolves through a token, so the light theme is a
  block of variable definitions rather than a parallel set of rules.
- **Density is the design.** This is a data-first tool interface, and a general
  component library's spacing would cost roughly half the rows on screen. The
  flow table is the screen users spend their time on.
- **Colour is never the only carrier of meaning** (REQ WUI-015). Flow flags
  carry text (`BLK`, `MOD`) or a hidden label; provenance notes carry their
  severity as a word and a shape; outcome badges say `skipped — response
  streamed` rather than merely turning amber.

### Table — windowed, not a virtualization library

SPEC-2 §2.2 requires the flow table to be virtualized. `FlowTable` renders the
client's cache directly, and the bound that makes that safe is upstream: the
client holds at most the daemon's ring-buffer size, dropping older rows rather
than accumulating (SPEC-2 §12). The PRF-004 risk is not row count, it is one
React render per event — and that is handled in `useFlows` by buffering and
flushing on an animation frame.

If the ring bound is ever raised past a few thousand rows, this is the first
decision to revisit; the component's props were kept free of table-library
concepts so that swap stays local.

### Editor — Monaco, lazily loaded

Required by SPEC-2 §2.1 (REQ WUI-006). Bundled locally, never CDN-loaded, and
imported lazily so the traffic view does not pay its bundle cost. `CodeEditor`
falls back to `PlainEditor` when Monaco cannot load, which is also what makes
the editor unit-testable under jsdom.

### Types — generated, never hand-written

Wire shapes come from `contracts/schemas/` via `contracts/generated/types.ts`,
imported as `@contracts/types`. `src/api/types.ts` re-exports them and adds only
API *envelopes* that SPEC-0 describes in prose rather than in a schema. Adding a
cross-component field means editing `contracts/` and running `make contracts`
first — never inventing it here.

---

## Layout

```
src/
  api/         ApiClient (the only fetch site) and the SSE EventStream
  components/  presentation; each takes an ApiClient and calls named methods
    detail/    FlowDetail + ProvenanceView — shared by live and session views
    editor/    Monaco wrapper and its plain fallback
    modules/   library and editor
    profiles/  profile management
    rules/     rule builder and create-rule-from-flow
    sessions/  session list, session browser, dry run
    settings/  effective configuration, redaction patterns
  hooks/       useDaemonState, useFlows — the only server-state owners
  lib/         router, formatting, redaction parsing, YAML round-tripping
  styles/      app.css
  test/        factories for wire shapes
```

### One flow table, one provenance view

The session browser (`components/sessions/SessionBrowser.tsx`) imports
`FlowTable`, `FlowDetail` and `ProvenanceView` unchanged (SPEC-2 §8.2). It owns
exactly three differences from the live view: where flows come from, that there
is no event stream, and that it passes no `onUnmask`.

That last one is the important one. **Unmasking is live-ring-only, web-UI-only,
one value at a time** (SPEC-0 §9.3, REQ CAP-043), and a session flow was
redacted before it reached the file (REQ CAP-045) — there is nothing there to
reveal. So the reveal control is gated on the *presence of a callback* rather
than on a boolean prop: a view that cannot supply one cannot render one, and a
later edit cannot flip a flag by accident. A test in
`components/sessions/SessionBrowser.test.tsx` fails if a reveal control ever
appears on a session flow.

---

## Security notes specific to this component

- The bearer token never enters a URL. `EventSource` cannot set headers, so the
  event stream uses streaming `fetch` with an `Authorization` header instead
  (`src/api/events.ts`).
- `X-Pporlock-Client: ui` is sent on **every** request, not only mutations: it
  is the CSRF defence on writes (REQ API-013) and the daemon also requires it on
  the unmask read.
- Nothing derived from flow content is ever passed to `dangerouslySetInnerHTML`.
  Bodies and diffs render as text. A page under test must not be able to inject
  into the tool inspecting it.
- The control origin is asserted to be loopback in `lib/control-origin.ts`.
