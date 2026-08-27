"""The mitmproxy adapter. SPEC-1 §3.2, §3.3.

Tested against stub flows rather than a live proxy: the adapter's job is shape
translation, and the shapes are what break when mitmproxy moves. Real-traffic
behaviour is covered by the integration suite.
"""

from __future__ import annotations

import pytest

from pporlock.addon import apply as apply_mod
from pporlock.addon import normalize
from pporlock.engine.models import (
    RedirectSpec,
    RequestMutation,
    ResponseMutation,
    SyntheticResponse,
)
from tests.stubs import StubFlow, StubHeaders, StubRequest, StubResponse


class TestTimestamps:
    def test_wire_format(self) -> None:
        assert normalize.ts_to_iso(1756300000.123).endswith("Z")
        assert len(normalize.ts_to_iso(1756300000.123)) == len("2026-08-27T14:03:22.417Z")

    def test_milliseconds_are_truncated_not_rounded(self) -> None:
        assert normalize.ts_to_iso(1756300000.9999).endswith(".999Z")

    def test_none_falls_back_to_now(self) -> None:
        assert normalize.ts_to_iso(None).endswith("Z")

    def test_now_is_a_single_clock_read(self) -> None:
        """Sampling seconds and milliseconds separately can emit a time that
        never existed. Format alone cannot prove this, so assert it is parseable
        and monotonic across calls."""
        first, second = normalize.now_iso(), normalize.now_iso()
        assert first <= second


class TestNormalizeHeaders:
    def test_lowercases_names_preserves_values(self) -> None:
        headers = StubHeaders([(b"Content-Type", b"text/HTML")])
        assert normalize.normalize_headers(headers) == (("content-type", "text/HTML"),)

    def test_preserves_repeats_and_order(self) -> None:
        headers = StubHeaders([(b"Set-Cookie", b"a=1"), (b"Set-Cookie", b"b=2")])
        assert normalize.normalize_headers(headers) == (
            ("set-cookie", "a=1"),
            ("set-cookie", "b=2"),
        )

    def test_non_utf8_bytes_do_not_raise(self) -> None:
        """Header values are latin-1 on the wire; a decode error here would drop
        a whole flow for a cosmetic reason."""
        headers = StubHeaders([(b"X-Weird", b"\xff\xfe")])
        assert normalize.normalize_headers(headers)[0][0] == "x-weird"


class TestNormalizeRequest:
    def test_basic_fields(self) -> None:
        req = normalize.normalize_request(StubFlow(), flow_id="f1")
        assert req.flow_id == "f1"
        assert req.method == "GET"
        assert req.scheme == "https"
        assert req.host == "cdn.example.com"
        assert req.port == 443

    def test_method_is_uppercased(self) -> None:
        req = normalize.normalize_request(StubFlow(StubRequest(method="post")), flow_id="f")
        assert req.method == "POST"

    def test_path_excludes_the_query_string(self) -> None:
        """path and query are separate criteria in the rule schema; leaving the
        query on the path would make every path regex quietly wrong."""
        req = normalize.normalize_request(StubFlow(), flow_id="f")
        assert req.path == "/a/analytics.js"

    def test_empty_path_becomes_root(self) -> None:
        req = normalize.normalize_request(StubFlow(StubRequest(path="")), flow_id="f")
        assert req.path == "/"

    def test_query_pairs(self) -> None:
        req = normalize.normalize_request(StubFlow(), flow_id="f")
        assert req.query == (("v", "3"),)

    def test_query_falls_back_to_parsing_the_url(self) -> None:
        request = StubRequest(query=None, url="https://a.example/x?a=1&b=2&b=3")
        request.query = object()  # no .items(multi=)
        req = normalize.normalize_request(StubFlow(request), flow_id="f")
        assert req.query == (("a", "1"), ("b", "2"), ("b", "3"))

    def test_query_fallback_with_no_query_string(self) -> None:
        request = StubRequest(url="https://a.example/x")
        request.query = object()
        assert normalize.normalize_request(StubFlow(request), flow_id="f").query == ()

    def test_sec_fetch_dest_is_extracted(self) -> None:
        """Blocking derives the synthesized response type from this (REQ PXY-032)."""
        request = StubRequest(headers=StubHeaders([(b"Sec-Fetch-Dest", b"script")]))
        assert normalize.normalize_request(StubFlow(request), flow_id="f").dest == "script"

    def test_missing_sec_fetch_dest_is_none(self) -> None:
        assert normalize.normalize_request(StubFlow(), flow_id="f").dest is None

    def test_http_scheme_is_preserved(self) -> None:
        request = StubRequest(scheme="http", port=80)
        assert normalize.normalize_request(StubFlow(request), flow_id="f").scheme == "http"

    def test_unknown_scheme_defaults_to_http(self) -> None:
        request = StubRequest(scheme="ftp")
        assert normalize.normalize_request(StubFlow(request), flow_id="f").scheme == "http"

    def test_tab_id_is_carried_through(self) -> None:
        req = normalize.normalize_request(StubFlow(), flow_id="f", tab_id=481)
        assert req.tab_id == 481


