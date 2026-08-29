"""Declared module settings — the manifest declaration, its persistence, and
what a module actually sees in ``ctx.config``.

Three things are worth pinning, and they are the three that would break
silently:

* a bad *declaration* is a load error, not a form that opens broken;
* a bad *value* is refused whole, so a form with one bad field does not half
  apply and leave the module running on a mixture;
* a value the user set outlives the process and outlives a reload — the same
  promise OI-8 made for enablement, for the same reason.

REQ MOD-004/MOD-014/MOD-020, SPEC-0 §8.1 (an optional manifest key is additive).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pporlock.engine.modules.loader import load_module
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.modules.settings import (
    ModuleSetting,
    SettingOption,
    SettingsError,
    coerce_config,
    coerce_value,
    effective_config,
    parse_settings,
)
from pporlock.engine.modules.state import STATE_FILENAME, ModuleStateStore
from pporlock.engine.modules.validate import validate_module_files

MANIFEST = """
name: settable
version: "1.0.0"
pporlock_api: "1"
description: a module with settings
priority: 55

config:
  greeting: from-the-manifest

settings:
  - key: greeting
    label: Greeting
    default: from-the-field
  - key: loud
    type: boolean
    default: true
  - key: repeats
    type: integer
    default: 2
    min: 1
    max: 9
  - key: mode
    type: enum
    default: b
    options: [a, b, c]
  - key: hosts
    type: string_list
    default: ["*"]
"""

PYTHON = """
SEEN = []


def on_load(ctx):
    SEEN.append(("load", dict(ctx.config)))


def on_config(ctx):
    SEEN.append(("config", dict(ctx.config)))
"""


def write_module(root: Path, name: str = "settable", manifest: str = MANIFEST) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "module.yaml").write_text(manifest.replace("name: settable", f"name: {name}", 1))
    return path


# --------------------------------------------------------------------------


class TestTheDeclaration:
    def test_a_field_with_no_default_gets_one_its_own_type_accepts(self) -> None:
        """Not None. A module reading `ctx.config["mode"]` should never get a
        value that is not one of the choices its own form offers."""
        settings = parse_settings(
            [
                {"key": "a", "type": "boolean"},
                {"key": "b", "type": "integer", "min": 3},
                {"key": "c", "type": "enum", "options": ["x", "y"]},
                {"key": "d", "type": "string_list"},
                {"key": "e"},
            ]
        )
        assert [s.default for s in settings] == [False, 3, "x", [], ""]

    def test_a_bare_string_option_is_its_own_label(self) -> None:
        (setting,) = parse_settings([{"key": "k", "type": "enum", "options": ["one"]}])
        assert setting.options == (SettingOption(value="one", label="one"),)

    @pytest.mark.parametrize(
        "declaration,fragment",
        [
            ({"key": "Nope"}, "must match"),
            ({"key": "k", "type": "colour"}, "unknown type"),
            ({"key": "k", "type": "enum"}, "non-empty 'options'"),
            ({"key": "k", "type": "enum", "options": ["a", "a"]}, "duplicate option"),
            ({"key": "k", "options": ["a"]}, "only meaningful on an enum"),
            ({"key": "k", "min": 1}, "only meaningful on an integer"),
            ({"key": "k", "defualt": "typo"}, "unknown keys"),
            ({"key": "k", "type": "integer", "default": "3"}, "invalid default"),
            ({"key": "k", "type": "integer", "min": 5, "default": 1}, "invalid default"),
        ],
    )
    def test_a_malformed_declaration_is_refused_with_a_reason(
        self, declaration: dict, fragment: str
    ) -> None:
        with pytest.raises(SettingsError, match=fragment):
            parse_settings([declaration])

    def test_a_key_declared_twice_is_refused(self) -> None:
        with pytest.raises(SettingsError, match="declared twice"):
            parse_settings([{"key": "k"}, {"key": "k"}])

    def test_a_bad_declaration_fails_the_module_and_not_the_daemon(self, tmp_path: Path) -> None:
        """REQ MOD-005. The load error names settings, so the author is pointed
        at the block that is wrong rather than at the file."""
        path = write_module(
            tmp_path,
            manifest=MANIFEST.replace("type: boolean", "type: boolish"),
        )
        module = load_module(path)
        assert module.state == "load_error"
        assert module.error is not None
        assert module.error.code == "module_invalid_settings"
        assert "boolish" in module.error.message

    def test_validate_reports_it_before_anything_is_installed(self) -> None:
        """REQ API-027 — the same finding, from POST /validate, with a line."""
        report = validate_module_files(
            "settable", {"module.yaml": MANIFEST.replace("type: boolean", "type: boolish")}
        )
        assert not report.ok
        codes = [issue.code for issue in report.errors]
        assert "module_invalid_settings" in codes
        placed = next(i for i in report.errors if i.code == "module_invalid_settings")
        assert placed.file == "module.yaml"
        assert placed.line is not None


class TestCoercingAValue:
    def setting(self, **kwargs: object) -> ModuleSetting:
        base: dict = {"key": "k", "type": "string", "label": "K"}
        base.update(kwargs)
        return ModuleSetting(**base)  # type: ignore[arg-type]

    def test_a_string_is_not_an_integer_and_a_true_is_not_a_one(self) -> None:
        """Strict rather than helpful. A "3" quietly accepted as 3 makes the
        stored shape depend on which client wrote it, and module code then has
        to handle both."""
        with pytest.raises(ValueError):
            coerce_value(self.setting(type="integer"), "3")
        with pytest.raises(ValueError):
            coerce_value(self.setting(type="integer"), True)
        with pytest.raises(ValueError):
            coerce_value(self.setting(type="boolean"), "true")

    def test_an_integer_outside_its_bounds_is_refused(self) -> None:
        setting = self.setting(type="integer", min=1, max=3)
        assert coerce_value(setting, 2) == 2
        with pytest.raises(ValueError, match="at most 3"):
            coerce_value(setting, 4)

    def test_a_string_list_drops_blank_entries(self) -> None:
        """A trailing newline in a textarea is not a validation failure."""
        setting = self.setting(type="string_list")
        assert coerce_value(setting, ["a.com", "  ", "", " b.com "]) == ["a.com", "b.com"]

    def test_an_enum_takes_only_what_it_offers(self) -> None:
        setting = self.setting(
            type="enum", options=(SettingOption("a", "a"), SettingOption("b", "b"))
        )
        assert coerce_value(setting, "b") == "b"
        with pytest.raises(ValueError, match="one of a, b"):
            coerce_value(setting, "z")

    def test_every_bad_field_is_reported_not_just_the_first(self) -> None:
        settings = parse_settings([{"key": "a", "type": "integer"}, {"key": "b"}])
        accepted, errors = coerce_config(settings, {"a": "x", "b": 1, "c": True})
        assert accepted == {}  # nothing is trusted while anything is wrong
        assert len(errors) == 3
        assert any("not a declared setting" in e for e in errors)

    def test_a_module_declaring_nothing_accepts_nothing(self) -> None:
        """This is what keeps `config:` the author's file. Without it, PATCH
        would be a second, hidden way to write a module's configuration."""
        _, errors = coerce_config((), {"anything": 1})
        assert errors == ["anything: not a declared setting"]


