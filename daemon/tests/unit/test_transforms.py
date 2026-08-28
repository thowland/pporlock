"""The transform registry and its transforms. SPEC-1 §4.6, SPEC-0 §5.5."""

from __future__ import annotations

import json

import pytest

from pporlock.engine.transforms import (
    TransformContext,
    TransformRegistry,
    TransformSpec,
    build_registry,
)
from pporlock.engine.transforms.headers import CSP_HEADERS, csp_headers_to_remove
from pporlock.errors import RuleValidationError, TransformError


@pytest.fixture
def registry() -> TransformRegistry:
    return build_registry()


def ctx(**kwargs: object) -> TransformContext:
    return TransformContext(**kwargs)  # type: ignore[arg-type]


CSP_PAGE = (
    '<html><head><script src="a.js" integrity="sha384-abc" crossorigin="anonymous">'
    "</script></head><body><p>hi</p></body></html>"
)


class TestRegistry:
    def test_registers_every_transform_the_spec_names(self, registry: TransformRegistry) -> None:
        for name in (
            "strip_integrity_attributes",
            "inject_script",
            "inject_style",
            "regex_sub",
            "replace_literal",
            "json_patch",
        ):
            assert registry.has(name), name

    def test_an_unknown_transform_fails_loudly_and_lists_what_exists(
        self, registry: TransformRegistry
    ) -> None:
        with pytest.raises(RuleValidationError, match="unknown transform"):
            registry.get("no_such_transform")

    def test_a_transform_with_no_kind_is_rejected(self, registry: TransformRegistry) -> None:
        with pytest.raises(RuleValidationError, match="requires a 'kind'"):
            registry.validate({})

    def test_missing_required_parameters_fail_at_load(self, registry: TransformRegistry) -> None:
        """REQ MOD-014 — never a runtime surprise."""
        with pytest.raises(RuleValidationError, match="requires"):
            registry.validate({"kind": "regex_sub", "pattern": "x"})

    def test_one_of_parameters_are_enforced(self, registry: TransformRegistry) -> None:
        with pytest.raises(RuleValidationError, match="one of"):
            registry.validate({"kind": "inject_script"})

    def test_either_alternative_satisfies_one_of(self, registry: TransformRegistry) -> None:
        registry.validate({"kind": "inject_script", "src": "/x.js"})
        registry.validate({"kind": "inject_script", "inline": "1"})

    def test_unknown_parameters_are_rejected(self, registry: TransformRegistry) -> None:
        """A typo must not become a parameter that is silently ignored."""
        with pytest.raises(RuleValidationError, match="does not take"):
            registry.validate({"kind": "regex_sub", "pattern": "a", "repl": "b", "flgs": "i"})

    def test_the_error_locates_the_rule(self, registry: TransformRegistry) -> None:
        with pytest.raises(RuleValidationError) as exc:
            registry.validate({"kind": "regex_sub"}, module="m", index=4)
        assert exc.value.module == "m"
        assert exc.value.rule_index == 4

    def test_a_registered_transform_can_be_replaced(self) -> None:
        """Modules extend the registry in Sprint 11."""
        from pporlock.engine.cost import Cost

        registry = TransformRegistry()
        registry.register(TransformSpec("custom", lambda text, _p: text.upper(), Cost.CHEAP))
        assert registry.apply({"kind": "custom"}, "abc", ctx()) == "ABC"

    def test_a_raising_transform_becomes_a_TransformError(self) -> None:
        from pporlock.engine.cost import Cost

        def explode(text: str, params: object) -> str:
            raise ValueError("boom")

        registry = TransformRegistry()
        registry.register(TransformSpec("explode", explode, Cost.CHEAP))
        with pytest.raises(TransformError, match="explode failed"):
            registry.apply({"kind": "explode"}, "x", ctx())


class TestStripIntegrity:
    """REQ PXY-040. The breakage is invisible from the proxy's side — a
    successful response the browser silently drops — so this is applied
    whenever a document is rewritten, not only when asked."""

    def test_removes_integrity_and_crossorigin(self, registry: TransformRegistry) -> None:
        out = registry.apply({"kind": "strip_integrity_attributes"}, CSP_PAGE, ctx())
        assert "integrity" not in out
        assert "crossorigin" not in out

    def test_leaves_the_rest_of_the_tag_intact(self, registry: TransformRegistry) -> None:
        out = registry.apply({"kind": "strip_integrity_attributes"}, CSP_PAGE, ctx())
        assert 'src="a.js"' in out
        assert "<p>hi</p>" in out

    def test_handles_single_quotes_and_bare_values(self, registry: TransformRegistry) -> None:
        html = "<script src='a.js' integrity='sha384-x' crossorigin=anonymous></script>"
        out = registry.apply({"kind": "strip_integrity_attributes"}, html, ctx())
        assert "integrity" not in out
        assert "crossorigin" not in out

    def test_strips_from_link_tags_too(self, registry: TransformRegistry) -> None:
        html = '<link rel="stylesheet" href="a.css" integrity="sha384-y">'
        out = registry.apply({"kind": "strip_integrity_attributes"}, html, ctx())
        assert "integrity" not in out

    def test_does_not_touch_other_tags(self, registry: TransformRegistry) -> None:
        html = '<div integrity="not-a-subresource"></div>'
        assert registry.apply({"kind": "strip_integrity_attributes"}, html, ctx()) == html

    def test_reports_what_it_removed(self, registry: TransformRegistry) -> None:
        context = ctx()
        registry.apply({"kind": "strip_integrity_attributes"}, CSP_PAGE, context)
        assert context.notes[0][0] == "sri_stripped"
        assert context.notes[0][2]["count"] == 2

    def test_says_nothing_when_there_was_nothing_to_do(self, registry: TransformRegistry) -> None:
        context = ctx()
        registry.apply({"kind": "strip_integrity_attributes"}, "<p>plain</p>", context)
        assert context.notes == []


