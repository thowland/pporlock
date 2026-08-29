"""Present the browser as a crawler, so you can see the page a site indexes.

Sites serve different HTML to Googlebot than to people — sometimes deliberately
(prerendered markup, no consent wall), sometimes as an accident of feature
detection, and occasionally as cloaking. The same is now true of the AI
fetchers: a site that has decided what it wants an assistant to read may serve
that instead of the page you see. You cannot compare against something you
cannot fetch, so this module fetches it.

**What this does and does not do.** It changes request headers: `User-Agent`,
optionally `Accept-Language`, and — the part that matters — it removes Chrome's
`Sec-CH-UA` client hints, which name the real browser and would otherwise
contradict every word of the disguise. It does *not* make you Googlebot. Google
publishes its crawler ranges and a site that verifies by reverse DNS will see
your address, not theirs. Treat a difference you find as a lead, not a verdict.

Everything is driven from `ctx.config`, which the module library's settings
dialog writes. Read per request rather than cached at load, so changing the
dropdown takes effect on the next page rather than the next restart — the only
state kept across a config change is the tally `on_report` renders.
"""

from pporlock.engine.models import RequestMutation

#: The strings each operator documents for its fetcher. Keys are the `identity`
#: setting's option values, which the manifest and this table have to agree on;
#: `on_load` checks that they do rather than leaving a typo to surface as a
#: module that silently does nothing.
#:
#: These are quoted from each operator's own documentation. They go stale — a
#: crawler's version number moves — and a stale string is still recognisably
#: that crawler, which is what a site's UA check looks for.
AGENTS = {
    "googlebot-smartphone": (
        "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.175 Mobile "
        "Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "googlebot-desktop": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "Googlebot/2.1; +http://www.google.com/bot.html) Chrome/125.0.6422.175 "
        "Safari/537.36"
    ),
    "gptbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
        "GPTBot/1.2; +https://openai.com/gptbot"
    ),
    "oai-searchbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
        "OAI-SearchBot/1.0; +https://openai.com/searchbot"
    ),
    "chatgpt-user": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
        "ChatGPT-User/1.0; +https://openai.com/bot"
    ),
    "claudebot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "ClaudeBot/1.0; +claudebot@anthropic.com)"
    ),
    "claude-searchbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "Claude-SearchBot/1.0; +Claude-SearchBot@anthropic.com)"
    ),
    "claude-user": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "Claude-User/1.0; +Claude-User@anthropic.com)"
    ),
    "perplexitybot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"
    ),
    "bingbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
        "bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/116.0.1938.76 "
        "Safari/537.36"
    ),
}

#: Chrome's user-agent client hints. Every one of these names the real browser,
#: so leaving any behind hands a site a `Sec-CH-UA: "Chromium";v="..."` that
#: flatly contradicts the `User-Agent` — which is a *more* interesting signal
#: to a site than an unmodified Chrome would have been.
CLIENT_HINTS = (
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-model",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-wow64",
)

#: What counts as "a page" for the `documents` scope. A framed document is
#: included: a site that serves its content in an iframe is still serving a
#: page, and excluding it would make the module do nothing on exactly those
#: sites.
DOCUMENT_DESTS = ("document", "iframe", "frame", "embed", "object")


def _user_agent(ctx):
    """The string to send, or None when the settings ask for nothing.

    `custom` with an empty box returns None rather than an empty header: a
    request with `User-Agent:` and no value is not a disguise, it is a broken
    request that some servers reject, and the resulting 400 would look like the
    site blocking crawlers.
    """
    identity = str(ctx.config.get("identity") or "")
    if identity == "custom":
        return str(ctx.config.get("custom_user_agent") or "").strip() or None
    return AGENTS.get(identity)


def _in_scope(request, ctx):
    hosts = ctx.config.get("hosts") or []
    if not any(ctx.matches(request, host=pattern) for pattern in hosts):
        return False
    if str(ctx.config.get("scope") or "all") == "documents":
        # `dest` is None on an insecure context or a client that sends no
        # Sec-Fetch-Dest. Treated as out of scope rather than guessed at: under
        # "documents only" the user asked for a narrow change, and widening it
        # on a guess is the wrong direction to be wrong in.
        return (request.dest or "") in DOCUMENT_DESTS
    return True


