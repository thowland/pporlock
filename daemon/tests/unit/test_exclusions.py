"""ClientHello exclusion list. SPEC-1 §3.5, REQ PXY-013/014/015."""

from __future__ import annotations

from pathlib import Path

import pytest

from pporlock.engine.exclusions import (
    DEFAULT_EXCLUSIONS_PATH,
    ExclusionEntry,
    ExclusionList,
    load_exclusions,
)


@pytest.fixture
def sample() -> ExclusionList:
    return ExclusionList(
        [
            ExclusionEntry("*.apple.com", "update: OS updates", "default"),
            ExclusionEntry("ocsp.*", "pinning: revocation", "default"),
            ExclusionEntry("exact.example.com", "sensitive: exact host", "user"),
            ExclusionEntry("10.0.0.0/8", "private range", "user"),
            ExclusionEntry("::1/128", "loopback v6", "user"),
        ]
    )


class TestSniMatching:
    @pytest.mark.parametrize(
        "sni,pattern",
        [
            ("swscan.apple.com", "*.apple.com"),
            ("www.apple.com", "*.apple.com"),
            ("ocsp.digicert.com", "ocsp.*"),
            ("exact.example.com", "exact.example.com"),
        ],
    )
    def test_matches(self, sample: ExclusionList, sni: str, pattern: str) -> None:
        decision = sample.decide(sni)
        assert decision.excluded
        assert decision.pattern == pattern

    @pytest.mark.parametrize(
        "sni",
        ["example.com", "apple.com.evil.net", "notexact.example.com", "cdn.example.org"],
    )
    def test_does_not_match(self, sample: ExclusionList, sni: str) -> None:
        assert not sample.should_exclude(sni)

    def test_apple_com_itself_does_not_match_a_subdomain_glob(self, sample: ExclusionList) -> None:
        """`*.apple.com` matches subdomains, not the bare apex.

        Worth pinning down: a user writing that pattern and expecting the apex to
        be covered would be silently wrong about a financial or pinning entry.
        """
        assert not sample.should_exclude("apple.com")

    def test_matching_is_case_insensitive(self, sample: ExclusionList) -> None:
        assert sample.should_exclude("WWW.APPLE.COM")

    def test_trailing_dot_is_stripped(self, sample: ExclusionList) -> None:
        """A fully-qualified SNI with a root dot must not slip past."""
        assert sample.should_exclude("www.apple.com.")

    def test_no_sni_falls_through(self, sample: ExclusionList) -> None:
        assert not sample.should_exclude(None)

    def test_empty_sni_falls_through(self, sample: ExclusionList) -> None:
        assert not sample.should_exclude("")

    def test_hostile_sni_characters_are_ignored(self, sample: ExclusionList) -> None:
        """A crafted SNI must not reach the glob matcher at all."""
        assert not sample.should_exclude("../../etc/passwd")
        assert not sample.should_exclude("host with spaces")


class TestIpMatching:
    def test_ipv4_in_range(self, sample: ExclusionList) -> None:
        decision = sample.decide(None, "10.1.2.3")
        assert decision.excluded
        assert decision.pattern == "10.0.0.0/8"

    def test_ipv4_outside_range(self, sample: ExclusionList) -> None:
        assert not sample.should_exclude(None, "192.168.1.1")

    def test_ipv6(self, sample: ExclusionList) -> None:
        assert sample.should_exclude(None, "::1")

    def test_bracketed_ipv6(self, sample: ExclusionList) -> None:
        assert sample.should_exclude(None, "[::1]")

    def test_ipv4_is_not_matched_against_an_ipv6_network(self, sample: ExclusionList) -> None:
        assert not sample.should_exclude(None, "1.2.3.4")

    def test_garbage_ip_is_not_an_error(self, sample: ExclusionList) -> None:
        assert not sample.should_exclude(None, "not-an-ip")

    def test_sni_wins_over_ip_when_both_match(self, sample: ExclusionList) -> None:
        decision = sample.decide("www.apple.com", "10.0.0.1")
        assert decision.pattern == "*.apple.com"


class TestDecisionDetail:
    def test_carries_comment_and_source(self, sample: ExclusionList) -> None:
        """The UI shows why a connection was tunneled; that needs the comment."""
        decision = sample.decide("www.apple.com")
        assert decision.comment == "update: OS updates"
        assert decision.source == "default"

    def test_non_match_carries_nothing(self, sample: ExclusionList) -> None:
        decision = sample.decide("example.com")
        assert decision.pattern is None
        assert decision.comment is None


