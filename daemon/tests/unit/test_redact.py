"""Redaction. SPEC-0 §9, SPEC-1 §6.4, REQ CAP-040/041/042/043/045."""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from pporlock.capture.records import FlowRecord
from pporlock.capture.redact import (
    MASK_PATTERN,
    FieldPathError,
    Redactor,
    is_masked,
    mask,
    resolve_field,
)
from pporlock.config import RedactionConfig
from pporlock.engine.models import NormalizedRequest, NormalizedResponse, WebSocketMessage

COOKIE = "session=9f3ac1de-4b77-11ef-bc1f-0242ac120002; theme=dark"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.super-secret"


def request_with(**kwargs: object) -> NormalizedRequest:
    defaults: dict[str, object] = {
        "flow_id": "f1",
        "timestamp": "2026-08-27T14:00:00.000Z",
        "scheme": "https",
        "method": "GET",
        "host": "api.example.com",
        "port": 443,
        "path": "/v1/me",
        "url": "https://api.example.com/v1/me",
    }
    defaults.update(kwargs)
    return NormalizedRequest(**defaults)  # type: ignore[arg-type]


class TestMaskFormat:
    def test_matches_the_published_format(self) -> None:
        """REQ CAP-042. Three clients parse this; the format is the contract."""
        assert MASK_PATTERN.match(mask("hunter2"))

    def test_carries_the_sha1_prefix_and_byte_length(self) -> None:
        digest = hashlib.sha1(b"hunter2", usedforsecurity=False).hexdigest()[:4]
        assert mask("hunter2") == f"«redacted:sha1={digest},len=7»"

    def test_length_is_bytes_not_characters(self) -> None:
        """A multi-byte value would otherwise report a length that does not
        match what crossed the wire."""
        assert mask("é") == mask("é")
        assert ",len=2»" in mask("é")

    def test_equal_values_mask_equally(self) -> None:
        """REQ CAP-042 — the user must be able to tell whether two requests
        carried the same token without seeing either."""
        assert mask(COOKIE) == mask(COOKIE)

    def test_different_values_differ(self) -> None:
        assert mask(COOKIE) != mask(BEARER)

    def test_never_contains_the_original(self) -> None:
        assert "9f3ac1de" not in mask(COOKIE)

    def test_is_masked_recognises_its_own_output(self) -> None:
        assert is_masked(mask(COOKIE))
        assert not is_masked(COOKIE)


class TestHeaderRedaction:
    @pytest.mark.parametrize(
        "name",
        [
            "cookie",
            "Cookie",
            "COOKIE",
            "set-cookie",
            "authorization",
            "Authorization",
            "proxy-authorization",
            "x-api-key",
            "x-auth-token",
        ],
    )
    def test_default_list_covers_the_named_headers(self, name: str) -> None:
        """REQ CAP-041 names these explicitly."""
        headers, changed = Redactor().redact_headers(((name, COOKIE),))
        assert changed
        assert is_masked(headers[0][1])

    def test_leaves_ordinary_headers_alone(self) -> None:
        headers, changed = Redactor().redact_headers((("accept", "*/*"),))
        assert not changed
        assert headers == (("accept", "*/*"),)

    def test_repeated_headers_are_all_masked(self) -> None:
        """Set-Cookie repeats; masking only the first would leak the rest."""
        headers, _ = Redactor().redact_headers((("set-cookie", "a=1"), ("set-cookie", "b=2")))
        assert all(is_masked(v) for _, v in headers)

    def test_a_configured_glob_pattern_matches(self) -> None:
        """REQ CAP-044 — the list is user-configurable."""
        cfg = RedactionConfig(header_patterns=("x-*-token",))
        headers, changed = Redactor(cfg).redact_headers((("X-Custom-Token", "abc"),))
        assert changed and is_masked(headers[0][1])

    def test_already_masked_values_are_not_masked_twice(self) -> None:
        once, _ = Redactor().redact_headers((("cookie", COOKIE),))
        twice, changed = Redactor().redact_headers(once)
        assert not changed
        assert once == twice

    def test_disabling_redaction_is_honoured(self) -> None:
        cfg = RedactionConfig(enabled=False)
        headers, changed = Redactor(cfg).redact_headers((("cookie", COOKIE),))
        assert not changed and headers[0][1] == COOKIE


