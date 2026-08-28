"""Module loading — SPEC-1 §5.1/§5.2, REQ MOD-001-005, MOD-026.

A module is a directory: ``module.yaml`` (manifest plus declarative rules), an
optional ``module.py`` (the Python tier), and optional ``assets/``.

The load rule that matters: **a module that fails to load disables only itself**
(REQ MOD-005). The daemon starts, every other module loads, and the failure is
reported with its traceback. A single bad module taking down the proxy would
make the module system more dangerous than useful.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ...errors import ModuleApiVersionError, ModuleLoadError, PporlockError
from ..cost import ModuleStat
from ..ruleset import DEFAULT_PRIORITY, CompiledRule, compile_rule
from .context import MODULE_API_VERSION, SUPPORTED_API_VERSIONS

MANIFEST_NAME = "module.yaml"
PYTHON_NAME = "module.py"
ASSETS_DIR = "assets"

KNOWN_MANIFEST_KEYS = frozenset(
    {
        "name",
        "version",
        "pporlock_api",
        "description",
        "author",
        "enabled",
        "priority",
        "rules",
        "config",
    }
)

#: Files a module write may contain, and the only files this loader reads
#: besides ``assets/``. Anything else is refused rather than written: a file the
#: loader never reads is a file whose author believes it does something it does
#: not.
WRITABLE_FILES = frozenset({MANIFEST_NAME, PYTHON_NAME})

#: Hooks a module may define (SPEC-0 §8.3).
HOOK_NAMES = ("on_load", "on_unload", "on_request", "on_response", "on_websocket_message")

MODULE_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


@dataclass(frozen=True, slots=True)
class ModuleError:
    """Why a module could not be loaded, in enough detail to fix it."""

    code: str
    message: str
    trace: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "trace": self.trace,
            "line": self.line,
        }


@dataclass(slots=True)
class LoadedModule:
    """One module, loaded or not."""

    name: str
    path: Path
    version: str = "0.0.0"
    api_version: str = MODULE_API_VERSION
    description: str = ""
    author: str = ""
    enabled: bool = False
    priority: int = DEFAULT_PRIORITY
    config: dict[str, Any] = field(default_factory=dict)
    rules: tuple[CompiledRule, ...] = ()
    python: Any = None
    state: str = "loaded"
    error: ModuleError | None = None
    #: Live cost and effect, accumulated from provenance (REQ PRF-007). Mutable
    #: and owned by the registry, which preserves it across reloads — the
    #: question "is this module expensive" is about the module, not about the
    #: particular load of it that happens to be resident.
    stats: ModuleStat = field(default_factory=lambda: ModuleStat(module=""))
    #: Consecutive hook failures, for quarantine (REQ MOD-025).
    failures: int = 0
    quarantine_reason: str | None = None
    quarantined_at: str | None = None

    @property
    def has_python(self) -> bool:
        return self.python is not None

    @property
    def assets(self) -> Path | None:
        directory = self.path / ASSETS_DIR
        return directory if directory.is_dir() else None

    def hooks(self) -> dict[str, Any]:
        if self.python is None:
            return {}
        return {
            name: getattr(self.python, name)
            for name in HOOK_NAMES
            if callable(getattr(self.python, name, None))
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "enabled": self.enabled,
            "priority": self.priority,
            "state": self.state,
            "has_python": self.has_python,
            "rule_count": len(self.rules),
            "description": self.description,
            "author": self.author,
            "error": self.error.to_dict() if self.error else None,
            # REQ PRF-007. Always present, never omitted: the module library
            # renders these columns, and a field the contract declares but the
            # daemon never sends is a field every client has to guess about.
            "stats": self.stats.to_status_dict(),
            "quarantine": (
                {
                    "reason": self.quarantine_reason,
                    "failures": self.failures,
                    "since": self.quarantined_at,
                }
                if self.state == "quarantined"
                else None
            ),
        }


def _fail(name: str, path: Path, error: ModuleError) -> LoadedModule:
    return LoadedModule(name=name, path=path, state="load_error", error=error)


def load_module(path: Path) -> LoadedModule:
    """Load one module directory. Never raises — failure is a returned state.

    Returning rather than raising is what makes REQ MOD-005 practical: the
    caller loads every directory and reports the failures, instead of the first
    bad module aborting the sweep.
    """
    name = path.name
    manifest_path = path / MANIFEST_NAME

    if not manifest_path.is_file():
        return _fail(
            name,
            path,
            ModuleError("module_missing_manifest", f"{MANIFEST_NAME} not found in {path}"),
        )

    try:
        raw = yaml.safe_load(manifest_path.read_text()) or {}
    except yaml.YAMLError as exc:
        return _fail(name, path, ModuleError("module_invalid_yaml", str(exc)))
    except OSError as exc:
        return _fail(name, path, ModuleError("module_unreadable", str(exc)))

    if not isinstance(raw, dict):
        return _fail(
            name, path, ModuleError("module_invalid_manifest", "manifest must be a mapping")
        )

    unknown = set(raw) - KNOWN_MANIFEST_KEYS
    if unknown:
        # Strict: a typo silently ignored is how a module ends up not doing what
        # its author believes it does (REQ MOD-014).
        return _fail(
            name,
            path,
            ModuleError(
                "module_unknown_key",
                f"unknown manifest keys: {', '.join(sorted(unknown))}",
            ),
        )

    declared = str(raw.get("name") or "")
    if declared != name:
        return _fail(
            name,
            path,
            ModuleError(
                "module_name_mismatch",
                f"manifest name {declared!r} does not match directory {name!r}",
            ),
        )

    import re

    if not re.match(MODULE_NAME_PATTERN, name):
        return _fail(
            name,
            path,
            ModuleError("module_invalid_name", f"{name!r} is not a valid module name"),
        )

    api_version = str(raw.get("pporlock_api") or "")
    if api_version not in SUPPORTED_API_VERSIONS:
        # REQ MOD-026 — refuse with a clear message rather than failing later at
        # runtime in a way the author cannot connect to a version mismatch.
        return _fail(
            name,
            path,
            ModuleError(
                "module_api_unsupported",
                f"{name} targets module API {api_version or '(unset)'}; "
                f"this daemon implements {MODULE_API_VERSION}",
            ),
        )

    module = LoadedModule(
        name=name,
        path=path,
        version=str(raw.get("version") or "0.0.0"),
        api_version=api_version,
        description=str(raw.get("description") or ""),
        author=str(raw.get("author") or ""),
        # Creating or updating a module never enables it (REQ MCP-030).
        enabled=bool(raw.get("enabled", False)),
        priority=int(raw.get("priority", DEFAULT_PRIORITY)),
        config=dict(raw.get("config") or {}),
    )

    entries = raw.get("rules") or []
    if not isinstance(entries, list):
        return _fail(name, path, ModuleError("module_invalid_rules", "'rules' must be a list"))

    compiled: list[CompiledRule] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return _fail(
                name,
                path,
                ModuleError("rule_invalid", f"rule {index} must be a mapping"),
            )
        try:
            compiled.append(compile_rule(entry, module=name, index=index, priority=module.priority))
        except PporlockError as exc:
            return _fail(name, path, ModuleError(exc.code, exc.message))
    module.rules = tuple(compiled)

    python_path = path / PYTHON_NAME
    if python_path.is_file():
        try:
            module.python = _import_python(name, python_path)
        except Exception as exc:
            return _fail(
                name,
                path,
                ModuleError(
                    "module_import_failed",
                    f"{type(exc).__name__}: {exc}",
                    trace=traceback.format_exc(),
                    # A SyntaxError never reaches a frame in the file, so its
                    # own line number is the only one there is — and a syntax
                    # error is the failure an author most wants pointed at.
                    line=_first_module_line(python_path) or getattr(exc, "lineno", None),
                ),
            )

    return module


def _import_python(name: str, path: Path) -> Any:
    """Import a module's Python file under a unique synthetic package name.

    Unique so that two modules may both have a helper called ``utils`` without
    colliding — which they will, because that is what people call helpers.
    """
    synthetic = f"pporlock_module_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(synthetic, path)
    if spec is None or spec.loader is None:
        raise ModuleLoadError(f"cannot import {path}")

    imported = importlib.util.module_from_spec(spec)
    # Registered before execution so a module importing itself, or using
    # dataclasses/pickle that look themselves up, resolves.
    sys.modules[synthetic] = imported
    try:
        # Compiled from source on every load rather than executed through the
        # import system's loader, which would reuse a cached .pyc. That cache
        # keys on mtime-to-the-second plus size, so an edit made within a second
        # of the last one that leaves the file the same length is invisible to
        # it — and a hot reload that runs the code the author just replaced is
        # worse than no hot reload at all.
        # Executing the module's own source IS the feature. This is the Python
        # tier, and MOD-030 states plainly that module code is fully trusted and
        # runs with the user's privileges — there is nothing to sandbox here
        # that would not be a sandbox in name only. See docs/module-authoring.md.
        exec(  # noqa: S102  # nosec B102
            compile(path.read_text(), str(path), "exec"), imported.__dict__
        )
    except BaseException:
        sys.modules.pop(synthetic, None)
        raise
    return imported


def unload_python(name: str) -> None:
    """Drop a module's synthetic package so a reload re-executes it."""
    sys.modules.pop(f"pporlock_module_{name.replace('-', '_')}", None)


def _first_module_line(path: Path) -> int | None:
    """Best-effort line number for an import failure."""
    for frame in reversed(traceback.extract_tb(sys.exc_info()[2] or None) or []):
        if frame.filename == str(path):
            return frame.lineno
    return None


def discover(root: Path) -> list[Path]:
    """Module directories under the root, in a stable order."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / MANIFEST_NAME).is_file())


def load_all(root: Path) -> list[LoadedModule]:
    """Load every module. One failure never stops the rest (REQ MOD-005)."""
    return [load_module(path) for path in discover(root)]


__all__ = [
    "ASSETS_DIR",
    "HOOK_NAMES",
    "MANIFEST_NAME",
    "PYTHON_NAME",
    "WRITABLE_FILES",
    "LoadedModule",
    "ModuleApiVersionError",
    "ModuleError",
    "discover",
    "load_all",
    "load_module",
    "unload_python",
]
