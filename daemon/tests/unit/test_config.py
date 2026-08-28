"""Configuration and the loopback invariant. SPEC-1 §9, REQ API-010."""

from __future__ import annotations

from pathlib import Path

import pytest

from pporlock.config import (
    DEFAULT_STATE_DIR,
    Config,
    ModulesConfig,
    assert_loopback,
    is_loopback,
    load_config,
)
from pporlock.errors import ConfigError, NonLoopbackBindError


class TestLoopbackDetection:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.1.2.3",
            "::1",
            "[::1]",
            "localhost",
            "LOCALHOST",
            "localhost.localdomain",
        ],
    )
    def test_accepts_loopback(self, host: str) -> None:
        assert is_loopback(host)

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "192.168.1.5",
            "10.0.0.1",
            "example.com",
            "127.0.0.1.evil.com",
            "::",
            "",
            "   ",
            "not an address",
        ],
    )
    def test_rejects_everything_else(self, host: str) -> None:
        assert not is_loopback(host)

    def test_zero_address_is_rejected_explicitly(self) -> None:
        """0.0.0.0 reads like a harmless default and binds every interface."""
        assert not is_loopback("0.0.0.0")


class TestAssertLoopback:
    def test_returns_the_host_when_valid(self) -> None:
        assert assert_loopback("127.0.0.1", setting="control.listen_host") == "127.0.0.1"

    def test_refuses_and_names_the_setting(self) -> None:
        with pytest.raises(NonLoopbackBindError) as exc:
            assert_loopback("0.0.0.0", setting="control.listen_host")
        assert exc.value.detail["setting"] == "control.listen_host"
        assert exc.value.detail["value"] == "0.0.0.0"
        assert exc.value.code == "non_loopback_bind"

    def test_the_message_explains_why(self) -> None:
        """A refusal the operator does not understand gets worked around."""
        with pytest.raises(NonLoopbackBindError) as exc:
            assert_loopback("10.0.0.5", setting="proxy.listen_host")
        assert "loopback" in str(exc.value).lower()


class TestDefaults:
    def test_defaults_validate(self) -> None:
        assert Config().validate() is not None

    def test_default_ports(self) -> None:
        cfg = Config()
        assert cfg.proxy.listen_port == 8080
        assert cfg.control.listen_port == 8081

    def test_default_buffering_matches_spec(self) -> None:
        """REQ PXY-021 — 2 MiB and the documented content-type allowlist."""
        cfg = Config()
        assert cfg.buffering.max_body_bytes == 2 * 1024 * 1024
        assert "text/html" in cfg.buffering.content_types
        assert "application/json" in cfg.buffering.content_types

    def test_default_budget_matches_spec(self) -> None:
        """REQ PXY-026."""
        assert Config().budget.per_flow_ms == 250.0

    def test_redaction_is_on_by_default(self) -> None:
        """REQ CAP-040 — opt-out, never opt-in."""
        cfg = Config()
        assert cfg.redaction.enabled
        assert "cookie" in cfg.redaction.header_patterns
        assert "authorization" in cfg.redaction.header_patterns
        assert "password" in cfg.redaction.json_key_patterns

    def test_quarantine_default_matches_spec(self) -> None:
        """REQ MOD-025."""
        assert Config().modules.quarantine_after_failures == 10


