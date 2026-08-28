"""Candidate-module validation — REQ API-027, MCP-012.

``POST /validate`` answers one question: *would this module load, and if not,
where is the mistake?* It installs nothing, writes nothing, and — the one place
it deliberately differs from the loader — **does not execute the module's
Python**. It compiles it.

That difference is the whole design. ``load_module`` executes ``module.py``,
because running module code is what the Python tier is (REQ MOD-030). Validation
is the step an author performs *before* deciding whether to let that code run,
so a validator that executed the file would have already done the thing the
author had not yet agreed to. A syntax error is therefore reported here, and an
``ImportError`` raised by the module's own top level is not — that one surfaces
at install or dry-run time, both of which are explicit acts.

Everything else — the manifest key set, the name rules, the API version, the
rule compiler — is the loader's own logic, called directly rather than
re-implemented, so a rule the validator accepts is a rule the loader accepts.

Findings carry ``file``, ``line`` and ``column`` because the web UI turns them
into editor markers. Lines are 1-based; a finding the daemon cannot place
carries ``None`` rather than a guess, since a marker on the wrong line is worse
than no marker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from ...errors import PporlockError
from ..ruleset import DEFAULT_PRIORITY, compile_rule
from .context import MODULE_API_VERSION, SUPPORTED_API_VERSIONS
from .loader import (
    HOOK_NAMES,
    KNOWN_MANIFEST_KEYS,
    MANIFEST_NAME,
    MODULE_NAME_PATTERN,
    PYTHON_NAME,
    WRITABLE_FILES,
)

Severity = str


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One finding. ``line``/``column`` are 1-based, or None when unplaceable."""

    code: str
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    severity: Severity = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "severity": self.severity,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity != "error")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        """The wire shape.

        ``ok`` and ``valid`` are the same boolean under two names: the web UI
        (``web/src/api/types.ts``) reads ``ok``, and ``contracts/openapi.yaml``
        declares ``valid``. Emitting both keeps every existing consumer correct
        while the contract is reconciled; neither is a lie.
        """
        return {
            "ok": self.ok,
            "valid": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }


def _mark(exc: yaml.YAMLError) -> tuple[int | None, int | None]:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return None, None
    return int(mark.line) + 1, int(mark.column) + 1


def _manifest_line(source: str, key: str) -> int | None:
    """The line a top-level manifest key sits on, for a marker.

    A scan rather than a parse: the document has already been parsed by the
    time this is wanted, and re-parsing to recover positions would mean holding
    a second representation of it purely for line numbers.
    """
    pattern = re.compile(rf"^{re.escape(key)}\s*:", re.MULTILINE)
    match = pattern.search(source)
    if match is None:
        return None
    return source.count("\n", 0, match.start()) + 1


def declared_name(files: Mapping[str, str]) -> str | None:
    """The name the manifest gives itself, if it parses at all."""
    source = files.get(MANIFEST_NAME)
    if source is None:
        return None
    try:
        raw = yaml.safe_load(source)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    return str(name) if isinstance(name, str) and name else None


def validate_module_files(name: str | None, files: Mapping[str, str]) -> ValidationReport:
    """Validate a candidate module's files without installing or running them.

    ``name`` is the directory the module would be installed into, which the
    loader requires the manifest to agree with. The web UI's editor validates a
    file it has not yet named, so ``None`` means "take the manifest's own name"
    — the name/directory agreement is then checked at write time by the module
    routes, which is the moment the directory exists.
    """
    issues: list[ValidationIssue] = []
    if name is None:
        name = declared_name(files) or ""

    unknown_files = sorted(set(files) - set(WRITABLE_FILES))
    for filename in unknown_files:
        issues.append(
            ValidationIssue(
                "module_unknown_file",
                f"{filename!r} is not a file the loader reads; "
                f"a module is {' and '.join(sorted(WRITABLE_FILES))} plus assets/",
                file=filename,
            )
        )

    if MANIFEST_NAME not in files:
        issues.append(ValidationIssue("module_missing_manifest", f"{MANIFEST_NAME} is required"))
        return ValidationReport(tuple(issues))

    if not re.match(MODULE_NAME_PATTERN, name):
        issues.append(
            ValidationIssue(
                "module_invalid_name",
                f"{name!r} is not a valid module name (lowercase, digits and dashes)",
                file=MANIFEST_NAME,
            )
        )

    manifest_source = files[MANIFEST_NAME]
    issues.extend(_validate_manifest(name, manifest_source))
    issues.extend(_validate_python(files.get(PYTHON_NAME)))
    return ValidationReport(tuple(issues))


