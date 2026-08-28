"""The module context — SPEC-0 §8.2.

Everything module code may rely on. Anything not here is private and may change
without notice, which is the whole point of writing the surface down: a module
written today has to keep working when the daemon around it moves.

Module code is **fully trusted** (REQ MOD-030). There is no import allowlist, no
sandbox, and no resource jail. This context is a convenience API, not a
boundary — it does not and cannot stop a module doing anything the user could
do. The only enforced guardrails are error isolation, failure quarantine, and
the per-flow time budget.
"""

from __future__ import annotations

import fnmatch
import json
import sqlite3
from pathlib import Path
from typing import Any

from ...errors import AssetPathError
from ..models import NormalizedRequest, NormalizedResponse, SyntheticResponse
from ..provenance import NoteCode, Severity

#: The module API version this daemon implements (SPEC-0 §8.1).
MODULE_API_VERSION = "1"

#: Versions this daemon will load. The current major and the one before it.
SUPPORTED_API_VERSIONS = frozenset({"1"})


class ModuleStore:
    """Module-scoped persistent key/value storage (REQ MOD-022).

    Backed by SQLite with a write-through in-memory cache, so ``get`` never
    touches disk on the proxy's event loop and ``set`` returns immediately. A
    module doing bookkeeping across flows should not be able to make browsing
    slower by doing it.
    """

    __slots__ = ("_cache", "_module", "_path")

    def __init__(self, path: Path, module: str) -> None:
        self._path = path
        self._module = module
        self._cache: dict[str, Any] = {}
        self._load()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS store "
            "(module TEXT NOT NULL, key TEXT NOT NULL, value TEXT, "
            "PRIMARY KEY (module, key))"
        )
        return connection

    def _load(self) -> None:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT key, value FROM store WHERE module = ?", (self._module,)
                ).fetchall()
        except sqlite3.Error:
            # A broken store is not a reason to refuse to load a module; it
            # starts empty and says nothing, which is the least surprising
            # behaviour for what is a convenience.
            return
        for key, value in rows:
            try:
                self._cache[key] = json.loads(value)
            except json.JSONDecodeError:
                continue

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO store (module, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(module, key) DO UPDATE SET value = excluded.value",
                    (self._module, key, json.dumps(value)),
                )
        except (sqlite3.Error, TypeError):
            # The in-memory value stands; persistence is best-effort.
            pass

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)
        try:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM store WHERE module = ? AND key = ?", (self._module, key)
                )
        except sqlite3.Error:
            pass

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._cache))