class TestTheEffectiveConfig:
    def test_the_manifest_beats_a_declared_default_and_the_user_beats_both(self) -> None:
        settings = parse_settings([{"key": "greeting", "default": "from-the-field"}])
        assert effective_config(settings, {}, None)["greeting"] == "from-the-field"
        assert effective_config(settings, {"greeting": "manifest"}, None)["greeting"] == "manifest"
        assert effective_config(settings, {"greeting": "manifest"}, {"greeting": "mine"}) == {
            "greeting": "mine"
        }

    def test_an_override_for_a_field_that_no_longer_exists_is_ignored(self) -> None:
        """A module rewritten since the value was set. Handing its code a key
        it never asked for is how a stale toggle survives a rename and quietly
        does nothing."""
        settings = parse_settings([{"key": "kept"}])
        assert effective_config(settings, {}, {"kept": "a", "gone": "b"}) == {"kept": "a"}

    def test_free_form_manifest_config_still_reaches_a_module_with_no_settings(self) -> None:
        """The pre-existing contract (SPEC-0 §8.2) is unchanged: declaring
        nothing must behave exactly as it did before this existed."""
        assert effective_config((), {"anything": [1, 2]}, None) == {"anything": [1, 2]}


class TestPersistence:
    def registry(self, tmp_path: Path) -> ModuleRegistry:
        write_module(tmp_path / "modules")
        registry = ModuleRegistry(
            tmp_path / "modules",
            store_path=tmp_path / "store.db",
            state_path=tmp_path / STATE_FILENAME,
        )
        registry.reload()
        return registry

    def test_a_set_value_reaches_ctx_config_without_reloading_the_module(
        self, tmp_path: Path
    ) -> None:
        """A reload would re-execute module.py and take `on_load` with it —
        turning "change a dropdown" into "restart the module", which for a
        module accumulating an audit is "throw the audit away"."""
        registry = self.registry(tmp_path)
        module, errors = registry.set_config("settable", {"mode": "c"})
        assert errors == []
        assert module is not None
        context = registry.context("settable")
        assert context is not None
        assert context.config["mode"] == "c"
        assert context.config["greeting"] == "from-the-manifest"

    def test_nothing_is_written_when_anything_is_refused(self, tmp_path: Path) -> None:
        registry = self.registry(tmp_path)
        _, errors = registry.set_config("settable", {"mode": "c", "repeats": 99})
        assert errors and "repeats" in errors[0]
        context = registry.context("settable")
        assert context is not None
        assert context.config["mode"] == "b", "a refused field must not let the good one through"
        assert json.loads((tmp_path / STATE_FILENAME).read_text())["settable"].get("config") is None

    def test_it_survives_a_restart_and_a_reload(self, tmp_path: Path) -> None:
        """OI-8's promise, extended to settings. A value that reverts on
        restart is worse than one that cannot be set: it looks like it worked."""
        registry = self.registry(tmp_path)
        registry.set_config("settable", {"mode": "c", "repeats": 7})

        fresh = ModuleRegistry(
            tmp_path / "modules",
            store_path=tmp_path / "store.db",
            state_path=tmp_path / STATE_FILENAME,
        )
        fresh.reload()
        context = fresh.context("settable")
        assert context is not None
        assert context.config["mode"] == "c"
        assert context.config["repeats"] == 7

        fresh.reload()
        context = fresh.context("settable")
        assert context is not None
        assert context.config["mode"] == "c"

    def test_toggling_enabled_does_not_clear_the_settings(self, tmp_path: Path) -> None:
        """`set` is called on every reload to seed a row; a seed that reset the
        config would undo the user's settings on the next restart."""
        registry = self.registry(tmp_path)
        registry.set_config("settable", {"mode": "c"})
        registry.set_enabled("settable", True)
        registry.set_priority("settable", 12)
        stored = json.loads((tmp_path / STATE_FILENAME).read_text())["settable"]
        assert stored == {"enabled": True, "priority": 12, "config": {"mode": "c"}}

    def test_omitting_a_key_resets_it(self, tmp_path: Path) -> None:
        """Replace, not merge. Under a merge, "put this back to its default"
        would be inexpressible."""
        registry = self.registry(tmp_path)
        registry.set_config("settable", {"mode": "c", "repeats": 7})
        registry.set_config("settable", {"repeats": 7})
        context = registry.context("settable")
        assert context is not None
        assert context.config["mode"] == "b"

    def test_the_sidecar_holds_no_config_key_for_a_module_with_no_overrides(
        self, tmp_path: Path
    ) -> None:
        """Every module gets a row on first sighting; `"config": {}` on all of
        them is noise in a file people do read."""
        registry = self.registry(tmp_path)
        registry.set_enabled("settable", True)
        assert json.loads((tmp_path / STATE_FILENAME).read_text()) == {
            "settable": {"enabled": True, "priority": 55}
        }

    def test_a_hand_broken_config_block_does_not_cost_the_toggle(self, tmp_path: Path) -> None:
        """Losing a module's enablement because someone edited the settings
        into a list would be a disproportionate response to a typo."""
        path = tmp_path / STATE_FILENAME
        path.write_text(json.dumps({"m": {"enabled": True, "priority": 3, "config": ["oops"]}}))
        store = ModuleStateStore(path)
        state = store.get("m")
        assert state is not None
        assert state.enabled is True
        assert state.config == {}

    def test_the_sidecar_is_not_world_readable(self, tmp_path: Path) -> None:
        """No setting type is a secret today (there is deliberately no password
        type), and this is what keeps that from being the only thing standing
        between a future one and a world-readable file."""
        path = tmp_path / STATE_FILENAME
        store = ModuleStateStore(path)
        store.set("m", enabled=True, priority=1)
        assert path.stat().st_mode & 0o077 == 0


class TestTheOnConfigHook:
    def test_it_runs_when_settings_change_and_not_otherwise(self, tmp_path: Path) -> None:
        """For modules that derive something from config at load time. The hook
        is optional — a module reading `ctx.config` per flow needs nothing."""
        path = write_module(tmp_path / "modules")
        (path / "module.py").write_text(PYTHON)
        registry = ModuleRegistry(
            tmp_path / "modules",
            store_path=tmp_path / "store.db",
            state_path=tmp_path / STATE_FILENAME,
        )
        registry.reload()
        module = registry.get("settable")
        assert module is not None

        assert [event for event, _ in module.python.SEEN] == ["load"]
        registry.set_config("settable", {"mode": "a"})
        assert [event for event, _ in module.python.SEEN] == ["load", "config"]
        assert module.python.SEEN[-1][1]["mode"] == "a"

        registry.set_config("settable", {"mode": "zzz"})
        assert [event for event, _ in module.python.SEEN] == ["load", "config"], (
            "a refused change is not a change"
        )
