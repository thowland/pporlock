"""Persisted module enablement — SPEC-1 §5.3, REQ MOD-004/MOD-020, OI-8.

Whether a module is on, and where it sits in the ordering, is *user* state. The
manifest is the *author's*. Recording the first in the second means the daemon
rewrites a file it does not own — losing the author's comments, key order and
formatting the first time someone flips a switch in the UI — so it does not.

Instead a sidecar file next to the module root holds ``{name: {enabled,
priority, config}}``, where ``config`` is only what the user changed through a
module's declared settings (``engine/modules/settings.py``). The manifest
**seeds** an entry the first time a module is seen and the sidecar wins
thereafter. That is exactly the in-memory rule
``ModuleRegistry.reload`` already applied across a reload; this only makes it
survive the process. The consequence is worth stating plainly: editing
``enabled:`` in a manifest after the module has been seen once does nothing.
The API is where enablement is set, which is also the only place it is audited.

**Nothing here is a secret.** The file holds a module name — already constrained
to ``MODULE_NAME_PATTERN`` — a boolean, an integer, and values for fields the
module's author declared as user-settable. No token, no header, no capture.
There is deliberately no redaction pass over it, because there is nothing for
one to do: the settings vocabulary has no secret type, exactly so that this
sentence stays true (see ``settings.py``). The file is written ``0600`` all the
same, because "no secrets today" is a property of the current setting types and
not something a future one should be able to quietly falsify.

**A corrupt sidecar is not fatal.** It is skipped the way a malformed profile
is: every module falls back to its manifest default, the reason is recorded on
``error``, and the daemon starts. The alternative — refusing to start because a
JSON file someone edited by hand has a trailing comma — would make the proxy
unrunnable over state that is, at worst, a few toggles.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The sidecar's name. Lives beside the module root, i.e. in the state
#: directory, so it moves with ``state_dir`` rather than being pinned to a
#: constant resolved at import (OI-10).
STATE_FILENAME = "module-state.json"


@dataclass(frozen=True, slots=True)
class ModuleState:
    """One module's user-set state."""

    enabled: bool
    priority: int
    #: User-set values for the module's declared settings. Only keys the user
    #: actually changed — a default is not an override, and storing it as one
    #: would freeze it against a later edit to the module.
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"enabled": self.enabled, "priority": self.priority}
        # Omitted when empty: every module gets a row on first sighting, and a
        # `"config": {}` on all of them is noise in a file people do read.
        if self.config:
            payload["config"] = dict(self.config)
        return payload


class ModuleStateStore:
    """The sidecar, held in memory and written through on every change.

    Every mutation persists immediately rather than at shutdown. A daemon that
    is killed — and this one is a launchd agent that gets killed at logout — must
    not lose the toggle the user flipped a minute ago, and there is no
    shutdown hook that can be relied on to run.
    """

    __slots__ = ("_state", "error", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._state: dict[str, ModuleState] = {}
        #: Why the sidecar could not be read or written, if it could not. Read
        #: by the runner so startup says so out loud rather than silently
        #: reverting everyone's modules to their manifest defaults.
        self.error: str | None = None
        self._read()

    # -- reading ---------------------------------------------------------

    def _read(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            self.error = f"{self.path}: unreadable, falling back to manifest defaults — {exc}"
            return
        if not isinstance(raw, dict):
            self.error = f"{self.path}: expected a JSON object, falling back to manifest defaults"
            return

        parsed: dict[str, ModuleState] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            enabled = entry.get("enabled")
            priority = entry.get("priority")
            # bool is a subclass of int, so priority is checked for the
            # subclass first; a JSON ``true`` is not a priority.
            if not isinstance(enabled, bool) or isinstance(priority, bool):
                continue
            if not isinstance(priority, int):
                continue
            # A config that is not an object is dropped and the rest of the row
            # kept: losing a toggle because someone hand-edited the settings
            # block into a list would be a disproportionate response.
            config = entry.get("config")
            parsed[str(name)] = ModuleState(
                enabled=enabled,
                priority=priority,
                config=dict(config) if isinstance(config, dict) else {},
            )
        self._state = parsed

    # -- queries ---------------------------------------------------------

    def get(self, name: str) -> ModuleState | None:
        return self._state.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._state

    def __len__(self) -> int:
        return len(self._state)

    def to_dict(self) -> dict[str, Any]:
        return {name: s.to_dict() for name, s in sorted(self._state.items())}

    # -- mutation --------------------------------------------------------

    def set(self, name: str, *, enabled: bool, priority: int) -> None:
        """Record a module's enablement and ordering, and write it through.

        Config is left alone: this is called on every reload to seed a row, and
        a seed that cleared the user's settings would undo them on restart.
        """
        current = self._state.get(name)
        new = ModuleState(
            enabled=bool(enabled),
            priority=int(priority),
            config=dict(current.config) if current is not None else {},
        )
        if current == new:
            return
        self._state[name] = new
        self.save()

    def set_config(self, name: str, config: dict[str, Any]) -> None:
        """Replace a module's settings overrides and write them through.

        Replace rather than merge, because "reset this field to its default"
        has to be expressible, and under a merge it would not be — a key the
        caller omits would keep its old value forever.
        """
        current = self._state.get(name)
        new = ModuleState(
            enabled=current.enabled if current is not None else False,
            priority=current.priority if current is not None else 100,
            config=dict(config),
        )
        if current == new:
            return
        self._state[name] = new
        self.save()

    def prune(self, known: Iterable[str]) -> None:
        """Drop entries for modules that are no longer on disk.

        Without this the sidecar grows a row for every module ever installed,
        and a module deleted and later reinstalled would silently come back
        enabled — inheriting a decision made about a different version of it.
        """
        keep = set(known)
        stale = set(self._state) - keep
        if not stale:
            return
        for name in stale:
            del self._state[name]
        self.save()

    def save(self) -> None:
        """Write the sidecar atomically.

        Written to a temporary file in the same directory and renamed, so a
        crash mid-write leaves the previous file rather than a truncated one
        that would read as corrupt on the next start.

        A write that fails is recorded and swallowed. The in-memory state is
        already correct, so the running daemon behaves as the user asked; only
        the next restart loses it. Raising here instead would turn an
        unwritable state directory into a failed API call on a route whose
        actual work succeeded.

        ``error`` is deliberately never *cleared* here. A read failure means the
        user's prior state is gone for the life of this process, and the first
        successful seed-write would otherwise erase the only record of it before
        the runner had a chance to print it.
        """
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
            )
            try:
                # mkstemp is already 0600; set it explicitly so the mode is a
                # stated property of this file rather than an inherited one.
                os.chmod(tmp_name, 0o600)
                with os.fdopen(handle, "w") as fh:
                    fh.write(payload)
                os.replace(tmp_name, self.path)
            except OSError:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.error = f"{self.path}: could not be written — {exc}"


__all__ = ["STATE_FILENAME", "ModuleState", "ModuleStateStore"]