class TestValidation:
    def test_rejects_non_loopback_proxy_host(self) -> None:
        cfg = Config()
        cfg.proxy.listen_host = "0.0.0.0"
        with pytest.raises(NonLoopbackBindError):
            cfg.validate()

    def test_rejects_non_loopback_control_host(self) -> None:
        cfg = Config()
        cfg.control.listen_host = "192.168.1.5"
        with pytest.raises(NonLoopbackBindError):
            cfg.validate()

    def test_rejects_colliding_ports(self) -> None:
        cfg = Config()
        cfg.control.listen_port = cfg.proxy.listen_port
        with pytest.raises(ConfigError, match="must differ"):
            cfg.validate()

    @pytest.mark.parametrize("port", [0, -1, 70000])
    def test_rejects_out_of_range_ports(self, port: int) -> None:
        cfg = Config()
        cfg.proxy.listen_port = port
        with pytest.raises(ConfigError, match="not a valid port"):
            cfg.validate()

    def test_rejects_non_positive_budget(self) -> None:
        cfg = Config()
        cfg.budget.per_flow_ms = 0
        with pytest.raises(ConfigError, match="per_flow_ms"):
            cfg.validate()

    @pytest.mark.parametrize("field,value", [("ring_max_flows", 0), ("ring_max_bytes", -1)])
    def test_rejects_non_positive_ring_bounds(self, field: str, value: int) -> None:
        cfg = Config()
        setattr(cfg.capture, field, value)
        with pytest.raises(ConfigError, match="ring bounds"):
            cfg.validate()

    def test_rejects_zero_quarantine_threshold(self) -> None:
        cfg = Config()
        cfg.modules.quarantine_after_failures = 0
        with pytest.raises(ConfigError, match="quarantine"):
            cfg.validate()

    def test_rejects_unknown_log_level(self) -> None:
        cfg = Config()
        cfg.logging.level = "chatty"
        with pytest.raises(ConfigError, match="not a known level"):
            cfg.validate()

    def test_to_dict_is_serializable(self) -> None:
        payload = Config().to_dict()
        assert payload["control"]["listen_port"] == 8081
        assert isinstance(payload["buffering"]["content_types"], tuple | list)


