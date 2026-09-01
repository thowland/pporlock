"""File-descriptor headroom — OI-36.

An HTTPS interception proxy holds two connections per flow, a client one and a
server one, and a browsing session reaches a few hundred without trying. macOS
gives a launchd agent a **soft limit of 256** (``launchctl limit maxfiles``),
and the daemon used to accept it.

At the ceiling everything that opens a file fails, and it fails in whatever
vocabulary that subsystem happens to use. The first report of this was a session
export returning ``sqlite3.OperationalError: unable to open database file`` on a
file that was present and readable — SQLite's message for ``EMFILE``. Measured
at the time: 247 descriptors open, ten export attempts, ten failures; after a
restart, 34 descriptors and a 1.27 MB export. Session writes, module reload and
the CA would have failed the same way, intermittently, clearing on restart, each
blaming something other than the cause.

So the daemon raises its own limit at startup rather than depending on how it
was launched, and reports the headroom it has, because "how close am I" should
be answerable before the failure rather than deduced after it.
"""

from __future__ import annotations

import os
import resource
from dataclasses import dataclass
from pathlib import Path

#: What we ask for. Two descriptors per in-flight flow plus the session
#: database, the log files and the control server's own sockets; a few thousand
#: is far past anything a single browser produces, and costs nothing to hold.
DESIRED_NOFILE = 8192

#: Descending fallbacks. macOS caps a process at ``kern.maxfilesperproc``
#: regardless of the hard limit, and a `setrlimit` above it fails outright
#: rather than clamping — so a request that is refused is retried smaller
#: instead of leaving the daemon on 256 because 8192 was ambitious.
FALLBACK_NOFILE = (4096, 2048, 1024)

#: Fraction of the soft limit above which we are close enough to say so.
PRESSURE_WARN = 0.7


@dataclass(frozen=True, slots=True)
class FileLimit:
    """The descriptor limit this process ended up with."""

    soft: int
    hard: int
    #: What the soft limit was before we touched it. Equal to ``soft`` when the
    #: raise was unnecessary or did not work.
    was: int
    detail: str

    @property
    def raised(self) -> bool:
        return self.soft > self.was

    def to_dict(self) -> dict[str, object]:
        return {"soft": self.soft, "hard": self.hard, "was": self.was, "detail": self.detail}


def _limits() -> tuple[int, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return int(soft), int(hard)


def raise_file_limit(desired: int = DESIRED_NOFILE) -> FileLimit:
    """Raise this process's descriptor soft limit toward ``desired``.

    Never raises. A daemon that cannot lift its own limit is worse off than one
    that can, but it is not broken — it is the daemon we shipped until now — and
    refusing to start over it would trade a degraded proxy for no proxy. The
    outcome is returned so the caller can say what happened, and `doctor` can
    warn about the case where it did not work.
    """
    soft, hard = _limits()
    if soft >= desired:
        return FileLimit(soft, hard, soft, "already above the target")

    unlimited = hard == resource.RLIM_INFINITY
    targets = [desired, *FALLBACK_NOFILE]
    for target in targets:
        if target <= soft:
            break
        capped = target if unlimited else min(target, hard)
        if capped <= soft:
            continue
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (capped, hard))
        except (OSError, ValueError):
            continue
        new_soft, new_hard = _limits()
        return FileLimit(new_soft, new_hard, soft, f"raised from {soft}")

    return FileLimit(
        soft,
        hard,
        soft,
        f"could not raise above {soft}; the kernel refused every target down to {targets[-1]}",
    )


def open_descriptors() -> int | None:
    """How many descriptors this process currently holds, or None.

    Reads a directory, so it belongs in an executor or a CLI — never on the
    proxy's event loop (REQ DD-3). ``/dev/fd`` on macOS, ``/proc/self/fd`` on
    Linux; anywhere else this returns None rather than guessing, and every
    caller treats None as "unknown" rather than as zero.
    """
    for path in (Path("/dev/fd"), Path("/proc/self/fd")):
        try:
            # The listing itself opens a descriptor, which is then closed; the
            # off-by-one is not worth correcting for a pressure indicator.
            return len(os.listdir(path))
        except OSError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class DescriptorUsage:
    """A sample of descriptor pressure, for `/metrics` and `doctor`."""

    soft: int
    hard: int
    open_count: int | None

    @property
    def pressure(self) -> float | None:
        """Fraction of the soft limit in use, or None when unknown."""
        if self.open_count is None or self.soft <= 0:
            return None
        return self.open_count / self.soft

    @property
    def tight(self) -> bool:
        pressure = self.pressure
        return pressure is not None and pressure >= PRESSURE_WARN

    def to_dict(self) -> dict[str, object]:
        return {
            "soft": self.soft,
            "hard": None if self.hard == resource.RLIM_INFINITY else self.hard,
            "open": self.open_count,
            "pressure": None if self.pressure is None else round(self.pressure, 3),
        }


def sample() -> DescriptorUsage:
    """Take a reading. Does filesystem work — see `open_descriptors`."""
    soft, hard = _limits()
    return DescriptorUsage(soft=soft, hard=hard, open_count=open_descriptors())
