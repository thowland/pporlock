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
    if not ctx.matches(request, dest="document", response=response, content_type="text/html"):
        return None

    text = response.text
    if text is None or "</body>" not in text:
        # Streamed, binary, or not a document we can safely edit. Reporting
        # nothing is correct; guessing is not.
        return None

    ctx.note(
        "script_injected",
        "added a scroll-unlock shim after hiding consent overlays",
        severity="warning",
        where="body_end",
    )
    return ResponseMutation(body=text.replace("</body>", UNLOCK + "</body>", 1).encode())
