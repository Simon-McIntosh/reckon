# ruff: noqa: I001
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reckon.crew.obligation_snapshot import CLI_MODULE_FILES


# One source file's content digest, kept beside the stat that vouches for it.
# A follower re-checks its stamp at a bounded cadence and most ticks find
# nothing new, so the mtime and size let an untouched file reuse the digest it
# had last time rather than reading every follower module on every tick.
_follower_source_digests: dict[str, tuple[int, int, str]] = {}


def _source_content_digest(source: Path, metadata: os.stat_result) -> str:
    """Return a digest of a source file's bytes, reused while its stat holds.

    The stat is a pre-check, not the identity: a tool that rewrites a file with
    identical bytes — a formatter, a checkout restoring the same revision —
    moves the mtime alone, and a stamp keyed on it reloaded every follower for
    nothing. An unchanged mtime and size reuse the last digest; a moved one
    reads the bytes and finds them the same, so the stamp does not move.
    """
    key = str(source)
    cached = _follower_source_digests.get(key)
    if (
        cached is not None
        and cached[0] == metadata.st_mtime_ns
        and cached[1] == metadata.st_size
    ):
        return cached[2]
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError:
        digest = ""
    _follower_source_digests[key] = (metadata.st_mtime_ns, metadata.st_size, digest)
    return digest


def follower_code_stamp() -> str:
    """Return a stamp that advances when code used by a follower changes.

    Keyed on content rather than on the mtime and size alone, because a touched
    file whose bytes are unchanged is not new code and must not reload a
    running follower.
    """
    package_dir = Path(__file__).resolve().parent.parent
    sources = [
        *(package_dir / name for name in CLI_MODULE_FILES),
        *sorted((package_dir / "crew").glob("*.py")),
    ]
    stamp = hashlib.sha256()
    for source in sources:
        try:
            metadata = source.stat()
        except OSError:
            continue
        stamp.update(str(source.relative_to(package_dir)).encode())
        stamp.update(f":{_source_content_digest(source, metadata)}\n".encode())
    return stamp.hexdigest()


def follower_dir(project: str) -> Path:
    """Directory holding one registration per session consuming the ticker."""
    return watch_lock_path(project).with_suffix(".followers")


