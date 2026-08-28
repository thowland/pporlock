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

import copy
import ipaddress
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, NonLoopbackBindError

DEFAULT_STATE_DIR = Path.home() / ".pporlock"
ENV_PREFIX = "PPORLOCK_"

#: Path-valued settings whose default is *derived from* ``state_dir`` rather
#: than fixed at import time, and the sub-path each derives (OI-10).
#:
#: ``ModulesConfig.root`` used to be a module-level constant built from
#: ``DEFAULT_STATE_DIR``. Setting ``state_dir`` therefore moved the token, the
#: sessions, the profiles, and ``rules.yaml``, and left modules loading from
#: ``~/.pporlock/modules``. The split-brain layout that produced is not a
#: cosmetic problem: an isolated test state directory silently read the
#: developer's real modules.
STATE_DIR_DERIVED: dict[str, str] = {"modules.root": "modules"}

#: What each derived setting holds when nobody has chosen a value.
_DEFAULT_DERIVED: dict[str, str] = {
    setting: str(DEFAULT_STATE_DIR / relative) for setting, relative in STATE_DIR_DERIVED.items()
}

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
    #: Query-string parameter names whose values are secrets (SPEC-0 §9.2).
    #:
    #: A separate list from ``json_key_patterns`` because the failure modes
    #: differ: a JSON key is matched by substring against a document nobody
    #: navigates to, while a query parameter appears in a URL that is displayed
    #: in the flow table, written into a session file, and echoed in a
    #: ``Referer``. The defaults are the names that actually carry bearer
    #: credentials in a URL — OAuth implicit flows, presigned URLs, and the long
    #: tail of analytics endpoints — rather than everything that could be a key.
    query_patterns: tuple[str, ...] = (
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "api_key",
        "apikey",
        "auth",
        "signature",
        "sig",
        "password",
        "secret",
        "code",
        "session",
        "x-amz-security-token",
        "x-amz-signature",
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
    """Where the daemon's own diagnostics go, and how large they may get.

    Rotation is by size with a retained-file count (REQ PXY-007); see
    ``cli/logs.py`` for why it is copy-and-truncate rather than rename.
    """

    level: str = "info"
    dir: str = str(Path.home() / "Library" / "Logs" / "pporlock")
    max_bytes: int = 8 * 1024 * 1024
    retain: int = 5


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

    def __post_init__(self) -> None:
        """Derive the state-dir-relative defaults for a directly-built Config.

        ``load_config`` does this with real knowledge of which settings the
        caller stated (see ``cascade_state_dir``). Here there is no such record,
        so "still holds the import-time default" stands in for "nobody chose
        it" — which is exactly the case ``Config(state_dir=tmp)`` in a test hits
        (OI-10).
        """
        if self.state_dir == str(DEFAULT_STATE_DIR):
            return
        state_dir = Path(self.state_dir).expanduser()
        for setting, relative in STATE_DIR_DERIVED.items():
            section_name, _, key = setting.partition(".")
            section = getattr(self, section_name)
            if getattr(section, key) == _DEFAULT_DERIVED[setting]:
                setattr(section, key, str(state_dir / relative))

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
        if self.logging.max_bytes <= 0:
            raise ConfigError("logging.max_bytes must be positive", setting="logging.max_bytes")
        if self.logging.retain < 1:
            raise ConfigError("logging.retain must be at least 1", setting="logging.retain")
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


def _apply(
    section: Any, values: dict[str, Any], *, path: str, explicit: set[str] | None = None
) -> None:
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
        if explicit is not None:
            explicit.add(f"{path}.{key}")


def _from_mapping(
    cfg: Config, data: dict[str, Any], *, source: str, explicit: set[str] | None = None
) -> Config:
    for key, value in data.items():
        if not hasattr(cfg, key):
            raise ConfigError(f"unknown configuration section: {key} (from {source})", setting=key)
        current = getattr(cfg, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, path=key, explicit=explicit)
        elif isinstance(value, dict):
            raise ConfigError(f"{key} does not take a mapping (from {source})", setting=key)
        else:
            setattr(cfg, key, _coerce(value, current))
            if explicit is not None:
                explicit.add(key)
    return cfg


def cascade_state_dir(cfg: Config, explicit: set[str]) -> Config:
    """Move the paths derived from ``state_dir`` when it has been configured.

    Only settings the caller did not set for themselves are moved. An explicit
    ``modules.root`` outranks the cascade, because a value someone wrote down
    is a decision and this is only a default.
    """
    state_dir = Path(cfg.state_dir).expanduser()
    for setting, relative in STATE_DIR_DERIVED.items():
        if setting in explicit:
            continue
        section_name, _, key = setting.partition(".")
        setattr(getattr(cfg, section_name), key, str(state_dir / relative))
    return cfg


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """``PPORLOCK_CONTROL_LISTEN_PORT=9000`` -> ``{'control': {'listen_port': '9000'}}``.

    Section names contain no underscores, so the first segment is the section and
    the remainder is the key.
    """
    top_level = {f.name for f in fields(Config)}
    out: dict[str, Any] = {}
    for raw_key, raw_value in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        remainder = raw_key[len(ENV_PREFIX) :].lower()
        # A top-level setting whose own name contains an underscore — the only
        # one today is ``state_dir`` — is matched whole before the split. It was
        # otherwise read as section ``state``, key ``dir``, and rejected: the
        # one setting the layout hangs off could not be set from the
        # environment at all.
        if remainder in top_level or "_" not in remainder:
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
    #: Which settings the caller stated for themselves, at any precedence level.
    #: The state_dir cascade below moves only the ones nobody chose.
    explicit: set[str] = set()

    if path is not None:
        config_path = Path(path)
        if config_path.exists():
            try:
                raw = yaml.safe_load(config_path.read_text()) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"{config_path}: invalid YAML — {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{config_path}: top level must be a mapping")
            _from_mapping(cfg, raw, source=str(config_path), explicit=explicit)

    _from_mapping(
        cfg,
        _env_overrides(env if env is not None else dict(os.environ)),
        source="env",
        explicit=explicit,
    )

    if overrides:
        _from_mapping(cfg, overrides, source="cli", explicit=explicit)

    cascade_state_dir(cfg, explicit)

    return cfg.validate() if validate else cfg


