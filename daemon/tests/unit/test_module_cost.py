"""Per-module cost — REQ PRF-007.

"An expensive module SHALL be identifiable rather than merely suspected." Two
consumers need different shapes of that: `GET /metrics` wants every module that
spent time, ordered by how much; the module library wants four declared columns
per installed module.

The accumulator lives in ``engine/`` and takes only the provenance shape, so it
is testable with no proxy and no registry (REQ TST-001, DD-2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from pporlock.addon.interceptor import Interceptor
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import ControlApp
from pporlock.engine.cost import ModuleCostIndex, ModuleStat
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.provenance import (
    Action,
    Outcome,
    Phase,
    ProvenanceBuilder,
)


def _provenance(*entries: tuple[str, Outcome, float]) -> Any:
    builder = ProvenanceBuilder("default")
    for module, outcome, duration in entries:
        builder.record(
            phase=Phase.RESPONSE_HEADERS,
            module=module,
            rule_id=f"{module}:0",
            action=Action.HEADERS,
            outcome=outcome,
            duration_ms=duration,
        )
    return builder.build()


class TestModuleStat:
    def test_avg_is_per_flow_not_per_entry(self) -> None:
        """A flow is what the user perceives. A module with three rules on one
        page cost that page one number, not three."""
        stat = ModuleStat("m", flows_matched=2, entries=6, total_ms=10.0)
        assert stat.avg_ms == 5.0
        assert stat.mean_entry_ms == pytest.approx(10.0 / 6)

    def test_no_division_by_zero_on_an_untouched_module(self) -> None:
        assert ModuleStat("m").avg_ms == 0.0
        assert ModuleStat("m").mean_entry_ms == 0.0

    def test_the_status_shape_is_exactly_the_four_declared_fields(self) -> None:
        """contracts/openapi.yaml ModuleStatus.stats declares four properties.
        Emitting more here would put fields on the wire no schema describes."""
        assert set(ModuleStat("m").to_status_dict()) == {
            "flows_matched",
            "flows_modified",
            "errors",
            "avg_ms",
        }


class TestModuleCostIndex:
    def test_a_flow_counts_once_per_module_however_many_rules_fired(self) -> None:
        index = ModuleCostIndex()
        index.record(
            _provenance(
                ("csp", Outcome.APPLIED, 1.0),
                ("csp", Outcome.APPLIED, 2.0),
                ("csp", Outcome.NO_CHANGE, 0.5),
            )
        )
        stat = index.get("csp")
        assert stat is not None
        assert stat.flows_matched == 1
        assert stat.flows_modified == 1
        assert stat.entries == 3
        assert stat.total_ms == pytest.approx(3.5)

    def test_matched_without_modified(self) -> None:
        """A module that matches four hundred flows and modifies none has rules
        that are wrong. Collapsing the two counts would hide that."""
        index = ModuleCostIndex()
        for _ in range(4):
            index.record(_provenance(("quiet", Outcome.NO_CHANGE, 0.1)))
        stat = index.get("quiet")
        assert stat is not None
        assert (stat.flows_matched, stat.flows_modified) == (4, 0)

    def test_errors_are_counted_separately(self) -> None:
        index = ModuleCostIndex()
        index.record(_provenance(("bad", Outcome.ERROR, 0.2)))
        stat = index.get("bad")
        assert stat is not None
        assert stat.errors == 1
        assert stat.flows_modified == 0

    def test_max_is_kept_alongside_the_average(self) -> None:
        """A module that is fast on four hundred flows and takes 300 ms on the
        one page you care about is invisible in a mean."""
        index = ModuleCostIndex()
        for _ in range(400):
            index.record(_provenance(("spiky", Outcome.APPLIED, 0.1)))
        index.record(_provenance(("spiky", Outcome.APPLIED, 300.0)))
        stat = index.get("spiky")
        assert stat is not None
        assert stat.max_ms == 300.0
        assert stat.avg_ms < 1.0

    def test_stats_are_ordered_most_expensive_first(self) -> None:
        index = ModuleCostIndex()
        index.record(_provenance(("cheap", Outcome.APPLIED, 0.1)))
        index.record(_provenance(("dear", Outcome.APPLIED, 40.0)))
        index.record(_provenance(("middling", Outcome.APPLIED, 5.0)))
        assert [s.module for s in index.stats()] == ["dear", "middling", "cheap"]

    def test_an_entry_with_no_module_is_still_counted(self) -> None:
        """Time that went somewhere unattributed is still time. Dropping it
        would make the totals quietly not add up."""
        index = ModuleCostIndex()
        index.record(_provenance(("", Outcome.APPLIED, 1.0)))
        assert index.get("(unattributed)") is not None

    def test_a_flow_with_no_entries_records_nothing(self) -> None:
        index = ModuleCostIndex()
        index.record(_provenance())
        assert len(index) == 0

    def test_it_survives_a_partial_provenance_object(self) -> None:
        """Never raises on the flow-completion path — a metrics accumulator that
        can take the proxy down is worse than no metrics."""
        index = ModuleCostIndex()
        index.record(object())
        assert len(index) == 0

    def test_reset(self) -> None:
        index = ModuleCostIndex()
        index.record(_provenance(("m", Outcome.APPLIED, 1.0)))
        index.reset()
        assert len(index) == 0


class TestRegistryStats:
    """The module library's four columns come from the registry (PRF-007)."""

    def _registry(self, tmp_path: Path, *names: str) -> ModuleRegistry:
        root = tmp_path / "modules"
        for name in names:
            directory = root / name
            directory.mkdir(parents=True)
            (directory / "module.yaml").write_text(f"name: {name}\npporlock_api: '1'\n")
        registry = ModuleRegistry(root)
        registry.reload()
        return registry

    def test_a_fresh_module_reports_zeros_not_a_missing_field(self, tmp_path: Path) -> None:
        """The field being absent crashed the module library, which read it
        unconditionally. "No data" and "zero" are different facts, and the
        daemon must say which one it means."""
        registry = self._registry(tmp_path, "csp")
        payload = registry.modules[0].to_dict()
        assert payload["stats"] == {
            "flows_matched": 0,
            "flows_modified": 0,
            "errors": 0,
            "avg_ms": 0.0,
        }

    def test_stats_accumulate_from_provenance(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path, "csp")
        registry.record_provenance(_provenance(("csp", Outcome.APPLIED, 2.0)))
        registry.record_provenance(_provenance(("csp", Outcome.NO_CHANGE, 1.0)))
        stats = registry.modules[0].to_dict()["stats"]
        assert stats["flows_matched"] == 2
        assert stats["flows_modified"] == 1
        assert stats["avg_ms"] == pytest.approx(1.5)

    def test_entries_naming_a_non_module_are_ignored(self, tmp_path: Path) -> None:
        """A rule from rules.yaml carries module "api" and has no row in the
        module library. /metrics counts it; this must not invent a module."""
        registry = self._registry(tmp_path, "csp")
        registry.record_provenance(_provenance(("api", Outcome.APPLIED, 9.0)))
        assert registry.modules[0].to_dict()["stats"]["flows_matched"] == 0
        assert registry.get("api") is None

    def test_stats_survive_a_reload(self, tmp_path: Path) -> None:
        """Zeroing on every edit would make the column useless exactly while
        someone is iterating on a module."""
        registry = self._registry(tmp_path, "csp")
        registry.record_provenance(_provenance(("csp", Outcome.APPLIED, 2.0)))
        registry.reload()
        assert registry.modules[0].to_dict()["stats"]["flows_matched"] == 1

    def test_record_provenance_never_raises(self, tmp_path: Path) -> None:
        registry = self._registry(tmp_path, "csp")
        registry.record_provenance(object())


