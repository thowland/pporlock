# Stub library

Scripts served in place of blocked ones (REQ PXY-033).

The reason these exist rather than an empty body: a page that calls
`analytics.track()` on a script which failed to load **throws**, and the throw
frequently breaks unrelated rendering or triggers an anti-adblock notice. A page
that loads a stub defining `analytics = { track(){} }` proceeds normally.

That is where most of the value in tracker suppression sits, and it is why
blocking synthesises a benign response rather than killing the connection
(REQ PXY-031).

Each stub defines the globals its target is called through, and nothing else.
They are delivered through the same `map_local` machinery as any other local
file, so nothing here is special-cased.

| Stub | Replaces |
|---|---|
| `analytics.js` | Segment-style `window.analytics` |
| `gtm.js` | Google Tag Manager; preserves `dataLayer` |
| `ga.js` | Google Analytics, both `gtag` and legacy `ga` |
| `facebook-pixel.js` | Meta pixel `fbq` |
| `noop.js` | Anything whose absence the page tolerates |

Add one by dropping a file here. `stub: <name>` in a rule resolves to
`<name>.js`.
