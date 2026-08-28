"""Rule matching. SPEC-1 §4.2, SPEC-0 §5.3."""

from __future__ import annotations

import pytest

from pporlock.engine.matcher import compile_matcher
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.errors import RuleValidationError


def req(**kwargs: object) -> NormalizedRequest:
    base: dict[str, object] = {
        "flow_id": "f",
        "timestamp": "t",
        "scheme": "https",
        "method": "GET",
        "host": "cdn.example.com",
        "port": 443,
        "path": "/a/analytics.js",
        "url": "https://cdn.example.com/a/analytics.js",
        "dest": "script",
    }
    base.update(kwargs)
    return NormalizedRequest(**base)  # type: ignore[arg-type]


def resp(status: int = 200, content_type: str = "application/javascript") -> NormalizedResponse:
    return NormalizedResponse(
        flow_id="f",
        timestamp="t",
        status=status,
        headers=(("content-type", content_type),),
    )


class TestNoCriteria:
    def test_an_empty_match_matches_everything(self) -> None:
        matcher = compile_matcher({})
        assert matcher.matches_everything
        assert matcher.matches_request(req())

    def test_none_matches_everything(self) -> None:
        assert compile_matcher(None).matches_everything

    def test_a_constrained_matcher_is_not_marked_as_matching_everything(self) -> None:
        """Worth knowing explicitly: an accidentally empty match block is a rule
        that fires on every flow, and the UI should be able to say so."""
        assert not compile_matcher({"host": "a"}).matches_everything


class TestHost:
    @pytest.mark.parametrize(
        "pattern,host,expected",
        [
            ("*.example.com", "cdn.example.com", True),
            ("*.example.com", "example.com", False),
            ("cdn.example.com", "cdn.example.com", True),
            ("cdn.example.com", "other.example.com", False),
            ("*", "anything.test", True),
        ],
    )
    def test_glob(self, pattern: str, host: str, expected: bool) -> None:
        assert compile_matcher({"host": pattern}).matches_request(req(host=host)) is expected

    def test_is_case_insensitive(self) -> None:
        assert compile_matcher({"host": "*.EXAMPLE.com"}).matches_request(
            req(host="CDN.example.COM")
        )


class TestPath:
    def test_is_a_search_not_a_fullmatch(self) -> None:
        """Documented in SPEC-0 §5.3 — anchor explicitly when you mean it."""
        assert compile_matcher({"path": "analytics"}).matches_request(req())

    def test_anchoring_works(self) -> None:
        assert not compile_matcher({"path": "^/other"}).matches_request(req())
        assert compile_matcher({"path": "^/a/"}).matches_request(req())

    def test_an_invalid_regex_fails_at_load_not_at_request_time(self) -> None:
        with pytest.raises(RuleValidationError, match="invalid regular expression"):
            compile_matcher({"path": "[unclosed"}, module="m", index=3)

    def test_the_error_names_the_rule_and_field(self) -> None:
        with pytest.raises(RuleValidationError) as exc:
            compile_matcher({"path": "[unclosed"}, module="m", index=3)
        assert exc.value.module == "m"
        assert exc.value.rule_index == 3
        assert exc.value.field == "path"


class TestMethodAndDest:
    def test_single_method(self) -> None:
        assert compile_matcher({"method": "GET"}).matches_request(req())
        assert not compile_matcher({"method": "POST"}).matches_request(req())

    def test_method_list(self) -> None:
        assert compile_matcher({"method": ["POST", "GET"]}).matches_request(req())

    def test_method_is_case_insensitive(self) -> None:
        assert compile_matcher({"method": "get"}).matches_request(req())

    def test_dest(self) -> None:
        assert compile_matcher({"dest": "script"}).matches_request(req())
        assert not compile_matcher({"dest": "image"}).matches_request(req())

    def test_dest_list(self) -> None:
        assert compile_matcher({"dest": ["image", "script"]}).matches_request(req())

    def test_a_request_with_no_dest_never_matches_a_dest_criterion(self) -> None:
        """Sec-Fetch-Dest is absent on plenty of real requests."""
        assert not compile_matcher({"dest": "script"}).matches_request(req(dest=None))


