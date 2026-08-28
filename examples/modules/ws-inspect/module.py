"""WebSocket frame inspection.

on_websocket_message is read-only by design (REQ PXY-051): whatever it returns
is ignored. That is deliberate — a hook whose return value were quietly dropped
while provenance claimed a change would be worse than one that cannot change
anything at all.

So this reports rather than acts, and keeps its running totals in ctx.store,
where they survive a reload.
"""


def on_load(ctx):
    ctx.log("info", "ws-inspect watching", terms=ctx.config.get("interesting", []))


def on_websocket_message(message, request, ctx):
    terms = [str(t).lower() for t in ctx.config.get("interesting", [])]
    limit = int(ctx.config.get("large_frame_bytes", 65536))

    total = int(ctx.store_get("frames", 0)) + 1
    ctx.store_set("frames", total)

    if message.size > limit:
        ctx.note(
            "module_error",
            f"large WebSocket frame: {message.size} bytes",
            severity="info",
            direction=message.direction,
            index=message.index,
        )

    # Text frames only. Decoding a binary frame as UTF-8 to search it for words
    # is how you turn a working socket into an exception on every frame.
    if message.opcode != "text":
        return

    try:
        text = message.payload.decode("utf-8", "strict").lower()
    except UnicodeDecodeError:
        return

    hit = next((t for t in terms if t in text), None)
    if hit is not None:
        ctx.note(
            "module_error",
            f"WebSocket frame contains {hit!r}",
            severity="warning",
            direction=message.direction,
            index=message.index,
            frames_seen=total,
        )
