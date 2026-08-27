"""Normalized flow model. SPEC-0 §3."""

from __future__ import annotations

import dataclasses

import pytest

from pporlock.engine.models import (
    HeaderMutation,
    NormalizedRequest,
    NormalizedResponse,
    RedirectSpec,
    RequestMutation,
    ResponseMutation,
    SyntheticResponse,
    WebSocketMessage,
)


def make_request(**kwargs: object) -> NormalizedRequest:
    base: dict[str, object] = {
        "flow_id": "01JB2K7Q9X4M8Z0V3T5R7W1Y2A",
        "timestamp": "2026-08-27T14:03:22.417Z",
        "scheme": "https",
        "method": "GET",
        "host": "cdn.example.com",
        "port": 443,
        "path": "/a/analytics.js",
        "url": "https://cdn.example.com/a/analytics.js?v=3",
    }
    base.update(kwargs)
    return NormalizedRequest(**base)  # type: ignore[arg-type]


def make_response(**kwargs: object) -> NormalizedResponse:
    base: dict[str, object] = {
        "flow_id": "01JB2K7Q9X4M8Z0V3T5R7W1Y2A",
        "timestamp": "2026-08-27T14:03:22.694Z",
        "status": 200,
    }
    base.update(kwargs)
    return NormalizedResponse(**base)  # type: ignore[arg-type]


class TestHeaderAccess:
    def test_header_lookup_is_case_insensitive(self) -> None:
        req = make_request(headers=(("content-type", "text/html"),))
        assert req.header("Content-Type") == "text/html"
        assert req.header("CONTENT-TYPE") == "text/html"

    def test_missing_header_is_none_not_an_error(self) -> None:
        assert make_request().header("x-nope") is None

    def test_headers_all_returns_every_value(self) -> None:
        """Headers repeat; this is why the model keeps pairs, not a map (SPEC-0 §2)."""
        req = make_request(headers=(("set-cookie", "a=1"), ("set-cookie", "b=2")))
        assert req.headers_all("set-cookie") == ["a=1", "b=2"]

    def test_headers_all_is_empty_when_absent(self) -> None:
        assert make_request().headers_all("set-cookie") == []

    def test_has_header(self) -> None:
        req = make_request(headers=(("accept", "*/*"),))
        assert req.has_header("Accept")
        assert not req.has_header("accept-encoding")

    def test_content_type_strips_parameters(self) -> None:
        req = make_request(headers=(("content-type", "text/html; charset=utf-8"),))
        assert req.content_type == "text/html"

    def test_content_type_lowercases(self) -> None:
        req = make_request(headers=(("content-type", "TEXT/HTML"),))
        assert req.content_type == "text/html"

    def test_content_type_absent_is_none(self) -> None:
        assert make_request().content_type is None

    def test_empty_content_type_is_none(self) -> None:
        req = make_request(headers=(("content-type", "  "),))
        assert req.content_type is None


class TestNormalizedRequest:
    def test_is_frozen(self) -> None:
        """Rule code proposes mutations; it never edits the request in place."""
        req = make_request()
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.host = "evil.example"  # type: ignore[misc]

    def test_query_param(self) -> None:
        req = make_request(query=(("v", "3"), ("t", "x")))
        assert req.query_param("v") == "3"
        assert req.query_param("missing") is None

    def test_query_params_all_preserves_repeats(self) -> None:
        req = make_request(query=(("id", "1"), ("id", "2")))
        assert req.query_params_all("id") == ["1", "2"]

    def test_body_size_without_body(self) -> None:
        assert make_request().body_size == 0

    def test_body_size_with_body(self) -> None:
        assert make_request(body=b"abcd").body_size == 4

    def test_dest_defaults_to_none(self) -> None:
        """Sec-Fetch-Dest is absent on plenty of real requests (SPEC-0 §3.1)."""
        assert make_request().dest is None

    def test_tab_id_defaults_to_none(self) -> None:
        """Attribution backfills later and may never arrive (SPEC-0 §3.6)."""
        assert make_request().tab_id is None


