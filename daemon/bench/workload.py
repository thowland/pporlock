"""The fixed reference workload — REQ PRF-003, open issue OI-3 (v0.3).

PRF-001 is stated against "a reference page load with a representative profile
enabled". A benchmark whose workload drifts is a benchmark whose numbers cannot
be compared across releases, so the workload is defined here as data and nowhere
else, and it is deliberately dull: an HTML document and a fixed set of
subresources served by the in-repo fixture origin.

Two properties matter more than realism:

* **Repeatable.** No network beyond loopback, no live site, no CDN. The number
  has to mean the same thing in six months.
* **Representative of the shape that costs us.** A page is one document plus
  many small subresources. The document is the flow a rule set actually works
  on; the subresources are the flows that must cost almost nothing, and there
  are twenty times as many of them. PRF-002 is about the second kind, which is
  why the workload has far more of them than a synthetic "one request" benchmark
  would.

The rule set is a *representative profile*: a handful of rules that mostly do
not match, which is the realistic case. A profile in which every rule matches
every flow would measure a situation nobody runs.
"""

from __future__ import annotations

from typing import Any

#: The reference page: the document, then its subresources, in load order.
#: Paths are served by ``testfixtures/origin/server.py``.
PAGE_PATHS: tuple[str, ...] = (
    "/csp/nonce",
    *("/dest/script",) * 8,
    *("/dest/style",) * 4,
    *("/dest/image",) * 20,
    *("/dest/json",) * 6,
    "/dest/font",
    "/json",
)

#: A representative profile. Two rules that can match the document, three that
#: cannot match anything in the workload — because a real rule set is mostly
#: rules that do not apply to the flow in front of you, and the cost of deciding
#: that is exactly what PRF-002 bounds.
REFERENCE_RULES: list[dict[str, Any]] = [
    {
        "name": "relax-csp-on-app",
        "action": "headers",
        "match": {"host": "127.0.0.1", "path": r"^/csp/"},
        "response": {"remove": ["content-security-policy"]},
    },
    {
        "name": "inject-into-document",
        "action": "body",
        "match": {"host": "127.0.0.1", "path": r"^/csp/nonce$", "content_type": "text/html"},
        "transforms": [{"kind": "inject_script", "content": "window.__bench = 1;"}],
    },
    {
        "name": "never-matches-host",
        "action": "headers",
        "match": {"host": "api.nonexistent.example"},
        "response": {"set": {"x-bench": "1"}},
    },
    {
        "name": "never-matches-method",
        "action": "block",
        "match": {"host": "*", "method": ["DELETE"]},
    },
    {
        "name": "never-matches-path",
        "action": "headers",
        "match": {"host": "*", "path": r"^/admin/(secret|hidden)/"},
        "request": {"set": {"x-bench-admin": "1"}},
    },
]

#: Rules for the PRF-002 measurement: the same representative profile, with the
#: two rules that *can* match removed. What is left is the pure cost of deciding
#: that nothing applies.
NON_MATCHING_RULES: list[dict[str, Any]] = [r for r in REFERENCE_RULES if "never" in r["name"]]

#: A non-matching flow, as the engine sees it. Kept close to a real subresource:
#: a scripted fetch of a small JSON document with an ordinary header set.
NON_MATCHING_FLOW: dict[str, Any] = {
    "method": "GET",
    "scheme": "http",
    "host": "127.0.0.1",
    "port": 8099,
    "path": "/dest/json",
    "dest": "empty",
    "request_headers": (
        ("host", "127.0.0.1:8099"),
        ("user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"),
        ("accept", "*/*"),
        ("accept-encoding", "gzip, deflate, br"),
        ("accept-language", "en-US,en;q=0.9"),
        ("sec-fetch-dest", "empty"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-site", "same-origin"),
        ("referer", "http://127.0.0.1:8099/csp/nonce"),
    ),
    "status": 200,
    "response_headers": (
        ("content-type", "application/json"),
        ("content-length", "16"),
        ("cache-control", "no-store"),
        ("date", "Thu, 27 Aug 2026 14:00:00 GMT"),
    ),
    "body": b'{"dest":"empty"}',
}