def _validate_manifest(name: str, source: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        raw = yaml.safe_load(source) or {}
    except yaml.YAMLError as exc:
        line, column = _mark(exc)
        return [
            ValidationIssue(
                "module_invalid_yaml", str(exc), file=MANIFEST_NAME, line=line, column=column
            )
        ]

    if not isinstance(raw, dict):
        return [
            ValidationIssue(
                "module_invalid_manifest",
                "the manifest must be a mapping",
                file=MANIFEST_NAME,
                line=1,
            )
        ]

    for key in sorted(set(raw) - KNOWN_MANIFEST_KEYS):
        issues.append(
            ValidationIssue(
                "module_unknown_key",
                f"unknown manifest key {key!r}; "
                f"known keys are {', '.join(sorted(KNOWN_MANIFEST_KEYS))}",
                file=MANIFEST_NAME,
                line=_manifest_line(source, key),
            )
        )

    declared = str(raw.get("name") or "")
    if declared != name:
        issues.append(
            ValidationIssue(
                "module_name_mismatch",
                f"manifest name {declared!r} does not match the module name {name!r}",
                file=MANIFEST_NAME,
                line=_manifest_line(source, "name"),
            )
        )

    api_version = str(raw.get("pporlock_api") or "")
    if api_version not in SUPPORTED_API_VERSIONS:
        issues.append(
            ValidationIssue(
                "module_api_unsupported",
                f"pporlock_api {api_version or '(unset)'} is not supported; "
                f"this daemon implements {MODULE_API_VERSION}",
                file=MANIFEST_NAME,
                line=_manifest_line(source, "pporlock_api"),
            )
        )

    issues.extend(_validate_rules(name, raw, source))
    return issues


def _validate_rules(name: str, raw: dict[str, Any], source: str) -> list[ValidationIssue]:
    entries = raw.get("rules") or []
    rules_line = _manifest_line(source, "rules")
    if not isinstance(entries, list):
        return [
            ValidationIssue(
                "module_invalid_rules",
                "'rules' must be a list",
                file=MANIFEST_NAME,
                line=rules_line,
            )
        ]

    try:
        priority = int(raw.get("priority", DEFAULT_PRIORITY))
    except (TypeError, ValueError):
        return [
            ValidationIssue(
                "module_invalid_manifest",
                f"priority must be an integer, got {raw.get('priority')!r}",
                file=MANIFEST_NAME,
                line=_manifest_line(source, "priority"),
            )
        ]

    issues: list[ValidationIssue] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(
                ValidationIssue(
                    "rule_invalid",
                    f"rule {index} must be a mapping",
                    file=MANIFEST_NAME,
                    line=rules_line,
                )
            )
            continue
        try:
            # The loader's own compiler, not a copy of its checks. Anything it
            # rejects here it would reject at load (REQ MOD-014).
            compile_rule(entry, module=name, index=index, priority=priority)
        except PporlockError as exc:
            issues.append(
                ValidationIssue(
                    exc.code,
                    f"rule {index} ({entry.get('name') or 'unnamed'}): {exc.message}",
                    file=MANIFEST_NAME,
                    line=_rule_line(source, entry) or rules_line,
                )
            )
    return issues


def _rule_line(source: str, entry: dict[str, Any]) -> int | None:
    """Locate a rule by its declared name, so the marker lands on the rule."""
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        return None
    match = re.search(rf"^\s*-?\s*name\s*:\s*[\"']?{re.escape(name)}", source, re.MULTILINE)
    if match is None:
        return None
    return source.count("\n", 0, match.start()) + 1


def _validate_python(source: str | None) -> list[ValidationIssue]:
    """Syntax only. See the module docstring for why this does not execute."""
    if source is None:
        return []
    try:
        compiled = compile(source, PYTHON_NAME, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return [
            ValidationIssue(
                "module_syntax_error",
                f"{type(exc).__name__}: {exc.msg}",
                file=PYTHON_NAME,
                line=exc.lineno,
                column=exc.offset,
            )
        ]
    except ValueError as exc:
        # A null byte or an over-deep expression: compile() raises ValueError,
        # not SyntaxError, and it is still the author's problem to fix.
        return [ValidationIssue("module_syntax_error", str(exc), file=PYTHON_NAME)]

    # Top-level names the module binds. A ``def on_request`` stores that name,
    # so it appears here without the module having been executed.
    return _hook_warnings(set(compiled.co_names))


def _hook_warnings(defined: set[str]) -> list[ValidationIssue]:
    if defined & set(HOOK_NAMES):
        return []
    return [
        ValidationIssue(
            "module_no_hooks",
            f"{PYTHON_NAME} defines none of {', '.join(HOOK_NAMES)}; nothing in it will be called",
            file=PYTHON_NAME,
            severity="warning",
        )
    ]


__all__ = ["ValidationIssue", "ValidationReport", "validate_module_files"]