def follower_lock_path(project: str, session: str) -> Path:
    """Stable advisory-lock path for one session's delivery registration."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "-", session).strip("-") or "session"
    digest = hashlib.sha256(session.encode()).hexdigest()[:12]
    return follower_dir(project) / f"{readable}-{digest}.lock"


def _pipe_reader_pids(inode: int, *, exclude: int) -> list[int]:
    """Return the pids holding the other end of one pipe, by its inode."""
    target = f"pipe:[{inode}]"
    readers: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == exclude:
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == target:
                    readers.append(pid)
                    break
            except OSError:
                continue
    return readers


def _descriptor_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "pipe"
    if stat.S_ISSOCK(mode):
        return "stream"
    if stat.S_ISCHR(mode):
        return "terminal"
    if stat.S_ISREG(mode):
        return "file"
    return "unknown"


def _trace_delivery(info: os.stat_result, *, pid: int, hops: int) -> str:
    """Follow one output descriptor to whatever finally consumes its lines."""
    seen: set[int] = set()
    for _hop in range(hops):
        kind = _descriptor_kind(info.st_mode)
        if kind != "pipe":
            return kind
        if info.st_ino in seen:
            return "unknown"
        seen.add(info.st_ino)
        readers = _pipe_reader_pids(info.st_ino, exclude=pid)
        if not readers:
            # Nothing holds the read end: the lines have nowhere to go at all.
            return "file"
        pid = readers[0]
        try:
            # stat rather than open: opening a FIFO can block, and a probe that
            # blocks is a worse failure than the one being detected.
            info = os.stat(f"/proc/{pid}/fd/1")
        except OSError:
            # A reader whose own output cannot be inspected is credited as a
            # reader: refusing on an unknown would refuse the ordinary case.
            return "stream"
    return "stream"


def delivery_mode(descriptor: int = 1, *, hops: int = 4) -> str:
    """Classify what will actually consume this process's lines.

    A follower is only a wake-up if something reads its lines as they are
    written. A socket or terminal has a reader doing exactly that; a regular
    file is read by whoever opens it later, which for a command that never
    exits is nobody.

    A pipe answers neither way by itself, and that is the case that matters: a
    filter between the follower and a file looks like a live consumer at the
    first hop while the chain still ends in a file nothing reads. So the pipe
    is followed to the process on its other end and the question is asked
    again of *that* process's output. The verdict belongs to the end of the
    chain, because that is where the lines stop.
    """
    try:
        info = os.fstat(descriptor)
    except OSError:
        return "unknown"
    return _trace_delivery(info, pid=os.getpid(), hops=hops)


# The pipe-chain walk scans every process's descriptors — 211 ms on a host with
# 1663 of them — so a repeated reader must not pay it repeatedly. Keyed on the
# process identity rather than the pid alone, and expiring, so a recycled pid
# and a genuinely changed descriptor are both noticed.
_DELIVERY_TRACE_TTL_SECONDS = 5.0
_DELIVERY_TRACE_CACHE: dict[tuple[int, str], tuple[float, str]] = {}


def delivery_mode_of(pid: int, *, hops: int = 4) -> str | None:
    """Classify what consumes another process's output, or None if unreadable.

    Read live rather than trusted from the registration, so a follower is
    judged by where its lines go *now*. A recorded verdict is a snapshot: it
    survives the consumer at the end of the chain going away, and it answers
    with whatever the check understood on the day it was written.
    """
    try:
        info = os.stat(f"/proc/{pid}/fd/1")
    except OSError:
        return None
    kind = _descriptor_kind(info.st_mode)
    if kind != "pipe":
        # The cheap answer, and the common one: no scan is needed to see that a
        # descriptor is a socket, a terminal or a file.
        return kind
    identity = (int(pid), str(_process_start_time(pid) or ""))
    cached = _DELIVERY_TRACE_CACHE.get(identity)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _DELIVERY_TRACE_TTL_SECONDS:
        return cached[1]
    resolved = _trace_delivery(info, pid=pid, hops=hops)
    _DELIVERY_TRACE_CACHE[identity] = (now, resolved)
    if len(_DELIVERY_TRACE_CACHE) > 256:
        for key, (stamp, _) in list(_DELIVERY_TRACE_CACHE.items()):
            if now - stamp >= _DELIVERY_TRACE_TTL_SECONDS:
                _DELIVERY_TRACE_CACHE.pop(key, None)
    return resolved


# Descriptor kinds whose reader sees a line when it is written. Anything else
# holds the ticker until the command exits, and a follower does not exit.
DELIVERING_MODES = ("stream", "terminal")


class _FollowerRegistration:
    """One session's delivery registration, claimable now or later.

    Registration and streaming are separable, and only registration satisfies
    the dispatch guard. A second follower for the same session therefore streams
    read-only while the first holds the lock — and if that first process then
    dies, the registration is released while the streamer keeps delivering
    lines, so every visible signal says attached and dispatch correctly refuses.
    Retrying the claim while streaming closes that gap: whoever is still
    delivering ends up holding the registration.
    """

    def __init__(
        self,
        project: str,
        session: str,
        *,
        delivery: str,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        self.project = project
        self.session = session
        self.delivery = delivery
        self.scope = dict(scope or {})
        self.held = False
        self.record: dict[str, Any] = {}
        self.blocked_by: dict[str, Any] = {}
        self._handle = None

    def _adopt_inherited(self) -> bool:
        """Keep the same advisory lock across an in-place process reload."""
        raw = os.environ.pop(_FOLLOWER_REGISTRATION_ENV, "")
        if not raw:
            return False
        try:
            inherited = json.loads(raw)
            fd = int(inherited["fd"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            inherited.get("project") != self.project
            or inherited.get("session") != self.session
        ):
            os.close(fd)
            return False
        try:
            handle = os.fdopen(fd, "a+b")
            os.set_inheritable(handle.fileno(), False)
            record = _read_watch_record(handle)
        except OSError:
            return False
        if (
            record.get("project") != self.project
            or record.get("session") != self.session
        ):
            handle.close()
            return False
        self._handle = handle
        self.record = record
        self.held = True
        self.blocked_by = {}
        return True

    def _open(self):
        if self._handle is None:
            path = follower_lock_path(self.project, self.session)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a+b")
        return self._handle

    def acquire(self) -> bool:
        """Take the registration if it is free, and report whether it is held."""
        if self.held:
            return True
        if self._adopt_inherited():
            return True
        handle = self._open()
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.blocked_by = _read_watch_record(handle)
            return False
        owner_pid, owner_start = follower_owner()
        self.record = {
            "project": self.project,
            "session": self.session,
            "pid": os.getpid(),
            "pid_start_time": _process_start_time(os.getpid()),
            "parent_pid": owner_pid,
            "parent_start_time": owner_start,
            "delivery": self.delivery,
            "scope": self.scope,
            "started_at": _utc_now(),
        }
        _write_watch_record(handle, self.record)
        self.held = True
        self.blocked_by = {}
        return True

    def prepare_reexec(self) -> None:
        """Make this registration survive replacement of the current process."""
        if not self.held or self._handle is None:
            return
        fd = self._handle.fileno()
        os.set_inheritable(fd, True)
        os.environ[_FOLLOWER_REGISTRATION_ENV] = json.dumps(
            {"fd": fd, "project": self.project, "session": self.session}
        )

    def cancel_reexec(self) -> None:
        """Undo descriptor inheritance when process replacement was refused."""
        os.environ.pop(_FOLLOWER_REGISTRATION_ENV, None)
        if self._handle is not None:
            os.set_inheritable(self._handle.fileno(), False)

    def release(self) -> None:
        """Drop the claim, leaving the file as a record rather than removing it.

        Deleting it would orphan a second follower that is holding the same
        inode read-only and about to take over: its claim would succeed on an
        unlinked file, so it would believe it was registered while every reader
        looked up a path that no longer exists. Liveness is the lock plus the
        pid, so a leftover record cannot lie about either.
        """
        if self._handle is None:
            return
        if self.held:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self.held = False
        self._handle.close()
        self._handle = None


@contextmanager
def follower_registration(
    project: str,
    session: str,
    *,
    delivery: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    """Hold one session's delivery registration for the life of a follower.

    The seat proves a producer exists. This proves a *reader* exists, for a
    named session, which is the only fact a dispatch guard can act on: a seat
    is project-global while the wake-up it feeds is session-local, so a caller
    dispatching against a peer's seat is told a watcher is live and still hears
    nothing.

    The registration is an advisory lock held by the live follower, so it is
    released by the process ending however it ends — the same property that
    keeps the seat honest. Call :meth:`_FollowerRegistration.acquire` again while
    streaming to take over a registration whose holder has since gone.
    """
    registration = _FollowerRegistration(
        project, session, delivery=delivery or delivery_mode(), scope=scope
    )
    registration.acquire()
    try:
        yield registration
    finally:
        registration.release()


@contextmanager
def follower_claim(
    project: str,
    session: str,
    *,
    delivery: str | None = None,
    scope: Mapping[str, Any] | None = None,
):
    """Register one session's delivery, reporting whether the claim succeeded."""
    with follower_registration(
        project, session, delivery=delivery, scope=scope
    ) as registration:
        yield (
            registration.held,
            (registration.record if registration.held else registration.blocked_by),
        )


