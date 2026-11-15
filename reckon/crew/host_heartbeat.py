"""One renewable heartbeat per host, so a reader elsewhere can judge its state.

A reservation, a lock or a resume mark pins its claim to a process id, and a
process id means something only on the host that issued it. A reader on another
host cannot probe it: the number may name an unrelated local process, or no
process at all, because hosts here share a home directory rather than a process
table. What such a reader can observe is whether that host is still running at
all. Each host therefore publishes one beat beside the fleet state it shares,
and a holder living on a host whose beat has lapsed is a holder on a machine
that has gone, which is the one fact an off-host reader can establish.

A beat is published once and then renewed for as long as the process that first
published it lives, because a holder that is working must keep the evidence of
its own host current: a beat written once and never refreshed lapses on a timer
and a working holder's reservation would be released out from under it. The
renewal is a thread the publishing process owns, so it ends when that process
is gone, which is exactly the event the beat is meant to mark.

Which process publishes therefore decides how long the beat speaks for a run.
The process that takes a reservation is not a run's holder: it records the
launch and returns while the supervisor it started goes on supervising the
supervisor's worker. So the beat is published from the supervisor as well, at
its own start, and it then outlives the launcher by the length of the run.

A beat whose renewal stopped while its holder is still visible in this process
table is not treated as a host that has gone: a stalled renewal is a slow host,
not a dead one.

A beat's age is its file's own write time, refreshed rather than rewritten, so a
reader never parses anything to answer how old it is.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from reckon.crew.host_lease import (
    _pid_namespace,
    _process_start,
    local_host,
    process_runs_here,
)

#: Directory under the crew home holding one beat per host.
HEARTBEAT_DIRECTORY = "hosts"

#: How often a publishing process refreshes its beat. Well inside the stale
#: window, so a beat lapses only when its renewing process has ended.
HEARTBEAT_RENEW_SECONDS = 20.0

#: How old a beat may be before its host is presumed gone. Sized several renewals
#: wide, so a host busy enough to fall behind on one renewal is not read as gone.
HEARTBEAT_STALE_SECONDS = 90.0

#: One renewing thread per (home, host), owned by the process that published.
_RENEWERS: dict[tuple[str, str], threading.Event] = {}
_RENEWERS_LOCK = threading.Lock()


def heartbeat_host() -> str:
    """The host identity a beat is published under and compared against."""
    return local_host()


def _readable(host: str) -> str:
    """A filesystem-safe rendering of a beat's host name."""
    name = "".join(char if char.isalnum() or char in "._-" else "-" for char in host)
    return name or "host"


def host_heartbeat_path(home: Path, host: str) -> Path:
    """Where one host's beat lives in the shared crew home."""
    return Path(home) / HEARTBEAT_DIRECTORY / f"{_readable(host)}.json"


def _write_beat(home: Path, host: str, instant: float) -> Path:
    path = host_heartbeat_path(home, host)
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    record = {
        "host": host,
        "pid": pid,
        "pid_start": _process_start(pid),
        "pid_namespace": _pid_namespace(),
        "at": instant,
    }
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


def publish_host_heartbeat(
    home: Path, host: str | None = None, *, now: float | None = None
) -> Path:
    """Write this host's beat, and keep it fresh while this process lives.

    The record is replaced atomically so a reader never sees a partial beat, and
    its write time is set explicitly so the refresh is the beat rather than a
    side effect of whatever clock the filesystem keeps. Renewal starts here and
    stops when this process ends, so a lapsed beat means its host stopped
    renewing, which is what a reader elsewhere is trying to find out.

    An explicit instant is a caller stating when the beat belongs, which a live
    renewal would overwrite; such a write is left un-renewed.
    """
    host = host or heartbeat_host()
    instant = now if now is not None else time.time()
    path = _write_beat(home, host, instant)
    if now is None:
        _start_renewing(Path(home), host)
    return path


def _start_renewing(home: Path, host: str) -> None:
    key = (str(home), host)
    with _RENEWERS_LOCK:
        if key in _RENEWERS:
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=_renew_until_stopped,
            args=(home, host, stop),
            name=f"heartbeat-{_readable(host)}",
            daemon=True,
        )
        _RENEWERS[key] = stop
        thread.start()


def _renew_until_stopped(home: Path, host: str, stop: threading.Event) -> None:
    try:
        while not stop.wait(HEARTBEAT_RENEW_SECONDS):
            try:
                os.utime(host_heartbeat_path(home, host), None)
            except OSError:
                # The beat was removed, by a sweep or by a fresh crew home. A
                # renewal has nothing to refresh, so it ends rather than writing
                # a beat for a home this process may no longer be using.
                break
    finally:
        # The key records that a renewer is running. Kept after the thread has
        # ended, it tells a later publish that renewal is already under way, so
        # a beat written from then on is never refreshed again.
        with _RENEWERS_LOCK:
            if _RENEWERS.get((str(home), host)) is stop:
                del _RENEWERS[(str(home), host)]


def stop_host_heartbeat(home: Path, host: str | None = None) -> bool:
    """End this process's renewal of a beat. The answer is whether one ran."""
    key = (str(Path(home)), host or heartbeat_host())
    with _RENEWERS_LOCK:
        stop = _RENEWERS.pop(key, None)
    if stop is None:
        return False
    stop.set()
    return True




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


def _holder_is_running_here(record: dict) -> bool:
    """Whether a beat names a process this reader can see running.

    A renewal lapses while its process is busy or blocked, and a slow renewal is
    not a host that has gone. The reader can tell the two apart only when the
    beat names a process in a process table it shares, which is the same
    comparison the fleet's node lease makes against its holder.
    """
    return process_runs_here(
        pid=record.get("pid"),
        pid_start=str(record.get("pid_start") or ""),
        pid_namespace=str(record.get("pid_namespace") or ""),
    )


def host_is_live(home: Path, host: str, *, now: float | None = None) -> bool:
    """Whether the beat record for a host still stands for something running.

    A beat inside the stale window is live. A lapsed beat is live while the
    process it names is visible in this reader's own process table, because a
    stalled renewal is not a host that has gone. Anything else — no beat, an
    unreadable one, or a lapsed one whose holder this reader cannot see — is not
    live: the reader has no evidence the host is still there, and the claim it
    guards is freed.
    """
    path = host_heartbeat_path(home, host)
    try:
        written = path.stat().st_mtime
    except OSError:
        return False
    instant = now if now is not None else time.time()
    if instant - written < HEARTBEAT_STALE_SECONDS:
        return True
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and _holder_is_running_here(record)