class TestJsonBodyRedaction:
    def test_masks_matching_keys(self) -> None:
        body = json.dumps({"user": "tim", "password": "hunter2"}).encode()
        out, changed = Redactor().redact_json_body(body)
        assert changed
        parsed = json.loads(out or b"{}")
        assert parsed["user"] == "tim"
        assert is_masked(parsed["password"])

    def test_matches_as_a_substring_case_insensitively(self) -> None:
        """SPEC-0 §9.2. ``authToken`` is how half the world spells it."""
        body = json.dumps({"authToken": "abc", "X-Secret": "def"}).encode()
        parsed = json.loads(Redactor().redact_json_body(body)[0] or b"{}")
        assert is_masked(parsed["authToken"]) and is_masked(parsed["X-Secret"])

    def test_descends_into_nested_objects(self) -> None:
        body = json.dumps({"data": {"credentials": {"secret": "s"}}}).encode()
        parsed = json.loads(Redactor().redact_json_body(body)[0] or b"{}")
        assert is_masked(parsed["data"]["credentials"]["secret"])

    def test_masks_every_element_of_a_matching_list(self) -> None:
        body = json.dumps({"tokens": ["a", "b"]}).encode()
        parsed = json.loads(Redactor().redact_json_body(body)[0] or b"{}")
        assert all(is_masked(v) for v in parsed["tokens"])

    def test_masks_non_string_values(self) -> None:
        """A numeric or boolean secret is still a secret."""
        body = json.dumps({"password": 12345}).encode()
        parsed = json.loads(Redactor().redact_json_body(body)[0] or b"{}")
        assert is_masked(parsed["password"])

    def test_a_non_json_body_is_returned_untouched(self) -> None:
        body = b"<html><body>hello</body></html>"
        out, changed = Redactor().redact_json_body(body)
        assert out == body and not changed

    def test_an_empty_body_is_untouched(self) -> None:
        assert Redactor().redact_json_body(b"") == (b"", False)
        assert Redactor().redact_json_body(None) == (None, False)


class TestRecordRedaction:
    def test_does_not_mutate_the_original(self) -> None:
        """The live ring buffer must keep the raw value or unmasking has
        nothing to reveal (REQ CAP-043)."""
        request = request_with(headers=(("cookie", COOKIE),))
        record = FlowRecord(flow_id="f1", kind="http", started_at="t", request=request)
        redacted = Redactor().redact_record(record)

        assert record.request is not None and record.request.header("cookie") == COOKIE
        assert redacted.request is not None
        assert is_masked(redacted.request.header("cookie") or "")

    def test_redacts_the_response_too(self) -> None:
        response = NormalizedResponse(
            flow_id="f1", timestamp="t", status=200, headers=(("set-cookie", "a=1"),)
        )
        record = FlowRecord(flow_id="f1", kind="http", started_at="t", response=response)
        out = Redactor().redact_record(record)
        assert out.response is not None
        assert is_masked(out.response.header("set-cookie") or "")

    def test_redacts_websocket_payloads(self) -> None:
        """REQ PXY-050 — payload is subject to redaction."""
        message = WebSocketMessage(
            flow_id="f1",
            index=0,
            timestamp="t",
            direction="outbound",
            opcode="text",
            payload=json.dumps({"access_token": "abc"}).encode(),
        )
        record = FlowRecord(flow_id="f1", kind="websocket", started_at="t", ws_messages=[message])
        out = Redactor().redact_record(record)
        assert is_masked(json.loads(out.ws_messages[0].payload)["access_token"])

    def test_disabled_returns_the_record_unchanged(self) -> None:
        record = FlowRecord(flow_id="f1", kind="http", started_at="t")
        redactor = Redactor(RedactionConfig(enabled=False))
        assert redactor.redact_record(record) is record


