"""Log files and their rotation — REQ PXY-007.

The daemon writes to stdout and stderr; launchd appends both to files under
``~/Library/Logs/pporlock/``. That arrangement is what dictates *how* rotation
has to work here.

launchd opens those files once, at load, and holds the descriptors for the life
of the agent. Renaming a file the way ``logrotate`` does leaves launchd writing
into the renamed inode: the old file keeps growing under its new name and the
fresh one stays empty forever. So rotation is **copy-and-truncate** — the
contents are copied out to ``pporlock.out.log.1`` and the original is truncated
in place, keeping the inode and therefore keeping launchd's descriptor valid.

The cost of copy-and-truncate is a race: anything written between the copy and
the truncate is lost. For a local development proxy's own diagnostic log, losing
a handful of lines once per rotation is the right trade against the alternative,
which is a log that silently stops being written to.

Bodies are never logged at default level (REQ PXY-007) — that is enforced at the
call sites, not here; this module only bounds the size of what they produce.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Rotate at 8 MiB, keep 5 generations: ~48 MiB worst case for one stream.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_RETAIN = 5

#: The two files launchd is configured to write (see ``launchd.plist_dict``).
LOG_NAMES: tuple[str, ...] = ("pporlock.out.log", "pporlock.err.log")

#: How often the running daemon checks its own log sizes. A minute is far
#: shorter than the time it takes to write 8 MiB of headers at default level,
#: and cheap enough that the check itself never shows up anywhere.
ROTATION_INTERVAL_S = 60.0


DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "pporlock"


def log_dir(configured: str | Path | None = None) -> Path:
    return Path(configured).expanduser() if configured else DEFAULT_LOG_DIR


def log_paths(directory: str | Path | None = None) -> list[Path]:
    base = log_dir(directory)
    return [base / name for name in LOG_NAMES]


def rotate_file(
    path: Path, *, max_bytes: int = DEFAULT_MAX_BYTES, retain: int = DEFAULT_RETAIN
) -> bool:
    """Rotate one file if it has grown past ``max_bytes``. Returns whether it did.

    Never raises on a missing file or a directory that has gone away: rotation
    is housekeeping, and housekeeping that can take the daemon down is worse
    than a large log.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < max_bytes or retain < 1:
        return False

    try:
        # Shift the generations down: .4 -> .5, .3 -> .4, ... The oldest falls
        # off the end rather than accumulating.
        oldest = path.with_name(f"{path.name}.{retain}")
        if oldest.exists():
            oldest.unlink()
        for index in range(retain - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))

        # Copy-and-truncate, not rename: launchd holds this descriptor open.
        first = path.with_name(f"{path.name}.1")
        first.write_bytes(path.read_bytes())
        first.chmod(0o600)
        os.truncate(path, 0)
    except OSError:
        return False
    return True


def rotate(
    directory: str | Path | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    retain: int = DEFAULT_RETAIN,
) -> list[Path]:
    """Rotate every daemon log in ``directory``. Returns the ones rotated."""
    return [
        path
        for path in log_paths(directory)
        if rotate_file(path, max_bytes=max_bytes, retain=retain)
    ]


def tail(path: Path, lines: int = 50) -> list[str]:
    """The last ``lines`` lines of a log, without reading the whole file.

    Reads backwards in blocks. A rotation threshold of 8 MiB means the naive
    ``readlines()`` would pull megabytes into memory to show fifty lines.
    """
    path = Path(path)
    if not path.exists() or lines < 1:
        return []
    block = 8192
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= lines:
                step = min(block, end)
                end -= step
                handle.seek(end)
                data = handle.read(step) + data
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]
