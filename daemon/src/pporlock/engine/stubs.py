"""Synthesised responses — SPEC-1 §4.7, REQ PXY-031/032/033.

Killing the connection is the wrong default for blocking. A page's JavaScript
routinely reacts to a failed fetch by retrying, by throwing into a handler that
breaks unrelated rendering, or by showing an anti-adblock notice. Synthesising a
benign response whose content type matches what the browser actually asked for
avoids all three.

What the browser asked for comes from ``Sec-Fetch-Dest`` rather than from
guessing at the URL extension — a script served from ``/api/v2/collect`` has no
extension to guess from, and a ``.js`` URL fetched by XHR wants JSON.

**Sec-Fetch-Dest is only sent on secure contexts.** On a plain-HTTP page Chrome
omits the Sec-Fetch metadata headers entirely, which was measured directly: a
blocked tracking pixel on an http:// page received a 204 and never rendered.
``Accept`` is always sent and distinguishes documents, stylesheets, and images
reliably, so it is the fallback. It cannot separate a script from an XHR — both
send ``*/*`` — and that ambiguity is resolved toward script, because a blocked
tracker requested with ``*/*`` is overwhelmingly a script tag and an empty
script body is harmless. An explicit ``stub:`` overrides all of this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import RuleValidationError
from .models import NormalizedRequest, SyntheticResponse

#: 1x1 transparent GIF. Smaller than a PNG and universally understood, which
#: matters when it is standing in for a tracking pixel a layout depends on.
TRANSPARENT_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)

#: Where the shipped stub scripts live (REQ PXY-033).
BUILTIN_STUB_DIR = Path(__file__).resolve().parents[3].parent / "stubs"

BLOCKED_DOCUMENT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Blocked by pporlock</title>
<style>body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
margin:8vh auto;max-width:34rem;padding:0 1.5rem;color:#1a1d23}}
code{{font-family:ui-monospace,Menlo,monospace;background:#f2f4f7;padding:.1rem .3rem;
border-radius:3px}}h1{{font-size:1.1rem}}p{{color:#5c6472}}</style></head>
<body><h1>Blocked by pporlock</h1>
<p>A navigation to <code>{url}</code> was blocked by rule <code>{rule}</code>.</p>
<p>This page is shown instead of a network error so the block is visible rather
than looking like a connectivity problem.</p></body></html>
"""


def _response(status: int, content_type: str | None, body: bytes, origin: str) -> SyntheticResponse:
    headers: list[tuple[str, str]] = []
    if content_type is not None:
        headers.append(("content-type", content_type))
    # A synthesised response must never be cached: the rule that produced it can
    # be edited or disabled a second later, and a cached stub would outlive it.
    headers.append(("cache-control", "no-store, no-cache, must-revalidate"))
    headers.append(("x-pporlock", "blocked"))
    return SyntheticResponse(status=status, body=body, headers=tuple(headers), origin=origin)


def infer_dest_from_accept(accept: str | None) -> str | None:
    """Infer the destination from ``Accept`` when Sec-Fetch-Dest is absent.

    Only the cases Accept genuinely distinguishes. Returns None when it does
    not, so the caller can apply its own default rather than guessing here.
    """
    if not accept:
        return None
    value = accept.lower()

    if "text/html" in value or "application/xhtml" in value:
        return "document"
    if "text/css" in value:
        return "style"
    if "image/" in value:
        return "image"
    if "font/" in value or "application/font" in value:
        return "font"
    return None


def auto_for(
    dest: str | None, request: NormalizedRequest, *, origin: str = "", rule: str = ""
) -> SyntheticResponse:
    """Derive a benign response from ``Sec-Fetch-Dest`` (REQ PXY-032).

    The table is normative and implemented exactly once, here. When the header
    is absent — which is every request from a non-secure context — the
    destination is inferred from ``Accept`` instead; see the module docstring.
    """
    kind = (dest or "").strip().lower()
    if not kind:
        inferred = infer_dest_from_accept(request.header("accept"))
        if inferred is not None:
            kind = inferred
        elif (request.header("accept") or "").strip() in {"*/*", ""}:
            # Indistinguishable between a script and an XHR. Resolved toward
            # script: an empty script body is harmless, and a blocked tracker
            # requested with */* is overwhelmingly a script tag.
            kind = "script"

    if kind == "script":
        return _response(200, "application/javascript", b"", origin)
    if kind == "image":
        return _response(200, "image/gif", TRANSPARENT_GIF, origin)
    if kind == "empty":
        # fetch/XHR. `{}` rather than an empty body: a caller doing
        # response.json() on nothing throws, which is the failure we are here to
        # prevent.
        return _response(200, "application/json", b"{}", origin)
    if kind == "iframe":
        return _response(200, "text/html", b"<!doctype html><title></title>", origin)
    if kind == "style":
        return _response(200, "text/css", b"", origin)
    if kind == "document":
        # A blocked top-level navigation must be visible to the user rather than
        # looking like a network failure, so it gets a page and a 403.
        html = BLOCKED_DOCUMENT_HTML.format(url=request.url, rule=rule or origin or "unnamed")
        return _response(403, "text/html; charset=utf-8", html.encode(), origin)

    # font, media, object, worker, or no Sec-Fetch-Dest at all.
    return _response(204, None, b"", origin)


class StubLibrary:
    """Named stub scripts, loaded once."""

    __slots__ = ("_dirs", "_stubs")

    def __init__(self, directories: list[Path] | None = None) -> None:
        self._dirs = directories if directories is not None else [BUILTIN_STUB_DIR]
        self._stubs: dict[str, bytes] = {}
        self._load()

    def _load(self) -> None:
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.js")):
                self._stubs.setdefault(path.stem, path.read_bytes())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._stubs))

    def has(self, name: str) -> bool:
        return name in self._stubs

    def named(self, name: str, *, origin: str = "") -> SyntheticResponse:
        body = self._stubs.get(name)
        if body is None:
            raise RuleValidationError(
                f"unknown stub {name!r}; available: {', '.join(self.names) or 'none'}",
                field="stub",
            )
        return _response(200, "application/javascript", body, origin)

    def resolve(
        self,
        spec: Any,
        request: NormalizedRequest,
        *,
        origin: str = "",
        rule: str = "",
    ) -> SyntheticResponse:
        """Turn a rule's ``stub:`` value into a response (SPEC-0 §5.6).

        ``auto`` derives from Sec-Fetch-Dest; a string names a library stub; a
        mapping is an inline specification.
        """
        if spec is None or spec == "auto":
            return auto_for(request.dest, request, origin=origin, rule=rule)

        if isinstance(spec, str):
            return self.named(spec, origin=origin)

        if isinstance(spec, dict):
            body = spec.get("body", "")
            payload = body.encode() if isinstance(body, str) else bytes(body)
            return _response(
                int(spec.get("status", 200)),
                spec.get("content_type"),
                payload,
                origin,
            )

        raise RuleValidationError(f"invalid stub specification: {spec!r}", field="stub")
