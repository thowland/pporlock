# Example modules

Ten modules covering every action and both authoring tiers. They are meant to
be read as much as run — each one carries the reasoning for its choices in
comments, including the choices that are not obvious.

Every module ships **disabled**. A test asserts it. An example that installs
itself already running teaches the wrong lesson about a tool that can rewrite
every page you load.

| Module | Priority | What it shows |
|---|---|---|
| [`adblock`](modules/adblock/) | 10 | `block` with `stub: auto` and `mode: kill`; host globs vs path regexes; scoping by `dest` |
| [`cookie-banners`](modules/cookie-banners/) | 20 | `inject_style`, and an `on_response` hook doing what CSS cannot |
| [`local-bundle`](modules/local-bundle/) | 30 | `map_local` from `assets/`, paired with `strip_integrity_attributes` |
| [`header-lab`](modules/header-lab/) | 40 | `headers` on both sides; `set` vs `add`; a rule deliberately left disabled |
| [`json-tamper`](modules/json-tamper/) | 50 | `json_patch` — feature flags, emptying a list, removing a nested key |
| [`css-tamper`](modules/css-tamper/) | 60 | A user stylesheet served from `assets/` via `map_local` |
| [`fault-lab`](modules/fault-lab/) | 70 | Python `on_request`, `short_circuit`, and durable state in `ctx.store` |
| [`ws-inspect`](modules/ws-inspect/) | 80 | `on_websocket_message` — read-only frame inspection |
| [`user-agent-switcher`](modules/user-agent-switcher/) | 85 | `settings:` — a form the module library renders, driving `on_request` from `ctx.config` |
| [`gpc-audit`](modules/gpc-audit/) | 90 | `on_report` — sends a Global Privacy Control signal, then audits the cookies set anyway |

Priorities do not collide, because the library is meant to be enabled together
and priority is what orders one module's rules against another's.

---

## Installing them

```bash
make examples          # copies them into ~/.pporlock/modules/, still disabled
```

Or by hand:

```bash
cp -R examples/modules/* ~/.pporlock/modules/
```

Then enable the ones you want — in the web UI's module library, or:

```bash
curl -X PATCH http://127.0.0.1:8081/modules/adblock \
     -H "Authorization: Bearer $(cat ~/.pporlock/token)" \
     -H 'Content-Type: application/json' \
     -H 'X-Pporlock-Client: cli' \
     -d '{"enabled": true}'
```

Reload the page and read the provenance to confirm it did what you meant.
`applied` and *did what you meant* are different claims.

---

## Before you enable anything

**Module code is fully trusted.** No sandbox, no import allowlist, no resource
jail. `module.py` runs in the proxy process with your full user privileges. Dry
run executes it too, by design — so dry-running an unread module is not safer
than enabling it.

Five of these ship Python: `cookie-banners`, `fault-lab`, `ws-inspect`,
`gpc-audit`, `user-agent-switcher`. Read
them. They are short, and reading them is the habit worth forming before you
read one an agent wrote.

Four of them weaken a page's own protections when enabled, and pporlock will
say so in the page and in provenance:

- `local-bundle` removes subresource-integrity attributes
- `cookie-banners` and `css-tamper` inject content into the document
- `header-lab` ships an HSTS-dropping rule **disabled** (`drop-hsts-for-local-testing`) — enable it per host, never globally

---

## Using them as starting points

Most are more useful edited than as-is. `adblock`'s host list is short by
design — a real list is thousands of entries and belongs somewhere it can be
updated. `json-tamper` and `header-lab` point at `example.com` hosts that do not
exist, so they match nothing until you change them.

`user-agent-switcher` is the one to edit from the UI rather than the file: click
its gear in the module library. Narrow **Hosts** to the site you are auditing
before enabling it — the shipped `*` announces you as a crawler to every site
you visit. And note what it can and cannot do: it changes headers, so a site
that checks the User-Agent will treat you as Googlebot, and a site that verifies
by reverse DNS will not. Treat a difference you find as a lead, not a verdict.

The two designed to be edited rather than copied:

- **`css-tamper`** — put your CSS in `assets/user.css`. It is read per request,
  so editing it needs a page reload and no module reload.
- **`local-bundle`** — put your build at `assets/app.js` and point the rule's
  path at the file you are replacing. Symlinks out of `assets/` are refused,
  so copy the file in.

---

## They are tested

`daemon/tests/unit/test_examples.py` loads every module through the ordinary
loader and exercises the ones with behaviour worth pinning: that `adblock`
returns a *script* stub for a blocked script, that `fault-lab` fails exactly
every third request, that `ws-inspect` does not raise on a binary frame, that
`json-tamper` leaves a non-JSON body alone.

Examples that are not tested become examples that do not work, and an example
that does not work is worse than none — it is read as a statement about what the
system does. It is also the closest thing this project has to a public API
conformance suite: a change that breaks a module written the documented way
breaks there.

`cookie-banners` earned that on its first run, by finding an engine bug in which
a Python hook's body edit was silently discarded whenever a declarative rule
also matched.

---

## See also

- [The module cookbook](../docs/module-cookbook.md) — the deep reference these draw on
- [Module authoring](../docs/module-authoring.md) — the shorter introduction and the trust model
- [Troubleshooting](../docs/troubleshooting.md) — when a module does nothing