class ModuleContext:
    """What a module hook receives. SPEC-0 §8.2, and nothing beyond it."""

    __slots__ = (
        "_assets",
        "_log",
        "_notes",
        "_registry",
        "_store",
        "config",
        "name",
        "profile",
        "version",
    )

    def __init__(
        self,
        *,
        name: str,
        version: str,
        config: dict[str, Any] | None = None,
        profile: str = "default",
        assets: Path | None = None,
        store: ModuleStore | None = None,
        registry: Any = None,
    ) -> None:
        self.name = name
        self.version = version
        self.config = config or {}
        self.profile = profile
        self._assets = assets
        self._store = store
        self._registry = registry
        self._log: list[tuple[str, str, dict[str, Any]]] = []
        self._notes: list[tuple[NoteCode, Severity, str, dict[str, Any]]] = []

    # -- matching helpers ------------------------------------------------

    def matches(
        self,
        request: NormalizedRequest,
        *,
        host: str | None = None,
        path: str | None = None,
        method: str | None = None,
        dest: str | None = None,
        content_type: str | None = None,
        response: NormalizedResponse | None = None,
    ) -> bool:
        """Convenience matching, so module code does not reimplement globbing."""
        import re

        if host is not None and not fnmatch.fnmatchcase(request.host.lower(), host.lower()):
            return False
        if path is not None and not re.search(path, request.path):
            return False
        if method is not None and request.method != method.upper():
            return False
        if dest is not None and request.dest != dest:
            return False
        if content_type is not None:
            actual = response.content_type if response is not None else request.content_type
            if actual is None or content_type.lower() not in actual.lower():
                return False
        return True

    # -- reporting -------------------------------------------------------

    def log(self, level: str, message: str, **fields: Any) -> None:
        """Structured, module-scoped logging. Surfaced in the UI."""
        self._log.append((level, message, fields))

    def note(self, code: str, message: str, severity: str = "warning", **detail: Any) -> None:
        """Record a provenance note against the flow.

        A module changing something a page depends on should say so, for the
        same reason the engine's own transforms do.
        """
        try:
            note_code = NoteCode(code)
        except ValueError:
            note_code = NoteCode.MODULE_ERROR
            detail = {**detail, "requested_code": code}
        try:
            note_severity = Severity(severity)
        except ValueError:
            note_severity = Severity.WARNING
        self._notes.append((note_code, note_severity, message, detail))

    @property
    def notes(self) -> tuple[tuple[NoteCode, Severity, str, dict[str, Any]], ...]:
        return tuple(self._notes)

    @property
    def logs(self) -> tuple[tuple[str, str, dict[str, Any]], ...]:
        return tuple(self._log)

    def drain(self) -> None:
        self._notes.clear()
        self._log.clear()

    # -- storage ---------------------------------------------------------

    def store_get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default) if self._store else default

    def store_set(self, key: str, value: Any) -> None:
        if self._store:
            self._store.set(key, value)

    def store_delete(self, key: str) -> None:
        if self._store:
            self._store.delete(key)

    # -- assets ----------------------------------------------------------

    def asset_path(self, relative: str) -> Path:
        """Resolve a path inside the module's assets/ directory.

        Containment is checked after symlink resolution — a symlink pointing out
        of the directory is exactly the case a naive prefix check misses. Module
        code is trusted, so this is a guard against mistakes rather than malice,
        but a module that accidentally reads outside its own directory is a
        mistake worth catching (implementation-plan.md §2.5).
        """
        if self._assets is None:
            raise AssetPathError(f"{self.name} has no assets directory", module=self.name)

        candidate = Path(relative)
        if candidate.is_absolute():
            raise AssetPathError(f"asset path must be relative: {relative!r}", path=relative)

        root = self._assets.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise AssetPathError(f"asset path escapes {self.name}/assets: {relative!r}")
        return resolved

    def asset_bytes(self, relative: str) -> bytes:
        return self.asset_path(relative).read_bytes()

    def asset_text(self, relative: str) -> str:
        return self.asset_path(relative).read_text()

    # -- registry extension ----------------------------------------------

    def register_transform(self, name: str, fn: Any, cost: str = "expensive") -> None:
        """Add a transform to the registry (SPEC-0 §8.2).

        The cost defaults to expensive because we know nothing about it: a
        module transform assumed fast on the proxy's event loop is how one
        module makes every page slow.
        """
        if self._registry is None:
            return
        from ..cost import Cost
        from ..transforms import TransformSpec

        try:
            resolved = Cost(cost)
        except ValueError:
            resolved = Cost.EXPENSIVE
        self._registry.register(TransformSpec(name, fn, resolved))

    # -- response construction -------------------------------------------

    def synthesize(
        self, *, status: int = 200, content_type: str | None = None, body: bytes | str = b""
    ) -> SyntheticResponse:
        payload = body.encode() if isinstance(body, str) else body
        headers = [("cache-control", "no-store"), ("x-pporlock", "module")]
        if content_type:
            headers.insert(0, ("content-type", content_type))
        return SyntheticResponse(
            status=status, body=payload, headers=tuple(headers), origin=self.name
        )

    def stub_for(self, dest: str | None, request: NormalizedRequest) -> SyntheticResponse:
        """The same Sec-Fetch-Dest derivation the block action uses."""
        from ..stubs import auto_for

        return auto_for(dest, request, origin=self.name, rule=self.name)
