"""Fixture origin server for pporlock integration and E2E tests.

Serves a fixed set of responses that exercise the pipeline's hard cases, so
tests never depend on the live internet. Started and stopped by the test
harness; run standalone with `make fixtures`.

Fixture inventory (implementation-plan.md §3.4):
  /                       index linking every fixture
  /csp/nonce              CSP with a script nonce  -> inject_script nonce reuse
  /csp/strict             CSP with no nonce        -> forces policy relaxation
  /csp/none               no CSP at all            -> control case
  /sri/page               page loading a script with an integrity attribute
  /sri/script.js          the script that attribute hashes
  /large                  body above the buffering threshold -> must stream
  /dest                   Sec-Fetch-Dest exerciser: loads every destination type
  /dest/<kind>            per-destination endpoints (script/image/style/font/...)
  /slow?ms=N              delayed response
  /encoded?enc=gzip|br    content-encoded body -> decode/re-encode round trip
  /conditional            ETag + Last-Modified -> 304 unless anticache strips them
  /json                   application/json body -> json_patch target
  /ws                     WebSocket echo (inspection-only, REQ PXY-051)
  /health                 liveness for the harness
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import threading
import time
import zlib
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Deterministic so integrity hashes and byte counts are stable across runs.
SRI_SCRIPT = b"window.__pporlock_sri_ok = true;\n"
SRI_SHA384 = "sha384-" + b64encode(hashlib.sha384(SRI_SCRIPT).digest()).decode()
NONCE = "pporlocktestnonce"
LARGE_BODY_BYTES = 4 * 1024 * 1024  # above the 2 MiB default buffering threshold

DEST_KINDS = {
    "script": ("application/javascript", b"window.__dest_script = true;\n"),
    "style": ("text/css", b".pporlock-dest-style{color:#000}\n"),
    "image": (
        "image/gif",
        bytes.fromhex(
            "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
        ),
    ),
    "font": ("font/woff2", b"\x77\x4f\x46\x32fixture"),
    "json": ("application/json", b'{"dest":"empty"}'),
    "iframe": ("text/html", b"<!doctype html><title>iframe</title>\n"),
    "media": ("video/mp4", b"\x00\x00\x00\x18ftypmp42fixture"),
}

INDEX = """<!doctype html>
<meta charset="utf-8"><title>pporlock fixtures</title>
<h1>pporlock fixture origin</h1>
<ul>
  <li><a href="/csp/nonce">/csp/nonce</a></li>
  <li><a href="/csp/strict">/csp/strict</a></li>
  <li><a href="/csp/none">/csp/none</a></li>
  <li><a href="/sri/page">/sri/page</a></li>
  <li><a href="/large">/large</a></li>
  <li><a href="/dest">/dest</a></li>
  <li><a href="/slow?ms=500">/slow</a></li>
  <li><a href="/encoded?enc=gzip">/encoded</a></li>
  <li><a href="/conditional">/conditional</a></li>
  <li><a href="/json">/json</a></li>
  <li><a href="/echo/headers">/echo/headers</a></li>
</ul>
"""

CSP_NONCE_PAGE = f"""<!doctype html>
<meta charset="utf-8"><title>csp nonce</title>
<script nonce="{NONCE}">window.__inline_ran = true;</script>
<p>CSP with nonce {NONCE}</p>
"""

CSP_STRICT_PAGE = """<!doctype html>
<meta charset="utf-8"><title>csp strict</title>
<p>CSP with no nonce; an injected script must be refused unless policy is relaxed.</p>
"""

SRI_PAGE = f"""<!doctype html>
<meta charset="utf-8"><title>sri</title>
<script src="/sri/script.js" integrity="{SRI_SHA384}" crossorigin="anonymous"></script>
<p>Script above carries an integrity attribute. Rewriting it without stripping
   integrity will cause the browser to drop it silently (REQ PXY-040).</p>