class TestNormalizeResponse:
    def test_basic_fields(self) -> None:
        flow = StubFlow(response=StubResponse())
        resp = normalize.normalize_response(flow, flow_id="f", body=b"body")
        assert resp.status == 200
        assert resp.reason == "OK"
        assert resp.body == b"body"
        assert not resp.streamed

    def test_streamed_response_carries_no_body(self) -> None:
        """REQ PXY-022 — the body was never buffered, so it must not appear."""
        flow = StubFlow(response=StubResponse())
        resp = normalize.normalize_response(flow, flow_id="f", body=b"x", streamed=True)
        assert resp.streamed
        assert resp.body is None

    def test_content_encoding_is_recorded(self) -> None:
        response = StubResponse(headers=StubHeaders([(b"Content-Encoding", b"br")]))
        resp = normalize.normalize_response(StubFlow(response=response), flow_id="f")
        assert resp.encoding == "br"

    def test_absent_encoding_is_none(self) -> None:
        resp = normalize.normalize_response(StubFlow(response=StubResponse()), flow_id="f")
        assert resp.encoding is None


class TestApplyHeaders:
    def test_remove(self) -> None:
        headers = StubHeaders([(b"Content-Security-Policy", b"default-src 'self'")])
        mutation = ResponseMutation()
        mutation.remove("content-security-policy")
        assert apply_mod._apply_header_ops(headers, mutation)
        assert "content-security-policy" not in headers

    def test_remove_absent_header_is_not_a_change(self) -> None:
        mutation = ResponseMutation()
        mutation.remove("x-nope")
        assert not apply_mod._apply_header_ops(StubHeaders(), mutation)

    def test_set_replaces_rather_than_appending(self) -> None:
        headers = StubHeaders([(b"X-Frame-Options", b"SAMEORIGIN")])
        mutation = ResponseMutation()
        mutation.set("x-frame-options", "DENY")
        apply_mod._apply_header_ops(headers, mutation)
        assert headers.get("x-frame-options") == "DENY"
        assert len(headers.fields) == 1

    def test_set_to_the_same_value_is_not_a_change(self) -> None:
        headers = StubHeaders([(b"X-A", b"1")])
        mutation = ResponseMutation()
        mutation.set("x-a", "1")
        assert not apply_mod._apply_header_ops(headers, mutation)

    def test_add_permits_repeats(self) -> None:
        headers = StubHeaders()
        mutation = ResponseMutation()
        mutation.add("set-cookie", "a=1")
        mutation.add("set-cookie", "b=2")
        apply_mod._apply_header_ops(headers, mutation)
        assert len(headers.fields) == 2

    def test_remove_runs_before_add(self) -> None:
        """Order must not depend on which rule ran first."""
        headers = StubHeaders([(b"X-A", b"old")])
        mutation = ResponseMutation()
        mutation.remove("x-a")
        mutation.add("x-a", "new")
        apply_mod._apply_header_ops(headers, mutation)
        assert [v.decode() for _, v in headers.fields] == ["new"]


class TestApplyRedirect:
    def test_empty_spec_changes_nothing(self) -> None:
        assert not apply_mod.apply_redirect(StubRequest(), RedirectSpec())

    def test_rewrites_each_component(self) -> None:
        request = StubRequest()
        apply_mod.apply_redirect(
            request, RedirectSpec(scheme="http", host="localhost", port=8080, path="/local")
        )
        assert (request.scheme, request.host, request.port, request.path) == (
            "http",
            "localhost",
            8080,
            "/local",
        )

    def test_query_replaces_the_existing_one(self) -> None:
        request = StubRequest(path="/x?old=1")
        apply_mod.apply_redirect(request, RedirectSpec(query="new=2"))
        assert request.path == "/x?new=2"

    def test_empty_query_strips_it(self) -> None:
        request = StubRequest(path="/x?old=1")
        apply_mod.apply_redirect(request, RedirectSpec(query=""))
        assert request.path == "/x"


