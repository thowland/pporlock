"""The golden corpus — REQ TST-004, SPEC-1 §13.

A corpus of recorded flows, each stored next to the rules that ran on it and the
provenance the engine produced. The point is not to prove any one rule works —
the unit tests do that — but to make a *behaviour change* visible: an edit to the
evaluator, the matcher, a transform or the stub library that alters what a flow
produces shows up here as a readable diff on a named field of a named provenance
entry, rather than as a surprise three sprints later.

Everything runs through the real ``Evaluator`` and the real ``DryRunner``. There
is no second implementation of anything here; if there were, the corpus would be
testing the corpus.

Regenerating: see ``tests/corpus/README.md``. ``PPORLOCK_REGEN_CORPUS=1`` rewrites
the goldens in place. That is how a deliberate behaviour change is *accepted*,
and it is never the way to make a red test green.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from pporlock.capture.dryrun import DryRunner, DryRunRequest
from pporlock.capture.records import FlowRecord
from pporlock.engine.evaluator import Evaluator, TimeBudget
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.provenance import ProvenanceBuilder
from pporlock.engine.ruleset import RuleSet

CORPUS_DIR = Path(__file__).resolve().parents[1] / "corpus"
CASES_DIR = CORPUS_DIR / "cases"

REGEN = os.environ.get("PPORLOCK_REGEN_CORPUS") == "1"

#: Fields whose value is a wall-clock measurement and therefore differs on every
#: run. They are normalised to a sentinel before comparison rather than dropped,
#: so a golden that *stops* carrying a timing field is still a visible diff.
#: (``total_ms``/``duration_ms`` are perf_counter deltas; ``avg_ms``/``p95_ms``
#: are aggregates of them; ``_duration_ms`` is the dry runner's per-flow copy.)
TIMING_FIELDS = frozenset({"duration_ms", "total_ms", "avg_ms", "p95_ms", "_duration_ms"})

TIMING_SENTINEL = "<timing>"


# --------------------------------------------------------------- comparison --


def normalise(value: Any) -> Any:
    """Replace every timing measurement with a sentinel, recursively."""
    if isinstance(value, dict):
        return {
            key: TIMING_SENTINEL if key in TIMING_FIELDS else normalise(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


def first_difference(actual: Any, expected: Any, path: str = "$") -> str | None:
    """The path and values of the first place two structures differ.

    A bare ``assert actual == expected`` on a nested provenance dict prints two
    walls of JSON and leaves the reader to find the one changed field. This
    reports the field.
    """
    if type(actual) is not type(expected) and not (
        isinstance(actual, int | float) and isinstance(expected, int | float)
    ):
        return f"{path}: type {type(actual).__name__} != {type(expected).__name__}"

    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        for key in expected:
            if key not in actual:
                return f"{path}.{key}: missing from actual (expected {expected[key]!r})"
        for key in actual:
            if key not in expected:
                return f"{path}.{key}: unexpected in actual ({actual[key]!r})"
        for key, sub in expected.items():
            found = first_difference(actual[key], sub, f"{path}.{key}")
            if found is not None:
                return found
        return None

    if isinstance(expected, list):
        assert isinstance(actual, list)
        for index in range(min(len(actual), len(expected))):
            found = first_difference(actual[index], expected[index], f"{path}[{index}]")
            if found is not None:
                return found
        if len(actual) != len(expected):
            longer, which = (
                (actual, "actual") if len(actual) > len(expected) else (expected, "expected")
            )
            extra = longer[min(len(actual), len(expected))]
            return (
                f"{path}: {len(actual)} entries, expected {len(expected)}; "
                f"first extra ({which}): {json.dumps(extra, default=str)[:400]}"
            )
        return None

    if actual != expected:
        return f"{path}: {actual!r} != {expected!r}"
    return None


def assert_matches_golden(case: dict[str, Any], path: Path, key: str, actual: Any) -> None:
    """Compare against the stored golden, or rewrite it when regenerating."""
    cleaned = normalise(actual)
    if REGEN:
        case[key] = cleaned
        path.write_text(json.dumps(case, indent=2, sort_keys=False) + "\n")
        return

    expected = normalise(case.get(key))
    difference = first_difference(cleaned, expected)
    if difference is None:
        return
    pytest.fail(
        f"corpus case {case['id']!r} ({path.name}) no longer matches its golden.\n"
        f"  first difference: {difference}\n"
        f"  case: {case.get('description', '')}\n"
        f"If this change is intended, read the diff, then accept it with "
        f"PPORLOCK_REGEN_CORPUS=1 (see tests/corpus/README.md).",
        pytrace=False,
    )


# ------------------------------------------------------------ construction --


def _pairs(raw: Any) -> tuple[tuple[str, str], ...]:
    return tuple((str(k), str(v)) for k, v in (raw or []))


def _body(raw: Any) -> bytes | None:
    return None if raw is None else str(raw).encode("utf-8")


def build_request(raw: dict[str, Any]) -> NormalizedRequest:
    host = str(raw.get("host", "example.test"))
    path = str(raw.get("path", "/"))
    scheme = str(raw.get("scheme", "https"))
    return NormalizedRequest(
        flow_id=str(raw.get("flow_id", "corpus-flow")),
        timestamp=str(raw.get("timestamp", "2026-01-01T00:00:00.000Z")),
        scheme="http" if scheme == "http" else "https",
        method=str(raw.get("method", "GET")),
        host=host,
        port=int(raw.get("port", 443)),
        path=path,
        url=str(raw.get("url", f"{scheme}://{host}{path}")),
        http_version=str(raw.get("http_version", "HTTP/1.1")),
        query=_pairs(raw.get("query")),
        headers=_pairs(raw.get("headers")),
        dest=raw.get("dest"),
        body=_body(raw.get("body")),
        body_truncated=bool(raw.get("body_truncated", False)),
        tab_id=raw.get("tab_id"),
    )


def build_response(raw: dict[str, Any] | None) -> NormalizedResponse | None:
    if raw is None:
        return None
    return NormalizedResponse(
        flow_id=str(raw.get("flow_id", "corpus-flow")),
        timestamp=str(raw.get("timestamp", "2026-01-01T00:00:01.000Z")),
        status=int(raw.get("status", 200)),
        reason=str(raw.get("reason", "")),
        http_version=str(raw.get("http_version", "HTTP/1.1")),
        headers=_pairs(raw.get("headers")),
        body=_body(raw.get("body")),
        body_truncated=bool(raw.get("body_truncated", False)),
        streamed=bool(raw.get("streamed", False)),
        encoding=raw.get("encoding"),
    )


def build_evaluator(case: dict[str, Any]) -> Evaluator:
    ruleset = RuleSet.from_rules(
        case.get("rules") or [],
        module=str(case.get("module", "corpus")),
        priority=int(case.get("priority", 100)),
    )
    asset_root = case.get("asset_root")
    return Evaluator(
        ruleset,
        asset_root=(CORPUS_DIR / str(asset_root)) if asset_root else None,
    )


# ------------------------------------------------------------------ replay --


def replay(case: dict[str, Any]) -> dict[str, Any]:
    """Drive the real evaluator over one recorded flow, exactly as live does."""
    evaluator = build_evaluator(case)
    builder = ProvenanceBuilder(str(case.get("profile", "default")))
    budget = None if case.get("budget_ms") is None else TimeBudget(float(case["budget_ms"]))

    request = build_request(case["request"])
    decision = evaluator.evaluate_request(request, builder, budget)

    buffering = case.get("buffering")
    if buffering is not None:
        evaluator.decide_buffering(
            request,
            buffering.get("content_type"),
            buffering.get("content_length"),
            decision.wants_body,
            builder,
        )

    response = build_response(case.get("response"))
    if response is not None and not decision.blocked:
        evaluator.evaluate_response(request, response, builder, budget)

    return builder.build().to_dict()


def replay_dry_run(case: dict[str, Any], installed_root: Path) -> dict[str, Any]:
    """Drive the real DryRunner over the case's recorded flows (REQ CAP-030..033)."""
    spec = case["dry_run"]
    runner = DryRunner(build_evaluator(case), installed_root=installed_root)
    flows = [
        FlowRecord(
            flow_id=str(raw.get("flow_id", f"corpus-{index}")),
            kind="http",
            started_at="2026-01-01T00:00:00.000Z",
            completed_at="2026-01-01T00:00:01.000Z",
            request=build_request(raw["request"]),
            response=build_response(raw.get("response")),
        )
        for index, raw in enumerate(case["flows"])
    ]
    return runner.run(flows, DryRunRequest.from_dict(spec))


