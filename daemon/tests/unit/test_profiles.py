"""Profiles. SPEC-1 §5.6, SPEC-0 §5.7, REQ MOD-040-044.

A profile is a working context, not just a list of modules, and exactly one is
active. The invariant these tests exist to protect is that ``default`` always
exists and cannot be removed (REQ MOD-041): there is no state in which the
daemon has no profile at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from pporlock.engine.profiles import DEFAULT_PROFILE, Profile, ProfileManager
from pporlock.errors import ConfigError


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "profiles"


@pytest.fixture
def manager(root: Path) -> ProfileManager:
    return ProfileManager(root)


def write_profile(root: Path, filename: str, body: Any) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_text(body if isinstance(body, str) else yaml.safe_dump(body, sort_keys=False))
    return path


class TestParsingAProfile:
    """Strict, for the same reason module manifests are: a key that is quietly
    ignored is a setting its author believes is in force."""

    def test_a_minimal_profile_needs_only_a_name(self) -> None:
        assert Profile.from_dict({"name": "debug"}).name == "debug"

    def test_toggles_default_to_off(self) -> None:
        """A profile that says nothing about anticache must not turn it on."""
        assert Profile.from_dict({"name": "debug"}).dev_toggles == {
            "anticache": False,
            "anticomp": False,
        }

    def test_declared_toggles_are_kept(self) -> None:
        profile = Profile.from_dict({"name": "debug", "dev_toggles": {"anticomp": True}})
        assert profile.dev_toggles == {"anticache": False, "anticomp": True}

    def test_an_unknown_toggle_is_dropped_rather_than_carried(self) -> None:
        """The toggle set is a contract with the addon; an unrecognised one
        would never be applied, so keeping it would only mislead."""
        profile = Profile.from_dict({"name": "debug", "dev_toggles": {"antigravity": True}})
        assert set(profile.dev_toggles) == {"anticache", "anticomp"}

    def test_an_unknown_key_is_refused_by_name(self) -> None:
        with pytest.raises(ConfigError, match="modules_add"):
            Profile.from_dict({"name": "debug", "modules_add": []})

    def test_a_name_with_a_slash_is_refused(self) -> None:
        """Names become filenames, so anything path-shaped is refused before it
        can be joined to a directory."""
        with pytest.raises(ConfigError, match="not a valid profile name"):
            Profile.from_dict({"name": "../escape"})

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(ConfigError):
            Profile.from_dict({"name": ""})

    def test_a_dict_round_trips(self) -> None:
        original = Profile.from_dict(
            {
                "name": "debug",
                "description": "for site x",
                "modules": ["tidy"],
                "dev_toggles": {"anticache": True},
                "exclusions_add": ["*.bank.example"],
            }
        )
        assert Profile.from_dict(original.to_dict()).to_dict() == original.to_dict()


class TestTheDefaultProfile:
    """REQ MOD-041 — there is always somewhere to fall back to."""

    def test_it_exists_before_anything_is_written(self, manager: ProfileManager) -> None:
        assert [p.name for p in manager.all_profiles()] == [DEFAULT_PROFILE]

    def test_it_is_active_on_a_fresh_manager(self, manager: ProfileManager) -> None:
        assert manager.active_name == DEFAULT_PROFILE

    def test_it_is_listed_first(self, manager: ProfileManager) -> None:
        """It is the one an operator returns to, so it does not get sorted into
        the middle of an alphabetical list."""
        manager.save(Profile(name="aaa"))
        assert next(p.name for p in manager.all_profiles()) == DEFAULT_PROFILE

    def test_it_cannot_be_written(self, manager: ProfileManager) -> None:
        """It is implicit — a file would let it be edited into something that is
        no longer a safe fallback."""
        with pytest.raises(ConfigError, match="implicit"):
            manager.save(Profile(name=DEFAULT_PROFILE))

    def test_it_cannot_be_deleted(self, manager: ProfileManager) -> None:
        with pytest.raises(ConfigError, match="cannot be deleted"):
            manager.delete(DEFAULT_PROFILE)

    def test_it_admits_every_module(self, manager: ProfileManager) -> None:
        """No filter at all, rather than a list that would have to be kept in
        step with the installed modules."""
        assert manager.module_filter() is None


class TestSavingAndReading:
    def test_a_saved_profile_is_readable_by_a_new_manager(self, root: Path) -> None:
        """Profiles outlive the process; they are settings, not session state."""
        ProfileManager(root).save(Profile(name="debug", modules=["tidy"]))
        profile = ProfileManager(root).get("debug")
        assert profile is not None
        assert profile.modules == ["tidy"]

    def test_saving_creates_the_directory(self, manager: ProfileManager, root: Path) -> None:
        manager.save(Profile(name="debug"))
        assert root.is_dir()

    def test_saving_again_replaces_the_previous_version(self, manager: ProfileManager) -> None:
        manager.save(Profile(name="debug", description="first"))
        manager.save(Profile(name="debug", description="second"))
        profile = manager.get("debug")
        assert profile is not None
        assert profile.description == "second"

    def test_an_unknown_profile_is_none_rather_than_an_error(self, manager: ProfileManager) -> None:
        assert manager.get("never-existed") is None

    def test_a_file_without_a_name_takes_it_from_the_filename(self, root: Path) -> None:
        """The filename is what an operator sees and edits, so it wins over an
        omission inside."""
        write_profile(root, "team.yaml", {"description": "shared"})
        assert ProfileManager(root).get("team") is not None

    def test_profiles_are_listed_alphabetically_after_the_default(
        self, manager: ProfileManager
    ) -> None:
        for name in ("zed", "alpha"):
            manager.save(Profile(name=name))
        assert [p.name for p in manager.all_profiles()] == [DEFAULT_PROFILE, "alpha", "zed"]


class TestMalformedProfilesAreSkipped:
    """One bad file must not cost the operator every other profile.

    The same reasoning as REQ MOD-005 for modules: partial function beats total
    failure, and the default is always there underneath.
    """

    def test_invalid_yaml_is_skipped(self, root: Path, manager: ProfileManager) -> None:
        write_profile(root, "broken.yaml", "modules: [unclosed\n")
        manager.save(Profile(name="good"))
        assert [p.name for p in manager.all_profiles()] == [DEFAULT_PROFILE, "good"]

    def test_an_unknown_key_skips_only_that_profile(
        self, root: Path, manager: ProfileManager
    ) -> None:
        write_profile(root, "broken.yaml", {"name": "broken", "nonsense": 1})
        manager.save(Profile(name="good"))
        assert [p.name for p in manager.all_profiles()] == [DEFAULT_PROFILE, "good"]

    def test_a_file_whose_name_is_not_a_valid_profile_name_is_skipped(self, root: Path) -> None:
        write_profile(root, "Not Valid.yaml", {"description": "x"})
        assert [p.name for p in ProfileManager(root).all_profiles()] == [DEFAULT_PROFILE]

    def test_an_empty_file_is_skipped(self, root: Path) -> None:
        write_profile(root, "empty.yaml", "")
        assert [p.name for p in ProfileManager(root).all_profiles()] == ["default", "empty"]

    def test_a_skipped_profile_does_not_break_lookup_of_the_others(
        self, root: Path, manager: ProfileManager
    ) -> None:
        write_profile(root, "broken.yaml", "  - not: a mapping\n")
        manager.save(Profile(name="good", modules=["tidy"]))
        assert manager.get("good") is not None


class TestDeleting:
    def test_deleting_removes_the_file(self, manager: ProfileManager, root: Path) -> None:
        manager.save(Profile(name="debug"))
        assert manager.delete("debug") is True
        assert not (root / "debug.yaml").exists()

    def test_deleting_something_absent_reports_that_it_was_not_there(
        self, manager: ProfileManager
    ) -> None:
        """Distinct from success, so the API can answer 404 rather than 204."""
        assert manager.delete("never-existed") is False

    def test_deleting_the_active_profile_falls_back_to_default(
        self, manager: ProfileManager
    ) -> None:
        """Otherwise the daemon would be pointing at a profile that no longer
        exists, with no module filter anyone could explain."""
        manager.save(Profile(name="debug"))
        manager.activate("debug")
        manager.delete("debug")
        assert manager.active_name == DEFAULT_PROFILE

    def test_deleting_an_inactive_profile_leaves_the_active_one_alone(
        self, manager: ProfileManager
    ) -> None:
        manager.save(Profile(name="debug"))
        manager.save(Profile(name="other"))
        manager.activate("debug")
        manager.delete("other")
        assert manager.active_name == "debug"


class TestActivating:
    def test_activating_makes_it_the_active_profile(self, manager: ProfileManager) -> None:
        manager.save(Profile(name="debug", description="for site x"))
        assert manager.activate("debug").description == "for site x"
        assert manager.active.name == "debug"

    def test_activating_an_unknown_profile_is_refused(self, manager: ProfileManager) -> None:
        """Silently falling back would leave the operator believing a context is
        applied when it is not."""
        with pytest.raises(ConfigError, match="no such profile"):
            manager.activate("never-existed")

    def test_a_failed_activation_leaves_the_previous_one_in_force(
        self, manager: ProfileManager
    ) -> None:
        manager.save(Profile(name="debug"))
        manager.activate("debug")
        with pytest.raises(ConfigError):
            manager.activate("never-existed")
        assert manager.active_name == "debug"

    def test_the_active_profile_narrows_the_module_set(self, manager: ProfileManager) -> None:
        """REQ MOD-043 — this list is what the registry filters against."""
        manager.save(Profile(name="debug", modules=["tidy"]))
        manager.activate("debug")
        assert manager.module_filter() == ["tidy"]

    def test_an_empty_module_list_means_no_modules_rather_than_all_of_them(
        self, manager: ProfileManager
    ) -> None:
        """The distinction that matters: None is 'do not filter', [] is 'run
        nothing'. A profile for reproducing a bug with modules off needs [] to
        mean what it says."""
        manager.save(Profile(name="clean", modules=[]))
        manager.activate("clean")
        assert manager.module_filter() == []

    def test_activating_default_again_removes_the_filter(self, manager: ProfileManager) -> None:
        manager.save(Profile(name="debug", modules=["tidy"]))
        manager.activate("debug")
        manager.activate(DEFAULT_PROFILE)
        assert manager.module_filter() is None

    def test_the_active_profile_survives_being_edited_on_disk(
        self, manager: ProfileManager
    ) -> None:
        """Profiles are read through rather than cached, so an edit takes effect
        without reactivating."""
        manager.save(Profile(name="debug", modules=["tidy"]))
        manager.activate("debug")
        manager.save(Profile(name="debug", modules=["tidy", "other"]))
        assert manager.module_filter() == ["tidy", "other"]

    def test_the_active_property_falls_back_when_the_file_vanishes(
        self, manager: ProfileManager, root: Path
    ) -> None:
        """A profile deleted out from under the daemon must not leave ``active``
        raising on the proxy's hot path."""
        manager.save(Profile(name="debug"))
        manager.activate("debug")
        (root / "debug.yaml").unlink()
        assert manager.active.name == DEFAULT_PROFILE