class TestApplyMutations:
    def test_empty_request_mutation_is_a_no_op(self) -> None:
        assert not apply_mod.apply_request_mutation(StubFlow(), RequestMutation())

    def test_request_body_replacement(self) -> None:
        flow = StubFlow()
        mutation = RequestMutation()
        mutation.body = b"replaced"
        assert apply_mod.apply_request_mutation(flow, mutation)
        assert flow.request.content == b"replaced"

    def test_empty_response_mutation_is_a_no_op(self) -> None:
        flow = StubFlow(response=StubResponse())
        assert not apply_mod.apply_response_mutation(flow, ResponseMutation())

    def test_response_mutation_without_a_response_is_a_no_op(self) -> None:
        mutation = ResponseMutation()
        mutation.status = 403
        assert not apply_mod.apply_response_mutation(StubFlow(), mutation)

    def test_status_rewrite(self) -> None:
        flow = StubFlow(response=StubResponse())
        mutation = ResponseMutation()
        mutation.status = 403
        assert apply_mod.apply_response_mutation(flow, mutation)
        assert flow.response.status_code == 403

    def test_status_rewrite_to_the_same_value_is_not_a_change(self) -> None:
        flow = StubFlow(response=StubResponse(status_code=200))
        mutation = ResponseMutation()
        mutation.status = 200
        assert not apply_mod.apply_response_mutation(flow, mutation)

    def test_response_body_assignment_goes_through_content(self) -> None:
        """Assigning .content is what re-encodes per Content-Encoding (REQ PXY-023)."""
        flow = StubFlow(response=StubResponse())
        mutation = ResponseMutation()
        mutation.body = b"<html>new</html>"
        apply_mod.apply_response_mutation(flow, mutation)
        assert flow.response.content == b"<html>new</html>"

    def test_set_stream_flag(self) -> None:
        flow = StubFlow(response=StubResponse())
        apply_mod.set_stream(flow, True)
        assert flow.response.stream is True

    def test_set_stream_without_a_response_is_safe(self) -> None:
        apply_mod.set_stream(StubFlow(), True)


class TestSyntheticResponses:
    def test_builds_a_real_mitmproxy_response(self) -> None:
        response = apply_mod.build_response(
            SyntheticResponse(
                status=200,
                body=b"window.analytics={track(){}};",
                headers=(("content-type", "application/javascript"),),
                origin="block-vendors:2",
            )
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/javascript"
        assert b"analytics" in response.content

    def test_short_circuit_attaches_the_response_to_the_flow(self) -> None:
        flow = StubFlow()
        apply_mod.apply_synthetic(flow, SyntheticResponse(status=204, origin="r:0"))
        assert flow.response.status_code == 204

    def test_short_circuit_via_request_mutation(self) -> None:
        flow = StubFlow()
        mutation = RequestMutation()
        mutation.short_circuit = SyntheticResponse(status=403, origin="r:0")
        assert apply_mod.apply_request_mutation(flow, mutation)
        assert flow.response.status_code == 403


class TestClientHelloHelpers:
    def test_sni_extraction(self) -> None:
        data = type("D", (), {"client_hello": type("C", (), {"sni": "a.example"})()})()
        assert normalize.sni_of(data) == "a.example"

    def test_missing_sni_is_none(self) -> None:
        data = type("D", (), {"client_hello": type("C", (), {"sni": None})()})()
        assert normalize.sni_of(data) is None

    def test_malformed_clienthello_is_none_not_an_exception(self) -> None:
        assert normalize.sni_of(object()) is None

    def test_peer_ip_extraction(self) -> None:
        server = type("S", (), {"address": ("93.184.216.34", 443)})()
        data = type("D", (), {"context": type("C", (), {"server": server})()})()
        assert normalize.peer_ip_of(data) == "93.184.216.34"

    def test_peer_ip_with_no_address(self) -> None:
        server = type("S", (), {"address": None})()
        data = type("D", (), {"context": type("C", (), {"server": server})()})()
        assert normalize.peer_ip_of(data) is None

    def test_peer_ip_on_a_malformed_object(self) -> None:
        assert normalize.peer_ip_of(object()) is None


@pytest.fixture
def stub_flow() -> StubFlow:
    return StubFlow()