class TestMutation:
    def test_add(self, sample: ExclusionList) -> None:
        assert sample.add(ExclusionEntry("new.example.com", "because"))
        assert sample.should_exclude("new.example.com")

    def test_add_is_idempotent(self, sample: ExclusionList) -> None:
        assert not sample.add(ExclusionEntry("*.apple.com", "duplicate"))
        assert len(sample) == 5

    def test_remove(self, sample: ExclusionList) -> None:
        assert sample.remove("*.apple.com")
        assert not sample.should_exclude("www.apple.com")

    def test_remove_unknown_returns_false(self, sample: ExclusionList) -> None:
        assert not sample.remove("nope.example.com")

    def test_add_recompiles_networks(self, sample: ExclusionList) -> None:
        sample.add(ExclusionEntry("172.16.0.0/12", "private"))
        assert sample.should_exclude(None, "172.16.5.5")

    def test_with_additions_does_not_mutate_the_original(self, sample: ExclusionList) -> None:
        """Profile-scoped additions must not leak into the base list (REQ MOD-044)."""
        extended = sample.with_additions(["profile.example.com"])
        assert extended.should_exclude("profile.example.com")
        assert not sample.should_exclude("profile.example.com")

    def test_with_additions_marks_the_source(self, sample: ExclusionList) -> None:
        extended = sample.with_additions(["profile.example.com"])
        assert extended.decide("profile.example.com").source == "profile"

    def test_with_additions_skips_duplicates(self, sample: ExclusionList) -> None:
        assert len(sample.with_additions(["*.apple.com"])) == len(sample)

    def test_blank_patterns_are_ignored(self) -> None:
        lst = ExclusionList([ExclusionEntry("   ", "blank")])
        assert not lst.should_exclude("anything.example.com")


class TestSerialization:
    def test_to_dict(self, sample: ExclusionList) -> None:
        payload = sample.to_dict()
        assert len(payload["entries"]) == 5
        assert payload["entries"][0] == {
            "pattern": "*.apple.com",
            "comment": "update: OS updates",
            "source": "default",
        }

    def test_from_dicts_round_trip(self, sample: ExclusionList) -> None:
        rebuilt = ExclusionList.from_dicts(sample.to_dict()["entries"])
        assert rebuilt.should_exclude("www.apple.com")
        assert len(rebuilt) == len(sample)

    def test_from_dicts_skips_entries_with_no_pattern(self) -> None:
        assert len(ExclusionList.from_dicts([{"comment": "orphan"}])) == 0


class TestShippedDefaults:
    def test_default_file_exists(self) -> None:
        assert DEFAULT_EXCLUSIONS_PATH.exists()

    def test_loads(self) -> None:
        assert len(load_exclusions()) > 0

    def test_every_shipped_entry_has_a_comment(self) -> None:
        """REQ PXY-013.

        An exclusion nobody can explain is indistinguishable from a bug, and this
        list is the first thing suspected when a site misbehaves.
        """
        uncommented = [e.pattern for e in load_exclusions().entries if not e.comment.strip()]
        assert not uncommented, f"undocumented exclusions: {uncommented}"

    def test_covers_the_categories_the_spec_requires(self) -> None:
        """Update endpoints, pinning applications, and financial hosts."""
        exclusions = load_exclusions()
        assert exclusions.should_exclude("swscan.apple.com"), "OS update endpoint"
        assert exclusions.should_exclude("update.googleapis.com"), "browser update"
        assert exclusions.should_exclude("ocsp.digicert.com"), "revocation"
        assert exclusions.should_exclude("www.chase.com"), "financial"
        assert exclusions.should_exclude("api.stripe.com"), "payments"

    def test_does_not_overmatch_ordinary_sites(self) -> None:
        exclusions = load_exclusions()
        for host in ("example.com", "cdn.jsdelivr.net", "news.ycombinator.com"):
            assert not exclusions.should_exclude(host), host

    def test_all_shipped_entries_are_marked_default(self) -> None:
        assert all(e.source == "default" for e in load_exclusions().entries)


class TestLoading:
    def test_user_entries_merge_on_top(self, tmp_path: Path) -> None:
        user = tmp_path / "exclusions.yaml"
        user.write_text("entries:\n  - pattern: mine.example.com\n    comment: mine\n")
        merged = load_exclusions(user_path=user)
        assert merged.should_exclude("mine.example.com")
        assert merged.should_exclude("www.apple.com")

    def test_user_duplicates_do_not_double_up(self, tmp_path: Path) -> None:
        user = tmp_path / "exclusions.yaml"
        user.write_text("entries:\n  - pattern: '*.apple.com'\n    comment: dupe\n")
        base = len(load_exclusions())
        assert len(load_exclusions(user_path=user)) == base

    def test_missing_user_file_is_fine(self, tmp_path: Path) -> None:
        assert len(load_exclusions(user_path=tmp_path / "nope.yaml")) > 0

    def test_malformed_user_file_raises_rather_than_falling_back(self, tmp_path: Path) -> None:
        """Silently reverting to defaults would leave the user believing an
        exclusion is in force when it is not — for a financial entry, that is
        exactly the wrong way to fail."""
        user = tmp_path / "exclusions.yaml"
        user.write_text("entries: [unclosed\n")
        with pytest.raises(ValueError, match="invalid YAML"):
            load_exclusions(user_path=user)

    def test_wrong_shape_raises(self, tmp_path: Path) -> None:
        user = tmp_path / "exclusions.yaml"
        user.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="entries"):
            load_exclusions(user_path=user)

    def test_missing_default_file_is_tolerated(self, tmp_path: Path) -> None:
        assert len(load_exclusions(default_path=tmp_path / "nope.yaml")) == 0