class TestTheActiveProfileSurvivesARestart:
    """Activation is user state, and a restart used to discard it.

    That mattered beyond the inconvenience: the active profile's
    ``exclusions_add`` are applied at startup (OI-9), so a daemon that always
    came back on ``default`` applied none of them — the feature worked until
    the first restart and then quietly stopped.
    """

    def _manager(self, tmp_path: Path) -> ProfileManager:
        return ProfileManager(tmp_path / "profiles", state_path=tmp_path / "active-profile")

    def _with_profile(self, tmp_path: Path, name: str) -> ProfileManager:
        manager = self._manager(tmp_path)
        manager.save(Profile(name=name))
        return manager

    def test_it_comes_back_active(self, tmp_path: Path) -> None:
        self._with_profile(tmp_path, "banking").activate("banking")
        assert self._manager(tmp_path).active_name == "banking"

    def test_the_default_needs_no_file_to_be_restored(self, tmp_path: Path) -> None:
        assert self._manager(tmp_path).active_name == DEFAULT_PROFILE

    def test_deleting_the_active_profile_is_remembered_too(self, tmp_path: Path) -> None:
        manager = self._with_profile(tmp_path, "banking")
        manager.activate("banking")
        manager.delete("banking")
        assert manager.active_name == DEFAULT_PROFILE
        # Not merely in this process: a restart must not resurrect a profile
        # that no longer exists.
        assert self._manager(tmp_path).active_name == DEFAULT_PROFILE

    def test_a_profile_deleted_behind_our_back_falls_back(self, tmp_path: Path) -> None:
        manager = self._with_profile(tmp_path, "banking")
        manager.activate("banking")
        (tmp_path / "profiles" / "banking.yaml").unlink()
        assert self._manager(tmp_path).active_name == DEFAULT_PROFILE

    def test_an_unreadable_state_file_does_not_stop_startup(self, tmp_path: Path) -> None:
        """`default` is always valid, so there is nothing here a user could act
        on — but a daemon refusing to start over a one-line file would be a
        real problem."""
        self._with_profile(tmp_path, "banking").activate("banking")
        (tmp_path / "active-profile").unlink()
        (tmp_path / "active-profile").mkdir()  # a directory: read_text raises
        assert self._manager(tmp_path).active_name == DEFAULT_PROFILE

    def test_an_unwritable_location_does_not_fail_the_activation(self, tmp_path: Path) -> None:
        """The profile is active in this process either way. Refusing to switch
        because a sidecar could not be written is the wrong trade."""
        manager = ProfileManager(tmp_path / "profiles", state_path=tmp_path / "nodir" / "x" / "p")
        manager.save(Profile(name="banking"))
        (tmp_path / "nodir").write_text("not a directory")
        assert manager.activate("banking").name == "banking"
        assert manager.active_name == "banking"
