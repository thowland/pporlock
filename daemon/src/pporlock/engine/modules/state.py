"""Persisted module enablement — SPEC-1 §5.3, REQ MOD-004/MOD-020, OI-8.

Whether a module is on, and where it sits in the ordering, is *user* state. The
manifest is the *author's*. Recording the first in the second means the daemon
rewrites a file it does not own — losing the author's comments, key order and
formatting the first time someone flips a switch in the UI — so it does not.

Instead a sidecar file next to the module root holds ``{name: {enabled,
priority}}``. The manifest **seeds** an entry the first time a module is seen
and the sidecar wins thereafter. That is exactly the in-memory rule
``ModuleRegistry.reload`` already applied across a reload; this only makes it
survive the process. The consequence is worth stating plainly: editing
``enabled:`` in a manifest after the module has been seen once does nothing.
The API is where enablement is set, which is also the only place it is audited.

**Nothing here is a secret.** The file holds a module name — already constrained
to ``MODULE_NAME_PATTERN`` — a boolean and an integer. No token, no header, no
capture. There is deliberately no redaction pass over it, because there is
nothing for one to do; a test asserts the written shape so that stays true.

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
from dataclasses import dataclass
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
            parsed[str(name)] = ModuleState(enabled=enabled, priority=priority)
        self._state = parsed

    # -- queries ---------------------------------------------------------

    def get(self, name: str) -> ModuleState | None:
        return self._state.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._state

    def __len__(self) -> int:
        return len(self._state)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: {"enabled": s.enabled, "priority": s.priority}
            for name, s in sorted(self._state.items())
        }

    # -- mutation --------------------------------------------------------

    def set(self, name: str, *, enabled: bool, priority: int) -> None:
        """Record a module's state and write it through."""
        current = self._state.get(name)
        new = ModuleState(enabled=bool(enabled), priority=int(priority))
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
                with os.fdopen(handle, "w") as fh:
                    fh.write(payload)
                os.replace(tmp_name, self.path)
            except OSError:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            self.error = f"{self.path}: could not be written — {exc}"


__all__ = ["STATE_FILENAME", "ModuleState", "ModuleStateStore"]
