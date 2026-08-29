"""Persisted module enablement — OI-8.

Enablement is user state that the *manifest* cannot hold: manifests ship
``enabled: false`` and the daemon must not rewrite a file the author owns. So it
goes in a sidecar, and the thing worth testing is that it outlives the process —
which is exactly what a test that keeps one registry alive cannot show. Every
persistence test here builds a registry, changes something, **drops it**, and
builds a fresh one from the same directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.modules.state import STATE_FILENAME, ModuleStateStore

MANIFEST = "name: {name}\npporlock_api: '1'\nenabled: false\npriority: 100\n"


def write_module(root: Path, name: str, body: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "module.yaml").write_text(body or MANIFEST.format(name=name))
    return directory


def build(tmp_path: Path) -> ModuleRegistry:
    """A registry over the same root and the same sidecar, every time.

    Standing in for a daemon restart: nothing is carried over in memory.
    """
    registry = ModuleRegistry(tmp_path / "modules", state_path=tmp_path / STATE_FILENAME)
    registry.reload()
    return registry


class TestPersistence:
    def test_enablement_survives_a_restart(self, tmp_path: Path) -> None:
        """OI-8. The whole point: a restart used to reset every module to its
        manifest default, which for a shipped module is off."""
        write_module(tmp_path / "modules", "csp")

        first = build(tmp_path)
        assert first.get("csp") is not None
        assert first.get("csp").enabled is False  # type: ignore[union-attr]
        first.set_enabled("csp", True)

        del first
        second = build(tmp_path)
        assert second.get("csp").enabled is True  # type: ignore[union-attr]

    def test_priority_survives_a_restart(self, tmp_path: Path) -> None:
        """Ordering is user state for the same reason enablement is (REQ MOD-023)."""
        write_module(tmp_path / "modules", "csp")

        first = build(tmp_path)
        first.set_priority("csp", 10)
        del first

        second = build(tmp_path)
        module = second.get("csp")
        assert module is not None
        assert module.priority == 10

    def test_disabling_survives_a_restart(self, tmp_path: Path) -> None:
        """A manifest that says ``enabled: true`` must not undo the user's off.

        Otherwise turning a noisy module off would last until the next restart,
        and the module would come back on by itself.
        """
        write_module(
            tmp_path / "modules",
            "loud",
            "name: loud\npporlock_api: '1'\nenabled: true\n",
        )
        first = build(tmp_path)
        assert first.get("loud").enabled is True  # type: ignore[union-attr]
        first.set_enabled("loud", False)
        del first

        assert build(tmp_path).get("loud").enabled is False  # type: ignore[union-attr]

    def test_the_manifest_seeds_once_and_the_sidecar_wins_after(self, tmp_path: Path) -> None:
        """Editing ``enabled:`` after first sight does nothing, on purpose.

        The alternative — the manifest winning on every load — would mean the
        API could never turn a module off for longer than one reload.
        """
        root = tmp_path / "modules"
        write_module(root, "csp", "name: csp\npporlock_api: '1'\nenabled: true\n")
        assert build(tmp_path).get("csp").enabled is True  # type: ignore[union-attr]

        write_module(root, "csp", "name: csp\npporlock_api: '1'\nenabled: false\n")
        assert build(tmp_path).get("csp").enabled is True  # type: ignore[union-attr]

    def test_the_daemon_never_rewrites_the_manifest(self, tmp_path: Path) -> None:
        """The firm constraint. A manifest is the author's file, comments and all."""
        root = tmp_path / "modules"
        manifest = root / "csp" / "module.yaml"
        write_module(
            root,
            "csp",
            "# hand-written, with a comment worth keeping\nname: csp\npporlock_api: '1'\n"
            "enabled: false\n",
        )
        before = manifest.read_bytes()

        registry = build(tmp_path)
        registry.set_enabled("csp", True)
        registry.set_priority("csp", 5)
        registry.reload()

        assert manifest.read_bytes() == before

    def test_a_change_persists_immediately_not_at_shutdown(self, tmp_path: Path) -> None:
        """This daemon is a launchd agent and gets killed rather than stopped."""
        write_module(tmp_path / "modules", "csp")
        registry = build(tmp_path)
        registry.set_enabled("csp", True)

        on_disk = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert on_disk["csp"]["enabled"] is True