# A claim takes the lock and then writes its record, so a reader can arrive
# between the two and see a held lock with nothing in it. Settling is measured in
# microseconds; treating that instant as "delivery unknown" would refuse a
# dispatch against a follower that is fine, so a reader waits out the gap.
_REGISTRATION_SETTLE_SECONDS = 0.25


FOLLOWER_FRESHNESS_SECONDS = 1.0
_FOLLOWER_REGISTRATION_ENV = "RECKON_FOLLOWER_REGISTRATION"

# The process that armed a follower, as pid plus kernel start time, is fixed
# once by the follower's first image and never re-derived afterwards. It cannot
# live in the registration record: a second follower that takes a registration
# over rewrites that record from the claimant's own parent, and a read-only
# follower has no record to read. It cannot be re-derived from ``os.getppid()``
# on each pass either, because once the arming session dies that names init or a
# subreaper — the very state the follower must recognise. The value is carried
# in this variable, supplied by whoever arms the follower and re-supplied to a
# reloaded image through the environment its re-exec receives.
_FOLLOWER_OWNER_ENV = "RECKON_FOLLOWER_OWNER"


class _FollowerOwnerCache:
    """The process that owns this image, resolved at the first read and kept.

    Held here rather than in ``os.environ`` so a follower started in-process —
    a reader driving the command under test, say — does not hand its own
    environment to a follower it goes on to start.
    """

    resolved: tuple[int, str] | None = None

    def resolve(self) -> tuple[int, str]:
        if self.resolved is not None:
            return self.resolved
        recorded = _parse_follower_owner(os.environ.get(_FOLLOWER_OWNER_ENV))
        if recorded is None:
            pid = os.getppid()
            recorded = (pid, _process_start_time(pid) or "")
        self.resolved = recorded
        return recorded