class TestPrecedence:
    def test_file_overrides_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  listen_port: 9001\n")
        assert load_config(path, env={}).control.listen_port == 9001

    def test_env_overrides_file(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  listen_port: 9001\n")
        cfg = load_config(path, env={"PPORLOCK_CONTROL_LISTEN_PORT": "9002"})
        assert cfg.control.listen_port == 9002

    def test_cli_overrides_env(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  listen_port: 9001\n")
        cfg = load_config(
            path,
            env={"PPORLOCK_CONTROL_LISTEN_PORT": "9002"},
            overrides={"control": {"listen_port": 9003}},
        )
        assert cfg.control.listen_port == 9003

    def test_env_values_are_coerced_to_the_default_type(self) -> None:
        """Environment variables are strings; an int setting must stay an int."""
        cfg = load_config(env={"PPORLOCK_CONTROL_LISTEN_PORT": "9999"})
        assert cfg.control.listen_port == 9999
        assert isinstance(cfg.control.listen_port, int)

    @pytest.mark.parametrize(
        "raw,expected", [("true", True), ("1", True), ("on", True), ("false", False), ("no", False)]
    )
    def test_boolean_coercion_from_env(self, raw: str, expected: bool) -> None:
        cfg = load_config(env={"PPORLOCK_MODULES_WATCH": raw})
        assert cfg.modules.watch is expected

    def test_float_coercion(self) -> None:
        cfg = load_config(env={"PPORLOCK_BUDGET_PER_FLOW_MS": "500"})
        assert cfg.budget.per_flow_ms == 500.0

    def test_list_becomes_a_tuple(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("buffering:\n  content_types: [text/html]\n")
        assert load_config(path, env={}).buffering.content_types == ("text/html",)

    def test_top_level_scalar_setting(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("state_dir: /tmp/pporlock-test\n")
        assert load_config(path, env={}, validate=False).state_dir == "/tmp/pporlock-test"

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """A fresh install has no config file and must still start."""
        assert load_config(tmp_path / "nope.yaml", env={}).control.listen_port == 8081

    def test_unprefixed_env_vars_are_ignored(self) -> None:
        cfg = load_config(env={"PATH": "/usr/bin", "HOME": "/root"})
        assert cfg.control.listen_port == 8081


class TestStrictness:
    def test_unknown_section_is_rejected(self, tmp_path: Path) -> None:
        """Strict parsing: a typo must fail loudly, not be silently ignored."""
        path = tmp_path / "config.yaml"
        path.write_text("proxxy:\n  listen_port: 9001\n")
        with pytest.raises(ConfigError, match="unknown configuration section"):
            load_config(path, env={})

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  listen_prot: 9001\n")
        with pytest.raises(ConfigError, match="unknown configuration key"):
            load_config(path, env={})

    def test_invalid_yaml_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  - [unbalanced\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(path, env={})

    def test_non_mapping_top_level_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(ConfigError, match="must be a mapping"):
            load_config(path, env={})

    def test_mapping_for_a_scalar_setting_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("state_dir:\n  nested: value\n")
        with pytest.raises(ConfigError, match="does not take a mapping"):
            load_config(path, env={})

    def test_uninterpretable_value_names_the_setting(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("control:\n  listen_port: not-a-number\n")
        with pytest.raises(ConfigError, match="listen_port"):
            load_config(path, env={})

    def test_empty_file_is_treated_as_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("")
        assert load_config(path, env={}).control.listen_port == 8081

    def test_validation_can_be_deferred(self) -> None:
        """The API needs to load-then-report rather than raise on a bad PUT."""
        cfg = load_config(env={"PPORLOCK_CONTROL_LISTEN_HOST": "0.0.0.0"}, validate=False)
        assert cfg.control.listen_host == "0.0.0.0"
        with pytest.raises(NonLoopbackBindError):
            cfg.validate()


class TestStateDirCascade:
    """OI-10 — a configured state_dir must move the paths derived from it.

    Setting ``state_dir`` used to move the token, the sessions, the profiles and
    ``rules.yaml`` while leaving modules loading from ``~/.pporlock/modules``.
    The E2E test that found it was reading the developer's real modules while
    believing it had an isolated state directory.
    """

    def test_a_configured_state_dir_moves_the_modules_root(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(f"state_dir: {tmp_path / 'state'}\n")
        cfg = load_config(path, env={})
        assert cfg.modules.root == str(tmp_path / "state" / "modules")

    def test_an_explicit_modules_root_outranks_the_cascade(self, tmp_path: Path) -> None:
        """A value someone wrote down is a decision; the cascade is a default."""
        path = tmp_path / "config.yaml"
        path.write_text(
            f"state_dir: {tmp_path / 'state'}\nmodules:\n  root: {tmp_path / 'elsewhere'}\n"
        )
        cfg = load_config(path, env={})
        assert cfg.modules.root == str(tmp_path / "elsewhere")

    def test_an_explicit_modules_root_equal_to_the_default_is_still_explicit(
        self, tmp_path: Path
    ) -> None:
        default_root = str(DEFAULT_STATE_DIR / "modules")
        path = tmp_path / "config.yaml"
        path.write_text(f"state_dir: {tmp_path / 'state'}\nmodules:\n  root: {default_root}\n")
        assert load_config(path, env={}).modules.root == default_root

    def test_the_env_can_set_the_state_dir_and_the_cascade_follows(self, tmp_path: Path) -> None:
        cfg = load_config(env={"PPORLOCK_STATE_DIR": str(tmp_path / "envstate")})
        assert cfg.modules.root == str(tmp_path / "envstate" / "modules")

    def test_a_cli_override_of_modules_root_wins(self, tmp_path: Path) -> None:
        cfg = load_config(
            env={"PPORLOCK_STATE_DIR": str(tmp_path / "envstate")},
            overrides={"modules": {"root": str(tmp_path / "cli")}},
        )
        assert cfg.modules.root == str(tmp_path / "cli")

    def test_the_default_state_dir_leaves_the_default_root_alone(self) -> None:
        assert load_config(env={}).modules.root == str(DEFAULT_STATE_DIR / "modules")

    def test_constructing_a_config_with_a_state_dir_cascades_too(self, tmp_path: Path) -> None:
        """``Config(state_dir=...)`` is what most tests do, and it was the
        shape that silently read the developer's real modules."""
        assert Config(state_dir=str(tmp_path)).modules.root == str(tmp_path / "modules")

    def test_an_explicit_root_passed_to_the_constructor_is_kept(self, tmp_path: Path) -> None:
        cfg = Config(state_dir=str(tmp_path), modules=ModulesConfig(root=str(tmp_path / "custom")))
        assert cfg.modules.root == str(tmp_path / "custom")
