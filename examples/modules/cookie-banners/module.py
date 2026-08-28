"""Restore what a consent overlay takes away.

CSS in the manifest hides the dialog. This handles the part CSS cannot: a
banner that has already run its script may have set inline styles on <body>,
and an inline style beats a stylesheet rule unless that rule is !important —
which the manifest's is, but only for the properties it names.

Nothing here reads page content or talks to the network. It injects one small
script and says so.
"""

from pporlock.engine.models import ResponseMutation

# Kept out of the hook so the string is built once at import rather than on
# every HTML response.
UNLOCK = """
<script>(function(){
  try {
    var d = document, s = d.documentElement.style, b = d.body && d.body.style;
    var free = function (el) {
      if (!el) return;
      el.style.setProperty('overflow', 'auto', 'important');
      el.style.setProperty('position', 'static', 'important');
    };
    free(d.documentElement); free(d.body);
    // Banners often re-apply the lock after their own script runs, so watch
    // briefly rather than assuming one pass is enough. Bounded: 20 x 250ms.
    var n = 0, t = setInterval(function () {
      free(d.documentElement); free(d.body);
      if (++n > 20) clearInterval(t);
    }, 250);
  } catch (e) { /* never break the page we are trying to fix */ }
})();</script>
"""


def on_response(request, response, ctx):
    """Add the unlock script to HTML documents only."""
    # ctx.matches takes the request positionally: the context is per-module and
    # long-lived, so it does not know which flow you mean.
    # No dest= here, for the same reason the manifest rule omits it:
    # Sec-Fetch-Dest is absent on insecure origins and in most command-line
    # testing, and a hook that requires it does nothing there while looking
    # like it ran. The </body> check below is the real guard.
    if not ctx.matches(request, response=response, content_type="text/html"):
        return None

    text = response.text
    if text is None:
        # Streamed or binary. Reporting nothing is correct; guessing is not.
        return None

    # </body> is the right place, but it is optional in HTML and real pages
    # omit it. An earlier version required it and therefore did nothing at all
    # on a valid document — silently, which is the failure this system exists
    # to prevent. Appending is a worse position and a much better outcome than
    # not running.
    if "</body>" in text:
        patched = text.replace("</body>", UNLOCK + "</body>", 1)
    else:
        patched = text + UNLOCK

    ctx.note(
        "script_injected",
        "added a scroll-unlock shim after hiding consent overlays",
        severity="warning",
        where="body_end" if "</body>" in text else "document_end",
    )
    return ResponseMutation(body=patched.encode())
