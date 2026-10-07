"""The session host: one supervisor of a session's crew followers.

Claude Code ends a monitor at thirty minutes, so a follower armed through the
Monitor primitive dies with it and every re-arm costs a turn. The session host
is the session's one monitor instead: it lives for as long as the Claude process
that started it, and it starts and restarts one ``crew follow`` per project and
session a dispatch asks it to.

Requests arrive one JSON object per line on a FIFO descriptor the plugin entry
point opens once and the host inherits, each naming a ``project`` and a
``session``. For every request the host runs ``reckon crew follow --project P
--session S`` with no ``--lifetime``. The follower's stdout is the host's, so a
ticker line reaches the pane exactly as the follower prints it; the host itself
writes nothing there, and a child's stderr goes to a log on the shared home
where a later session can read it.

A child is always the host's direct child, started with a parent-death signal
and with the host recorded as its owner, so it ends with the host or with the
silence that follows one. The host never exits on its own -- Claude Code would
not restart it -- and ends only when its owner is gone or when it is signalled,
in both of which it stops every child first.

Where the record lives and where the session host resolves its owner are
overridable so a harness can drive this reader against a temporary tree:
``RECKON_SESSION_HOST_STATE_DIR`` for the record directory,
``RECKON_SESSION_HOST_LOG_DIR`` for the child-stderr directory, and
``RECKON_SESSION_HOST_POLL_SECONDS`` / ``RECKON_SESSION_HOST_BACKOFF`` for the
owner-check cadence and the first restart delay.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reckon._store import cache_root, write_json_atomically
from reckon.crew import fleet_supervisor
from reckon.crew.obligation_snapshot import process_start_time
from reckon.crew.runs import (
    _FOLLOWER_OWNER_ENV,
    _format_follower_owner,
    _reckon_console_script,
    crew_home,
    follower_state,
    process_alive,
)

STATE_DIR_ENV = "RECKON_SESSION_HOST_STATE_DIR"
LOG_DIR_ENV = "RECKON_SESSION_HOST_LOG_DIR"
POLL_ENV = "RECKON_SESSION_HOST_POLL_SECONDS"
BACKOFF_ENV = "RECKON_SESSION_HOST_BACKOFF"

# The owner is re-checked this often while the host waits for a request. It is
# the same cadence a follower uses to notice its owner, so a session that ends
# takes its followers down within one tick of the session host noticing.
DEFAULT_POLL_SECONDS = 5.0

# The grace a child is given to stop on its own before it is killed, and the
# cadence at which the stop path re-reads it.
STOP_GRACE_SECONDS = 10.0
STOP_POLL_SECONDS = 0.1

# The signal a child is sent when its parent -- this host -- dies, so a host
# killed without a chance to run its own stop path still takes its children.
PARENT_DEATH_SIGNAL = signal.SIGTERM
PR_SET_PDEATHSIG = 1

# The owner identity is encoded the same way a follower's owner is, so a child
# records this host as the process consuming its output. Both the variable name
# and the encoding are the follower's own, imported from ``runs`` so the two
# are the same bytes rather than two spellings that agree today.
RECORD_SUFFIX = ".json"


def _state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The directory holding one record per live session host."""
    environ = os.environ if environ is None else environ
    override = environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    return crew_home() / "session-hosts"


def _log_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The directory holding a child's stderr, on the shared home.

    The explicit override wins; the default is resolved by the cache owner so
    the whole precedence — environment variable, then XDG cache home, then the
    user cache — lives in one place rather than being re-read here.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    return cache_root("session-host")


def _poll_seconds(environ: Mapping[str, str] | None = None) -> float:
    """How often the owner is re-checked, and the idle wait for a request."""
    environ = os.environ if environ is None else environ
    return _non_negative_float(environ.get(POLL_ENV), DEFAULT_POLL_SECONDS)


def _initial_backoff(environ: Mapping[str, str] | None = None) -> float:
    """The delay before a child's first restart, before ``next_backoff`` grows it."""
    environ = os.environ if environ is None else environ
    return _non_negative_float(
        environ.get(BACKOFF_ENV), fleet_supervisor.FIRST_BACKOFF_SECONDS
    )


def _non_negative_float(raw: Any, default: float) -> float:
    """Parse a non-negative float from the environment, or the default."""
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0.0 else default


@dataclass
class _Child:
    """One follower the host runs, and the schedule that brings it back."""

    project: str
    session: str
    pid: int | None = None
    delay: float = fleet_supervisor.FIRST_BACKOFF_SECONDS
    next_attempt: float | None = None
    started_at: float | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.session)


