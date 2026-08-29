"""The paired extension survives a daemon restart — OI-19, REQ API-012, EXT-002.

Pairing was held only in memory. The bearer token *is* persisted, so after a
restart the extension still presented a valid token from an origin the policy
no longer knew, and every call came back 403 "origin not permitted". The daemon
is a launchd agent that restarts at login, so this was routine, and the symptom
pointed away from the cause: nothing about the message suggests re-pairing.

This is structural rule 8's other half. OI-8 and OI-9 moved module enablement
and the active profile into state_dir sidecars; the pairing was missed, and had
no test asserting it outlived the process.
"""

from __future__ import annotations

from pathlib import Path

from pporlock.control.auth import PAIRED_FILENAME, OriginPolicy

EXTENSION = "a" * 32
ORIGIN = f"chrome-extension://{EXTENSION}"


def _policy(state_dir: Path) -> OriginPolicy:
    return OriginPolicy("127.0.0.1", 8081, state_path=state_dir / PAIRED_FILENAME)


def test_pairing_outlives_the_process(tmp_path: Path) -> None:
    """The reported bug, pinned: pair, restart, still allowed.

    The second policy is a genuinely new object, which is what a restart is.
    """
    first = _policy(tmp_path)
    first.pair_extension(ORIGIN)
    assert first.allows(ORIGIN)

    restarted = _policy(tmp_path)

    assert restarted.extension_id == EXTENSION
    assert restarted.allows(ORIGIN), "a restart silently revoked the paired extension"


def test_an_unpaired_daemon_still_refuses_an_extension(tmp_path: Path) -> None:
    """Persistence must not become a blanket allow for extension origins."""
    assert not _policy(tmp_path).allows(ORIGIN)


def test_a_different_extension_is_still_refused(tmp_path: Path) -> None:
    """Only the paired id is restored, not the chrome-extension scheme."""
    _policy(tmp_path).pair_extension(ORIGIN)

    other = f"chrome-extension://{'b' * 32}"

    assert not _policy(tmp_path).allows(other)


def test_a_hand_edited_id_cannot_widen_the_allowlist(tmp_path: Path) -> None:
    """The sidecar decides who may drive the control API, so it is validated.

    It is not a secret — an extension id rides in every Origin header — but a
    file that is trusted verbatim would let anyone who can write to state_dir
    nominate an allowed origin. Garbage is discarded, not accepted.
    """
    (tmp_path / PAIRED_FILENAME).write_text("../../evil")

    policy = _policy(tmp_path)

    assert policy.extension_id is None
    assert not policy.allows("chrome-extension://../../evil")


def test_a_corrupt_sidecar_does_not_stop_the_daemon(tmp_path: Path) -> None:
    """Unpaired-and-running beats refusing to start over a bad file.

    `pporlock pair` recovers from unpaired. Nothing recovers from a daemon that
    will not boot.
    """
    (tmp_path / PAIRED_FILENAME).write_text("\x00 not an id at all\n")

    policy = _policy(tmp_path)

    assert policy.extension_id is None
    assert policy.allows("http://127.0.0.1:8081"), "the web UI must still work"


def test_an_explicit_extension_id_is_not_overwritten_by_the_file(tmp_path: Path) -> None:
    """A caller that names an id means it — tests and the CLI both do."""
    (tmp_path / PAIRED_FILENAME).write_text("c" * 32)

    policy = OriginPolicy(
        "127.0.0.1", 8081, extension_id=EXTENSION, state_path=tmp_path / PAIRED_FILENAME
    )

    assert policy.extension_id == EXTENSION


def test_the_sidecar_holds_nothing_secret(tmp_path: Path) -> None:
    """Pinned deliberately: this file must never grow into a credential store.

    It holds one extension id and nothing else. If that changes, the redaction
    argument in the docstring stops being true and this test should fail first.
    """
    _policy(tmp_path).pair_extension(ORIGIN)

    assert (tmp_path / PAIRED_FILENAME).read_text() == EXTENSION


def test_pairing_works_when_no_state_path_is_configured(tmp_path: Path) -> None:
    """Persistence is additive — the in-memory behaviour is unchanged without it."""
    policy = OriginPolicy("127.0.0.1", 8081)

    policy.pair_extension(ORIGIN)

    assert policy.allows(ORIGIN)
