"""Header transforms — REQ PXY-042.

`strip_csp` is listed among the body transforms in SPEC-0 §5.5, but it operates
on headers, and Sprint 9 established that header work must happen at
`responseheaders`: once a response streams, its headers are already on the wire
and a later mutation is recorded as applied while changing nothing.

So it is registered as a transform for the rule schema's sake, and applied in
the response-header phase where it can actually take effect.
"""

from __future__ import annotations

CSP_HEADERS = ("content-security-policy", "content-security-policy-report-only")


def csp_headers_to_remove(report_only: bool = True) -> tuple[str, ...]:
    """Which CSP headers `strip_csp` removes.

    Both by default. Removing only the enforcing header while leaving
    report-only in place produces a page that works but floods a report endpoint
    with violations the operator did not cause.
    """
    return CSP_HEADERS if report_only else CSP_HEADERS[:1]
