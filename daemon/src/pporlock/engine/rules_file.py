"""Loading rules from YAML — SPEC-0 §5.3.

Sprint 7 loads a single ``rules.yaml``; the full module directory format arrives
in Sprint 11 and will replace this loader while keeping the same compiled
output. Strict parsing throughout: an unknown key is an error, not a warning
(REQ MOD-014), because a typo that silently disables a block is the worst way
for this system to fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..errors import ConfigError, RuleValidationError
from .ruleset import DEFAULT_PRIORITY, RuleSet, compile_rule

KNOWN_TOP_LEVEL = frozenset({"rules", "name", "priority", "description"})


def load_rules_file(path: Path, *, default_module: str = "rules") -> RuleSet:
    """Parse and compile a rules file. Raises on anything malformed."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}", path=str(path)) from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping", path=str(path))

    unknown = set(raw) - KNOWN_TOP_LEVEL
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(sorted(unknown))}", path=str(path))

    entries = raw.get("rules") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: 'rules' must be a list", path=str(path))

    module = str(raw.get("name") or default_module)
    priority = int(raw.get("priority", DEFAULT_PRIORITY))

    compiled = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuleValidationError(
                "each rule must be a mapping", module=module, rule_index=index
            )
        compiled.append(compile_rule(entry, module=module, index=index, priority=priority))

    return RuleSet(compiled, modules=(module,))


def rules_to_dicts(ruleset: RuleSet) -> list[dict[str, Any]]:
    """Round-trip a compiled set back to plain dicts, for the API."""
    out: list[dict[str, Any]] = []
    for rule in sorted(ruleset.all_rules, key=lambda r: r.sort_key):
        entry: dict[str, Any] = {
            "name": rule.name,
            "action": str(rule.action),
            "enabled": rule.enabled,
            "rule_id": rule.rule_id,
            "module": rule.module,
            "priority": rule.priority,
        }
        entry.update(rule.params)
        out.append(entry)
    return out