def parse_request(line: str) -> tuple[str, str] | None:
    """Read one request line into its project and session, or None.

    A line the host cannot act on is skipped rather than ending the reader: the
    stream is shared with whatever the plugin later writes, and one malformed
    line must not take every follower down.
    """
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    project = str(payload.get("project") or "").strip()
    session = str(payload.get("session") or "").strip()
    if not project or not session:
        return None
    return project, session


def owner_alive(owner: Mapping[str, Any]) -> bool:
    """Whether the process named by a recorded owner identity still lives.

    Liveness is the pid *and* its kernel start time: a pid alone is reused, and
    a reused pid would keep a dead session's followers alive forever.
    """
    pid = owner.get("pid")
    start = owner.get("start_time")
    if not isinstance(pid, int) or pid <= 1:
        return False
    if process_alive(pid) is not True:
        return False
    return bool(start) and process_start_time(pid) == start


def default_owner() -> dict[str, Any]:
    """Resolve the owner as the parent process, the way a follower does."""
    pid = os.getppid()
    return {"pid": pid, "start_time": process_start_time(pid) or ""}


def default_follower_argv() -> list[str]:
    """The command the host runs per request: this checkout's ``crew follow``."""
    return [_reckon_console_script(), "crew", "follow"]


def _fifo_path(owner: Mapping[str, Any]) -> Path | None:
    """The request FIFO the plugin entry point named for this host's owner.

    The entry point creates the FIFO under the node-local runtime root, named
    for the owner pid and its kernel start tick, and the host removes that same
    path when it stops. The directory name and the runtime-root order are the
    dispatcher's own -- the module that resolves the path to ask this host for
    a follower -- and are imported here so one spelling of each serves both,
    rather than two that agree only until one changes. The import is deferred
    to call time because ``dispatch`` reaches back into this module lazily, and
    a module-level import would close a cycle.
    """
    from reckon.crew.dispatch import (
        SESSION_HOST_DIRECTORY,
        _session_host_runtime_root,
    )

    root = _session_host_runtime_root()
    pid = owner.get("pid")
    if root is None or not pid:
        return None
    start = owner.get("start_time") or "0"
    return root / SESSION_HOST_DIRECTORY / f"{pid}-{start}.fifo"


