"""Response-shaping guardrails — REQ MCP-004, MCP-005."""

from __future__ import annotations

import pytest

from conftest import MASKED_COOKIE, flow
from pporlock_mcp.errors import ContractViolation
from pporlock_mcp.guardrails import (
    MASK_RE,
    clamp,
    coerce_detail,
    has_provenance,
    require_provenance,
    summarize_notes,
    truncate_files,
    truncate_text,
)


def test_masked_value_format_matches_the_contract() -> None:
    """SPEC-0 §9.1 — the format is fixed so all clients render it the same way."""
    assert MASK_RE.fullmatch(MASKED_COOKIE)


def test_flow_page_without_provenance_is_a_contract_violation() -> None:
    """REQ MCP-004 — a flow whose modifications cannot be explained is not served."""
    page = {"flows": [flow("f1", provenance={})]}
    with pytest.raises(ContractViolation) as excinfo:
        require_provenance(page, where="list_flows")
    assert "f1" in str(excinfo.value)


def test_single_flow_without_provenance_is_a_contract_violation() -> None:
    with pytest.raises(ContractViolation):
        require_provenance(flow("f9", provenance={}), where="get_flow")


def test_provenance_bearing_payloads_pass_through_unchanged() -> None:
    page = {"flows": [flow("f1"), flow("f2")]}
    assert require_provenance(page, where="list_flows") is page


def test_non_flow_payloads_are_left_alone() -> None:
    payload = {"modules": []}
    assert require_provenance(payload, where="x") is payload


def test_has_provenance_rejects_a_non_dict() -> None:
    assert has_provenance({"provenance": "yes"}) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "summary"), ("bodies", "bodies"), ("FULL", "full"), ("nonsense", "summary")],
)
def test_detail_is_clamped_to_the_three_known_levels(value: object, expected: str) -> None:
    """SPEC-0 §6.3 — an unknown level falls back to the cheap default, not an error."""
    assert coerce_detail(value, "summary") == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 50), (10, 10), (0, 50), (-5, 50), (9999, 200), ("bad", 50), ("25", 25)],
)
def test_page_size_is_bounded(value: object, expected: int) -> None:
    """REQ MCP-005 — an agent cannot ask for the whole ring buffer in one call."""
    assert clamp(value, 50, 200) == expected


def test_truncate_text_marks_the_cut() -> None:
    body, cut = truncate_text("x" * 100, 10)
    assert body == "x" * 10
    assert cut is True
    assert truncate_text("short", 10) == ("short", False)


def test_truncate_files_reports_what_was_hidden() -> None:
    """REQ MCP-005 — truncation the agent cannot see is worse than truncation."""
    out = truncate_files({"module.py": "y" * 50, "module.yaml": "small"}, 20)
    assert out["module.py"] == "y" * 20
    assert out["module.py__truncated"]["total_chars"] == 50
    assert "module.yaml__truncated" not in out


def test_truncate_files_passes_non_string_values_through() -> None:
    assert truncate_files({"assets": ["a.png"]}, 10)["assets"] == ["a.png"]


def test_note_histogram_counts_silent_breakage_codes() -> None:
    """SPEC-0 §4.4 — the note histogram is the cheap 'what broke' signal."""
    flows = [
        flow("f1", provenance={"notes": [{"code": "csp_modified"}, {"code": "sri_stripped"}]}),
        flow("f2", provenance={"notes": [{"code": "csp_modified"}]}),
        flow("f3", provenance={"notes": []}),
        flow("f4", provenance={"notes": "not a list"}),
        {"provenance": None},
    ]
    assert summarize_notes(flows) == {"csp_modified": 2, "sri_stripped": 1}
