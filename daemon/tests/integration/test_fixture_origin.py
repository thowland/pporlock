"""The fixture origin serves every case the pipeline tests depend on.

If these fail, every integration test from Sprint 2 onward is untrustworthy.
"""

from __future__ import annotations

import gzip
import urllib.request

import pytest

pytestmark = pytest.mark.integration


def _get(url: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(url, headers=headers or {})  # noqa: S310 — loopback fixture
    return urllib.request.urlopen(req, timeout=5)  # noqa: S310


def test_health(fixture_origin) -> None:
    with _get(f"{fixture_origin.base_url}/health") as r:
        assert r.status == 200
        assert b'"ok":true' in r.read()


def test_csp_nonce_page_carries_a_nonce(fixture_origin) -> None:
    """Target for inject_script nonce reuse (REQ PXY-041)."""
    with _get(f"{fixture_origin.base_url}/csp/nonce") as r:
        csp = r.headers["Content-Security-Policy"]
        assert "nonce-" in csp


def test_csp_strict_page_has_no_nonce(fixture_origin) -> None:
    """Forces the policy-relaxation fallback path."""
    with _get(f"{fixture_origin.base_url}/csp/strict") as r:
        assert "nonce-" not in r.headers["Content-Security-Policy"]


def test_sri_page_carries_integrity_and_crossorigin(fixture_origin) -> None:
    """Target for strip_integrity_attributes (REQ PXY-040)."""
    with _get(f"{fixture_origin.base_url}/sri/page") as r:
        body = r.read().decode()
        assert "integrity=" in body
        assert "crossorigin=" in body


def test_large_body_exceeds_the_buffering_threshold(fixture_origin) -> None:
    """Must stream rather than buffer (REQ PXY-021)."""
    with _get(f"{fixture_origin.base_url}/large") as r:
        assert int(r.headers["Content-Length"]) > 2 * 1024 * 1024


@pytest.mark.parametrize("kind", ["script", "style", "image", "font", "json", "iframe", "media"])
def test_dest_endpoints(fixture_origin, kind: str) -> None:
    """One endpoint per Sec-Fetch-Dest value (REQ PXY-032)."""
    with _get(f"{fixture_origin.base_url}/dest/{kind}") as r:
        assert r.status == 200
        assert r.read()


def test_gzip_encoded_body_round_trips(fixture_origin) -> None:
    """Decode/re-encode target (REQ PXY-023)."""
    with _get(f"{fixture_origin.base_url}/encoded?enc=gzip") as r:
        assert r.headers["Content-Encoding"] == "gzip"
        assert b"pporlock encoded fixture body" in gzip.decompress(r.read())


def test_conditional_returns_304_when_etag_matches(fixture_origin) -> None:
    """The case anticache exists to defeat (REQ PXY-043)."""
    url = f"{fixture_origin.base_url}/conditional"
    with _get(url) as first:
        etag = first.headers["ETag"]
        assert first.status == 200

    try:
        with _get(url, {"If-None-Match": etag}) as second:
            assert second.status == 304
    except urllib.error.HTTPError as exc:  # urllib raises on 304 without a body
        assert exc.code == 304


def test_json_fixture_is_patchable(fixture_origin) -> None:
    """Target for json_patch (SPEC-0 §5.5)."""
    import json

    with _get(f"{fixture_origin.base_url}/json") as r:
        payload = json.loads(r.read())
        assert "ads" in payload
        assert payload["content"] == "keep me"


def test_echo_headers_reports_what_the_origin_received(fixture_origin) -> None:
    """The endpoint that lets a test ask the *origin* what arrived.

    Every other check of a header rewrite asks pporlock whether it rewrote the
    header, which is the thing under test reporting on itself. This is how the
    module-settings E2E can assert that a `User-Agent` really changed on the
    wire rather than in a mutation object.
    """
    import json

    with _get(f"{fixture_origin.base_url}/echo/headers", {"User-Agent": "pporlock-test/1.0"}) as r:
        received = json.loads(r.read())
        # Lowercased, so a caller does not have to guess the sender's casing.
        assert received["user-agent"] == "pporlock-test/1.0"
        assert "host" in received


def test_unknown_path_is_404(fixture_origin) -> None:
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{fixture_origin.base_url}/nope")
    assert exc.value.code == 404