"""

DEST_PAGE = """<!doctype html>
<meta charset="utf-8"><title>dest exerciser</title>
<link rel="stylesheet" href="/dest/style">
<script src="/dest/script"></script>
<img src="/dest/image" alt="">
<iframe src="/dest/iframe" title="f"></iframe>
<script>fetch('/dest/json');</script>
<p>Loads one subresource per Sec-Fetch-Dest value.</p>
"""


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "pporlock-fixture/1.0"
    protocol_version = "HTTP/1.1"

    # Quiet by default; the harness owns the output.
    def log_message(self, fmt: str, *args: object) -> None:
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def handle_one_request(self) -> None:
        """Swallow client-side disconnects.

        Clients legitimately drop the connection mid-response — urllib does it
        on a 304, and the proxy under test does it when a rule short-circuits.
        Letting those surface as tracebacks would bury real failures in noise.
        """
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -- helpers ---------------------------------------------------------
    def _send(
        self,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        status: int = 200,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            return self._send(b'{"ok":true}', "application/json")

        if path == "/":
            return self._send(INDEX.encode())

        if path == "/csp/nonce":
            return self._send(
                CSP_NONCE_PAGE.encode(),
                extra={"Content-Security-Policy": f"script-src 'nonce-{NONCE}'; object-src 'none'"},
            )

        if path == "/csp/strict":
            return self._send(
                CSP_STRICT_PAGE.encode(),
                extra={"Content-Security-Policy": "script-src 'self'; object-src 'none'"},
            )

        if path == "/csp/none":
            return self._send(CSP_STRICT_PAGE.encode())

        if path == "/sri/page":
            return self._send(SRI_PAGE.encode())

        if path == "/sri/script.js":
            return self._send(SRI_SCRIPT, "application/javascript")

        if path == "/large":
            return self._send(b"x" * LARGE_BODY_BYTES, "text/plain")

        if path == "/dest":
            return self._send(DEST_PAGE.encode())

        if path.startswith("/dest/"):
            kind = path.removeprefix("/dest/")
            if kind not in DEST_KINDS:
                return self._send(b"unknown dest", "text/plain", status=404)
            content_type, body = DEST_KINDS[kind]
            return self._send(body, content_type)

        if path == "/slow":
            delay_ms = min(int(query.get("ms", ["500"])[0]), 30_000)
            time.sleep(delay_ms / 1000)
            return self._send(f"waited {delay_ms}ms".encode(), "text/plain")

        if path == "/encoded":
            enc = query.get("enc", ["gzip"])[0]
            raw = b"pporlock encoded fixture body\n" * 200
            if enc == "gzip":
                return self._send(
                    gzip.compress(raw), "text/plain", extra={"Content-Encoding": "gzip"}
                )
            if enc == "deflate":
                return self._send(
                    zlib.compress(raw), "text/plain", extra={"Content-Encoding": "deflate"}
                )
            return self._send(raw, "text/plain")

        if path == "/conditional":
            etag = '"pporlock-fixture-v1"'
            last_modified = "Wed, 27 Aug 2025 00:00:00 GMT"
            inm = self.headers.get("If-None-Match")
            ims = self.headers.get("If-Modified-Since")
            if inm == etag or ims == last_modified:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                return None
            return self._send(
                b"conditional fixture body\n",
                "text/plain",
                extra={"ETag": etag, "Last-Modified": last_modified},
            )

        if path == "/json":
            payload = {"ads": [1, 2, 3], "content": "keep me", "nested": {"tracker": True}}
            return self._send(json.dumps(payload).encode(), "application/json")

        if path == "/echo/headers":
            # What the origin actually received, lowercased. The only way to
            # answer "did the proxy really change that request header" without
            # trusting the thing under test to report on itself — which is what
            # every unit test on both sides of the wire necessarily does.
            #
            # Repeated headers are joined with ", ": this exists to check a
            # rewrite, and losing a duplicate would hide the case where a `set`
            # appended instead of replacing.
            received: dict[str, str] = {}
            for key, value in self.headers.items():
                lowered = key.lower()
                received[lowered] = (
                    f"{received[lowered]}, {value}" if lowered in received else value
                )
            return self._send(json.dumps(received).encode(), "application/json")

        return self._send(b"not found", "text/plain", status=404)

    def do_HEAD(self) -> None:
        self.do_GET()


class FixtureServer:
    """Context-managed fixture origin, for use from the test harness."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, verbose: bool = False) -> None:
        self._httpd = ThreadingHTTPServer((host, port), FixtureHandler)
        self._httpd.verbose = verbose  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> FixtureServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> FixtureServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="pporlock fixture origin server")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = FixtureServer(host=args.host, port=args.port, verbose=args.verbose).start()
    print(f"fixture origin listening on {server.base_url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