# ------------------------------------------------------------------- tests --


def load_cases() -> list[tuple[str, Path]]:
    return [(path.stem, path) for path in sorted(CASES_DIR.glob("*.json"))]


CASES = load_cases()


def test_the_corpus_is_not_empty() -> None:
    """REQ TST-004 — a corpus directory that quietly emptied would pass silently."""
    assert len(CASES) >= 10, f"only {len(CASES)} corpus cases found in {CASES_DIR}"


@pytest.mark.parametrize("name,path", CASES, ids=[name for name, _ in CASES])
def test_corpus_case(name: str, path: Path, tmp_path: Path) -> None:
    """REQ TST-004 — a recorded flow still produces its recorded provenance."""
    case = json.loads(path.read_text())
    kind = case.get("kind", "engine")

    if kind == "dryrun":
        installed = tmp_path / "modules"
        installed.mkdir()
        result = replay_dry_run(case, installed)
        assert_matches_golden(case, path, "expected_dry_run", result)
    else:
        assert_matches_golden(case, path, "expected_provenance", replay(case))


def test_every_case_declares_what_it_covers() -> None:
    """A case with no id, description or requirement IDs is unreadable as a diff."""
    for name, path in CASES:
        case = json.loads(path.read_text())
        assert case.get("id") == name, f"{path.name}: 'id' must equal the file stem"
        assert case.get("description"), f"{path.name}: needs a 'description'"
        assert case.get("requirements"), f"{path.name}: needs 'requirements'"