class TestInjectScript:
    """REQ PXY-041 — reuse the page's nonce before relaxing its policy."""

    def test_reuses_an_existing_nonce(self, registry: TransformRegistry) -> None:
        context = ctx(headers=(("content-security-policy", "script-src 'nonce-abc123'"),))
        out = registry.apply({"kind": "inject_script", "inline": "x"}, CSP_PAGE, context)
        assert 'nonce="abc123"' in out

    def test_reports_that_it_reused_one(self, registry: TransformRegistry) -> None:
        context = ctx(headers=(("content-security-policy", "script-src 'nonce-abc'"),))
        registry.apply({"kind": "inject_script", "inline": "x"}, CSP_PAGE, context)
        assert context.notes[0][2]["nonce_reused"] is True

    def test_reads_the_nonce_from_report_only_too(self, registry: TransformRegistry) -> None:
        context = ctx(headers=(("content-security-policy-report-only", "script-src 'nonce-ro1'"),))
        out = registry.apply({"kind": "inject_script", "inline": "x"}, CSP_PAGE, context)
        assert 'nonce="ro1"' in out

    def test_injects_without_a_nonce_when_the_page_has_none(
        self, registry: TransformRegistry
    ) -> None:
        context = ctx(headers=(("content-security-policy", "script-src 'self'"),))
        out = registry.apply({"kind": "inject_script", "inline": "x"}, CSP_PAGE, context)
        assert "nonce=" not in out
        assert context.notes[0][2]["nonce_reused"] is False

    def test_reuse_can_be_turned_off(self, registry: TransformRegistry) -> None:
        context = ctx(headers=(("content-security-policy", "script-src 'nonce-abc'"),))
        out = registry.apply(
            {"kind": "inject_script", "inline": "x", "reuse_nonce": False}, CSP_PAGE, context
        )
        assert "nonce=" not in out

    def test_injects_a_src_tag(self, registry: TransformRegistry) -> None:
        out = registry.apply({"kind": "inject_script", "src": "/x.js"}, CSP_PAGE, ctx())
        assert '<script src="/x.js"></script>' in out

    def test_head_start_places_it_first(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "inject_script", "inline": "x", "position": "head_start"},
            CSP_PAGE,
            ctx(),
        )
        assert out.index("<script>x</script>") < out.index('src="a.js"')

    def test_head_end_places_it_last_in_head(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "inject_script", "inline": "x", "position": "head_end"}, CSP_PAGE, ctx()
        )
        assert out.index("<script>x</script>") > out.index('src="a.js"')
        assert out.index("<script>x</script>") < out.index("</head>")

    def test_body_end_places_it_before_the_closing_body(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "inject_script", "inline": "x", "position": "body_end"}, CSP_PAGE, ctx()
        )
        assert out.index("<script>x</script>") > out.index("<p>hi</p>")

    def test_a_fragment_with_no_structure_still_gets_the_injection(
        self, registry: TransformRegistry
    ) -> None:
        """Appending is the least surprising thing to do, and better than
        dropping the injection on the floor."""
        out = registry.apply({"kind": "inject_script", "inline": "x"}, "<p>frag</p>", ctx())
        assert "<script>x</script>" in out


class TestInjectStyle:
    def test_injects_a_stylesheet_link(self, registry: TransformRegistry) -> None:
        out = registry.apply({"kind": "inject_style", "href": "/a.css"}, CSP_PAGE, ctx())
        assert '<link rel="stylesheet" href="/a.css">' in out

    def test_injects_inline_css(self, registry: TransformRegistry) -> None:
        out = registry.apply({"kind": "inject_style", "inline": "p{color:red}"}, CSP_PAGE, ctx())
        assert "<style>p{color:red}</style>" in out


