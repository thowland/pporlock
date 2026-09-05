"""The module system. SPEC-1 §5, SPEC-0 §8.

The load rule everything else rests on: a module that fails disables only
itself (REQ MOD-005). These tests are written against real directories on disk
and real Python imports, because the failure modes worth catching — a syntax
error, a name collision between two modules' helpers, a symlink out of an
assets directory — do not exist in a mocked loader.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from pporlock.engine.cost import Cost
from pporlock.engine.evaluator import Evaluator
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.modules.context import (
    MODULE_API_VERSION,
    ModuleContext,
    ModuleStore,
)
from pporlock.engine.modules.loader import discover, load_all, load_module
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.provenance import NoteCode, ProvenanceBuilder, Severity
from pporlock.engine.transforms import TransformRegistry
from pporlock.errors import AssetPathError

BLOCK_RULE = {"name": "block-ads", "action": "block", "match": {"host": "ads.example"}}


def manifest(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": name, "version": "1.0.0", "pporlock_api": MODULE_API_VERSION}
    base.update(overrides)
    return base


def write_module(
    root: Path,
    name: str,
    *,
    raw: str | None = None,
    python: str | None = None,
    **overrides: Any,
) -> Path:
    """Create a module directory. ``raw`` writes the manifest verbatim."""
    import yaml

    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else yaml.safe_dump(manifest(name, **overrides), sort_keys=False)
    (path / "module.yaml").write_text(text)
    if python is not None:
        (path / "module.py").write_text(python)
    return path


def request(**kwargs: Any) -> NormalizedRequest:
    base: dict[str, Any] = {
        "flow_id": "f",
        "timestamp": "t",
        "scheme": "https",
        "method": "GET",
        "host": "cdn.example.com",
        "port": 443,
        "path": "/a.js",
        "url": "https://cdn.example.com/a.js",
        "dest": "script",
    }
    base.update(kwargs)
    return NormalizedRequest(**base)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "modules"
    directory.mkdir()
    return directory


@pytest.fixture(autouse=True)
def _drop_synthetic_imports() -> Any:
    """Module imports land in sys.modules under a synthetic name.

    Left behind, they leak between tests and a reload would silently reuse the
    previous test's code — exactly the bug the synthetic naming exists to make
    impossible in production.
    """
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("pporlock_module_"):
            del sys.modules[name]


class TestLoadingAValidModule:
    """The happy path, in enough detail that the failure tests mean something."""

    def test_a_manifest_alone_is_a_module(self, root: Path) -> None:
        module = load_module(write_module(root, "tidy"))
        assert module.state == "loaded"
        assert module.error is None

    def test_the_manifest_supplies_the_metadata(self, root: Path) -> None:
        module = load_module(
            write_module(root, "tidy", version="2.1.0", description="tidies", author="me")
        )
        assert (module.version, module.description, module.author) == ("2.1.0", "tidies", "me")

    def test_declarative_rules_are_compiled_at_load_time(self, root: Path) -> None:
        """Compiling now rather than on first flow is what makes a bad rule a
        load error the author sees, instead of a surprise mid-session."""
        module = load_module(write_module(root, "tidy", rules=[BLOCK_RULE]))
        assert len(module.rules) == 1
        assert module.rules[0].module == "tidy"

    def test_a_module_is_disabled_unless_its_manifest_says_otherwise(self, root: Path) -> None:
        """REQ MCP-030 — arriving on disk is not consent to run."""
        assert load_module(write_module(root, "tidy")).enabled is False

    def test_python_is_optional(self, root: Path) -> None:
        assert load_module(write_module(root, "tidy")).has_python is False

    def test_python_is_imported_when_present(self, root: Path) -> None:
        module = load_module(
            write_module(root, "tidy", python="def on_request(ctx, req):\n    pass\n")
        )
        assert module.has_python
        assert "on_request" in module.hooks()

    def test_only_callables_count_as_hooks(self, root: Path) -> None:
        """A module assigning ``on_request = None`` to disable it should not be
        called with None."""
        module = load_module(write_module(root, "tidy", python="on_request = None\n"))
        assert module.hooks() == {}

    def test_assets_are_found_when_the_directory_exists(self, root: Path) -> None:
        path = write_module(root, "tidy")
        (path / "assets").mkdir()
        assert load_module(path).assets == path / "assets"

    def test_no_assets_directory_means_no_assets(self, root: Path) -> None:
        assert load_module(write_module(root, "tidy")).assets is None


class TestRefusingToLoad:
    """Every refusal names what is wrong and which module is at fault.

    A module that quietly does not load is worse than one that fails loudly:
    the author concludes their code ran and did nothing.
    """

    def test_a_directory_without_a_manifest_is_not_a_module(self, root: Path) -> None:
        (root / "empty").mkdir()
        module = load_module(root / "empty")
        assert module.state == "load_error"
        assert module.error is not None
        assert module.error.code == "module_missing_manifest"

    def test_invalid_yaml_is_reported_rather_than_raised(self, root: Path) -> None:
        module = load_module(write_module(root, "tidy", raw="name: [unclosed\n"))
        assert module.error is not None
        assert module.error.code == "module_invalid_yaml"

    def test_a_manifest_that_is_not_a_mapping_is_refused(self, root: Path) -> None:
        module = load_module(write_module(root, "tidy", raw="- just\n- a\n- list\n"))
        assert module.error is not None
        assert module.error.code == "module_invalid_manifest"

    def test_an_unknown_manifest_key_is_an_error(self, root: Path) -> None:
        """REQ MOD-014 — a typo silently ignored is how a module ends up not
        doing what its author believes it does."""
        module = load_module(write_module(root, "tidy", enbaled=True))
        assert module.error is not None
        assert module.error.code == "module_unknown_key"
        assert "enbaled" in module.error.message

    def test_the_manifest_name_must_match_the_directory(self, root: Path) -> None:
        """Two names for one module means logs, audit entries, and rule ids that
        refer to something the user cannot find on disk."""
        module = load_module(
            write_module(root, "tidy", raw="name: something-else\npporlock_api: '1'\n")
        )
        assert module.error is not None
        assert module.error.code == "module_name_mismatch"

    def test_an_unsupported_api_version_is_refused_with_both_versions_named(
        self, root: Path
    ) -> None:
        """REQ MOD-026 — failing later at runtime gives the author nothing to
        connect the failure to."""
        module = load_module(write_module(root, "tidy", pporlock_api="99"))
        assert module.error is not None
        assert module.error.code == "module_api_unsupported"
        assert "99" in module.error.message
        assert MODULE_API_VERSION in module.error.message

    def test_a_missing_api_version_is_refused(self, root: Path) -> None:
        module = load_module(write_module(root, "tidy", raw="name: tidy\nversion: '1.0.0'\n"))
        assert module.error is not None
        assert module.error.code == "module_api_unsupported"

    def test_rules_must_be_a_list(self, root: Path) -> None:
        module = load_module(write_module(root, "tidy", rules={"name": "x"}))
        assert module.error is not None
        assert module.error.code == "module_invalid_rules"

    def test_a_bad_rule_fails_only_its_own_module(self, root: Path) -> None:
        """REQ MOD-005 at rule granularity: the neighbour still loads."""
        write_module(root, "good", rules=[BLOCK_RULE])
        write_module(root, "bad", rules=[{"name": "x", "action": "nonsense"}])
        by_name = {m.name: m for m in load_all(root)}
        assert by_name["bad"].state == "load_error"
        assert by_name["good"].state == "loaded"

    def test_the_rule_error_carries_the_engine_code(self, root: Path) -> None:
        module = load_module(write_module(root, "bad", rules=[{"name": "x", "action": "nonsense"}]))
        assert module.error is not None
        assert module.error.code == "rule_invalid"

    def test_a_python_import_error_is_isolated_and_keeps_its_traceback(self, root: Path) -> None:
        """REQ MOD-005. The traceback is the whole value of the report: without
        it the author knows only that 'the module failed'."""
        module = load_module(write_module(root, "boom", python="raise ValueError('nope')\n"))
        assert module.state == "load_error"
        assert module.error is not None
        assert module.error.code == "module_import_failed"
        assert "nope" in module.error.message
        assert module.error.trace is not None
        assert "ValueError" in module.error.trace

    def test_an_import_error_points_at_the_offending_line(self, root: Path) -> None:
        module = load_module(
            write_module(root, "boom", python="x = 1\ny = 2\nraise RuntimeError('here')\n")
        )
        assert module.error is not None
        assert module.error.line == 3

    def test_a_failed_import_leaves_nothing_in_sys_modules(self, root: Path) -> None:
        """A half-executed module left behind would be returned by the next
        import instead of re-running the fixed code."""
        load_module(write_module(root, "boom", python="raise ValueError('nope')\n"))
        assert "pporlock_module_boom" not in sys.modules

    def test_one_bad_module_does_not_stop_the_sweep(self, root: Path) -> None:
        write_module(root, "boom", python="raise ValueError('nope')\n")
        write_module(root, "fine")
        assert {m.name for m in load_all(root)} == {"boom", "fine"}


class TestImportIsolation:
    """Two modules are two namespaces, whatever their authors called things."""

    def test_same_named_helpers_in_two_modules_do_not_collide(self, root: Path) -> None:
        """They will collide: 'utils' is what everybody calls a helper. Under a
        shared import name the second module would silently get the first."""
        write_module(root, "alpha", python="def utils():\n    return 'alpha'\n")
        write_module(root, "beta", python="def utils():\n    return 'beta'\n")
        by_name = {m.name: m for m in load_all(root)}
        assert by_name["alpha"].python.utils() == "alpha"
        assert by_name["beta"].python.utils() == "beta"

    def test_each_module_gets_its_own_entry_in_sys_modules(self, root: Path) -> None:
        write_module(root, "alpha", python="X = 1\n")
        write_module(root, "beta", python="X = 2\n")
        load_all(root)
        assert sys.modules["pporlock_module_alpha"].X == 1
        assert sys.modules["pporlock_module_beta"].X == 2

    def test_a_hyphenated_name_becomes_a_legal_import_name(self, root: Path) -> None:
        write_module(root, "no-ads", python="X = 1\n")
        assert load_module(root / "no-ads").has_python
        assert "pporlock_module_no_ads" in sys.modules


class TestDiscovery:
    def test_only_directories_with_a_manifest_are_modules(self, root: Path) -> None:
        write_module(root, "real")
        (root / "notes").mkdir()
        (root / "README.md").write_text("hi")
        assert [p.name for p in discover(root)] == ["real"]

    def test_a_missing_root_is_not_an_error(self, tmp_path: Path) -> None:
        """A fresh install has no modules directory, and that is a daemon that
        starts, not one that refuses to."""
        assert discover(tmp_path / "nothing-here") == []

    def test_discovery_order_is_stable(self, root: Path) -> None:
        """Ordering decides tie-breaks between equal priorities, so it must not
        depend on the filesystem's iteration order."""
        for name in ("zed", "alpha", "mid"):
            write_module(root, name)
        assert [p.name for p in discover(root)] == ["alpha", "mid", "zed"]


