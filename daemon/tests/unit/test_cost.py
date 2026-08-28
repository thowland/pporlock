"""Work classification and offload. SPEC-1 §4.5, REQ PXY-024.

The control server and the addon share the proxy's single event loop, so what
runs inline and what goes to a worker thread is not a performance detail — it is
the difference between a slow transform and a stalled browser.
"""

from __future__ import annotations

import pytest

from pporlock.engine.cost import (
    DEFAULT_OFFLOAD_THRESHOLD_BYTES,
    TRANSFORM_COST,
    Cost,
    cost_of,
    decide_offload,
)


class TestClassification:
    def test_every_built_in_transform_is_classified(self) -> None:
        """SPEC-0 §5.5. An unclassified transform would default to expensive,
        which is safe but would silently give up the inline path."""
        for kind in (
            "strip_integrity_attributes",
            "strip_csp",
            "inject_script",
            "inject_style",
            "regex_sub",
            "replace_literal",
            "json_patch",
        ):
            assert kind in TRANSFORM_COST, kind

    @pytest.mark.parametrize(
        "kind",
        ["strip_integrity_attributes", "inject_script", "inject_style"],
    )
    def test_html_transforms_are_unconditionally_expensive(self, kind: str) -> None:
        """They parse a document rather than scanning bytes, so cost is driven
        by structure and a size threshold would not predict it."""
        assert cost_of(kind) is Cost.EXPENSIVE

    @pytest.mark.parametrize("kind", ["regex_sub", "replace_literal", "json_patch"])
    def test_scanning_transforms_are_size_dependent(self, kind: str) -> None:
        assert cost_of(kind) is Cost.SIZED

    def test_header_only_work_is_cheap(self) -> None:
        assert cost_of("strip_csp") is Cost.CHEAP

    def test_an_unknown_transform_is_assumed_expensive(self) -> None:
        """The safe direction: a module-provided transform we know nothing about
        must not be assumed fast on the proxy's event loop."""
        assert cost_of("some_module_transform") is Cost.EXPENSIVE


class TestOffloadDecision:
    def test_cheap_work_stays_inline(self) -> None:
        """Paying thread-pool handoff for a header edit would be slower than
        doing it."""
        decision = decide_offload("strip_csp", 10_000_000)
        assert not decision.offload
        assert decision.cost is Cost.CHEAP

    def test_expensive_work_always_offloads(self) -> None:
        decision = decide_offload("inject_script", 10)
        assert decision.offload
        assert "expensive" in decision.reason

    def test_a_small_body_keeps_sized_work_inline(self) -> None:
        assert not decide_offload("regex_sub", 1024).offload

    def test_a_large_body_pushes_sized_work_off_the_loop(self) -> None:
        assert decide_offload("regex_sub", DEFAULT_OFFLOAD_THRESHOLD_BYTES).offload

    def test_the_threshold_is_configurable(self) -> None:
        assert decide_offload("regex_sub", 100, threshold=50).offload
        assert not decide_offload("regex_sub", 100, threshold=1000).offload

    def test_the_reason_names_the_numbers(self) -> None:
        """A decision the operator cannot explain is one they cannot tune."""
        reason = decide_offload("regex_sub", 500_000, threshold=1000).reason
        assert "500000" in reason
        assert "1000" in reason

    def test_serializes_without_colliding_with_a_provenance_reason(self) -> None:
        # A provenance entry already carries `reason`; two different facts under
        # one key is how a detail block becomes misleading.
        payload = decide_offload("regex_sub", 10).to_dict()
        assert set(payload) == {"offload", "cost", "offload_reason"}