def on_load(ctx):
    """Check the manifest's option list against `AGENTS` and say so if it drifts.

    A `settings:` enum and a lookup table in another file are two lists that
    have to agree, and nothing else checks that they do. An identity the form
    offers but the table lacks would present as "the module is enabled and
    changes nothing", which is the hardest kind of failure to notice.
    """
    identity = str(ctx.config.get("identity") or "")
    if identity not in AGENTS and identity != "custom":
        ctx.log(
            "warning",
            "unknown identity; no user agent will be sent",
            identity=identity,
            known=sorted(AGENTS),
        )
        return
    ctx.log("info", "user-agent-switcher armed", identity=identity)


def on_config(ctx):
    """Re-run the load check when the settings change.

    Nothing here is derived state — `on_request` reads `ctx.config` every time —
    so this exists purely to log the change and re-check the identity. A user
    who has just picked something from a dropdown is exactly the person who
    wants to be told it is not usable.
    """
    on_load(ctx)


def on_request(request, ctx):
    agent = _user_agent(ctx)
    if agent is None or not _in_scope(request, ctx):
        return None

    mutation = RequestMutation()
    mutation.set("user-agent", agent)

    language = str(ctx.config.get("accept_language") or "").strip()
    if language:
        mutation.set("accept-language", language)

    if ctx.config.get("strip_client_hints", True):
        for hint in CLIENT_HINTS:
            # Removing a header the request never carried is a no-op, so this
            # does not need to check `has_header` first; keeping the list
            # unconditional means a hint Chrome adds in a later version is
            # already handled if it is in the table.
            mutation.remove(hint)

    # A tally rather than a per-flow note. There is no note code in the SPEC-0
    # §4.4 taxonomy for "a module did the ordinary thing it is enabled to do",
    # and an informational note sent under an unrecognised code arrives as
    # MODULE_ERROR (`ModuleContext.note`) — which would mark every crawler
    # request as a fault. Provenance already records this module as having
    # applied a header change; `on_report` answers "how many, as what".
    #
    # Keyed by the string actually sent rather than by the identity: under
    # `custom` the identity is the word "custom" for every string anyone ever
    # types, and a tally that cannot tell two custom agents apart is not a
    # record of what was sent.
    seen = dict(ctx.store_get("seen", {}))
    seen[agent] = int(seen.get(agent, 0)) + 1
    ctx.store_set("seen", seen)
    return mutation


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def on_report(ctx):
    """How many requests went out under each user agent, since the store was made.

    Small, but it answers the question that comes up every time: *was this page
    actually fetched as the crawler, or did I leave the host list narrow?* The
    traffic view can answer it one flow at a time; this answers it at a glance.
    """
    # Escaped: under `custom` these strings are whatever the user typed, and
    # this report renders in the browser of the person doing the auditing. A
    # tool that turns its own input into markup is the thing being audited.
    seen = ctx.store_get("seen", {}) or {}
    rows = "".join(
        f"<tr><td>{_escape(agent)}</td><td>{int(count):,}</td></tr>"
        for agent, count in sorted(seen.items(), key=lambda item: -int(item[1]))
    )
    body = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>user-agent-switcher</title>"
        "<style>body{font:14px system-ui;margin:2rem}"
        "table{border-collapse:collapse}td,th{padding:.3rem .8rem;text-align:left;"
        "border-bottom:1px solid #ddd}td+td,th+th{text-align:right}</style>"
        "<h1>Requests sent as a crawler</h1>"
        + (
            f"<table><tr><th>User agent</th><th>Requests</th></tr>{rows}</table>"
            if rows
            else "<p>Nothing yet. Enable the module and load a page in scope.</p>"
        )
        + "<p><small>Counts every request this module changed, across restarts. "
        "It does not mean the site treated you as that crawler — operators "
        "verify by address, not by header.</small></p>"
    )
    return {"content_type": "text/html; charset=utf-8", "body": body}