class TestPruning:
    def test_a_deleted_module_does_not_leave_state_behind(self, tmp_path: Path) -> None:
        write_module(tmp_path / "modules", "csp")
        registry = build(tmp_path)
        registry.set_enabled("csp", True)
        assert "csp" in registry.state

        import shutil

        shutil.rmtree(tmp_path / "modules" / "csp")
        registry.reload()

        assert "csp" not in registry.state
        assert json.loads((tmp_path / STATE_FILENAME).read_text()) == {}

    def test_a_reinstalled_module_does_not_come_back_enabled(self, tmp_path: Path) -> None:
        """A decision made about one module's code should not silently carry
        over to whatever is installed under the same name later."""
        import shutil

        write_module(tmp_path / "modules", "csp")
        first = build(tmp_path)
        first.set_enabled("csp", True)
        shutil.rmtree(tmp_path / "modules" / "csp")
        first.reload()
        del first

        write_module(tmp_path / "modules", "csp")
        assert build(tmp_path).get("csp").enabled is False  # type: ignore[union-attr]

    def test_a_module_that_failed_to_load_keeps_its_state(self, tmp_path: Path) -> None:
        """A broken manifest is a typo, not a deletion. Losing the user's
        enablement because they mistyped a key would be a second failure on top
        of the first."""
        root = tmp_path / "modules"
        write_module(root, "csp")
        first = build(tmp_path)
        first.set_enabled("csp", True)
        del first

        write_module(root, "csp", "name: different-name\n")
        second = build(tmp_path)
        assert second.get("csp").error is not None  # type: ignore[union-attr]
        assert "csp" in second.state


class TestCorruptSidecar:
    """A malformed sidecar is skipped, not fatal — as a malformed profile is."""

    @pytest.mark.parametrize(
        "content",
        [
            "{ not json",
            "[]",
            '"a string"',
            '{"csp": "not an object"}',
            '{"csp": {"enabled": "yes", "priority": 1}}',
            '{"csp": {"enabled": true, "priority": true}}',
            '{"csp": {"enabled": true}}',
        ],
        ids=[
            "invalid-json",
            "array",
            "scalar",
            "entry-not-an-object",
            "enabled-not-a-bool",
            "priority-is-a-bool",
            "priority-missing",
        ],
    )
    def test_a_corrupt_sidecar_falls_back_to_manifest_defaults(
        self, tmp_path: Path, content: str
    ) -> None:
        write_module(tmp_path / "modules", "csp", "name: csp\npporlock_api: '1'\nenabled: true\n")
        (tmp_path / STATE_FILENAME).write_text(content)

        registry = build(tmp_path)
        assert registry.get("csp").enabled is True  # type: ignore[union-attr]

    def test_an_unreadable_sidecar_says_so(self, tmp_path: Path) -> None:
        """Silently reverting every module to its default would look like the
        modules had stopped working. The runner prints this at startup."""
        (tmp_path / STATE_FILENAME).write_text("{ not json")
        store = ModuleStateStore(tmp_path / STATE_FILENAME)
        assert store.error is not None
        assert STATE_FILENAME in store.error

    def test_a_well_formed_sidecar_reports_no_error(self, tmp_path: Path) -> None:
        (tmp_path / STATE_FILENAME).write_text('{"csp": {"enabled": true, "priority": 3}}')
        store = ModuleStateStore(tmp_path / STATE_FILENAME)
        assert store.error is None
        assert store.get("csp") is not None

    def test_an_unwritable_sidecar_does_not_raise(self, tmp_path: Path) -> None:
        """The in-memory change already succeeded; only the next restart loses
        it. Raising would fail an API call whose actual work was done."""
        blocked = tmp_path / "not-a-dir" / STATE_FILENAME
        (tmp_path / "not-a-dir").write_text("I am a file")

        store = ModuleStateStore(blocked)
        store.set("csp", enabled=True, priority=1)

        assert store.error is not None
        assert store.get("csp") is not None


class TestNoSecrets:
    """The sidecar carries no secret, so there is no redaction pass over it.

    Confirmed rather than assumed: this asserts the written shape, so a later
    change that started stashing anything else in here fails here first.
    """

    def test_the_file_holds_only_a_name_a_bool_and_an_int(self, tmp_path: Path) -> None:
        write_module(tmp_path / "modules", "csp")
        write_module(tmp_path / "modules", "hsts")
        registry = build(tmp_path)
        registry.set_enabled("csp", True)
        registry.set_priority("hsts", 42)

        raw = json.loads((tmp_path / STATE_FILENAME).read_text())
        assert set(raw) == {"csp", "hsts"}
        for entry in raw.values():
            assert set(entry) == {"enabled", "priority"}
            assert isinstance(entry["enabled"], bool)
            assert isinstance(entry["priority"], int)

    def test_the_file_is_written_atomically(self, tmp_path: Path) -> None:
        """A crash mid-write must leave the previous file, not a truncated one
        that would read as corrupt and silently reset every module."""
        write_module(tmp_path / "modules", "csp")
        registry = build(tmp_path)
        registry.set_enabled("csp", True)

        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestDefaultLocation:
    def test_the_sidecar_defaults_beside_the_module_root(self, tmp_path: Path) -> None:
        registry = ModuleRegistry(tmp_path / "modules")
        assert registry.state.path == tmp_path / STATE_FILENAME