class TestFieldPathResolution:
    """REQ CAP-043 — one value per call, named explicitly."""

    def record(self) -> FlowRecord:
        request = request_with(
            headers=(("cookie", COOKIE), ("authorization", BEARER)),
            body=json.dumps({"auth": {"token": "tok"}, "items": [{"secret": "s0"}]}).encode(),
        )
        response = NormalizedResponse(
            flow_id="f1",
            timestamp="t",
            status=200,
            headers=(("set-cookie", "a=1"), ("set-cookie", "b=2")),
        )
        message = WebSocketMessage(
            flow_id="f1",
            index=0,
            timestamp="t",
            direction="inbound",
            opcode="text",
            payload=b"frame-0",
        )
        return FlowRecord(
            flow_id="f1",
            kind="http",
            started_at="t",
            request=request,
            response=response,
            ws_messages=[message],
        )

    def test_a_request_header(self) -> None:
        assert resolve_field(self.record(), "request.headers.cookie") == COOKIE

    def test_header_lookup_is_case_insensitive(self) -> None:
        assert resolve_field(self.record(), "request.headers.Authorization") == BEARER

    def test_a_specific_header_occurrence(self) -> None:
        assert resolve_field(self.record(), "response.headers.set-cookie.1") == "b=2"

    def test_a_json_body_field(self) -> None:
        assert resolve_field(self.record(), "request.body.auth.token") == "tok"

    def test_a_numeric_segment_indexes_a_list(self) -> None:
        assert resolve_field(self.record(), "request.body.items.0.secret") == "s0"

    def test_a_websocket_payload(self) -> None:
        assert resolve_field(self.record(), "websocket.messages.0.payload") == "frame-0"

    @pytest.mark.parametrize(
        "path",
        [
            "cookie",
            "request.headers.x-missing",
            "request.headers.set-cookie",
            "response.body.anything",
            "request.body.nope",
            "websocket.messages.9.payload",
            "elsewhere.headers.cookie",
        ],
    )
    def test_anything_else_is_refused(self, path: str) -> None:
        with pytest.raises(FieldPathError):
            resolve_field(self.record(), path)

    def test_there_is_no_wildcard_form(self) -> None:
        """No bulk reveal: the affordance is one value on one explicit action."""
        with pytest.raises(FieldPathError):
            resolve_field(self.record(), "request.headers.*")


def test_no_masked_value_leaks_the_original_anywhere() -> None:
    """A blunt end-to-end check on the format itself."""
    secret = "correct-horse-battery-staple"
    body = json.dumps({"password": secret}).encode()
    out, _ = Redactor().redact_json_body(body)
    assert out is not None
    assert secret not in out.decode()
    assert re.search(r"«redacted:sha1=[0-9a-f]{4},len=28»", out.decode())