#: Sections ``PUT /config`` may change (REQ CAP-044). Deliberately excludes
#: ``proxy``, ``control``, and ``state_dir``: a listener is already bound by the
#: time the API is reachable, so accepting a new bind address would either be a
#: lie or a way to move a loopback-only listener at runtime. Both are worse than
#: refusing.
SETTABLE_SECTIONS: frozenset[str] = frozenset(
    {"redaction", "buffering", "capture", "budget", "logging"}
)


def update_config(cfg: Config, data: dict[str, Any]) -> Config:
    """A validated copy of ``cfg`` with ``data`` applied.

    A copy rather than an in-place edit so a rejected update leaves the running
    configuration exactly as it was — the same reasoning as compiling a rule set
    before swapping it in.
    """
    unknown = set(data) - SETTABLE_SECTIONS
    if unknown:
        raise ConfigError(
            f"cannot set {', '.join(sorted(unknown))} at runtime",
            setting=sorted(unknown)[0],
        )
    candidate = copy.deepcopy(cfg)
    _from_mapping(candidate, data, source="api")
    return candidate.validate()


def _plain(value: Any) -> Any:
    """Tuples to lists, recursively, so PyYAML can dump the result."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def save_config(cfg: Config, path: Path, sections: frozenset[str] = SETTABLE_SECTIONS) -> None:
    """Persist the settable sections so a change survives a restart.

    Only the settable sections are written. Rewriting the whole file would turn
    every default this build happens to carry into an explicit pin, and the next
    upgrade would silently keep the old values.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except yaml.YAMLError:
            # A file we cannot parse is replaced rather than merged into: the
            # alternative is refusing to save because of unrelated damage.
            existing = {}
    full = cfg.to_dict()
    for name in sorted(sections):
        # Tuples are how the dataclasses hold ordered, immutable lists; YAML has
        # no tuple, and safe_dump refuses one rather than guessing.
        existing[name] = _plain(full[name])
    path.write_text(yaml.safe_dump(existing, sort_keys=True, default_flow_style=False))