class TestQueryAndHeaders:
    def test_query_value_regex(self) -> None:
        matcher = compile_matcher({"query": {"tid": "^UA-"}})
        assert matcher.matches_request(req(query=(("tid", "UA-123"),)))
        assert not matcher.matches_request(req(query=(("tid", "G-123"),)))

    def test_a_missing_query_parameter_does_not_match(self) -> None:
        assert not compile_matcher({"query": {"tid": ".*"}}).matches_request(req())

    def test_header_presence(self) -> None:
        matcher = compile_matcher({"request_headers": {"x-flag": None}})
        assert matcher.matches_request(req(headers=(("x-flag", "anything"),)))
        assert not matcher.matches_request(req())

    def test_header_value_regex(self) -> None:
        matcher = compile_matcher({"request_headers": {"referer": "^https://target\\."}})
        assert matcher.matches_request(req(headers=(("referer", "https://target.example/"),)))
        assert not matcher.matches_request(req(headers=(("referer", "https://other/"),)))

    def test_header_names_are_matched_case_insensitively(self) -> None:
        matcher = compile_matcher({"request_headers": {"X-Flag": None}})
        assert matcher.matches_request(req(headers=(("x-flag", "1"),)))


class TestResponseCriteria:
    def test_exact_status(self) -> None:
        matcher = compile_matcher({"status": 404})
        assert matcher.matches_response(req(), resp(404))
        assert not matcher.matches_response(req(), resp(200))

    def test_status_range(self) -> None:
        matcher = compile_matcher({"status": "300-399"})
        assert matcher.matches_response(req(), resp(301))
        assert not matcher.matches_response(req(), resp(200))

    def test_status_list(self) -> None:
        matcher = compile_matcher({"status": [404, "500-599"]})
        assert matcher.matches_response(req(), resp(404))
        assert matcher.matches_response(req(), resp(503))
        assert not matcher.matches_response(req(), resp(200))

    def test_content_type(self) -> None:
        matcher = compile_matcher({"content_type": "javascript"})
        assert matcher.matches_response(req(), resp())
        assert not matcher.matches_response(req(), resp(content_type="text/html"))

    def test_response_criteria_still_honour_request_criteria(self) -> None:
        matcher = compile_matcher({"host": "other.example", "status": 200})
        assert not matcher.matches_response(req(), resp())

    def test_is_response_side_is_reported(self) -> None:
        assert compile_matcher({"status": 200}).is_response_side
        assert compile_matcher({"content_type": "text/html"}).is_response_side
        assert not compile_matcher({"host": "a"}).is_response_side

    def test_an_invalid_status_fails_at_load(self) -> None:
        with pytest.raises(RuleValidationError, match="invalid status"):
            compile_matcher({"status": "not-a-status"})

    def test_an_invalid_status_range_fails_at_load(self) -> None:
        with pytest.raises(RuleValidationError, match="invalid status range"):
            compile_matcher({"status": "abc-def"})


class TestValidation:
    def test_unknown_criteria_are_rejected(self) -> None:
        """A typo must not become a rule that silently never fires."""
        with pytest.raises(RuleValidationError, match="unknown match criteria"):
            compile_matcher({"hostname": "a.example"})

    def test_a_response_criterion_on_a_request_action_is_a_load_time_error(self) -> None:
        """REQ MOD-011. The rule could never fire, and one that silently never
        fires is worse than one that fails loudly."""
        with pytest.raises(RuleValidationError, match="response side"):
            compile_matcher({"status": 200}, request_phase=True)

    def test_content_type_on_a_request_action_is_also_rejected(self) -> None:
        with pytest.raises(RuleValidationError, match="response side"):
            compile_matcher({"content_type": "text/html"}, request_phase=True)

    def test_request_criteria_are_fine_on_a_request_action(self) -> None:
        assert compile_matcher({"host": "a.example"}, request_phase=True)


class TestCombination:
    def test_all_present_criteria_must_match(self) -> None:
        matcher = compile_matcher({"host": "*.example.com", "method": "GET", "dest": "script"})
        assert matcher.matches_request(req())
        assert not matcher.matches_request(req(method="POST"))
        assert not matcher.matches_request(req(dest="image"))
        assert not matcher.matches_request(req(host="other.test"))
