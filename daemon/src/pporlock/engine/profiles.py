"""Profiles — SPEC-1 §5.6, SPEC-0 §5.7, REQ MOD-040-044.

A profile is a named set of active modules, plus the working context that goes
with them: development toggles and exclusion additions. Switching to a
"debugging site X" profile should apply the whole context at once rather than
leaving the operator to remember three separate settings.

Exactly one profile is active. ``default`` always exists and cannot be deleted,
so there is never a state with no profile at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError

DEFAULT_PROFILE = "default"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
KNOWN_KEYS = frozenset({"name", "description", "modules", "dev_toggles", "exclusions_add"})


@dataclass(slots=True)
class Profile:
    name: str
    description: str = ""
    modules: list[str] = field(default_factory=list)
    dev_toggles: dict[str, bool] = field(
        default_factory=lambda: {"anticache": False, "anticomp": False}
    )
    exclusions_add: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "modules": list(self.modules),
            "dev_toggles": dict(self.dev_toggles),
            "exclusions_add": list(self.exclusions_add),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Profile:
        unknown = set(raw) - KNOWN_KEYS
        if unknown:
            raise ConfigError(f"unknown profile keys: {', '.join(sorted(unknown))}")

        name = str(raw.get("name") or "")
        if not NAME_PATTERN.match(name):
            raise ConfigError(f"{name!r} is not a valid profile name")

        toggles = dict(raw.get("dev_toggles") or {})
        return cls(
            name=name,
            description=str(raw.get("description") or ""),
            modules=[str(m) for m in (raw.get("modules") or [])],
            dev_toggles={
                "anticache": bool(toggles.get("anticache", False)),
                "anticomp": bool(toggles.get("anticomp", False)),
            },
            exclusions_add=[str(e) for e in (raw.get("exclusions_add") or [])],
        )


class ProfileManager:
    """Profiles on disk, one YAML file each."""

    __slots__ = ("_active", "root", "state_path")

    def __init__(self, root: Path, state_path: Path | None = None) -> None:
        self.root = root
        #: Where the active profile name is remembered across restarts.
        #:
        #: Which profile is active is user state, like module enablement, and
        #: belongs beside it rather than in a profile file — a profile does not
        #: know whether it is the chosen one, and writing that into one would
        #: mean rewriting two files on every switch to keep them agreeing.
        self.state_path = state_path or (root.parent / "active-profile")
        self._active = DEFAULT_PROFILE
        self._restore()

    def _restore(self) -> None:
        """Read the remembered profile, falling back to default.

        A profile that has since been deleted, or an unreadable file, falls
        back silently: `default` is always valid and always exists, so there is
        nothing here a user could act on. What must not happen is the daemon
        refusing to start because a one-line file went missing.
        """
        try:
            name = self.state_path.read_text().strip()
        except OSError:
            return
        if name and any(p.name == name for p in self.all_profiles()):
            self._active = name

    def _remember(self) -> None:
        """Persist the active profile. Best effort, and deliberately quiet.

        Failing to write it must not fail the activation: the profile *is*
        active in this process either way, and refusing to switch because a
        sidecar could not be written would be the wrong trade.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(f"{self._active}\n")
        except OSError:
            pass

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.yaml"

    def all_profiles(self) -> list[Profile]:
        profiles: dict[str, Profile] = {
            DEFAULT_PROFILE: Profile(
                name=DEFAULT_PROFILE,
                description="Every enabled module. Always present.",
            )
        }
        if self.root.is_dir():
            for path in sorted(self.root.glob("*.yaml")):
                try:
                    raw = yaml.safe_load(path.read_text()) or {}
                    if not isinstance(raw, dict):
                        raise ConfigError(f"{path.name}: a profile must be a mapping")
                    raw.setdefault("name", path.stem)
                    profile = Profile.from_dict(raw)
                except (yaml.YAMLError, ConfigError, OSError):
                    # A malformed profile is skipped rather than fatal: the
                    # others still work, and the default always exists.
                    continue
                profiles[profile.name] = profile
        return sorted(profiles.values(), key=lambda p: (p.name != DEFAULT_PROFILE, p.name))

    def get(self, name: str) -> Profile | None:
        for profile in self.all_profiles():
            if profile.name == name:
                return profile
        return None

    def save(self, profile: Profile) -> Profile:
        if profile.name == DEFAULT_PROFILE:
            raise ConfigError("the default profile is implicit and cannot be written")
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(profile.name).write_text(yaml.safe_dump(profile.to_dict(), sort_keys=False))
        return profile

    def delete(self, name: str) -> bool:
        if name == DEFAULT_PROFILE:
            # REQ MOD-041 — there must always be a profile to fall back to.
            raise ConfigError("the default profile cannot be deleted")
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        if self._active == name:
            self._active = DEFAULT_PROFILE
            self._remember()
        return True

    def activate(self, name: str) -> Profile:
        profile = self.get(name)
        if profile is None:
            raise ConfigError(f"no such profile: {name}")
        self._active = name
        self._remember()
        return profile

    @property
    def active_name(self) -> str:
        return self._active

    @property
    def active(self) -> Profile:
        return self.get(self._active) or Profile(name=DEFAULT_PROFILE)

    def module_filter(self) -> list[str] | None:
        """Which modules the active profile admits, or None for all of them."""
        profile = self.active
        return None if profile.name == DEFAULT_PROFILE else list(profile.modules)