_RESOLVED_FOLLOWER_OWNER = _FollowerOwnerCache()


def _parse_follower_owner(raw: Any) -> tuple[int, str] | None:
    """Read an owner identity from its environment encoding, or None."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid, str(payload.get("start_time") or "")


def _format_follower_owner(owner: tuple[int, str]) -> str:
    """Encode an owner identity for the environment a re-exec carries."""
    return json.dumps({"pid": int(owner[0]), "start_time": str(owner[1])})


def follower_owner() -> tuple[int, str]:
    """The pid and start time of the process that armed this follower.

    Read from the environment when an armer supplied one, from the previous
    resolution otherwise, and computed from ``os.getppid()`` exactly once when
    neither exists. A process that arms a follower without stamping an owner —
    a test harness, say — is therefore treated as that follower's owner without
    any extra cooperation, and the value is fixed from that first read onward so
    a later re-parent cannot move it.
    """
    return _RESOLVED_FOLLOWER_OWNER.resolve()


def _follower_liveness(path: Path) -> dict[str, Any]:
    """Read one registration and decide whether it still delivers."""
    if not path.is_file():
        return {
            "registered": False,
            "live": False,
            # No registration file at all: the session never armed a follower,
            # which is not the same as a release and must not be treated as one.
            "released": False,
            "not_live_because": "no registration remains",
            "delivery": None,
            "follower": {},
        }
    deadline = time.monotonic() + _REGISTRATION_SETTLE_SECONDS
    while True:
        with path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                registered = True
                record = _read_watch_record(handle)
            else:
                registered = False
                record = _read_watch_record(handle)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if record or not registered or time.monotonic() >= deadline:
            break
        time.sleep(0.005)

    pid = record.get("pid")
    running = record_process_alive(record) is True

    # An orphaned follower has lost the session it was reporting to, so its
    # lines go nowhere even while the process runs.
    consumer_alive = True
    if "parent_pid" in record:
        try:
            parent_pid = int(record.get("parent_pid") or 0)
        except (TypeError, ValueError):
            parent_pid = 0
        consumer_alive = parent_pid > 1 and process_alive(parent_pid) is True

    # Prefer what the descriptor says now over what registration recorded: a
    # recorded verdict survives its consumer going away, and answers with
    # whatever the check understood when it was written. The one reader that
    # keeps the declaration is the registering process itself, where the
    # declaration is a statement about its own output and deceives nobody
    # else; a dispatch guard is always a different process, which is the case
    # this measures.
    observed = (
        delivery_mode_of(pid)
        if isinstance(pid, int) and running and pid != os.getpid()
        else None
    )
    delivery = observed or str(record.get("delivery") or "unknown")
    live = bool(
        registered and running and consumer_alive and delivery in DELIVERING_MODES
    )
    # A row that is not live says which condition ended it. Its `since` records
    # when it attached, so a dead row's only timestamp makes it look older and
    # better established rather than stale — and a reader counting rows to ask
    # "is this project covered" is then answered by a registration that ended.
    reason = ""
    if not live:
        if not registered and record:
            reason = (
                f"the registration was released; its process {record.get('pid')} "
                "is gone"
                if not running
                else "the registration was released"
            )
        elif not registered:
            reason = "no registration remains"
        elif not running:
            reason = f"the registered process {record.get('pid')} is gone"
        elif not consumer_alive:
            reason = (
                f"the session consuming it (process {record.get('parent_pid')}) "
                "has exited"
            )
        else:
            reason = (
                f"its lines end in a {delivery}, which nothing reads until the "
                "command exits — and a follower does not exit"
            )
    return {
        "registered": registered,
        "live": live,
        # A registration file that exists while its advisory lock is free is a
        # follower that was armed and has since expired; a path that was never
        # created is a session that never armed one. Both read as not
        # registered, so a release keeps a record of its own — the file the
        # release left behind — and this is the flag that tells them apart.
        "released": bool(record) and not registered,
        "not_live_because": reason,
        "delivery": delivery,
        "delivery_recorded": str(record.get("delivery") or "unknown"),
        "delivery_observed": observed,
        "consumer_alive": consumer_alive,
        "follower": record,
    }


def follower_state(project: str, session: str) -> dict[str, Any]:
    """Report whether one session will be woken by this project's ticker."""
    state = _follower_liveness(follower_lock_path(project, session))
    return {
        "project": project,
        "session": session,
        "attach_line": _watch_attach_line(project, session=session),
        **state,
    }


