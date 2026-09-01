#!/usr/bin/env python3
"""One version, propagated — OI-25.

The version was declared independently in nine places: two `pyproject.toml`s,
two `package.json`s, the extension manifest, and four Python literals. Nothing
checked that they agreed, and predictably nothing ever moved them: every one
still said 0.1.0 from Sprint 0 while eighteen sprints and a dozen fixes shipped.
A version that never changes cannot answer the only question it exists to
answer — "is the thing I am running the thing I just built?"

So `VERSION` at the repository root is the source, and everything else is
generated from it, the same arrangement `contracts/` already uses for wire
types. `make version-check` fails the gate when a file drifts, which is what
makes the single source real rather than aspirational.

**Semver, including the parts Chrome cannot store.** A manifest `version` must
be one to four dot-separated integers, so `0.3.0-rc.1` is not a legal value.
The numeric core goes in `version`, and the full string in `version_name`,
which is the field Chrome provides for exactly this and which is what the
popup should show a human.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO / "VERSION"

#: The year of the first commit. A fixed fact, not derived: a shallow clone has
#: no first commit to read, and the year a work was first published does not
#: change however the history is later rewritten.
COPYRIGHT_START = 2026

#: Every place the notice appears, and the one occurrence in each. Three files,
#: one of which is a README that nothing compiles, lints or serves — which is
#: exactly why this is generated rather than remembered. The two constants were
#: written by hand as 2025 and were wrong from the moment they were typed.
COPYRIGHT_FILES = (
    REPO / "web" / "src" / "lib" / "about.ts",
    REPO / "extension" / "src" / "shared" / "about.ts",
    REPO / "README.md",
)

#: `© 2026 Tim Howland` or `© 2026-2027 Tim Howland`.
#:
#: A hyphen rather than the en dash a year range takes typographically: ruff
#: flags an en dash in source as an ambiguous character, and a suppression on
#: five lines to win a punctuation argument is a bad trade. A hyphenated range
#: is what almost every copyright line in software uses anyway.
COPYRIGHT_PATTERN = re.compile(r"© (\d{4}(?:-\d{4})?) Tim Howland")


def copyright_years(today: datetime.date | None = None) -> str:
    """The notice's year, as of now.

    **The current year, not the year of the last commit.** A copyright notice
    claims a year of modification, and this is only ever written immediately
    before a commit — `make version-sync` is run by `make bump-*`, and CLAUDE.md
    requires a bump on every branch before merge. So "now" is the year the
    modification is being made, while the last commit's year is the *previous*
    modification and would be a year stale every January.

    It also keeps this script independent of git, which matters: it runs in the
    gate, and a gate step that needs a git history fails in a tarball.

    Collapses to a single year while the project is still in its first one, and
    becomes a range on its own the first time it is synced in a later year.
    """
    year = (today or datetime.date.today()).year
    return str(COPYRIGHT_START) if year <= COPYRIGHT_START else f"{COPYRIGHT_START}-{year}"


#: Full semver, anchored. Prerelease and build metadata are permitted here even
#: though the Chrome manifest cannot hold them — see the module docstring.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


def read_version() -> str:
    raw = VERSION_FILE.read_text().strip()
    if not SEMVER.match(raw):
        raise SystemExit(f"VERSION is not valid semver: {raw!r}")
    return raw


def numeric_core(version: str) -> str:
    """The `major.minor.patch` part, which is all a Chrome manifest accepts."""
    match = SEMVER.match(version)
    assert match is not None  # read_version validated it
    return f"{match['major']}.{match['minor']}.{match['patch']}"


def _sub_once(path: Path, pattern: str, replacement: str) -> bool:
    """Rewrite one occurrence, reporting whether the file changed.

    Raises rather than silently doing nothing if the anchor is gone: a sync
    script that quietly skips a file it can no longer find is how the drift
    this exists to prevent comes back.
    """
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path.relative_to(REPO)}: could not find the version to update")
    if new == text:
        return False
    path.write_text(new)
    return True


def _json_version(path: Path, version: str) -> bool:
    data = json.loads(path.read_text())
    if data.get("version") == version:
        return False
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def targets(version: str) -> list[tuple[Path, str]]:
    """Every file that carries the version, and what it should say."""
    core = numeric_core(version)
    return [
        (REPO / "daemon" / "pyproject.toml", version),
        (REPO / "mcp" / "pyproject.toml", version),
        (REPO / "web" / "package.json", version),
        (REPO / "extension" / "package.json", version),
        # The manifest gets the numeric core; the full string rides in
        # version_name, which Chrome allows to be anything.
        (REPO / "extension" / "src" / "manifest.config.ts", core),
    ]


def sync_copyright(years: str) -> list[Path]:
    """Rewrite the notice wherever it appears."""
    changed: list[Path] = []
    for path in COPYRIGHT_FILES:
        text = path.read_text()
        new, count = COPYRIGHT_PATTERN.subn(f"© {years} Tim Howland", text)
        if count != 1:
            # Same rule as _sub_once: a sync that quietly skips a file it can no
            # longer find is how the drift this exists to prevent comes back.
            raise SystemExit(
                f"{path.relative_to(REPO)}: expected one copyright notice, found {count}"
            )
        if new != text:
            path.write_text(new)
            changed.append(path)
    return changed


def check_copyright(today: datetime.date | None = None) -> list[str]:
    """Notices that disagree with each other, or claim a year not yet reached.

    Deliberately *not* "every file says the current year". A project untouched
    since 2026 correctly says 2026 for ever, and a check that failed every 1
    January on an unchanged tree would be noise — the kind that teaches people
    to run the gate with one step disabled.

    What must hold is that the three agree, and that none of them claims a year
    that has not happened. `make version-sync` moves them forward, and it runs
    on every version bump, which CLAUDE.md requires on every branch.
    """
    problems: list[str] = []
    seen: dict[str, list[str]] = {}
    for path in COPYRIGHT_FILES:
        found = COPYRIGHT_PATTERN.search(path.read_text())
        name = str(path.relative_to(REPO))
        if found is None:
            problems.append(f"{name}: no copyright notice found")
            continue
        seen.setdefault(found.group(1), []).append(name)

    if len(seen) > 1:
        for years, files in sorted(seen.items()):
            problems.append(f"copyright {years} in {', '.join(files)}")

    now = (today or datetime.date.today()).year
    for years in seen:
        latest = int(years.split("-")[-1])
        if latest > now:
            problems.append(f"copyright {years} is in the future")
        if int(years.split("-")[0]) != COPYRIGHT_START:
            problems.append(f"copyright {years} does not start at {COPYRIGHT_START}")

    return problems


def sync(version: str) -> list[Path]:
    changed: list[Path] = []
    core = numeric_core(version)

    for path, want in targets(version):
        if path.suffix == ".toml":
            if _sub_once(path, r'^version = "[^"]*"$', f'version = "{want}"'):
                changed.append(path)
        elif path.suffix == ".json":
            if _json_version(path, want):
                changed.append(path)

    manifest = REPO / "extension" / "src" / "manifest.config.ts"
    if _sub_once(manifest, r"^  version: '[^']*',$", f"  version: '{core}',"):
        changed.append(manifest)
    if _sub_once(manifest, r"^  version_name: '[^']*',$", f"  version_name: '{version}',"):
        if manifest not in changed:
            changed.append(manifest)

    changed.extend(sync_copyright(copyright_years()))

    return changed


def check(version: str) -> list[str]:
    """Files whose version disagrees with VERSION."""
    problems: list[str] = []

    for path, want in targets(version):
        text = path.read_text()
        if path.suffix == ".toml":
            found = re.search(r'^version = "([^"]*)"$', text, re.MULTILINE)
        elif path.suffix == ".json":
            found = re.search(r'"version":\s*"([^"]*)"', text)
        else:
            found = re.search(r"^  version: '([^']*)',$", text, re.MULTILINE)
        actual = found.group(1) if found else None
        if actual != want:
            problems.append(f"{path.relative_to(REPO)}: {actual!r}, expected {want!r}")

    manifest = (REPO / "extension" / "src" / "manifest.config.ts").read_text()
    named = re.search(r"^  version_name: '([^']*)',$", manifest, re.MULTILINE)
    if named is None or named.group(1) != version:
        problems.append(
            f"extension/src/manifest.config.ts: version_name "
            f"{named.group(1) if named else None!r}, expected {version!r}"
        )

    problems.extend(check_copyright())
    return problems


def bump(part: str, version: str) -> str:
    match = SEMVER.match(version)
    assert match is not None
    major, minor, patch = int(match["major"]), int(match["minor"]), int(match["patch"])
    if part == "major":
        major, minor, patch = major + 1, 0, 0
    elif part == "minor":
        minor, patch = minor + 1, 0
    elif part == "patch":
        patch += 1
    else:  # pragma: no cover - argparse constrains this
        raise SystemExit(f"unknown part: {part}")
    # A bump drops any prerelease: 0.2.0-rc.1 bumped as a patch is 0.2.1, not
    # 0.2.1-rc.1. Carrying it would make every subsequent release a prerelease.
    return f"{major}.{minor}.{patch}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read, sync, or bump the project version")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("sync")
    sub.add_parser("check")
    bumper = sub.add_parser("bump")
    bumper.add_argument("part", choices=["major", "minor", "patch"])

    args = parser.parse_args(argv)
    version = read_version()

    if args.command == "show":
        print(version)
        return 0

    if args.command == "sync":
        changed = sync(version)
        for path in changed:
            print(f"  updated {path.relative_to(REPO)}")
        print(f"version {version}" + ("" if changed else " (already in sync)"))
        return 0

    if args.command == "check":
        problems = check(version)
        if problems:
            print(f"VERSION says {version}, but:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print("\nRun `make version-sync`.", file=sys.stderr)
            return 1
        print(f"version {version}, copyright {copyright_years()} — all files agree")
        return 0

    new = bump(args.part, version)
    VERSION_FILE.write_text(new + "\n")
    for path in sync(new):
        print(f"  updated {path.relative_to(REPO)}")
    print(f"{version} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
