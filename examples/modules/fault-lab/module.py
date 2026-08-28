"""Fault injection you can reproduce.

The counter is deliberate. Random failure finds bugs but cannot be replayed,
so a failure you saw once is a failure you argue about; "every third request"
fails the same request every time you run the same page.

State lives in ctx.store, not in a module-level variable, so the count survives
a module reload — otherwise editing the file silently resets the experiment.
"""

from pporlock.engine.models import RequestMutation

BODY = b'{"error":"injected by pporlock fault-lab","retryable":true}'


def on_load(ctx):
    ctx.log("info", "fault-lab armed", every=ctx.config.get("every", 0))


def on_request(request, ctx):
    every = int(ctx.config.get("every", 0) or 0)
    if every <= 0:
        return None

    if not ctx.matches(request, host=ctx.config.get("host", "*")):
        return None
    if not request.path.startswith(str(ctx.config.get("path_prefix", "/"))):
        return None

    seen = int(ctx.store_get("seen", 0)) + 1
    ctx.store_set("seen", seen)
    if seen % every != 0:
        return None

    status = int(ctx.config.get("status", 503))
    ctx.note(
        "module_error",
        f"injected HTTP {status} into request {seen}",
        severity="warning",
        path=request.path,
        nth=seen,
    )
    # short_circuit ends the flow with this response. The request never reaches
    # the origin, which is the point — a real 503 from a real server would also
    # have side effects.
    return RequestMutation(
        short_circuit=ctx.synthesize(status=status, content_type="application/json", body=BODY)
    )