def _set_parent_death_signal(expected_parent: int) -> None:
    """Runs between fork and exec: arm the kernel parent-death signal.

    The signal fires when this child's parent thread dies, so a host killed
    outright still takes its children. The parent may already be gone by the
    time this runs -- the fork and the prctl are not one step -- so the parent
    is re-read afterwards and the child leaves at once if it has changed; a
    signal armed against a parent that is already dead would never fire.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, PARENT_DEATH_SIGNAL, 0, 0, 0)
    if os.getppid() != expected_parent:
        os._exit(1)


class SessionHost:
    """Supervises one follower per project and session for one owner session.

    Children are keyed by ``(project, session)``, so a second request for a pair
    already followed changes nothing. A child that exits while the owner lives
    is restarted on the schedule ``fleet_supervisor.next_backoff`` applies to a
    declared service, unless a live follower this host did not start holds the
    pair's registration -- a coordinator who armed one by hand is never
    contended with. A child already running is never stopped because another
    follower appeared.
    """

    def __init__(
        self,
        *,
        owner: Mapping[str, Any] | None = None,
        follower_argv: Sequence[str] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._environ = dict(os.environ if environ is None else environ)
        resolved = owner if owner is not None else default_owner()
        self._owner = {
            "pid": int(resolved.get("pid") or 0),
            "start_time": str(resolved.get("start_time") or ""),
        }
        self._follower_argv = list(
            follower_argv if follower_argv is not None else default_follower_argv()
        )
        self._clock = clock
        self._now = now
        self._children: dict[tuple[str, str], _Child] = {}
        self._stopping = False
        self._stopped = False
        self._last_record: str | None = None
        self._stem = f"{self._owner['pid']}-{self._owner['start_time'] or '0'}"
        self._record_path = _state_dir(self._environ) / f"{self._stem}{RECORD_SUFFIX}"
        self._log_path = _log_dir(self._environ) / f"{self._stem}.log"
        self._fifo_path = _fifo_path(self._owner)

    # -- identity -----------------------------------------------------------

    def _record_name(self) -> str:
        return f"{self._stem}{RECORD_SUFFIX}"

    @property
    def owner(self) -> dict[str, Any]:
        return dict(self._owner)

    @property
    def record_path(self) -> Path:
        return self._record_path

    # -- record -------------------------------------------------------------

    def record(self) -> dict[str, Any]:
        """The census payload naming this host and every child it runs."""
        return {
            "pid": os.getpid(),
            "owner": dict(self._owner),
            "children": [
                {
                    "project": child.project,
                    "session": child.session,
                    "pid": child.pid,
                }
                for child in sorted(self._children.values(), key=lambda c: c.key)
                if child.pid is not None
            ],
            "updated_at": self._now(),
        }

    def _write_record(self, *, force: bool = False) -> None:
        """Write the census record, and only when it has changed.

        ``updated_at`` moves on every call, so it is excluded from the change
        comparison: the record is written when a child starts, exits or is
        stopped, which is the change a census cares about.
        """
        payload = self.record()
        comparable = json.dumps({k: v for k, v in payload.items() if k != "updated_at"})
        if not force and comparable == self._last_record:
            return
        self._last_record = comparable
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomically(
            self._record_path, payload, fsync=False, create_parents=True
        )

    # -- request handling ---------------------------------------------------

    def handle(self, line: str) -> None:
        """Act on one request line: run a follower for its project and session."""
        parsed = parse_request(line)
        if parsed is None:
            return
        project, session = parsed
        key = (project, session)
        if key in self._children:
            return
        if self._foreign_live_follower(project, session, pid=None):
            # A live follower this host did not start owns the pair -- a
            # coordinator armed one by hand. The pair is wanted but deferred:
            # record it with no pid and a retry due, so tick() starts this
            # host's own follower once that one exits. A Monitor-armed follower
            # ends at twenty-nine minutes, and without this the pair would go
            # unwatched until the next dispatch re-requested it.
            deferred = _Child(project=project, session=session)
            deferred.delay = _initial_backoff(self._environ)
            deferred.next_attempt = self._clock() + max(
                deferred.delay, _poll_seconds(self._environ)
            )
            self._children[key] = deferred
            self._write_record()
            return
        child = _Child(project=project, session=session)
        child.delay = _initial_backoff(self._environ)
        pid = self._spawn(child)
        if pid is None:
            return
        child.pid = pid
        child.started_at = self._clock()
        self._children[key] = child
        self._write_record()

    def _foreign_live_follower(
        self, project: str, session: str, *, pid: int | None
    ) -> bool:
        """Whether a live follower this host did not start holds the pair."""
        try:
            state = follower_state(project, session)
        except (OSError, ValueError):
            return False
        if not state.get("live"):
            return False
        return state.get("follower", {}).get("pid") != pid

    def _spawn(self, child: _Child) -> int | None:
        """Fork one follower, inheriting this host's stdout and the death signal."""
        argv = [
            *self._follower_argv,
            "--project",
            child.project,
            "--session",
            child.session,
        ]
        environ = dict(self._environ)
        environ[_FOLLOWER_OWNER_ENV] = _format_follower_owner(
            (os.getpid(), process_start_time(os.getpid()) or "")
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        parent = os.getpid()
        try:
            with open(self._log_path, "ab") as sink:
                process = subprocess.Popen(
                    argv,
                    env=environ,
                    stdin=subprocess.DEVNULL,
                    stdout=None,
                    stderr=sink,
                    # The host runs single-threaded, so the fork-to-exec window
                    # this closes is not shared with another thread; the death
                    # signal can only be armed there.
                    preexec_fn=lambda: _set_parent_death_signal(parent),  # noqa: PLW1509
                )
        except OSError as exc:
            _log(f"follower for {child.project}/{child.session} failed: {exc}")
            return None
        return process.pid

    # -- supervision --------------------------------------------------------

    def tick(self) -> None:
        """Re-check every child once: restart the exited, leave the hand-armed."""
        now = self._clock()
        for child in list(self._children.values()):
            if child.pid is not None and not self._alive(child):
                ran = now - child.started_at if child.started_at is not None else 0.0
                delay, following = fleet_supervisor.next_backoff(child.delay, ran)
                child.delay = following
                child.pid = None
                child.started_at = None
                child.next_attempt = now + delay
            if child.pid is None and child.next_attempt is not None:
                if now < child.next_attempt:
                    continue
                child.next_attempt = None
                if self._foreign_live_follower(child.project, child.session, pid=None):
                    child.next_attempt = now + max(
                        child.delay, _poll_seconds(self._environ)
                    )
                    continue
                pid = self._spawn(child)
                if pid is None:
                    child.next_attempt = now + child.delay
                    continue
                child.pid = pid
                child.started_at = now
        self._write_record()

    def _alive(self, child: _Child) -> bool:
        """Whether a child's recorded pid still runs, collecting it if it exited."""
        if child.pid is None:
            return False
        try:
            reaped, _status = os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            return False
        return reaped != child.pid

    def stop(self) -> None:
        """Stop every child and wait for it, then write the final record.

        The guard is the performed-stop flag, not the request flag: a signal
        sets the request flag to end the read, and the stop path must still run
        afterwards. Guarding on the request flag made every signal-path stop a
        no-op, leaving child teardown resting on the parent-death signal alone.
        """
        if self._stopped:
            return
        self._remove_fifo()
        self._stopped = True
        for child in list(self._children.values()):
            self._stop_child(child)
        self._write_record(force=True)

    def _remove_fifo(self) -> None:
        """Remove the request FIFO this host was handed, if it still exists.

        An owner that ends and a signal both reach the stop path, so a session
        that ends leaves no stale FIFO in the runtime directory for a later
        session to inherit. Only this host's own FIFO -- the path named from its
        owner pair -- is touched; a FIFO belonging to another session is never
        removed.
        """
        if self._fifo_path is None:
            return
        with contextlib.suppress(OSError):
            self._fifo_path.unlink(missing_ok=True)

    def _stop_child(self, child: _Child) -> None:
        """Signal one child by its recorded pid and wait for it to end."""
        pid = child.pid
        child.pid = None
        if pid is None:
            return
        _signal(pid, signal.SIGTERM)
        deadline = self._clock() + STOP_GRACE_SECONDS
        while process_alive(pid) is True:
            if self._clock() >= deadline:
                _signal(pid, signal.SIGKILL)
                break
            time.sleep(STOP_POLL_SECONDS)
        _reap(pid)

    def request_stop(self) -> None:
        """Ask the reader to end: used by a signal handler and by tests."""
        self._stopping = True

    def supervise(
        self, requests: Any, wake_read: int, first_request: str | None = None
    ) -> int:
        """Read request lines until the owner ends, a signal comes, or EOF.

        Each pass reads whatever request is waiting, re-checks the owner, and
        runs one supervision tick. The idle wait is the owner-check cadence, so
        a quiet session host still notices its owner's end within one tick; a
        request or a signal wakes it at once rather than at the tick.

        ``first_request`` is a line the caller already read off the descriptor
        before handing it over -- the plugin entry point reads exactly one line
        to decide its exec, then passes both that line and the still-open
        descriptor. It is handled through the same path as a line read here, so
        the two orders are indistinguishable; handling it before the loop keeps
        it ahead of whatever the descriptor delivers next.
        """
        self._write_record(force=True)
        if first_request:
            self.handle(first_request)
        descriptor = requests.fileno()
        buffer = b""
        try:
            while not self._stopping:
                ready, _w, _x = select.select(
                    [requests, wake_read], [], [], _poll_seconds(self._environ)
                )
                if wake_read in ready:
                    _drain(wake_read)
                    break
                if requests in ready:
                    # Read the descriptor raw rather than through a buffered
                    # reader: a ``readline`` on a text stream pulls the whole
                    # available chunk into the stream's own buffer, so requests
                    # arriving together are invisible to ``select`` after the
                    # first line and every later one waits for new input.
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        self.handle(raw.decode("utf-8", errors="replace"))
                if not owner_alive(self._owner):
                    break
                self.tick()
        finally:
            self.stop()
        return 0


def run(
    *,
    owner: Mapping[str, Any] | None = None,
    follower_argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    requests: Any | None = None,
    first_request: str | None = None,
) -> int:
    """Run one session host: install signal handlers, then read to its end.

    ``requests`` is a text-mode object the host reads request lines from -- by
    default this process's stdin, which the plugin points at the FIFO before it
    execs. A self-pipe is watched beside it so a signal ends the read at once
    rather than after the idle tick. ``first_request`` is a line the caller
    already read off that descriptor and passes in beside it.
    """
    environ = os.environ if environ is None else environ
    host = SessionHost(owner=owner, follower_argv=follower_argv, environ=environ)
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)

    def _on_signal(_signum: int, _frame: Any) -> None:
        host.request_stop()
        with contextlib.suppress(OSError):
            os.write(wake_write, b"x")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _on_signal)
    stream = requests if requests is not None else sys.stdin
    try:
        host.supervise(stream, wake_read, first_request=first_request)
    finally:
        host.stop()
        os.close(wake_read)
        os.close(wake_write)
    return 0


def _drain(descriptor: int) -> None:
    """Empty a non-blocking descriptor so its readable notify is cleared."""
    while True:
        try:
            if not os.read(descriptor, 4096):
                return
        except BlockingIOError:
            return
        except OSError:
            return


def _signal(pid: int, signum: int) -> None:
    """Signal one pid, ignoring a process that has already gone."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return


def _reap(pid: int) -> None:
    """Collect a child that has exited, so it leaves no dead slot."""
    for _ in range(20):
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == pid:
            return
        time.sleep(STOP_POLL_SECONDS)


def _log(line: str) -> None:
    """Write one diagnostic line to stderr, never to the pane's stdout."""
    print(f"[session-host] {line}", file=sys.stderr, flush=True)