class TestQueryStringRedaction:
    """A credential in a URL is still a credential — REQ CAP-045, §2.5 (A02).

    Until Sprint 16 the same bearer token was masked in the ``Authorization``
    header and written verbatim into ``request.query`` and ``request.url``. A
    session file therefore contained the unredacted secret, which is exactly
    what CAP-045 says must never happen, and the flow table displayed it.
    """

    def _request(self, url: str, query: tuple[tuple[str, str], ...]) -> NormalizedRequest:
        return NormalizedRequest(
            flow_id="f",
            timestamp="2026-08-27T14:00:00.000Z",
            scheme="https",
            method="GET",
            host="api.example.com",
            port=443,
            path="/v1/me",
            url=url,
            query=query,
        )

    def test_a_token_in_the_query_is_masked(self) -> None:
        redactor = Redactor()
        pairs, changed = redactor.redact_query((("access_token", "SUPERSECRET"),))
        assert changed is True
        assert is_masked(pairs[0][1])

    def test_an_ordinary_parameter_is_left_alone(self) -> None:
        redactor = Redactor()
        pairs, changed = redactor.redact_query((("page", "2"), ("q", "kittens")))
        assert changed is False
        assert pairs == (("page", "2"), ("q", "kittens"))

    def test_matching_is_by_substring_like_json_keys(self) -> None:
        """The names that carry secrets are spelled every way a hundred APIs
        could spell them: authToken, x-secret, user_password."""
        redactor = Redactor()
        pairs, _ = redactor.redact_query((("oauth_access_token", "x"), ("userPassword", "y")))
        assert all(is_masked(value) for _, value in pairs)

    def test_presigned_url_parameters_are_covered(self) -> None:
        redactor = Redactor()
        pairs, _ = redactor.redact_query(
            (("X-Amz-Signature", "deadbeef"), ("X-Amz-Security-Token", "tok"))
        )
        assert all(is_masked(value) for _, value in pairs)

    def test_the_url_is_rewritten_to_match_the_masked_query(self) -> None:
        """The URL is a second copy of the same secret, and it is the copy the
        flow table shows and the session file stores."""
        redactor = Redactor()
        request = self._request(
            "https://api.example.com/v1/me?access_token=SUPERSECRET&page=2",
            (("access_token", "SUPERSECRET"), ("page", "2")),
        )
        out, changed = redactor.redact_request(request)
        assert changed is True
        assert "SUPERSECRET" not in out.url
        assert "page=2" in out.url

    def test_the_mask_stays_readable_in_the_url(self) -> None:
        """Percent-encoding the mask makes it look like data rather than an
        absence, which defeats the point of the mask format (SPEC-0 §9.1)."""
        redactor = Redactor()
        out, _ = redactor.redact_request(
            self._request("https://api.example.com/v1/me?token=SECRET", (("token", "SECRET"),))
        )
        assert "«redacted:" in out.url

    def test_the_fragment_survives(self) -> None:
        redactor = Redactor()
        out, _ = redactor.redact_request(
            self._request(
                "https://api.example.com/v1/me?token=SECRET#section",
                (("token", "SECRET"),),
            )
        )
        assert out.url.endswith("#section")

    def test_a_url_with_no_query_is_untouched(self) -> None:
        """Appending a '?' would invent a query the request never carried."""
        redactor = Redactor()
        request = self._request("https://api.example.com/v1/me", ())
        out, changed = redactor.redact_request(request)
        assert changed is False
        assert out.url == "https://api.example.com/v1/me"

    def test_an_already_masked_value_is_not_masked_twice(self) -> None:
        redactor = Redactor()
        first, _ = redactor.redact_query((("token", "SECRET"),))
        second, changed = redactor.redact_query(first)
        assert changed is False
        assert second == first

    def test_disabling_redaction_disables_this_too(self) -> None:
        redactor = Redactor(RedactionConfig(enabled=False))
        pairs, changed = redactor.redact_query((("access_token", "SECRET"),))
        assert changed is False
        assert pairs[0][1] == "SECRET"

    def test_a_whole_record_carries_no_query_secret(self) -> None:
        """The end-to-end guarantee: what reaches the session writer is clean."""
        redactor = Redactor()
        record = FlowRecord(
            flow_id="f",
            kind="http",
            started_at="2026-08-27T14:00:00.000Z",
            request=self._request(
                "https://api.example.com/v1/me?access_token=SUPERSECRET",
                (("access_token", "SUPERSECRET"),),
            ),
        )
        redacted = redactor.redact_record(record)
        assert redacted.request is not None
        assert "SUPERSECRET" not in redacted.request.url
        assert "SUPERSECRET" not in str(redacted.request.query)
        # And the original is untouched, so unmasking from the ring still works.
        assert record.request is not None
        assert "SUPERSECRET" in record.request.url