class TestRegistryReload:
    """Reload is a snapshot swap (REQ MOD-004), not a mutation."""

    def test_reload_reports_what_it_found(self, root: Path) -> None:
        write_module(root, "one", enabled=True)
        write_module(root, "two")
        result = ModuleRegistry(root).reload()
        assert (result.loaded, result.enabled) == (2, 1)
        assert result.errors == ()

    def test_load_errors_are_reported_with_the_module_named(self, root: Path) -> None:
        write_module(root, "boom", python="raise ValueError('nope')\n")
        result = ModuleRegistry(root).reload()
        assert [e["module"] for e in result.to_dict()["errors"]] == ["boom"]

    def test_an_earlier_snapshot_is_unaffected_by_a_reload(self, root: Path) -> None:
        """An in-flight flow finishes against the rules it started with, which
        is what removes any need for locking (REQ MOD-004)."""
        write_module(root, "tidy", enabled=True, rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        before = registry.build_ruleset()

        write_module(root, "tidy", enabled=True, rules=[BLOCK_RULE, dict(BLOCK_RULE, name="two")])
        registry.reload()

        assert len(before) == 1
        assert len(registry.build_ruleset()) == 2

    def test_a_reload_re_executes_module_python(self, root: Path) -> None:
        """Editing a module and reloading must run the new code; a cached import
        would make the edit invisible until the daemon restarted."""
        registry = ModuleRegistry(root)
        write_module(root, "tidy", python="VALUE = 1\n")
        registry.reload()
        write_module(root, "tidy", python="VALUE = 2\n")
        registry.reload()
        module = registry.get("tidy")
        assert module is not None
        assert module.python.VALUE == 2

    def test_a_reload_does_not_turn_running_modules_off(self, root: Path) -> None:
        """The manifest seeds enablement once; after that it is API state.

        Otherwise installing one module would silently disable every other one,
        because their manifests never said `enabled: true` — the user did.
        """
        write_module(root, "tidy")
        registry = ModuleRegistry(root)
        registry.reload()
        registry.set_enabled("tidy", True)

        write_module(root, "newcomer")
        registry.reload()

        assert [m.name for m in registry.active()] == ["tidy"]

    def test_a_reload_keeps_a_priority_set_through_the_api(self, root: Path) -> None:
        write_module(root, "tidy", rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        registry.set_enabled("tidy", True)
        registry.set_priority("tidy", 7)
        registry.reload()
        module = registry.get("tidy")
        assert module is not None
        assert module.priority == 7
        assert registry.build_ruleset().short_circuit[0].priority == 7

    def test_a_module_seen_for_the_first_time_takes_its_manifest_state(self, root: Path) -> None:
        """A module shipped enabled has to be able to say so once."""
        registry = ModuleRegistry(root)
        registry.reload()
        write_module(root, "tidy", enabled=True)
        registry.reload()
        assert [m.name for m in registry.active()] == ["tidy"]

    def test_a_deleted_module_is_gone_after_reload(self, root: Path) -> None:
        import shutil

        registry = ModuleRegistry(root)
        write_module(root, "tidy")
        registry.reload()
        shutil.rmtree(root / "tidy")
        registry.reload()
        assert registry.get("tidy") is None


class TestLifecycleHooks:
    """on_load and on_unload, and what happens when they misbehave."""

    LOG = (
        "from pathlib import Path\n"
        "LOG = Path(__file__).parent.parent / 'lifecycle.log'\n"
        "def _note(what, ctx):\n"
        "    with LOG.open('a') as fh:\n"
        "        fh.write(what + ':' + ctx.name + '\\n')\n"
        "def on_load(ctx):\n"
        "    _note('load', ctx)\n"
        "def on_unload(ctx):\n"
        "    _note('unload', ctx)\n"
    )

    def test_on_load_runs_when_the_module_is_loaded(self, root: Path) -> None:
        write_module(root, "tidy", python=self.LOG)
        ModuleRegistry(root).reload()
        assert (root / "lifecycle.log").read_text() == "load:tidy\n"

    def test_on_unload_runs_before_the_replacement_loads(self, root: Path) -> None:
        """A module holding something — a file, a connection — gets the chance
        to release it before its successor takes over."""
        write_module(root, "tidy", python=self.LOG)
        registry = ModuleRegistry(root)
        registry.reload()
        registry.reload()
        assert (root / "lifecycle.log").read_text().splitlines() == [
            "load:tidy",
            "unload:tidy",
            "load:tidy",
        ]

    def test_a_raising_on_unload_does_not_prevent_the_reload(self, root: Path) -> None:
        """Refusing to reload because the outgoing version misbehaved would make
        a broken module unfixable."""
        write_module(root, "tidy", python="def on_unload(ctx):\n    raise RuntimeError('bye')\n")
        registry = ModuleRegistry(root)
        registry.reload()
        result = registry.reload()
        assert result.loaded == 1

    def test_a_raising_hook_is_recorded_against_its_module(self, root: Path) -> None:
        write_module(root, "tidy", python="def on_unload(ctx):\n    raise RuntimeError('bye')\n")
        registry = ModuleRegistry(root)
        registry.reload()
        registry.reload()
        failure = registry.failures[0]
        assert (failure.module, failure.hook) == ("tidy", "on_unload")
        assert "bye" in failure.message

    def test_a_raising_on_load_does_not_stop_other_modules(self, root: Path) -> None:
        write_module(root, "boom", python="def on_load(ctx):\n    raise RuntimeError('x')\n")
        write_module(root, "fine", enabled=True)
        result = ModuleRegistry(root).reload()
        assert result.loaded == 2

    def test_a_module_that_failed_to_load_gets_no_context(self, root: Path) -> None:
        """Hooks cannot run on a module whose code never executed, so there is
        nothing for a context to belong to."""
        write_module(root, "boom", python="raise ValueError('nope')\n")
        registry = ModuleRegistry(root)
        registry.reload()
        assert registry.context("boom") is None


class TestActiveModules:
    """Which modules actually run, and why the others do not."""

    def _registry(self, root: Path) -> ModuleRegistry:
        write_module(root, "on", enabled=True)
        write_module(root, "off")
        write_module(root, "broken", enabled=True, python="raise ValueError('nope')\n")
        registry = ModuleRegistry(root)
        registry.reload()
        return registry

    def test_a_disabled_module_does_not_run(self, root: Path) -> None:
        assert [m.name for m in self._registry(root).active()] == ["on"]

    def test_a_module_that_failed_to_load_does_not_run(self, root: Path) -> None:
        """Even though its manifest said enabled: there is no loaded code to
        run (REQ MOD-005)."""
        assert "broken" not in [m.name for m in self._registry(root).active()]

    def test_a_quarantined_module_does_not_run(self, root: Path) -> None:
        registry = self._registry(root)
        registry.quarantine("on", "testing")
        assert registry.active() == []

    def test_a_profile_narrows_the_set_further(self, root: Path) -> None:
        """REQ MOD-043 — a profile selects from the enabled modules; it cannot
        conjure a disabled one into running."""
        write_module(root, "also-on", enabled=True)
        registry = self._registry(root)
        registry.reload()
        assert [m.name for m in registry.active(["on"])] == ["on"]

    def test_a_profile_cannot_enable_a_disabled_module(self, root: Path) -> None:
        registry = self._registry(root)
        assert registry.active(["off"]) == []

    def test_no_profile_means_every_enabled_module(self, root: Path) -> None:
        write_module(root, "also-on", enabled=True)
        registry = self._registry(root)
        registry.reload()
        assert {m.name for m in registry.active(None)} == {"on", "also-on"}


class TestRuleSetConstruction:
    """REQ MOD-023 — ordering across modules is by priority, then declaration."""

    def test_rules_from_all_active_modules_are_combined(self, root: Path) -> None:
        write_module(root, "one", enabled=True, rules=[BLOCK_RULE])
        write_module(root, "two", enabled=True, rules=[dict(BLOCK_RULE, name="other")])
        registry = ModuleRegistry(root)
        registry.reload()
        assert len(registry.build_ruleset()) == 2

    def test_the_lower_priority_number_evaluates_first(self, root: Path) -> None:
        """First-match-wins for block, so ordering decides which module's block
        is the one that fires."""
        write_module(root, "late", enabled=True, priority=200, rules=[BLOCK_RULE])
        write_module(root, "early", enabled=True, priority=10, rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        assert [r.module for r in registry.build_ruleset().short_circuit] == ["early", "late"]

    def test_declaration_order_breaks_a_priority_tie(self, root: Path) -> None:
        write_module(
            root,
            "one",
            enabled=True,
            rules=[dict(BLOCK_RULE, name="first"), dict(BLOCK_RULE, name="second")],
        )
        registry = ModuleRegistry(root)
        registry.reload()
        assert [r.name for r in registry.build_ruleset().short_circuit] == ["first", "second"]

    def test_the_ruleset_names_the_modules_that_contributed(self, root: Path) -> None:
        """Provenance attributes a decision to a module, so the set has to know
        which ones are in it."""
        write_module(root, "one", enabled=True, rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        assert registry.build_ruleset().modules == ("one",)

    def test_changing_a_priority_reorders_the_rules(self, root: Path) -> None:
        """Rules carry the priority they were compiled with, so a priority
        change that did not rebuild them would appear to do nothing."""
        write_module(root, "late", enabled=True, priority=200, rules=[BLOCK_RULE])
        write_module(root, "early", enabled=True, priority=10, rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        registry.set_priority("late", 1)
        assert [r.module for r in registry.build_ruleset().short_circuit] == ["late", "early"]

    def test_a_disabled_module_contributes_no_rules(self, root: Path) -> None:
        write_module(root, "off", rules=[BLOCK_RULE])
        registry = ModuleRegistry(root)
        registry.reload()
        assert len(registry.build_ruleset()) == 0


class TestQuarantine:
    """REQ MOD-025 — a module failing on every flow buries real findings."""

    def _registry(self, root: Path, after: int = 3) -> ModuleRegistry:
        write_module(root, "flaky", enabled=True)
        registry = ModuleRegistry(root, quarantine_after=after)
        registry.reload()
        return registry

    def test_failures_below_the_threshold_do_not_quarantine(self, root: Path) -> None:
        """Modules fail occasionally on odd pages; that is not a broken module."""
        registry = self._registry(root)
        assert [registry.record_failure("flaky") for _ in range(2)] == [False, False]
        assert registry.active()

    def test_the_nth_consecutive_failure_quarantines(self, root: Path) -> None:
        registry = self._registry(root)
        results = [registry.record_failure("flaky") for _ in range(3)]
        assert results == [False, False, True]
        assert registry.active() == []

    def test_a_quarantined_module_reports_why_and_when(self, root: Path) -> None:
        """A module that stopped working with no explanation is a support
        ticket."""
        registry = self._registry(root)
        for _ in range(3):
            registry.record_failure("flaky")
        payload = registry.modules[0].to_dict()
        assert payload["state"] == "quarantined"
        assert payload["quarantine"]["failures"] == 3
        assert "consecutive" in payload["quarantine"]["reason"]
        assert payload["quarantine"]["since"]

    def test_quarantine_writes_a_provenance_note(self, root: Path) -> None:
        """The flow that tipped it over should say so; otherwise the moment a
        module stopped running is invisible."""
        registry = self._registry(root)
        builder = ProvenanceBuilder("f", "t")
        for _ in range(3):
            registry.record_failure("flaky", builder)
        codes = [n.code for n in builder.build().notes]
        assert NoteCode.MODULE_QUARANTINED in codes

    def test_a_success_resets_the_count(self, root: Path) -> None:
        """Consecutive is the point: a module that fails on one page in ten is
        not the module that fails on everything."""
        registry = self._registry(root)
        registry.record_failure("flaky")
        registry.record_failure("flaky")
        registry.record_success("flaky")
        assert registry.record_failure("flaky") is False

    def test_re_enabling_clears_the_quarantine(self, root: Path) -> None:
        """Otherwise a user who fixed the module has no way to say so."""
        registry = self._registry(root)
        for _ in range(3):
            registry.record_failure("flaky")
        registry.set_enabled("flaky", True)
        module = registry.get("flaky")
        assert module is not None
        assert module.state == "loaded"
        assert module.failures == 0
        assert registry.active()

    def test_disabling_a_quarantined_module_leaves_it_quarantined(self, root: Path) -> None:
        """Turning something off is not a claim that it was fixed."""
        registry = self._registry(root)
        for _ in range(3):
            registry.record_failure("flaky")
        registry.set_enabled("flaky", False)
        module = registry.get("flaky")
        assert module is not None
        assert module.state == "quarantined"

    def test_recording_against_an_unknown_module_is_harmless(self, root: Path) -> None:
        """Hook failures race a reload that removed the module."""
        registry = self._registry(root)
        assert registry.record_failure("gone") is False
        registry.record_success("gone")
        registry.quarantine("gone", "why")


class TestModuleStore:
    """REQ MOD-022 — per-module persistence that cannot slow browsing down.

    Writes are queued to a background thread and flushed on reload and
    shutdown, so a test that reopens the file has to flush first (SEP_5_REVIEW
    F-13). That the flush is *needed* is the point: before it, the SQLite write
    happened on the proxy event loop.
    """

    def test_a_value_survives_a_new_store_on_the_same_file(self, tmp_path: Path) -> None:
        store = ModuleStore(tmp_path / "s.db", "tidy")
        store.set("seen", 3)
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "tidy").get("seen") == 3

    def test_modules_cannot_see_each_others_keys(self, tmp_path: Path) -> None:
        """Shared storage would make one module's bookkeeping another's bug."""
        store = ModuleStore(tmp_path / "s.db", "alpha")
        store.set("k", "alpha")
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "beta").get("k") is None

    def test_a_missing_key_returns_the_default(self, tmp_path: Path) -> None:
        assert ModuleStore(tmp_path / "s.db", "tidy").get("nope", "fallback") == "fallback"

    def test_delete_removes_it_from_disk_too(self, tmp_path: Path) -> None:
        store = ModuleStore(tmp_path / "s.db", "tidy")
        store.set("k", 1)
        store.flush()
        store.delete("k")
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "tidy").get("k") is None

    def test_deleting_something_absent_is_not_an_error(self, tmp_path: Path) -> None:
        ModuleStore(tmp_path / "s.db", "tidy").delete("never-existed")

    def test_structured_values_round_trip(self, tmp_path: Path) -> None:
        store = ModuleStore(tmp_path / "s.db", "tidy")
        store.set("k", {"a": [1, 2]})
        store.flush()
        assert ModuleStore(tmp_path / "s.db", "tidy").get("k") == {"a": [1, 2]}

    def test_an_unserialisable_value_still_reads_back_in_this_process(self, tmp_path: Path) -> None:
        """Persistence is best-effort; a module must not crash because it
        stashed something exotic."""
        store = ModuleStore(tmp_path / "s.db", "tidy")
        store.set("k", object())
        store.flush()
        assert store.get("k") is not None
        assert store.last_error is not None
        assert ModuleStore(tmp_path / "s.db", "tidy").get("k") is None

    def test_keys_are_listed_in_order(self, tmp_path: Path) -> None:
        store = ModuleStore(tmp_path / "s.db", "tidy")
        store.set("b", 1)
        store.set("a", 1)
        assert store.keys() == ("a", "b")

    def test_a_broken_store_file_leaves_the_module_working(self, tmp_path: Path) -> None:
        """A corrupt convenience database is not a reason to refuse to load a
        module."""
        path = tmp_path / "s.db"
        path.write_bytes(b"this is not a database")
        store = ModuleStore(path, "tidy")
        assert store.get("k") is None


class TestModuleContextStorage:
    def test_the_context_reads_and_writes_through_to_the_store(self, tmp_path: Path) -> None:
        context = ModuleContext(
            name="tidy", version="1", store=ModuleStore(tmp_path / "s.db", "tidy")
        )
        context.store_set("k", "v")
        assert context.store_get("k") == "v"
        context.store_delete("k")
        assert context.store_get("k", "gone") == "gone"

    def test_a_context_without_a_store_is_still_usable(self, tmp_path: Path) -> None:
        """Dry-run and test contexts have no store, and module code should not
        have to know which kind it got."""
        context = ModuleContext(name="tidy", version="1")
        context.store_set("k", "v")
        context.store_delete("k")
        assert context.store_get("k", "default") == "default"


class TestAssetPaths:
    """implementation-plan.md §2.5 — containment survives symlinks."""

    @pytest.fixture
    def context(self, tmp_path: Path) -> ModuleContext:
        assets = tmp_path / "tidy" / "assets"
        (assets / "nested").mkdir(parents=True)
        (assets / "style.css").write_text("body {}")
        (assets / "nested" / "deep.txt").write_text("deep")
        return ModuleContext(name="tidy", version="1", assets=assets)

    def test_a_file_inside_assets_resolves(self, context: ModuleContext) -> None:
        assert context.asset_text("style.css") == "body {}"

    def test_a_nested_file_resolves(self, context: ModuleContext) -> None:
        assert context.asset_bytes("nested/deep.txt") == b"deep"

    def test_a_parent_traversal_is_refused(self, context: ModuleContext) -> None:
        with pytest.raises(AssetPathError):
            context.asset_path("../../secrets.txt")

    def test_an_absolute_path_is_refused(self, context: ModuleContext) -> None:
        with pytest.raises(AssetPathError, match="must be relative"):
            context.asset_path("/etc/passwd")

    def test_a_symlink_pointing_outside_is_refused(
        self, context: ModuleContext, tmp_path: Path
    ) -> None:
        """The case a prefix check misses: the path looks contained and the
        bytes come from somewhere else entirely."""
        outside = tmp_path / "secrets.txt"
        outside.write_text("secret")
        (tmp_path / "tidy" / "assets" / "escape.txt").symlink_to(outside)
        with pytest.raises(AssetPathError, match="escapes"):
            context.asset_path("escape.txt")

    def test_a_symlink_staying_inside_is_allowed(self, context: ModuleContext) -> None:
        assets = context.asset_path("style.css").parent
        (assets / "alias.css").symlink_to(assets / "style.css")
        assert context.asset_text("alias.css") == "body {}"

    def test_a_module_with_no_assets_says_so(self) -> None:
        context = ModuleContext(name="tidy", version="1")
        with pytest.raises(AssetPathError, match="no assets"):
            context.asset_path("style.css")


class TestContextMatching:
    """Convenience matching, so module code does not reimplement globbing."""

    @pytest.fixture
    def context(self) -> ModuleContext:
        return ModuleContext(name="tidy", version="1")

    def test_no_criteria_matches_everything(self, context: ModuleContext) -> None:
        assert context.matches(request()) is True

    def test_a_host_glob_matches(self, context: ModuleContext) -> None:
        assert context.matches(request(), host="*.example.com")

    def test_host_matching_ignores_case(self, context: ModuleContext) -> None:
        """Hosts are case-insensitive, and a module author writing 'CDN' should
        not get silence."""
        assert context.matches(request(host="CDN.Example.com"), host="cdn.example.com")

    def test_a_non_matching_host_fails(self, context: ModuleContext) -> None:
        assert context.matches(request(), host="*.other.test") is False

    def test_path_is_a_regex_search(self, context: ModuleContext) -> None:
        assert context.matches(request(), path=r"\.js$")
        assert context.matches(request(), path=r"\.css$") is False

    def test_method_matching_is_case_insensitive(self, context: ModuleContext) -> None:
        assert context.matches(request(), method="get")

    def test_dest_matching(self, context: ModuleContext) -> None:
        assert context.matches(request(), dest="script")
        assert context.matches(request(), dest="document") is False

    def test_content_type_comes_from_the_response_when_given(self, context: ModuleContext) -> None:
        response = NormalizedResponse(
            flow_id="f", timestamp="t", status=200, headers=(("content-type", "text/html"),)
        )
        assert context.matches(request(), content_type="text/html", response=response)

    def test_a_missing_content_type_does_not_match(self, context: ModuleContext) -> None:
        assert context.matches(request(), content_type="text/html") is False

    def test_criteria_are_combined_with_and(self, context: ModuleContext) -> None:
        assert context.matches(request(), host="*.example.com", method="POST") is False


class TestContextReporting:
    """note() and log() — what a module tells the rest of the system."""

    def test_a_note_is_recorded_with_its_code_and_severity(self) -> None:
        context = ModuleContext(name="tidy", version="1")
        context.note("module_error", "something", severity="error", detail_key=1)
        code, severity, message, detail = context.notes[0]
        assert (code, severity, message) == (NoteCode.MODULE_ERROR, Severity.ERROR, "something")
        assert detail["detail_key"] == 1

    def test_an_unknown_note_code_becomes_a_module_error(self) -> None:
        """A module inventing a code should still get its message through
        rather than raising inside a hook."""
        context = ModuleContext(name="tidy", version="1")
        context.note("not-a-real-code", "message")
        code, _, _, detail = context.notes[0]
        assert code is NoteCode.MODULE_ERROR
        assert detail["requested_code"] == "not-a-real-code"

    def test_an_unknown_severity_falls_back_to_warning(self) -> None:
        context = ModuleContext(name="tidy", version="1")
        context.note("module_error", "m", severity="catastrophic")
        assert context.notes[0][1] is Severity.WARNING

    def test_logs_keep_their_structured_fields(self) -> None:
        context = ModuleContext(name="tidy", version="1")
        context.log("info", "hello", host="a.example")
        assert context.logs == (("info", "hello", {"host": "a.example"}),)

    def test_draining_clears_both(self) -> None:
        """The context outlives one flow, so notes left behind would be
        attributed to the next flow as well."""
        context = ModuleContext(name="tidy", version="1")
        context.log("info", "hello")
        context.note("module_error", "m")
        context.drain()
        assert context.logs == () and context.notes == ()


class TestRegisterTransform:
    def test_a_module_transform_is_expensive_unless_it_says_otherwise(self) -> None:
        """We know nothing about it. Assuming it is fast on the proxy's event
        loop is how one module makes every page slow."""
        registry = TransformRegistry()
        ModuleContext(name="tidy", version="1", registry=registry).register_transform(
            "mine", lambda *a: None
        )
        assert registry.get("mine").cost is Cost.EXPENSIVE

    def test_a_declared_cost_is_honoured(self) -> None:
        registry = TransformRegistry()
        ModuleContext(name="tidy", version="1", registry=registry).register_transform(
            "mine", lambda *a: None, cost="cheap"
        )
        assert registry.get("mine").cost is Cost.CHEAP

    def test_an_unrecognised_cost_falls_back_to_expensive(self) -> None:
        registry = TransformRegistry()
        ModuleContext(name="tidy", version="1", registry=registry).register_transform(
            "mine", lambda *a: None, cost="instant"
        )
        assert registry.get("mine").cost is Cost.EXPENSIVE

    def test_registering_without_a_registry_is_a_no_op(self) -> None:
        """Dry-run contexts have no registry, and a module should not crash for
        being evaluated rather than run."""
        ModuleContext(name="tidy", version="1").register_transform("mine", lambda *a: None)


class TestSyntheticResponses:
    def test_a_synthesised_response_is_attributed_to_the_module(self) -> None:
        """Provenance has to name what produced the bytes the page received."""
        response = ModuleContext(name="tidy", version="1").synthesize(body="hi")
        assert response.origin == "tidy"
        assert response.body == b"hi"

    def test_a_synthesised_response_is_never_cached(self) -> None:
        """A cached synthetic response would outlive the module that made it."""
        response = ModuleContext(name="tidy", version="1").synthesize()
        assert ("cache-control", "no-store") in response.headers

    def test_a_content_type_is_declared_first(self) -> None:
        response = ModuleContext(name="tidy", version="1").synthesize(content_type="text/css")
        assert response.headers[0] == ("content-type", "text/css")

    def test_a_stub_matches_the_requested_destination(self) -> None:
        """The same derivation the block action uses, so a module-produced stub
        is indistinguishable from a rule-produced one."""
        context = ModuleContext(name="tidy", version="1")
        response = context.stub_for("script", request())
        assert response.origin == "tidy"
        assert response.status == 200


class TestWebSocketHookIsActuallyCalled:
    """REQ MOD-021, PXY-051.

    ``on_websocket_message`` was a declared hook name that nothing invoked. A
    module defining it loaded cleanly, reported healthy, and did nothing — the
    exact silent failure the provenance design exists to prevent. These assert
    it runs, that a raise is contained, and that a return value is ignored.
    """

    @staticmethod
    def _registry(tmp_path: Path, python: str) -> Any:
        from pporlock.engine.modules.registry import ModuleRegistry

        directory = tmp_path / "wsmod"
        directory.mkdir(parents=True)
        (directory / "module.yaml").write_text("name: wsmod\npporlock_api: '1'\nenabled: true\n")
        (directory / "module.py").write_text(python)
        registry = ModuleRegistry(tmp_path, store_path=tmp_path / "store.db")
        registry.reload()
        return registry

    @staticmethod
    def _message() -> Any:
        from pporlock.engine.models import WebSocketMessage

        return WebSocketMessage(
            flow_id="f1",
            index=0,
            timestamp="2026-08-28T00:00:00Z",
            direction="outgoing",
            opcode="text",
            payload=b'{"type":"ping"}',
        )

    def test_the_hook_is_invoked(self, tmp_path: Path) -> None:
        registry = self._registry(
            tmp_path,
            "SEEN = []\n"
            "def on_websocket_message(msg, req, ctx):\n"
            "    SEEN.append(msg.payload)\n"
            "    ctx.log('info', 'saw a frame', size=msg.size)\n",
        )
        ev = Evaluator(registry=registry)
        b = ProvenanceBuilder("default")
        ev.observe_websocket_message(self._message(), request(), b)

        module = registry.get("wsmod")
        assert module is not None and module.python is not None
        assert module.python.SEEN == [b'{"type":"ping"}']

    def test_a_note_from_the_hook_reaches_provenance(self, tmp_path: Path) -> None:
        registry = self._registry(
            tmp_path,
            "def on_websocket_message(msg, req, ctx):\n"
            "    ctx.note('module_error', 'frame looked wrong', severity='warning')\n",
        )
        ev = Evaluator(registry=registry)
        b = ProvenanceBuilder("default")
        ev.observe_websocket_message(self._message(), request(), b)
        prov = b.build()
        assert prov.has_note(NoteCode.MODULE_ERROR)
        assert any(n.module == "wsmod" for n in prov.notes)

    def test_a_raising_hook_is_contained_and_attributed(self, tmp_path: Path) -> None:
        """A module must not be able to break a socket that is working."""
        registry = self._registry(
            tmp_path,
            "def on_websocket_message(msg, req, ctx):\n    raise RuntimeError('boom')\n",
        )
        ev = Evaluator(registry=registry)
        b = ProvenanceBuilder("default")
        ev.observe_websocket_message(self._message(), request(), b)  # does not raise
        prov = b.build()
        assert prov.has_note(NoteCode.MODULE_ERROR)
        assert any("boom" in n.message for n in prov.notes)

    def test_a_returned_mutation_is_ignored(self, tmp_path: Path) -> None:
        """Frames are inspection-only in v1. A module that believes it rewrote
        one should find that it did not, rather than find provenance claiming a
        change the wire never saw."""
        registry = self._registry(
            tmp_path,
            "from pporlock.engine.models import ResponseMutation\n"
            "def on_websocket_message(msg, req, ctx):\n"
            "    return ResponseMutation(body=b'rewritten')\n",
        )
        ev = Evaluator(registry=registry)
        b = ProvenanceBuilder("default")
        ev.observe_websocket_message(self._message(), request(), b)
        prov = b.build()
        assert not prov.has_note(NoteCode.MODULE_ERROR)
        assert prov.entries == ()

    def test_no_registry_is_not_an_error(self) -> None:
        Evaluator().observe_websocket_message(
            self._message(), request(), ProvenanceBuilder("default")
        )


class TestTheTwoTiersCompose:
    """REQ MOD-023. A hook and a declarative rule on the same body must not
    overwrite one another.

    They did. Transforms ran, then hooks ran against the *original* response,
    and then the transform result was written over ``decision.mutation.body`` —
    so a hook that edited the body had its edit silently discarded whenever any
    body rule also matched, while provenance recorded the hook as applied.
    """

    @staticmethod
    def _registry(tmp_path: Path) -> Any:
        from pporlock.engine.modules.registry import ModuleRegistry

        directory = tmp_path / "both"
        directory.mkdir(parents=True)
        (directory / "module.yaml").write_text(
            "name: both\n"
            "pporlock_api: '1'\n"
            "enabled: true\n"
            "rules:\n"
            "  - name: add-a-marker\n"
            "    action: body\n"
            "    match: {content_type: 'text/html'}\n"
            "    transform: {kind: replace_literal, find: 'ORIGINAL', replace: 'BY-RULE'}\n"
        )
        (directory / "module.py").write_text(
            "from pporlock.engine.models import ResponseMutation\n"
            "def on_response(request, response, ctx):\n"
            "    text = response.text\n"
            "    if text is None:\n"
            "        return None\n"
            "    return ResponseMutation(body=(text + '<!--BY-HOOK-->').encode())\n"
        )
        registry = ModuleRegistry(tmp_path, store_path=tmp_path / "store.db")
        registry.reload()
        return registry

    def _run(self, tmp_path: Path) -> bytes:
        registry = self._registry(tmp_path)
        ev = Evaluator(registry.build_ruleset(["both"]), registry=registry)
        decision = ev.evaluate_response_body(
            request(),
            NormalizedResponse(
                flow_id="f1",
                timestamp="2026-08-28T00:00:00Z",
                status=200,
                headers=(("content-type", "text/html"),),
                body=b"<html><body>ORIGINAL</body></html>",
            ),
            ProvenanceBuilder("default"),
        )
        assert decision.mutation.body is not None
        return decision.mutation.body

    def test_both_edits_survive(self, tmp_path: Path) -> None:
        body = self._run(tmp_path)
        assert b"BY-RULE" in body
        assert b"BY-HOOK" in body
        assert b"ORIGINAL" not in body

    def test_the_hook_sees_what_the_rule_produced(self, tmp_path: Path) -> None:
        """Not merely both-present: the hook must read the transformed body,
        or the two tiers are composing by luck rather than by order."""
        body = self._run(tmp_path)
        assert body.index(b"BY-RULE") < body.index(b"BY-HOOK")
