"""Declared module settings — the manifest half of module-specific configuration.

A module's ``config`` block has always been free-form and passed to ``ctx.config``
(SPEC-0 §8.2), which is enough for an author editing their own YAML and no use at
all to someone who just wants to change one value. There was no way to *ask* a
module what it can be configured with, so the web UI could offer nothing but the
file editor — and editing a file to flip a switch is exactly the interaction the
module library exists to avoid for ``enabled``.

``settings:`` is that missing declaration: a flat, ordered list of fields, each
with a type, a label and a default. The daemon validates a proposed value
against it and the web UI renders a form from it. Deliberately **not** JSON
Schema — the expressive half of JSON Schema is unrenderable as a form, and a
declaration that can say more than the UI can show is a declaration whose author
will be surprised. Six types, no nesting, no conditionals.

Adding an optional manifest key is a minor module-API change under SPEC-0 §8.1,
so no version bump: a module that declares nothing behaves exactly as before.

**Values are not secrets.** There is no ``password`` type on purpose. A declared
setting's value is persisted in the module-state sidecar in clear text and is
returned by ``GET /modules/{name}`` to any holder of the bearer token, so a
module that needs a credential must take it from the environment rather than
from here. The absence of the type is the documentation of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Setting keys are the keys of ``ctx.config``, so they follow Python-ish
#: identifier shape rather than the module slug's stricter rule: an author
#: writing ``ctx.config["strip_client_hints"]`` should not have to spell it
#: with a hyphen.
SETTING_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: What a field may be. Each maps to one renderable control and one coercion.
SETTING_TYPES = frozenset({"string", "text", "boolean", "integer", "enum", "string_list"})

#: Keys a setting declaration may carry. Strict for the same reason the manifest
#: is (REQ MOD-014): a misspelled ``defualt`` that is silently ignored produces a
#: form whose default is wrong and an author who cannot see why.
KNOWN_SETTING_KEYS = frozenset(
    {
        "key",
        "label",
        "type",
        "description",
        "default",
        "options",
        "placeholder",
        "min",
        "max",
    }
)

KNOWN_OPTION_KEYS = frozenset({"value", "label", "description"})


class SettingsError(Exception):
    """A settings *declaration* is malformed. Raised at load, not at use."""


@dataclass(frozen=True, slots=True)
class SettingOption:
    """One choice of an ``enum`` field."""

    value: str
    label: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label, "description": self.description}


@dataclass(frozen=True, slots=True)
class ModuleSetting:
    """One declared, user-settable field."""

    key: str
    type: str
    label: str
    description: str = ""
    default: Any = None
    options: tuple[SettingOption, ...] = ()
    placeholder: str = ""
    min: int | None = None
    max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "label": self.label,
            "description": self.description,
            "default": self.default,
        }
        if self.type == "enum":
            payload["options"] = [o.to_dict() for o in self.options]
        if self.placeholder:
            payload["placeholder"] = self.placeholder
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        return payload


def _option(raw: Any, *, key: str, index: int) -> SettingOption:
    if isinstance(raw, str):
        # `options: [a, b, c]` — the common case, where value and label are the
        # same word and spelling it twice is noise.
        return SettingOption(value=raw, label=raw)
    if not isinstance(raw, dict):
        raise SettingsError(f"setting {key!r}: option {index} must be a string or a mapping")
    unknown = set(raw) - KNOWN_OPTION_KEYS
    if unknown:
        raise SettingsError(f"setting {key!r}: unknown option keys: {', '.join(sorted(unknown))}")
    value = raw.get("value")
    if not isinstance(value, str) or value == "":
        raise SettingsError(f"setting {key!r}: option {index} needs a non-empty string 'value'")
    label = raw.get("label")
    return SettingOption(
        value=value,
        label=str(label) if isinstance(label, str) and label else value,
        description=str(raw.get("description") or ""),
    )


def parse_settings(raw: Any) -> tuple[ModuleSetting, ...]:
    """Parse a manifest's ``settings:`` block. Raises ``SettingsError``.

    Raising rather than returning a partial list: a module whose settings block
    is half-understood would render a form missing the field the author cared
    about, and the module would run with a default nobody chose. The loader
    turns this into a load error, which is visible.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SettingsError("'settings' must be a list")

    parsed: list[ModuleSetting] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SettingsError(f"setting {index} must be a mapping")
        unknown = set(entry) - KNOWN_SETTING_KEYS
        if unknown:
            raise SettingsError(f"setting {index}: unknown keys: {', '.join(sorted(unknown))}")

        key = entry.get("key")
        if not isinstance(key, str) or not SETTING_KEY_PATTERN.match(key):
            raise SettingsError(
                f"setting {index}: 'key' must match {SETTING_KEY_PATTERN.pattern} (got {key!r})"
            )
        if key in seen:
            raise SettingsError(f"setting {key!r} is declared twice")
        seen.add(key)

        type_ = entry.get("type", "string")
        if type_ not in SETTING_TYPES:
            raise SettingsError(
                f"setting {key!r}: unknown type {type_!r}; "
                f"expected one of {', '.join(sorted(SETTING_TYPES))}"
            )

        options: tuple[SettingOption, ...] = ()
        if type_ == "enum":
            raw_options = entry.get("options")
            if not isinstance(raw_options, list) or not raw_options:
                raise SettingsError(f"setting {key!r}: an enum needs a non-empty 'options' list")
            options = tuple(_option(o, key=key, index=i) for i, o in enumerate(raw_options))
            values = [o.value for o in options]
            if len(set(values)) != len(values):
                raise SettingsError(f"setting {key!r}: duplicate option values")
        elif "options" in entry:
            raise SettingsError(f"setting {key!r}: 'options' is only meaningful on an enum")

        bounds: dict[str, int | None] = {"min": None, "max": None}
        for bound in ("min", "max"):
            if bound not in entry:
                continue
            if type_ != "integer":
                raise SettingsError(f"setting {key!r}: {bound!r} is only meaningful on an integer")
            value = entry[bound]
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsError(f"setting {key!r}: {bound!r} must be an integer")
            bounds[bound] = value

        setting = ModuleSetting(
            key=key,
            type=type_,
            label=str(entry.get("label") or key),
            description=str(entry.get("description") or ""),
            default=None,
            options=options,
            placeholder=str(entry.get("placeholder") or ""),
            min=bounds["min"],
            max=bounds["max"],
        )

        # The default goes through the same coercion a user's value does, so a
        # declaration whose default its own field would reject is a load error
        # rather than a form that is invalid the moment it opens.
        raw_default = entry.get("default", _implicit_default(setting))
        try:
            default = coerce_value(setting, raw_default)
        except ValueError as exc:
            raise SettingsError(f"setting {key!r}: invalid default — {exc}") from exc

        parsed.append(
            ModuleSetting(
                key=setting.key,
                type=setting.type,
                label=setting.label,
                description=setting.description,
                default=default,
                options=setting.options,
                placeholder=setting.placeholder,
                min=setting.min,
                max=setting.max,
            )
        )
    return tuple(parsed)


