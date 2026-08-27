"""Configuration — SPEC-1 §9.

Precedence, highest first:

    CLI flags  >  environment (PPORLOCK_*)  >  config file  >  defaults

Profile-scoped overrides sit between the file and the defaults and are applied
by the profile manager, not here, because they change on profile activation.

The one rule this module enforces unconditionally: **listen hosts must be
loopback**, asserted rather than defaulted (REQ API-010). pporlock terminates
TLS and holds session cookies in memory. Binding that to a routable interface is
not a configuration we offer, so an attempt to do so fails at startup rather
than producing a quietly exposed daemon.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, NonLoopbackBindError

DEFAULT_STATE_DIR = Path.home() / ".pporlock"
ENV_PREFIX = "PPORLOCK_"

#: Hostnames accepted as loopback in addition to anything in 127.0.0.0/8 or ::1.
LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def is_loopback(host: str) -> bool:
    """True if ``host`` can only reach this machine."""
    candidate = host.strip().lower()
    if not candidate:
        return False
    if candidate in LOOPBACK_NAMES:
        return True
    candidate = candidate.strip("[]")
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def assert_loopback(host: str, *, setting: str) -> str:
    """Return ``host`` if it is loopback, else refuse.

    ``0.0.0.0`` is the dangerous case worth calling out: it reads like a default
    and binds every interface.
    """
    if not is_loopback(host):
        raise NonLoopbackBindError(
            f"{setting}={host!r} is not a loopback address. pporlock terminates TLS "
            f"and holds session cookies in memory; it binds loopback only.",
            setting=setting,
            value=host,
        )
    return host


@dataclass(slots=True)
class ProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080


@dataclass(slots=True)
class ControlConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8081


@dataclass(slots=True)
class BufferingConfig:
    """The stream-or-buffer guard (REQ PXY-021).

    Anything outside these bounds is streamed and therefore not transformable —
    a fact the UI states explicitly, because a silently-skipped transform is
    exactly the failure mode provenance exists to surface.
    """

    max_body_bytes: int = 2 * 1024 * 1024
    content_types: tuple[str, ...] = (
        "text/html",
        "text/css",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
        "application/json",
    )


@dataclass(slots=True)
class BudgetConfig:
    per_flow_ms: float = 250.0
    executor_threshold_bytes: int = 256 * 1024
    executor_workers: int = 4


@dataclass(slots=True)
class CaptureConfig:
    ring_max_flows: int = 2000
    ring_max_bytes: int = 256 * 1024 * 1024
    max_body_bytes: int = 512 * 1024
    session_max_bytes: int = 5 * 1024 * 1024 * 1024


@dataclass(slots=True)
class ModulesConfig:
    root: str = str(DEFAULT_STATE_DIR / "modules")
    quarantine_after_failures: int = 10
    watch: bool = True


@dataclass(slots=True)
class RedactionConfig:
    """Redaction defaults (SPEC-0 §9.2).

    On by default. Applied at write time for sessions so a session file on disk
    never contains the secret (REQ CAP-045).
    """

    enabled: bool = True
    header_patterns: tuple[str, ...] = (
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-auth-token",
    )
    json_key_patterns: tuple[str, ...] = (
        "password",
        "token",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "session",
        "auth",
        "credential",
    )


@dataclass(slots=True)
class LoggingConfig:
    level: str = "info"
    dir: str = str(Path.home() / "Library" / "Logs" / "pporlock")


@dataclass(slots=True)
class Config:
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    buffering: BufferingConfig = field(default_factory=BufferingConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    modules: ModulesConfig = field(default_factory=ModulesConfig)
    redaction: RedactionConfig = field(default_factory=RedactionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    state_dir: str = str(DEFAULT_STATE_DIR)

    def validate(self) -> Config:
        """Enforce the invariants that must hold before anything binds a socket."""
        assert_loopback(self.proxy.listen_host, setting="proxy.listen_host")
        assert_loopback(self.control.listen_host, setting="control.listen_host")

        if self.proxy.listen_port == self.control.listen_port:
            raise ConfigError(
                "proxy.listen_port and control.listen_port must differ",
                port=self.proxy.listen_port,
            )
        for name, port in (
            ("proxy.listen_port", self.proxy.listen_port),
            ("control.listen_port", self.control.listen_port),
        ):
            if not 1 <= port <= 65535:
                raise ConfigError(f"{name}={port} is not a valid port", setting=name)

        if self.budget.per_flow_ms <= 0:
            raise ConfigError("budget.per_flow_ms must be positive")
        if self.capture.ring_max_flows <= 0 or self.capture.ring_max_bytes <= 0:
            raise ConfigError("capture ring bounds must be positive")
        if self.modules.quarantine_after_failures < 1:
            raise ConfigError("modules.quarantine_after_failures must be at least 1")
        if self.logging.level.lower() not in {"debug", "info", "warning", "error"}:
            raise ConfigError(f"logging.level={self.logging.level!r} is not a known level")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ loading ---


def _coerce(value: Any, target: Any) -> Any:
    """Coerce a loaded value to the type of the default it is replacing."""
    if isinstance(target, tuple):
        return tuple(value)
    if isinstance(target, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(target, int) and not isinstance(target, bool):
        return int(value)
    if isinstance(target, float):
        return float(value)
    return value


def _apply(section: Any, values: dict[str, Any], *, path: str) -> None:
    known = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ConfigError(f"unknown configuration key: {path}.{key}", setting=f"{path}.{key}")
        current = getattr(section, key)
        try:
            setattr(section, key, _coerce(value, current))
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{path}.{key}: cannot interpret {value!r} — {exc}", setting=f"{path}.{key}"
            ) from exc


def _from_mapping(cfg: Config, data: dict[str, Any], *, source: str) -> Config:
    for key, value in data.items():
        if not hasattr(cfg, key):
            raise ConfigError(f"unknown configuration section: {key} (from {source})", setting=key)
        current = getattr(cfg, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, path=key)
        elif isinstance(value, dict):
            raise ConfigError(f"{key} does not take a mapping (from {source})", setting=key)
        else:
            setattr(cfg, key, _coerce(value, current))
    return cfg


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """``PPORLOCK_CONTROL_LISTEN_PORT=9000`` -> ``{'control': {'listen_port': '9000'}}``.

    Section names contain no underscores, so the first segment is the section and
    the remainder is the key.
    """
    out: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        remainder = raw_key[len(ENV_PREFIX) :].lower()
        if "_" not in remainder:
            out[remainder] = raw_value
            continue
        section, _, key = remainder.partition("_")
        out.setdefault(section, {})[key] = raw_value
    return out


def load_config(
    path: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
    validate: bool = True,
) -> Config:
    """Build a Config with the documented precedence.

    ``overrides`` carries CLI flags and wins over everything.
    """
    cfg = Config()

    if path is not None:
        config_path = Path(path)
        if config_path.exists():
            try:
                raw = yaml.safe_load(config_path.read_text()) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"{config_path}: invalid YAML — {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{config_path}: top level must be a mapping")
            _from_mapping(cfg, raw, source=str(config_path))

    _from_mapping(cfg, _env_overrides(env if env is not None else dict(os.environ)), source="env")

    if overrides:
        _from_mapping(cfg, overrides, source="cli")

    return cfg.validate() if validate else cfg
