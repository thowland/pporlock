"""ClientHello exclusion list — SPEC-1 §3.5, REQ PXY-013/014/015.

Excluding at the ClientHello means the connection is tunneled without
decryption, so there is no downstream failure to handle: we never see the
bytes, never mint a certificate, and the application's own pinning is never
challenged.

This lives in ``engine/`` because it is a pure matching decision. The addon
turns the answer into ``data.ignore_connection = True``.

Two properties matter more than they look:

* Every entry carries a comment. An exclusion nobody can explain is
  indistinguishable from a bug, and this list is the first thing anyone will
  suspect when a site misbehaves.
* An excluded connection is still *recorded*, as a passthrough with host and
  timing but no content (REQ PXY-015). Silence would make excluded traffic
  invisible, which is a different failure from the one exclusion is solving.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

ExclusionSource = Literal["default", "user", "profile"]


@dataclass(frozen=True, slots=True)
class ExclusionEntry:
    """One exclusion rule: an SNI glob or a CIDR/IP literal."""

    pattern: str
    comment: str = ""
    source: ExclusionSource = "user"

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "comment": self.comment, "source": self.source}


@dataclass(frozen=True, slots=True)
class ExclusionDecision:
    """Whether to tunnel, and which entry decided it."""

    excluded: bool
    pattern: str | None = None
    comment: str | None = None
    source: ExclusionSource | None = None


NOT_EXCLUDED = ExclusionDecision(excluded=False)

#: A pattern is treated as a network literal when it parses as one. Anything
#: else is an SNI glob. Keeping the distinction implicit avoids a second syntax.
_HOST_SAFE = re.compile(r"^[A-Za-z0-9.\-*?\[\]!]+$")


def _as_network(pattern: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(pattern, strict=False)
    except ValueError:
        return None


class ExclusionList:
    """Compiled exclusion list with a single ``decide`` entry point."""

    __slots__ = ("_entries", "_globs", "_networks")

    def __init__(self, entries: list[ExclusionEntry] | None = None) -> None:
        self._entries: list[ExclusionEntry] = list(entries or [])
        self._globs: list[tuple[str, ExclusionEntry]] = []
        self._networks: list[
            tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ExclusionEntry]
        ] = []
        self._compile()

    def _compile(self) -> None:
        self._globs.clear()
        self._networks.clear()
        for entry in self._entries:
            pattern = entry.pattern.strip()
            if not pattern:
                continue
            network = _as_network(pattern)
            if network is not None:
                self._networks.append((network, entry))
            else:
                self._globs.append((pattern.lower(), entry))

    # -- queries ---------------------------------------------------------

    def decide(self, sni: str | None, ip: str | None = None) -> ExclusionDecision:
        """Tunnel this connection undecrypted?

        SNI is checked first because it is what the user actually wrote rules
        about. The IP fallback exists for connections with no SNI at all, which
        would otherwise slip past a hostname-only list.
        """
        if sni:
            host = sni.strip().lower().rstrip(".")
            if _HOST_SAFE.match(host):
                for pattern, entry in self._globs:
                    if fnmatch.fnmatchcase(host, pattern):
                        return ExclusionDecision(True, entry.pattern, entry.comment, entry.source)

        if ip:
            try:
                address = ipaddress.ip_address(ip.strip().strip("[]"))
            except ValueError:
                return NOT_EXCLUDED
            for network, entry in self._networks:
                if address.version == network.version and address in network:
                    return ExclusionDecision(True, entry.pattern, entry.comment, entry.source)

        return NOT_EXCLUDED

    def should_exclude(self, sni: str | None, ip: str | None = None) -> bool:
        return self.decide(sni, ip).excluded

    # -- mutation --------------------------------------------------------

    @property
    def entries(self) -> tuple[ExclusionEntry, ...]:
        return tuple(self._entries)

    def add(self, entry: ExclusionEntry) -> bool:
        """Add an entry. Returns False if that pattern is already present."""
        if any(e.pattern == entry.pattern for e in self._entries):
            return False
        self._entries.append(entry)
        self._compile()
        return True

    def remove(self, pattern: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.pattern != pattern]
        if len(self._entries) == before:
            return False
        self._compile()
        return True

    def with_additions(
        self, patterns: list[str], *, source: ExclusionSource = "profile"
    ) -> ExclusionList:
        """A copy carrying extra patterns, for profile-scoped additions (REQ MOD-044)."""
        merged = list(self._entries)
        known = {e.pattern for e in merged}
        for pattern in patterns:
            if pattern not in known:
                merged.append(ExclusionEntry(pattern=pattern, comment="", source=source))
        return ExclusionList(merged)

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.to_dict() for e in self._entries]}

    def __len__(self) -> int:
        return len(self._entries)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dicts(
        cls, raw: list[dict[str, Any]], *, default_source: ExclusionSource = "user"
    ) -> ExclusionList:
        entries = []
        for item in raw:
            pattern = str(item.get("pattern", "")).strip()
            if not pattern:
                continue
            entries.append(
                ExclusionEntry(
                    pattern=pattern,
                    comment=str(item.get("comment", "")),
                    source=item.get("source", default_source),
                )
            )
        return cls(entries)


#: The seeded list shipped with the package (REQ PXY-013).
DEFAULT_EXCLUSIONS_PATH = Path(__file__).resolve().parents[1] / "data" / "exclusions-default.yaml"


def load_exclusions(
    *,
    default_path: Path | None = None,
    user_path: Path | None = None,
) -> ExclusionList:
    """Load the shipped defaults, then merge any user list on top.

    A missing user file is normal — a fresh install has none — and is not an
    error. A malformed one is: silently falling back to defaults would leave the
    user believing an exclusion is in force when it is not, which for a
    financial or pinning entry is exactly the wrong way to fail.
    """
    entries: list[ExclusionEntry] = []

    path = default_path if default_path is not None else DEFAULT_EXCLUSIONS_PATH
    if path.exists():
        entries.extend(_read(path, source="default"))

    if user_path is not None and user_path.exists():
        known = {e.pattern for e in entries}
        for entry in _read(user_path, source="user"):
            if entry.pattern not in known:
                entries.append(entry)

    return ExclusionList(entries)


def _read(path: Path, *, source: ExclusionSource) -> list[ExclusionEntry]:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML — {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries", []), list):
        raise ValueError(f"{path}: expected a mapping with an 'entries' list")
    return list(ExclusionList.from_dicts(raw.get("entries", []), default_source=source).entries)