class TestTextTransforms:
    def test_regex_substitution(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "regex_sub", "pattern": r"track\(.*?\)", "repl": "void 0"},
            "analytics.track('x');",
            ctx(),
        )
        assert out == "analytics.void 0;"

    def test_regex_flags_by_name(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "regex_sub", "pattern": "HELLO", "repl": "bye", "flags": "i"},
            "hello world",
            ctx(),
        )
        assert out == "bye world"

    def test_an_unknown_flag_fails_rather_than_being_ignored(
        self, registry: TransformRegistry
    ) -> None:
        """Silently dropping a flag changes what a pattern matches."""
        with pytest.raises(TransformError, match="unknown regex flag"):
            registry.apply(
                {"kind": "regex_sub", "pattern": "a", "repl": "b", "flags": "z"}, "a", ctx()
            )

    def test_an_invalid_pattern_is_reported(self, registry: TransformRegistry) -> None:
        with pytest.raises(TransformError, match="invalid pattern"):
            registry.apply({"kind": "regex_sub", "pattern": "[", "repl": "b"}, "a", ctx())

    def test_count_limits_replacements(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "regex_sub", "pattern": "a", "repl": "b", "count": 1}, "aaa", ctx()
        )
        assert out == "baa"

    def test_literal_replacement_does_not_treat_input_as_a_pattern(
        self, registry: TransformRegistry
    ) -> None:
        """Escaping a literal into a pattern is exactly the step people get
        wrong, and getting it wrong silently changes what is matched."""
        out = registry.apply(
            {"kind": "replace_literal", "find": "a.b", "replace": "X"}, "a.b axb", ctx()
        )
        assert out == "X axb"

    def test_literal_count(self, registry: TransformRegistry) -> None:
        out = registry.apply(
            {"kind": "replace_literal", "find": "a", "replace": "b", "count": 2}, "aaa", ctx()
        )
        assert out == "bba"


class TestJsonPatch:
    def _apply(self, registry: TransformRegistry, ops: list[dict], body: str) -> dict:
        return json.loads(registry.apply({"kind": "json_patch", "ops": ops}, body, ctx()))

    def test_removes_a_field(self, registry: TransformRegistry) -> None:
        out = self._apply(
            registry, [{"op": "remove", "path": "/ads"}], '{"ads": [1], "keep": true}'
        )
        assert out == {"keep": True}

    def test_replaces_a_value(self, registry: TransformRegistry) -> None:
        out = self._apply(
            registry, [{"op": "replace", "path": "/on", "value": False}], '{"on": true}'
        )
        assert out == {"on": False}

    def test_adds_a_field(self, registry: TransformRegistry) -> None:
        out = self._apply(registry, [{"op": "add", "path": "/new", "value": 1}], "{}")
        assert out == {"new": 1}

    def test_descends_into_nested_objects(self, registry: TransformRegistry) -> None:
        out = self._apply(
            registry,
            [{"op": "remove", "path": "/a/b"}],
            '{"a": {"b": 1, "c": 2}}',
        )
        assert out == {"a": {"c": 2}}

    def test_a_path_that_does_not_exist_is_not_an_error(self, registry: TransformRegistry) -> None:
        """The rule describes a shape the body may or may not have."""
        out = self._apply(registry, [{"op": "remove", "path": "/nope"}], '{"a": 1}')
        assert out == {"a": 1}

    def test_a_non_json_body_is_left_alone_and_reported(self, registry: TransformRegistry) -> None:
        """A rule matching on path may legitimately meet a non-JSON response;
        failing the flow for that would be worse than doing nothing."""
        context = ctx()
        out = registry.apply(
            {"kind": "json_patch", "ops": [{"op": "remove", "path": "/a"}]},
            "<html>not json</html>",
            context,
        )
        assert out == "<html>not json</html>"
        assert context.notes[0][0] == "module_error"

    def test_an_unsupported_op_is_rejected(self, registry: TransformRegistry) -> None:
        with pytest.raises(TransformError, match="unsupported op"):
            registry.apply(
                {"kind": "json_patch", "ops": [{"op": "move", "path": "/a", "from": "/b"}]},
                "{}",
                ctx(),
            )

    def test_a_whole_document_path_is_rejected(self, registry: TransformRegistry) -> None:
        with pytest.raises(TransformError, match="address a member"):
            registry.apply(
                {"kind": "json_patch", "ops": [{"op": "remove", "path": ""}]}, "{}", ctx()
            )

    def test_a_malformed_pointer_is_rejected(self, registry: TransformRegistry) -> None:
        with pytest.raises(TransformError, match="must start with"):
            registry.apply(
                {"kind": "json_patch", "ops": [{"op": "remove", "path": "a/b"}]}, "{}", ctx()
            )

    def test_an_op_that_is_not_a_mapping_is_rejected(self, registry: TransformRegistry) -> None:
        with pytest.raises(TransformError, match="must be a mapping"):
            registry.apply({"kind": "json_patch", "ops": ["nope"]}, "{}", ctx())


class TestCspHeaders:
    def test_removes_both_headers_by_default(self) -> None:
        """Removing only the enforcing header leaves report-only in place,
        producing a page that works while flooding a report endpoint with
        violations the operator did not cause."""
        assert csp_headers_to_remove() == CSP_HEADERS

    def test_can_be_limited_to_the_enforcing_header(self) -> None:
        assert csp_headers_to_remove(report_only=False) == ("content-security-policy",)