class TestNormalizedResponse:
    def test_text_decodes_utf8_by_default(self) -> None:
        assert make_response(body=b"hello").text == "hello"

    def test_text_honours_the_declared_charset(self) -> None:
        resp = make_response(
            headers=(("content-type", "text/plain; charset=latin-1"),),
            body="café".encode("latin-1"),
        )
        assert resp.text == "café"

    def test_text_is_none_when_undecodable_rather_than_raising(self) -> None:
        """A transform that cannot read a body records no_change; it does not break the flow."""
        assert make_response(body=b"\xff\xfe\x00\x80").text is None

    def test_text_is_none_for_an_unknown_charset(self) -> None:
        resp = make_response(
            headers=(("content-type", "text/plain; charset=nonsuch-9000"),), body=b"x"
        )
        assert resp.text is None

    def test_text_is_none_without_a_body(self) -> None:
        assert make_response().text is None

    def test_empty_charset_falls_back_to_utf8(self) -> None:
        resp = make_response(headers=(("content-type", "text/plain; charset="),), body=b"ok")
        assert resp.text == "ok"

    def test_streamed_response_carries_no_body(self) -> None:
        """The buffering guard declined, so transforms are unavailable (REQ PXY-022)."""
        resp = make_response(streamed=True)
        assert resp.streamed
        assert resp.body is None
        assert resp.body_size == 0


class TestWebSocketMessage:
    def test_size_derives_from_payload(self) -> None:
        msg = WebSocketMessage(
            flow_id="f",
            index=0,
            timestamp="t",
            direction="outbound",
            opcode="text",
            payload=b"hello",
        )
        assert msg.size == 5

    def test_is_frozen_because_v1_is_inspection_only(self) -> None:
        """REQ PXY-051."""
        msg = WebSocketMessage(
            flow_id="f",
            index=0,
            timestamp="t",
            direction="inbound",
            opcode="binary",
            payload=b"",
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.payload = b"tampered"  # type: ignore[misc]


class TestRedirectSpec:
    def test_empty_spec_is_detected(self) -> None:
        assert RedirectSpec().is_empty()

    @pytest.mark.parametrize(
        "kwargs",
        [{"scheme": "http"}, {"host": "a"}, {"port": 8080}, {"path": "/x"}, {"query": "a=1"}],
    )
    def test_any_component_makes_it_non_empty(self, kwargs: dict[str, object]) -> None:
        assert not RedirectSpec(**kwargs).is_empty()  # type: ignore[arg-type]


class TestSyntheticResponse:
    def test_content_type_is_read_from_headers(self) -> None:
        synth = SyntheticResponse(
            status=200, headers=(("Content-Type", "application/javascript; charset=utf-8"),)
        )
        assert synth.content_type == "application/javascript"

    def test_content_type_absent(self) -> None:
        assert SyntheticResponse(status=204).content_type is None

    def test_origin_attributes_the_response(self) -> None:
        """A synthesized response must always be traceable to what made it."""
        synth = SyntheticResponse(status=200, origin="block-vendors:2")
        assert synth.origin == "block-vendors:2"


class TestMutations:
    def test_fresh_header_mutation_is_empty(self) -> None:
        assert HeaderMutation().is_empty()

    def test_remove_lowercases_and_deduplicates(self) -> None:
        mut = HeaderMutation()
        mut.remove("Content-Security-Policy")
        mut.remove("content-security-policy")
        assert mut.remove_headers == ["content-security-policy"]

    def test_set_lowercases(self) -> None:
        mut = HeaderMutation()
        mut.set("X-Frame-Options", "DENY")
        assert mut.set_headers == {"x-frame-options": "DENY"}

    def test_add_appends_and_allows_repeats(self) -> None:
        mut = HeaderMutation()
        mut.add("Set-Cookie", "a=1")
        mut.add("set-cookie", "b=2")
        assert mut.add_headers == [("set-cookie", "a=1"), ("set-cookie", "b=2")]

    def test_request_mutation_empty_then_not(self) -> None:
        mut = RequestMutation()
        assert mut.is_empty()
        mut.redirect = RedirectSpec(host="localhost")
        assert not mut.is_empty()

    def test_request_mutation_short_circuit_makes_it_non_empty(self) -> None:
        mut = RequestMutation()
        mut.short_circuit = SyntheticResponse(status=204)
        assert not mut.is_empty()

    def test_request_mutation_body_makes_it_non_empty(self) -> None:
        mut = RequestMutation()
        mut.body = b"x"
        assert not mut.is_empty()

    def test_request_mutation_inherits_header_emptiness(self) -> None:
        mut = RequestMutation()
        mut.remove("cookie")
        assert not mut.is_empty()

    def test_response_mutation_status_makes_it_non_empty(self) -> None:
        mut = ResponseMutation()
        assert mut.is_empty()
        mut.status = 403
        assert not mut.is_empty()

    def test_response_mutation_body_makes_it_non_empty(self) -> None:
        mut = ResponseMutation()
        mut.body = b"<html></html>"
        assert not mut.is_empty()

    def test_response_mutation_inherits_header_emptiness(self) -> None:
        mut = ResponseMutation()
        mut.set("content-type", "text/plain")
        assert not mut.is_empty()
