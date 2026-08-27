"""Error hierarchy and code stability. SPEC-0 §6.2."""

from __future__ import annotations

import pytest

from pporlock import errors

ALL_ERRORS = [
    errors.PporlockError,
    errors.ConfigError,
    errors.NonLoopbackBindError,
    errors.ModuleLoadError,
    errors.ModuleApiVersionError,
    errors.ModuleRuntimeError,
    errors.RuleValidationError,
    errors.TransformError,
    errors.AssetPathError,
    errors.SessionError,
    errors.AuthError,
    errors.PairingError,
]


@pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
def test_every_error_declares_a_code(cls: type[errors.PporlockError]) -> None:
    assert isinstance(cls.code, str) and cls.code


def test_codes_are_unique_across_the_hierarchy() -> None:
    """Clients branch on the code, so two errors sharing one is a contract bug."""
    codes = [c.code for c in ALL_ERRORS]
    duplicates = {c for c in codes if codes.count(c) > 1}
    assert not duplicates, f"duplicate error codes: {duplicates}"


@pytest.mark.parametrize("cls", ALL_ERRORS, ids=lambda c: c.__name__)
def test_every_error_is_a_pporlock_error(cls: type[errors.PporlockError]) -> None:
    assert issubclass(cls, errors.PporlockError)


def test_to_dict_matches_the_wire_shape() -> None:
    err = errors.SessionError("cannot open", session_id="abc")
    assert err.to_dict() == {
        "code": "session_error",
        "message": "cannot open",
        "detail": {"session_id": "abc"},
    }


def test_detail_is_empty_when_not_supplied() -> None:
    assert errors.AuthError("nope").to_dict()["detail"] == {}


def test_rule_validation_error_carries_location() -> None:
    """A rule error without a location is nearly useless in the editor."""
    err = errors.RuleValidationError("bad host glob", module="m", rule_index=3, field="host")
    assert err.module == "m"
    assert err.rule_index == 3
    assert err.field == "host"
    assert err.to_dict()["detail"]["rule_index"] == 3


def test_specialisations_inherit_sensibly() -> None:
    assert issubclass(errors.NonLoopbackBindError, errors.ConfigError)
    assert issubclass(errors.ModuleApiVersionError, errors.ModuleLoadError)


def test_repr_is_useful_in_a_traceback() -> None:
    assert "non_loopback_bind" in repr(errors.NonLoopbackBindError("x"))


def test_message_is_preserved_as_str() -> None:
    assert str(errors.TransformError("regex blew up")) == "regex blew up"