class TestMetricsRoute:
    """`GET /metrics` carries the per-module breakdown (REQ PRF-007)."""

    @pytest.fixture
    def app(self, tmp_path: Path) -> ControlApp:
        config = Config()
        config.state_dir = str(tmp_path)
        interceptor = Interceptor(config)
        return ControlApp(config, ring=RingBuffer(), interceptor=interceptor)

    def test_modules_key_is_present_even_with_no_traffic(self, app: ControlApp) -> None:
        client = TestClient(app.asgi)
        response = client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {app.tokens.ensure()}", "X-Pporlock-Client": "ui"},
        )
        assert response.status_code == 200
        assert response.json()["modules"] == []

    def test_it_reports_accumulated_module_cost(self, app: ControlApp) -> None:
        assert app.interceptor is not None
        app.interceptor.module_cost.record(_provenance(("csp", Outcome.APPLIED, 12.0)))
        app.interceptor.module_cost.record(_provenance(("tidy", Outcome.APPLIED, 1.0)))
        client = TestClient(app.asgi)
        modules = client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {app.tokens.ensure()}", "X-Pporlock-Client": "ui"},
        ).json()["modules"]
        assert [m["module"] for m in modules] == ["csp", "tidy"]
        assert modules[0]["total_ms"] == pytest.approx(12.0)

    def test_metrics_stays_an_inline_route(self) -> None:
        """It must not start walking the ring buffer to answer: /metrics runs on
        the proxy's own event loop and may only read in-memory state."""
        from pporlock.control.app import INLINE_ROUTES, OFFLOAD_ROUTES

        assert "/metrics" in INLINE_ROUTES
        assert "/metrics" not in OFFLOAD_ROUTES