def list_followers(project: str) -> list[dict[str, Any]]:
    """List every registered consumer of one project's ticker."""
    directory = follower_dir(project)
    if not directory.is_dir():
        return []
    rows = []
    for path in sorted(directory.glob("*.lock")):
        state = _follower_liveness(path)
        session = str(state["follower"].get("session") or path.stem)
        rows.append({"project": project, "session": session, **state})
    return rows


# A released registration keeps its file: ``release`` drops the advisory lock
# and leaves the path in place on purpose, because a second follower may hold the
# same inode read-only and take over. Nothing on a read path removes one either —
# a reader that unlinked while another process was opening the same path would
# put two inodes behind one session's name, and a claim on each would look
# successful. So the directory only grows, one file per session that has ever
# followed the project, and only an explicit maintenance call trims it. The
# window is generous by design: it has to outlast any pause between a released
# registration and the session restarting, and longer than that leaves a residue
# the caller can reason about rather than a registry that grows without bound.
FOLLOWER_REGISTRY_STALE_SECONDS = 14 * 24 * 60 * 60


def sweep_released_followers(
    project: str,
    *,
    stale_after_seconds: float = FOLLOWER_REGISTRY_STALE_SECONDS,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Remove released registrations older than ``stale_after_seconds``.

    A registration is removed only when two things hold at once: its advisory
    lock is free, so nothing is delivering from it, and its file is older than
    the threshold, so a session that released moments ago and is restarting is
    left alone. The check that keeps a delivering registration is the lock and
    not the timestamp — a follower armed in the morning and still delivering at
    night survives a sweep whose other candidates are half its age — so this is
    safe to run against a directory holding a live follower.

    The sweep takes each file's lock before unlinking it, which is the one place
    a registration file is removed. A caller must not invoke it from a read path:
    unlinking there races a process opening the same path, letting one hold the
    old inode and the other a fresh one for a single session. Even here a claim
    that arrives while the sweep holds the lock is refused by
    :meth:`_FollowerRegistration.acquire` rather than silently succeeding, so run
    it from an explicit maintenance path where no registration is in flight; the
    residual window is a claimant that has opened the path without yet taking the
    lock.

    ``now`` supplies the reference time and lets a caller reason about a fixed
    clock. Returns one entry per removed registration, so the caller reports a
    count rather than re-listing the directory to discover it.
    """
    directory = follower_dir(project)
    if not directory.is_dir():
        return []
    reference = time.time() if now is None else now
    removed: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.lock")):
        try:
            handle = path.open("a+b")
        except OSError:
            continue
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Its lock is held, so it delivers right now whatever its age.
                continue
            try:
                try:
                    age = reference - path.stat().st_mtime
                except OSError:
                    continue
                if age < stale_after_seconds:
                    continue
                session = str(_read_watch_record(handle).get("session") or path.stem)
                path.unlink()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        removed.append(
            {
                "project": project,
                "session": session,
                "path": str(path),
                "age_seconds": age,
            }
        )
    return removed


from .process_liveness import (  # noqa: E402
    _process_start_time,
    process_alive,
    record_process_alive,
)


from .run_paths import (  # noqa: E402
    _read_watch_record,
    _utc_now,
    _write_watch_record,
    watch_lock_path,
)


from .watch_unit import (  # noqa: E402
    _watch_attach_line,
)