def test_the_corpus_covers_the_outcome_and_note_taxonomy() -> None:
    """REQ TST-004 — the spread is the point; a happy-path-only corpus is not one.

    Guards the corpus itself: deleting the streamed case, or the map_local
    failure, would otherwise leave the suite green while the coverage it exists
    to provide had gone.
    """
    outcomes: set[str] = set()
    notes: set[str] = set()
    for _, path in CASES:
        case = json.loads(path.read_text())
        for blob in (case.get("expected_provenance"), case.get("expected_dry_run")):
            for entry, note in _walk_provenance(blob):
                outcomes |= entry
                notes |= note

    required_outcomes = {"applied", "no_change", "skipped_streamed", "skipped_budget", "error"}
    required_notes = {
        "response_streamed",
        "csp_modified",
        "sri_stripped",
        "script_injected",
        "map_local_missing",
        "transform_budget_exceeded",
        "module_error",
    }
    assert required_outcomes <= outcomes, (
        f"missing outcomes: {sorted(required_outcomes - outcomes)}"
    )
    assert required_notes <= notes, f"missing note codes: {sorted(required_notes - notes)}"


def _walk_provenance(blob: Any) -> list[tuple[set[str], set[str]]]:
    """Every provenance dict reachable in a golden, as (outcomes, note codes)."""
    found: list[tuple[set[str], set[str]]] = []
    if isinstance(blob, dict):
        if "entries" in blob and "notes" in blob:
            found.append(
                (
                    {str(e.get("outcome")) for e in blob["entries"]},
                    {str(n.get("code")) for n in blob["notes"]},
                )
            )
        for value in blob.values():
            found.extend(_walk_provenance(value))
    elif isinstance(blob, list):
        for value in blob:
            found.extend(_walk_provenance(value))
    return found


class TestTheComparisonHelper:
    """The helper is what makes a corpus failure readable, so it is tested too."""

    def test_reports_the_path_of_a_changed_scalar(self) -> None:
        found = first_difference(
            {"entries": [{"outcome": "applied"}]}, {"entries": [{"outcome": "no_change"}]}
        )
        assert found == "$.entries[0].outcome: 'applied' != 'no_change'"

    def test_reports_a_missing_key(self) -> None:
        found = first_difference({"a": 1}, {"a": 1, "b": 2})
        assert found is not None
        assert "$.b" in found and "missing" in found

    def test_reports_an_unexpected_key(self) -> None:
        found = first_difference({"a": 1, "b": 2}, {"a": 1})
        assert found is not None
        assert "$.b" in found and "unexpected" in found

    def test_reports_a_length_mismatch_with_the_first_extra_entry(self) -> None:
        found = first_difference({"notes": [{"code": "x"}]}, {"notes": []})
        assert found is not None
        assert "1 entries, expected 0" in found
        assert '"code": "x"' in found

    def test_identical_structures_have_no_difference(self) -> None:
        assert first_difference({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}) is None

    def test_timing_is_normalised_out_at_every_depth(self) -> None:
        cleaned = normalise({"total_ms": 3.7, "entries": [{"duration_ms": 0.4, "seq": 0}]})
        assert cleaned == {
            "total_ms": TIMING_SENTINEL,
            "entries": [{"duration_ms": TIMING_SENTINEL, "seq": 0}],
        }