def _implicit_default(setting: ModuleSetting) -> Any:
    """What a field means when its author states no default.

    An enum's first option rather than ``None``: "unset" is not one of the
    choices the form can show, and a module reading ``ctx.config["preset"]``
    should never get a value that is not in its own list.
    """
    if setting.type == "boolean":
        return False
    if setting.type == "integer":
        return setting.min if setting.min is not None else 0
    if setting.type == "string_list":
        return []
    if setting.type == "enum":
        return setting.options[0].value if setting.options else ""
    return ""


def coerce_value(setting: ModuleSetting, value: Any) -> Any:
    """One value, checked and normalised for its field. Raises ``ValueError``.

    Strict about types rather than helpful about them: a ``"true"`` accepted as
    a boolean and a ``"3"`` accepted as an integer make the stored shape depend
    on which client wrote it, and module code then has to handle both. JSON has
    the types; callers should send them.
    """
    if setting.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"expected a boolean, got {type(value).__name__}")
        return value

    if setting.type == "integer":
        # bool is a subclass of int, so it is refused first: `true` is not 1 here.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"expected an integer, got {type(value).__name__}")
        if setting.min is not None and value < setting.min:
            raise ValueError(f"must be at least {setting.min}")
        if setting.max is not None and value > setting.max:
            raise ValueError(f"must be at most {setting.max}")
        return value

    if setting.type == "enum":
        allowed = [o.value for o in setting.options]
        if value not in allowed:
            raise ValueError(f"expected one of {', '.join(allowed)}")
        return value

    if setting.type == "string_list":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError("expected a list of strings")
        # Blank entries are dropped rather than rejected: they are what a
        # textarea produces from a trailing newline, and refusing the form over
        # one would be obtuse.
        return [v.strip() for v in value if v.strip()]

    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    return value


def coerce_config(
    settings: tuple[ModuleSetting, ...], values: Any
) -> tuple[dict[str, Any], list[str]]:
    """Check a proposed override map against a module's declaration.

    Returns ``(accepted, errors)``. Errors are collected rather than raised on
    the first one, so a form with two bad fields reports both.

    A module that declares no settings accepts no configuration through this
    path. That is the point: ``config`` in the manifest stays the author's, and
    the API only writes what the author asked to be writable.
    """
    if not isinstance(values, dict):
        return {}, ["config must be an object"]

    declared = {s.key: s for s in settings}
    accepted: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in values.items():
        setting = declared.get(str(key))
        if setting is None:
            errors.append(f"{key}: not a declared setting")
            continue
        try:
            accepted[setting.key] = coerce_value(setting, value)
        except ValueError as exc:
            errors.append(f"{key}: {exc}")
    return accepted, errors


def effective_config(
    settings: tuple[ModuleSetting, ...],
    manifest_config: dict[str, Any],
    overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    """What ``ctx.config`` holds: declared defaults, the manifest, then the user.

    The manifest wins over a field's declared default because an author who
    writes both means the ``config`` block — it is the value the module was
    shipped with. The user's override wins over both, and storing *only* what
    the user actually changed is what lets a later edit to the manifest still
    move an untouched value.
    """
    config: dict[str, Any] = {s.key: s.default for s in settings}
    config.update(manifest_config)
    for key, value in (overrides or {}).items():
        # An override for a key that is no longer declared is dropped, not
        # carried: the module has been rewritten since, and handing it a value
        # its current code never asked for is how a stale toggle survives a
        # rename and quietly does nothing.
        if any(s.key == key for s in settings):
            config[key] = value
    return config


__all__ = [
    "KNOWN_SETTING_KEYS",
    "SETTING_TYPES",
    "ModuleSetting",
    "SettingOption",
    "SettingsError",
    "coerce_config",
    "coerce_value",
    "effective_config",
    "parse_settings",
]
