"""One renewable heartbeat per host, so a reader elsewhere can judge its state.

A reservation, a lock or a resume mark pins its claim to a process id, and a
process id means something only on the host that issued it. A reader on another
host cannot probe it: the number may name an unrelated local process, or no
process at all, because hosts here share a home directory rather than a process
table. What such a reader can observe is whether that host is still running at
all. Each host therefore publishes one beat beside the fleet state it shares,
and a holder living on a host whose beat has lapsed is a holder on a machine
that has gone, which is the one fact an off-host reader can establish.

A beat's age is its file's own write time, refreshed rather than rewritten, so a
reader never parses anything to answer how old it is.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from pathlib import Path

#: Directory under the crew home holding one beat per host.
HEARTBEAT_DIRECTORY = "hosts"

#: How old a beat may be before its host is presumed gone. Sized well above the
#: interval at which anything publishes one, so a host busy enough to fall
#: behind on its beat is not read as a host that has gone.
HEARTBEAT_STALE_SECONDS = 90.0


def heartbeat_host() -> str:
    """The host identity a beat is published under and compared against."""
    return socket.gethostname()


def _readable(host: str) -> str:
    """A filesystem-safe rendering of a host name."""
    name = "".join(char if char.isalnum() or char in "._-" else "-" for char in host)
    return name or "host"


def host_heartbeat_path(home: Path, host: str) -> Path:
    """Where one host's beat lives in the shared crew home."""
    return Path(home) / HEARTBEAT_DIRECTORY / f"{_readable(host)}.json"


def publish_host_heartbeat(
    home: Path, host: str | None = None, *, now: float | None = None
) -> Path:
    """Write this host's beat, and refresh the instant a reader measures.

    The record is replaced atomically so a reader never sees a partial beat, and
    its write time is set explicitly so the refresh is the beat rather than a
    side effect of whatever clock the filesystem keeps.
    """
    host = host or heartbeat_host()
    path = host_heartbeat_path(home, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    instant = now if now is not None else time.time()
    record = {"host": host, "pid": os.getpid(), "at": instant}
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".beat-",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
            os.utime(temporary, (instant, instant))
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)
    return path


def host_heartbeat_age(
    home: Path, host: str, *, now: float | None = None
) -> float | None:
    """Seconds since the host's beat was last written, or None when it has none."""
    try:
        written = host_heartbeat_path(home, host).stat().st_mtime
    except OSError:
        return None
    instant = now if now is not None else time.time()
    return instant - written


def host_is_live(home: Path, host: str, *, now: float | None = None) -> bool:
    """Whether the host published a beat inside the stale window.

    A host with no beat at all is not live. That is a fact about a host never
    seen holding state here, not a claim that it has stopped.
    """
    age = host_heartbeat_age(home, host, now=now)
    return age is not None and age < HEARTBEAT_STALE_SECONDS
