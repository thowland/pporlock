"""Record which cookies a site sets despite a Global Privacy Control signal.

The declarative half of this module sends `Sec-GPC: 1`. This half watches what
comes back, because the interesting question is not whether we asked but
whether asking changed anything.

Two jobs:

* `on_response` tallies every `Set-Cookie` into the module's own store, which is
  SQLite-backed and survives a restart (REQ MOD-022). An audit that reset with
  the daemon would never accumulate enough to be worth reading.
* `on_report` renders it. The daemon serves that at
  `/modules/gpc-audit/report`, linked from the module library — a tally nobody
  can find is not an audit.

Nothing here modifies a real response. It reads headers and answers one URL of
its own.
"""


def _cookie_name(set_cookie_value):
    """The cookie's name from a Set-Cookie value.

    `name=value; Path=/; Secure` -> `name`. None for anything unparseable,
    because a wrong name in an audit is worse than a missing one.
    """
    head = set_cookie_value.split(";", 1)[0].strip()
    if "=" not in head:
        return None
    return head.split("=", 1)[0].strip() or None


def _classify(name, ctx):
    """`ad`, `essential`, or `unclassified` — never a guess dressed as a verdict."""
    lowered = name.lower()
    for prefix in ctx.config.get("ad_prefixes", []):
        if lowered.startswith(prefix.lower()):
            return "ad"
    for prefix in ctx.config.get("essential_prefixes", []):
        if lowered.startswith(prefix.lower()):
            return "essential"
    return "unclassified"


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _report_html(ctx):
    """The audit, as a page. Cookie names are escaped — they come off the wire.

    A site can name a cookie `<script>`, and this report is rendered in the
    browser of the person auditing that site. Treating captured values as
    markup would turn an audit tool into the delivery mechanism.
    """
    hosts = ctx.store_get("hosts") or []
    rows = []
    totals = {"ad": 0, "essential": 0, "unclassified": 0}

    for host in sorted(hosts):
        tally = ctx.store_get(f"host:{host}") or {}
        for name, entry in sorted(tally.items()):
            # Classified at render, not read back from the tally. Editing the
            # prefix lists and reloading then corrects the whole history at
            # once, instead of only the cookies that happen to be set again.
            category = _classify(name, ctx)
            totals[category] = totals.get(category, 0) + 1
            rows.append(
                "<tr class='{c}'><td>{h}</td><td>{n}</td><td>{c}</td>"
                "<td class='num'>{k}</td><td>{f}</td></tr>".format(
                    c=_escape(category),
                    h=_escape(host),
                    n=_escape(name),
                    k=_escape(entry.get("count", 0)),
                    f=_escape(entry.get("first_seen", "")),
                )
            )

    return """<!doctype html><meta charset=utf-8><title>GPC cookie audit</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:2rem;max-width:60rem}}
 h1{{font-size:1.4rem;margin:0 0 .25rem}}
 p.sub{{color:#555;margin:0 0 1.5rem}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{text-align:left;padding:.4rem .6rem;border-bottom:1px solid #ddd}}
 th{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#666}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums}}
 tr.ad td{{background:#fff4f4}}
 tr.ad td:nth-child(3){{color:#a12; font-weight:600}}
 tr.essential td:nth-child(3){{color:#1a6}}
 tr.unclassified td:nth-child(3){{color:#a70}}
 .totals{{margin:1.5rem 0;font-size:13px;color:#333}}
</style>
<h1>GPC cookie audit</h1>
<p class=sub>Cookies set by sites while <code>Sec-GPC: 1</code> was being sent.
GPC is a legal request not to sell or share personal information — it is not a
technical block, so anything below was set <em>anyway</em>.</p>
<p class=totals><b>{ad}</b> advertising &middot; <b>{ess}</b> essential &middot;
<b>{unc}</b> unclassified &middot; {hosts} host(s)</p>
<table><thead><tr><th>Host</th><th>Cookie</th><th>Category</th>
<th>Times set</th><th>First seen</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class=sub style="margin-top:1.5rem">Unclassified means this module has no
opinion, not that the cookie is harmless. Add a prefix to
<code>ad_prefixes</code> or <code>essential_prefixes</code> in
<code>module.yaml</code> and reload to reclassify — existing tallies are
re-categorised on the next sighting.</p>
""".format(
        ad=totals.get("ad", 0),
        ess=totals.get("essential", 0),
        unc=totals.get("unclassified", 0),
        hosts=len(hosts),
        rows="".join(rows) or "<tr><td colspan=5>Nothing recorded yet.</td></tr>",
    )


def on_report(ctx):
    """The audit, served by the daemon at /modules/gpc-audit/report (OI-29).

    This used to be an `on_request` hook answering a magic path through the
    proxy, which meant the report could only be read while browsing some other
    site, by someone who remembered the URL. It is now reachable from the
    module library, and the module no longer short-circuits every request to
    check whether it is the report.
    """
    return {"content_type": "text/html; charset=utf-8", "body": _report_html(ctx)}


def on_response(request, response, ctx):
    """Tally Set-Cookie headers. Always returns None — this module observes."""
    values = response.headers_all("set-cookie")
    if not values:
        return None

    host = request.host
    key = f"host:{host}"
    # A dict, not a list: the same cookie reset on every page load should not
    # produce a hundred identical rows in the report.
    seen = ctx.store_get(key) or {}

    newly_set = []
    for value in values:
        name = _cookie_name(value)
        if name is None:
            continue
        category = _classify(name, ctx)
        entry = seen.get(name)
        if entry is None:
            seen[name] = {
                "category": category,
                "count": 1,
                "first_seen": request.timestamp,
            }
            newly_set.append((name, category))
        else:
            entry["count"] = entry.get("count", 0) + 1
            # Re-classify on every sighting, so editing the config and reloading
            # corrects an existing tally rather than leaving it stale.
            entry["category"] = category

    ctx.store_set(key, seen)

    hosts = ctx.store_get("hosts") or []
    if host not in hosts:
        hosts.append(host)
        ctx.store_set("hosts", hosts)

    ads = [name for name, category in newly_set if category == "ad"]
    if ads:
        # There is no note code for "a site ignored a privacy signal" — the
        # taxonomy is closed (OI-15) — and an invented code degrades to
        # `module_error`, which would read as this module failing. So: the log.
        ctx.log("warning", "advertising cookies set despite Sec-GPC", host=host, cookies=ads)
    return None
