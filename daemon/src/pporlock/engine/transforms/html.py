"""HTML transforms — REQ PXY-040/041.

Regex over HTML is the wrong tool in general. It is the right one here for a
narrow reason: these operate on attributes of specific tags in a document that
has already been served, and the alternative — parsing and re-serialising with a
full HTML parser — rewrites markup the page never asked us to touch, which for a
tool whose whole job is *not* breaking pages is the worse risk.

Each transform is scoped to the smallest pattern that does its job, and each
reports what it did so the change is never silent (SPEC-0 §4.4).
"""

from __future__ import annotations

import re
from typing import Any

#: `integrity` and `crossorigin` on a script or link tag.
#:
#: Any modification to a subresource carrying `integrity` will fail its hash
#: check and be dropped by the browser — silently from the proxy's point of
#: view, since the proxy sees a successful response and only the browser console
#: shows the failure (REQ PXY-040).
_INTEGRITY_ATTR = re.compile(
    r"""\s+(integrity|crossorigin)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)
_SCRIPT_OR_LINK_TAG = re.compile(r"<\s*(script|link)\b[^>]*>", re.IGNORECASE)

_HEAD_OPEN = re.compile(r"<\s*head\b[^>]*>", re.IGNORECASE)
_HEAD_CLOSE = re.compile(r"<\s*/\s*head\s*>", re.IGNORECASE)
_BODY_CLOSE = re.compile(r"<\s*/\s*body\s*>", re.IGNORECASE)

#: A `nonce-...` value in a CSP directive.
_CSP_NONCE = re.compile(r"'nonce-([A-Za-z0-9+/=_-]+)'")


def strip_integrity_attributes(text: str, params: Any) -> str:
    """Remove `integrity` and `crossorigin` from script and link tags.

    Applied whenever a document's body is rewritten, whether or not a rule asked
    for it: the breakage is invisible from the proxy's side, so leaving it to
    the operator to remember would guarantee it is eventually forgotten.
    """
    removed = 0

    def scrub(match: re.Match[str]) -> str:
        nonlocal removed
        tag = match.group(0)
        cleaned, count = _INTEGRITY_ATTR.subn("", tag)
        removed += count
        return cleaned

    result = _SCRIPT_OR_LINK_TAG.sub(scrub, text)
    if removed:
        params.note(
            "sri_stripped",
            f"removed {removed} integrity/crossorigin attribute(s)",
            count=removed,
        )
    return result


def _existing_nonce(params: Any) -> str | None:
    """The page's own script-src nonce, if it has one."""
    for header in ("content-security-policy", "content-security-policy-report-only"):
        value = params.header(header)
        if not value:
            continue
        found = _CSP_NONCE.search(value)
        if found:
            return found.group(1)
    return None


def _insert(text: str, snippet: str, position: str) -> str:
    """Place a snippet, falling back down the document as anchors are missing."""
    if position == "head_start":
        match = _HEAD_OPEN.search(text)
        if match:
            return text[: match.end()] + snippet + text[match.end() :]
    if position in ("head_start", "head_end"):
        match = _HEAD_CLOSE.search(text)
        if match:
            return text[: match.start()] + snippet + text[match.start() :]
    match = _BODY_CLOSE.search(text)
    if match:
        return text[: match.start()] + snippet + text[match.start() :]
    # No recognisable structure — a fragment, or malformed markup. Appending is
    # the least surprising thing to do, and better than dropping the injection.
    return text + snippet


def inject_script(text: str, params: Any) -> str:
    """Inject a script tag, reusing the page's own nonce where one exists.

    REQ PXY-041. Reusing the nonce is preferred over relaxing the policy because
    it leaves the page's own protections intact: a relaxed `script-src` admits
    every other script on the page too, which is a far larger change than the one
    being asked for.
    """
    src = params.get("src")
    inline = params.get("inline")
    position = str(params.get("position") or "head_end")
    reuse = params.get("reuse_nonce", True)

    nonce = _existing_nonce(params) if reuse else None
    nonce_attr = f' nonce="{nonce}"' if nonce else ""

    if src:
        snippet = f'<script{nonce_attr} src="{src}"></script>'
    else:
        snippet = f"<script{nonce_attr}>{inline}</script>"

    params.note(
        "script_injected",
        "injected a script tag" + (" reusing the page's nonce" if nonce else ""),
        nonce_reused=bool(nonce),
        position=position,
        src=src or None,
    )
    return _insert(text, snippet, position)


def inject_style(text: str, params: Any) -> str:
    """Inject a stylesheet link or inline style."""
    href = params.get("href")
    inline = params.get("inline")
    position = str(params.get("position") or "head_end")

    snippet = f'<link rel="stylesheet" href="{href}">' if href else f"<style>{inline}</style>"
    params.note("script_injected", "injected a stylesheet", position=position, style=True)
    return _insert(text, snippet, position)
