"""``Profile.exclusions_add``, applied — REQ MOD-044, OI-9.

The field was parsed, persisted, returned by the API and never applied to
anything. Closing it needed semantics rather than a fix, and these are them:

* the effective list is the user's base list **plus** the active profile's
  additions, the additions tagged ``source: "profile"``;
* activating recomputes from the base, so switching away takes the outgoing
  profile's entries off;
* the base is never mutated by a profile — a ``PUT /exclusions`` round trip
  cannot silently adopt a profile's entries as the user's own;
* and a connection **already tunnelled cannot be un-tunnelled**, which is a
  property of TLS and is asserted here rather than left to be discovered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient

from pporlock.addon.interceptor import Interceptor
from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import ControlApp
from pporlock.control.events import EventHub
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList
from pporlock.engine.profiles import ProfileManager

from .test_interceptor import client_hello

BASE = ExclusionList(
    [
        ExclusionEntry("*.apple.com", "OS update", "default"),
        ExclusionEntry("bank.example", "pinned", "user"),
    ]
)


def write_profile(root: Path, name: str, **fields: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.yaml").write_text(yaml.safe_dump({"name": name, **fields}, sort_keys=False))


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    config = Config()
    config.state_dir = str(tmp_path)
    config.modules.root = str(tmp_path / "modules")

    profiles = ProfileManager(tmp_path / "profiles")
    write_profile(
        tmp_path / "profiles",
        "banking",
        modules=[],
        exclusions_add=["*.stripe.com", "10.0.0.0/8"],
    )
    write_profile(tmp_path / "profiles", "plain", modules=[])

    interceptor = Interceptor(config, exclusions=ExclusionList(list(BASE.entries)))
    return ControlApp(
        config,
        ring=RingBuffer(),
        interceptor=interceptor,
        events=EventHub(),
        profiles=profiles,
        base_exclusions=ExclusionList(list(BASE.entries)),
    )


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def headers(app: ControlApp) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.tokens.ensure()}", "X-Pporlock-Client": "ui"}


def patterns(payload: dict[str, Any]) -> set[str]:
    return {e["pattern"] for e in payload["entries"]}


def sources(payload: dict[str, Any]) -> dict[str, str]:
    return {e["pattern"]: e["source"] for e in payload["entries"]}


class TestActivation:
    def test_the_default_profile_adds_nothing(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        assert patterns(client.get("/exclusions", headers=headers).json()) == {
            "*.apple.com",
            "bank.example",
        }

    def test_activating_a_profile_adds_its_exclusions(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """REQ MOD-044. Parsed and stored since Sprint 11; applied since OI-9."""
        client.post("/profiles/banking/activate", headers=headers, json={})
        assert patterns(client.get("/exclusions", headers=headers).json()) == {
            "*.apple.com",
            "bank.example",
            "*.stripe.com",
            "10.0.0.0/8",
        }

    def test_the_additions_reach_the_live_decision_path(self, app: ControlApp) -> None:
        """The list the *interceptor* consults, not a copy the app keeps.

        ``tls_clienthello`` reads ``interceptor.exclusions``; an app-side list
        nothing installed would be OI-11 in miniature.
        """
        assert app.interceptor is not None
        assert app.interceptor.exclusions.should_exclude("api.stripe.com") is False
        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        assert app.interceptor.exclusions.should_exclude("api.stripe.com") is True

    def test_the_evaluator_sees_the_same_list_as_the_interceptor(self, app: ControlApp) -> None:
        """They held separate references and only one was ever updated, so a dry
        run stopped predicting live behaviour after any exclusion change."""
        assert app.interceptor is not None
        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        assert app.interceptor.evaluator.exclusions is app.interceptor.exclusions

    def test_switching_away_removes_that_profiles_additions(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        client.post("/profiles/banking/activate", headers=headers, json={})
        client.post("/profiles/plain/activate", headers=headers, json={})
        assert patterns(client.get("/exclusions", headers=headers).json()) == {
            "*.apple.com",
            "bank.example",
        }

    def test_switching_between_profiles_swaps_the_additions(
        self, client: TestClient, headers: dict[str, str], tmp_path: Path
    ) -> None:
        write_profile(tmp_path / "profiles", "media", modules=[], exclusions_add=["*.netflix.com"])
        client.post("/profiles/banking/activate", headers=headers, json={})
        client.post("/profiles/media/activate", headers=headers, json={})
        found = patterns(client.get("/exclusions", headers=headers).json())
        assert "*.netflix.com" in found
        assert "*.stripe.com" not in found

    def test_deleting_the_active_profile_removes_its_additions(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """Deleting falls back to ``default``, which has none (REQ MOD-041)."""
        client.post("/profiles/plain/activate", headers=headers, json={})
        client.post("/profiles/banking/activate", headers=headers, json={})
        assert client.delete("/profiles/banking", headers=headers).status_code == 204
        assert "*.stripe.com" not in patterns(client.get("/exclusions", headers=headers).json())


class TestProvenanceOfAnEntry:
    def test_a_profile_entry_says_it_came_from_the_profile(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """``source`` is what stops a UI inviting someone to 'remove' an entry
        that comes straight back on the next recompute."""
        client.post("/profiles/banking/activate", headers=headers, json={})
        by_source = sources(client.get("/exclusions", headers=headers).json())
        assert by_source["*.stripe.com"] == "profile"
        assert by_source["bank.example"] == "user"
        assert by_source["*.apple.com"] == "default"

    def test_a_tunnelled_connection_reports_the_profile_as_its_source(
        self, app: ControlApp
    ) -> None:
        """The passthrough record names why it was tunnelled (REQ PXY-015), and
        "because the active profile said so" is a different answer from
        "because you configured it"."""
        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        decision = app.exclusions.decide("api.stripe.com")
        assert decision.excluded is True
        assert decision.source == "profile"


class TestTheBaseListIsNeverMutated:
    def test_a_profiles_additions_do_not_become_the_users_own(
        self, client: TestClient, headers: dict[str, str], app: ControlApp
    ) -> None:
        client.post("/profiles/banking/activate", headers=headers, json={})
        assert {e.pattern for e in app.base_exclusions.entries} == {
            "*.apple.com",
            "bank.example",
        }

    def test_a_get_then_put_round_trip_does_not_adopt_them(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        """The web UI edits the list it was shown. If a PUT wrote the effective
        list back as the base, a profile's entries would survive switching away
        from it — and there would be no way to get rid of them."""
        client.post("/profiles/banking/activate", headers=headers, json={})
        shown = client.get("/exclusions", headers=headers).json()
        client.put("/exclusions", headers=headers, json=shown)
        client.post("/profiles/plain/activate", headers=headers, json={})
        assert patterns(client.get("/exclusions", headers=headers).json()) == {
            "*.apple.com",
            "bank.example",
        }

    def test_a_put_still_replaces_the_users_own_entries(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.put(
            "/exclusions",
            headers=headers,
            json={"entries": [{"pattern": "*.example.test", "comment": "mine"}]},
        )
        assert patterns(response.json()) == {"*.example.test"}

    def test_a_put_under_an_active_profile_keeps_the_profiles_additions(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        client.post("/profiles/banking/activate", headers=headers, json={})
        response = client.put(
            "/exclusions",
            headers=headers,
            json={"entries": [{"pattern": "*.example.test", "comment": "mine"}]},
        )
        assert patterns(response.json()) == {"*.example.test", "*.stripe.com", "10.0.0.0/8"}


class TestAnAlreadyTunnelledConnectionCannotBeUntunnelled:
    """The honest half of OI-9, and the reason it needed a decision at all.

    ``ignore_connection`` means mitmproxy never terminated the TLS. There are no
    plaintext bytes to reach into and no session key to acquire after the fact,
    so a list change cannot reach backwards into a connection already tunnelled.
    The rule is: **the new list applies to new connections**. Chrome holds
    keep-alive connections, so a host can keep tunnelling for as long as one
    stays open after the entry that excluded it has gone.
    """

    def test_the_decision_is_made_once_at_the_clienthello(self, app: ControlApp) -> None:
        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        interceptor = app.interceptor
        assert interceptor is not None

        data = client_hello("api.stripe.com")
        interceptor.tls_clienthello(data)
        assert data.ignore_connection is True

        # Switch to a profile that does not exclude it. Nothing re-decides for
        # the connection that is already up.
        app.profiles.activate("plain")  # type: ignore[union-attr]
        app.apply_exclusions()
        assert data.ignore_connection is True

    def test_the_new_list_governs_the_next_connection(self, app: ControlApp) -> None:
        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        interceptor = app.interceptor
        assert interceptor is not None
        interceptor.tls_clienthello(client_hello("api.stripe.com"))

        app.profiles.activate("plain")  # type: ignore[union-attr]
        app.apply_exclusions()
        fresh = client_hello("api.stripe.com")
        interceptor.tls_clienthello(fresh)
        assert fresh.ignore_connection is False

    def test_an_addition_does_not_reach_into_a_connection_being_decrypted(
        self, app: ControlApp
    ) -> None:
        """The inverse, and the same reason: interception was already chosen."""
        interceptor = app.interceptor
        assert interceptor is not None
        already_up = client_hello("api.stripe.com")
        interceptor.tls_clienthello(already_up)
        assert already_up.ignore_connection is False

        app.profiles.activate("banking")  # type: ignore[union-attr]
        app.apply_exclusions()
        assert already_up.ignore_connection is False

    def test_there_is_no_other_hook_that_revisits_the_decision(self) -> None:
        """A guard, not a behaviour test.

        If a later sprint adds a second place that sets ``ignore_connection``,
        the docstring above stops being true and this fails rather than the
        claim quietly rotting.
        """
        import inspect

        from pporlock.addon import interceptor as module

        source = inspect.getsource(module)
        assert source.count("ignore_connection = True") == 1
