"""Loading rules from YAML. SPEC-0 §5.3."""

from __future__ import annotations

from pathlib import Path

import pytest

from pporlock.engine.rules_file import load_rules_file, rules_to_dicts
from pporlock.errors import ConfigError, RuleValidationError

VALID = """
name: my-rules
priority: 50
rules:
  - name: block-vendor
    action: block
    match:
      host: "*.vendor.example"
    stub: auto
  - name: strip-csp
    action: headers
    match:
      dest: document
    response:
      remove: ["content-security-policy"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(text)
    return path


class TestLoading:
    def test_loads_and_compiles(self, tmp_path: Path) -> None:
        ruleset = load_rules_file(write(tmp_path, VALID))
        assert len(ruleset) == 2
        assert [r.name for r in ruleset.short_circuit] == ["block-vendor"]

    def test_module_name_comes_from_the_file(self, tmp_path: Path) -> None:
        assert load_rules_file(write(tmp_path, VALID)).modules == ("my-rules",)

    def test_priority_applies_to_every_rule(self, tmp_path: Path) -> None:
        ruleset = load_rules_file(write(tmp_path, VALID))
        assert all(r.priority == 50 for r in ruleset.all_rules)

    def test_an_empty_file_yields_an_empty_set(self, tmp_path: Path) -> None:
        assert len(load_rules_file(write(tmp_path, ""))) == 0

    def test_a_file_with_no_rules_key(self, tmp_path: Path) -> None:
        assert len(load_rules_file(write(tmp_path, "name: empty\n"))) == 0


class TestStrictness:
    """A typo that silently disables a block is the worst way for this to fail."""

    def test_invalid_yaml_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_rules_file(write(tmp_path, "rules: [unclosed\n"))

    def test_a_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_rules_file(write(tmp_path, "- a\n- b\n"))

    def test_unknown_top_level_keys_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="unknown keys"):
            load_rules_file(write(tmp_path, "rulez:\n  - a\n"))

    def test_rules_must_be_a_list(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="must be a list"):
            load_rules_file(write(tmp_path, "rules:\n  a: b\n"))

    def test_each_rule_must_be_a_mapping(self, tmp_path: Path) -> None:
        with pytest.raises(RuleValidationError, match="must be a mapping"):
            load_rules_file(write(tmp_path, "rules:\n  - just-a-string\n"))

    def test_a_bad_rule_fails_the_whole_load(self, tmp_path: Path) -> None:
        """Silently dropping it would leave the user believing a block is in
        force when it is not."""
        text = "rules:\n  - name: ok\n    action: block\n  - name: bad\n    action: nonsense\n"
        with pytest.raises(RuleValidationError, match="unknown action"):
            load_rules_file(write(tmp_path, text))

    def test_the_error_locates_the_rule(self, tmp_path: Path) -> None:
        text = "rules:\n  - name: ok\n    action: block\n  - name: bad\n    action: nonsense\n"
        with pytest.raises(RuleValidationError) as exc:
            load_rules_file(write(tmp_path, text))
        assert exc.value.rule_index == 1


class TestRoundTrip:
    def test_compiles_back_to_dicts(self, tmp_path: Path) -> None:
        ruleset = load_rules_file(write(tmp_path, VALID))
        payload = rules_to_dicts(ruleset)
        assert {r["name"] for r in payload} == {"block-vendor", "strip-csp"}

    def test_carries_identity_and_ordering(self, tmp_path: Path) -> None:
        payload = rules_to_dicts(load_rules_file(write(tmp_path, VALID)))
        assert payload[0]["rule_id"] == "my-rules:0"
        assert payload[0]["module"] == "my-rules"
        assert payload[0]["priority"] == 50

    def test_carries_action_parameters(self, tmp_path: Path) -> None:
        payload = rules_to_dicts(load_rules_file(write(tmp_path, VALID)))
        assert payload[0]["stub"] == "auto"

    def test_is_json_serializable(self, tmp_path: Path) -> None:
        import json

        json.dumps(rules_to_dicts(load_rules_file(write(tmp_path, VALID))))
