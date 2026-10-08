# ruff: noqa: I001, UP035
from __future__ import annotations
import dataclasses
import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import (
    Path,
)
from typing import (
    Any,
    Mapping,
)
from reckon import (
    _store,
)
from reckon.crew.node import (
    CrewError,
    WatcherRequired,
)
from reckon.crew.refusals import (
    format_refusal,
)
from reckon.crew.runs import (
    WATCH_LOG_ENV,
    _process_start_time,
    _write_json,
    watch_lock_path,
    watch_log_path,
    watch_observer_alive,
    watch_state,
    watch_stream_path,
)



# Process startup and registration may receive only one scheduler slice in six
# while two CPU-bound jobs share a loaded host. Keep every watcher condition
# wait on this one six-times-unloaded bound so a red test reports a producer
# defect rather than which process won the scheduler.
WATCHER_LOAD_BOUND_SECONDS = 30.0


# Arming spawns a detached supervisor on purpose: a coordinator's producer has
# to outlive the process that armed it. Under a test the same act is a leak —
# the test ends, its configuration home is deleted, and the producer keeps
# polling a directory nothing will ever write to again. Measured before a
# manual reap: 25 live producers for one fixture project, the oldest 204 hours
# old, 14 of them polling an already-deleted temporary home. So arming refuses
# when the resolved configuration home lies under a pytest temporary
# directory, and the refusal is raised at the caller rather than skipped
# quietly. A pytest-named directory above the home is one signal; a pytest
# session's own declared --basetemp is the other, so a custom base temp whose
# directory carries no pytest name is still recognised. A test whose own
# subject is the producer lifecycle, and which reaps what it starts, says so
# through this variable.
WATCH_ARMING_ENV = "RECKON_WATCH_ARMING"
_PYTEST_TEMPORARY_ROOT = re.compile(r"^(pytest-of-.+|pytest-\d+)$")


def _watch_arming_intent() -> str:
    """Return the environment's stated arming intent: ``on``, ``off`` or ``""``."""
    return os.environ.get(WATCH_ARMING_ENV, "").strip().lower()


def watch_arming_suppressed() -> bool:
    """True when the environment forbids arming, so callers waive the watch.

    The suppression is expressed through the same waiver a `--no-watch`
    dispatch records, so a suppressed run is visible on its own record rather
    than being a producer that silently never existed.
    """
    return _watch_arming_intent() == "off"


def _running_under_pytest() -> bool:
    """True when this process is a pytest session or one of its workers."""
    return bool(
        os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST_VERSION")
    )


def _current_and_ancestor_argvs(limit: int = 12) -> list[list[str]]:
    """The argv of this process and its ancestors, nearest first, bounded."""
    argvs: list[list[str]] = []
    pid = os.getpid()
    for _ in range(limit):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        argvs.append([os.fsdecode(item) for item in raw.split(b"\0") if item])
        rest = stat.rsplit(")", 1)[-1].split()
        parent = int(rest[1]) if len(rest) > 1 else 2
        if parent in (0, 1, pid):
            break
        pid = parent
    return argvs


def _declared_basetemp() -> Path | None:
    """The temporary root the enclosing pytest session was told to use.

    A session given a custom ``--basetemp`` names it on the command line, which
    this process carries itself in a serial run and inherits from the session
    master through its ancestors under ``pytest-xdist``. The default base temp
    is discovered instead by the ``pytest-of-*`` ancestor name, so only a
    declared one is read here, and only when a pytest session is running.
    """
    if not _running_under_pytest():
        return None
    for argv in _current_and_ancestor_argvs():
        for index, arg in enumerate(argv):
            if arg == "--basetemp" and index + 1 < len(argv):
                return Path(argv[index + 1])
            if arg.startswith("--basetemp="):
                return Path(arg.split("=", 1)[1])
    return None


def _temporary_home_root(home: Path) -> Path | None:
    """Return the throwaway test root containing ``home``, if there is one.

    Two signals name a throwaway home: a pytest-named directory above it, and
    the temporary root a running pytest session declared on its own command
    line. The second is what carries a custom ``--basetemp`` such as
    ``/tmp/anything``, whose directory carries no pytest name for the first to
    match, and it is read from the running session so an ordinary home outside
    that root is still armed.
    """
    for candidate in (home, *home.parents, *home.resolve().parents):
        if _PYTEST_TEMPORARY_ROOT.match(candidate.name):
            return candidate
    declared = _declared_basetemp()
    if declared is not None:
        root = declared.resolve()
        resolved = home.resolve()
        if resolved == root or root in resolved.parents:
            return declared
    return None


def _refuse_arming_under_a_throwaway_home(project: str) -> None:
    """Refuse to arm a producer that would outlive the home it reports into."""
    if _watch_arming_intent() == "on":
        return
    home = _store._config_home()
    root = _temporary_home_root(home)
    if root is None:
        return
    raise CrewError(
        format_refusal(
            "D18",
            f"refusing to arm the watch producer for {project}: the resolved "
            f"configuration home {home} lies under the throwaway test directory "
            f"{root}, so a detached producer would outlive the run that armed it "
            f"and poll a deleted home. Set {WATCH_ARMING_ENV}=on for a caller "
            "that reaps the producer it starts, or waive the watch instead.",
        )
    )


def _watch_executable() -> str:
    """Resolve the console entry point beside the running interpreter first."""
    adjacent = Path(sys.executable).with_name("reckon")
    if adjacent.is_file():
        return str(adjacent)
    executable = shutil.which("reckon")
    if executable:
        return executable
    raise CrewError(
        format_refusal("D19", "cannot start the project watcher: reckon is not on PATH")
    )


# The supervisor that starts one project's watcher as a background process and
# waits on it. It exports the watcher's log path into the environment the
# watcher inherits, taking the variable name and the path from its own argv.
#
# The path rides the argv rather than this process's environment because the
# fleet-delegated route does not spawn here: it hands this argv to the
# allocation's batch step, which starts it from the step's own environment, so
# a path only the arming process held would reach the watcher on the route that
# does not need it and be missing on the fleet route that does. Both routes are
# covered once the supervisor exports it, whichever process runs the argv.
_WATCH_PRODUCER_SUPERVISOR = (
    "import os, subprocess, sys; "
    "os.environ[sys.argv[1]] = sys.argv[2]; "
    "producer = subprocess.Popen(sys.argv[3:], stdin=subprocess.DEVNULL, "
    "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True); "
    "raise SystemExit(producer.wait())"
)


def _watch_producer_argv(project: str) -> list[str]:
    """Build the argv that starts one project's watcher as a background process."""
    return [
        sys.executable,
        "-c",
        _WATCH_PRODUCER_SUPERVISOR,
        WATCH_LOG_ENV,
        str(watch_log_path(project)),
        _watch_executable(),
        "crew",
        "watch",
        "--project",
        project,
    ]


@dataclasses.dataclass
class _SpawnedHandle:
    """The handle a fleet-delegated launch returns in place of a Popen.

    ``_ensure_watch_producer`` polls a producer handle to notice a producer
    that died before it took its seat. A spawn the batch step made cannot be
    polled from here — it is not this process's child — so the handle reports
    ``poll()`` as None and liveness is decided, as it always is, by the
    process-backed seat the producer registers when it starts.
    """

    pid: int

    def poll(self) -> None:
        return None


def _start_watch_producer(project: str) -> Any:
    """Start a detached supervisor that remains the watcher's live parent.

    On the fleet node the detached child is reaped with the session step that
    made it, so the producer is spawned through the same FIFO a worker is, by
    the allocation's batch step.
    """
    _refuse_arming_under_a_throwaway_home(project)
    argv = _watch_producer_argv(project)
    fleet = _read_fleet_record()
    if (
        _fleet_spawn_enabled()
        and fleet is not None
        and _runs_inside_fleet_allocation(fleet)
    ):
        runtime_dir = _fleet_runtime_dir(fleet)
        if runtime_dir is not None:
            directory = watch_lock_path(project).parent
            directory.mkdir(parents=True, exist_ok=True)
            spec_path = directory / "producer.json"
            _write_json(spec_path, {"project": project, "argv": argv})
            pid = _spawn_through_fleet(
                runtime_dir,
                f"watch-{_watch_request_slug(project)}",
                spec_path,
                directory / FLEET_SPAWN_ACK_NAME,
            )
            return _SpawnedHandle(pid=pid)
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def _stop_watch_producer_within(project: str, timeout: float) -> None:
    """Bound seat release even when the producer does not honour SIGTERM.

    Unwatch waits for the seat lock after signalling. A separate process lets
    arming cancel that wait without leaving a thread holding the arm lock.
    The child inherits neither the arm descriptor nor any other held lock.
    """
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from reckon.crew.recovery import unwatch; unwatch(sys.argv[2])"
                ),
                str(Path(__file__).resolve().parents[2]),
                project,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            close_fds=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise CrewError(
            f"watch producer for {project} did not release its seat within "
            f"{WATCHER_LOAD_BOUND_SECONDS:g}s; arming stopped"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise CrewError(
            f"cannot release watch producer for {project}: {exc.stderr}"
        ) from exc


def _ensure_watch_producer(
    project: str, *, session: str | None = None
) -> dict[str, Any]:
    """Return the watcher state, starting at most one producer across calls.

    The returned state reports whether a producer is live rather than raising
    when one cannot be brought up: admission is the caller's decision, and the
    caller is also the only place that holds the session whose delivery is a
    separate question from the watcher's liveness.
    """
    arming_lock = watch_stream_path(project).with_suffix(".arm.lock")
    arming_lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    with arming_lock.open("a+b") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CrewError(
                        f"watch arming lock {arming_lock} remained held for "
                        f"{WATCHER_LOAD_BOUND_SECONDS:g}s; arming stopped"
                    ) from None
                time.sleep(0.05)
        # Only producer identity belongs inside the launch mutex. Following
        # pipes or enumerating the fleet can wait on unrelated long-lived work.
        state = watch_state(project)
        if state["watcher_live"] and watch_observer_alive(state["watcher"]) is False:
            _stop_watch_producer_within(project, max(0.0, deadline - time.monotonic()))
            state = watch_state(project)
        if not state["watcher_live"]:
            supervisor = _start_watch_producer(project)
            while time.monotonic() < deadline:
                if watch_state(project)["watcher_live"]:
                    break
                if supervisor.poll() is not None:
                    break
                time.sleep(0.05)
    return watch_state(project, session=session)


# The session host a Claude Code session runs declares its request FIFO in a
# directory under the node-local runtime root, named for the Claude process and
# its kernel start tick so the pair survives /clear. Dispatch resolves the same
# path to ask that session's host for a follower before it refuses.
SESSION_HOST_DIRECTORY = "reckon-session-host"


def _session_host_runtime_root() -> Path | None:
    """The node-local root a session host's FIFO lives under, or None.

    The same three candidates the host entry point chooses from, so a dispatch
    and the host it asks resolve the same directory: the session's own runtime
    directory first, then the per-user directory a login node provides, then the
    scratch root. Each is node-local, so waiting on the FIFO costs nothing on
    shared storage.
    """
    runtime = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime:
        return Path(runtime)
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        return run_user
    scratch = str(os.environ.get("TMPDIR") or "").strip()
    return Path(scratch) if scratch else None


def _session_host_owner() -> tuple[int, str] | None:
    """The calling Claude process as ``(pid, start tick)``, or None without one.

    The host belongs to this session's Claude process, not to the crew session
    name, so every path that names it -- the request FIFO and the census record
    -- is built from this pair, which stays fixed across ``/clear``. A caller
    not running under Claude Code, or one whose Claude process the kernel no
    longer reports, has no host to name.
    """
    harness, _session, _transcript = _coordinator_runtime()
    if harness != "claude-code":
        return None
    try:
        pid = int(str(os.environ.get("CLAUDE_PID") or ""))
    except ValueError:
        return None
    if pid <= 1:
        return None
    start = _process_start_time(pid)
    if not start:
        return None
    return pid, start


def _session_host_fifo_path(owner: tuple[int, str]) -> Path | None:
    """Name the request FIFO a session host owns from its ``(pid, start)`` pair.

    Both this module and the host entry point resolve one FIFO for a session's
    Claude process, so the composition lives here once -- the host imports it
    deferred, exactly as it imports the directory name and runtime-root order
    from here -- rather than two spellings that agree only until one changes.
    A missing runtime root yields no path, the same way an unresolvable owner
    does for each caller.
    """
    root = _session_host_runtime_root()
    if root is None:
        return None
    pid, start = owner
    return root / SESSION_HOST_DIRECTORY / f"{pid}-{start}.fifo"


def _session_host_fifo() -> Path | None:
    """Resolve the calling Claude session's host FIFO, or None when there is none."""
    owner = _session_host_owner()
    if owner is None:
        return None
    return _session_host_fifo_path(owner)


def _session_host_waiting() -> bool:
    """Whether the calling session's host is waiting on its FIFO to be asked.

    A dry run must report the delivery a real dispatch would reach without
    writing to the FIFO, because a dry run starts nothing and a write starts a
    follower. A host publishes that it is waiting by holding its FIFO's
    descriptor open across the wait, so the liveness read here is the same
    non-blocking open the real request uses, closed without a byte: an open
    succeeds only while a reader holds the other end, and a FIFO with no reader
    is a host that has gone. A real dispatch writes that reader and the host
    attaches; with no reader it falls through to the Monitor path, and the
    prediction does too. A host that is wedged while still holding its FIFO
    open reads the same as a live one here; a dry run cannot tell a wedged host
    from a live one without writing a request, which is the one thing it must
    not do.
    """
    fifo = _session_host_fifo()
    if fifo is None:
        return False
    try:
        # A deadline already in the past asks for a single attempt: a reader
        # not yet on the FIFO is a fallback, not a wait.
        descriptor = _open_request_fifo(fifo, time.monotonic())
    except CrewError:
        return False
    os.close(descriptor)
    return True


# The host writes its census of running children to a directory the host module
# owns, one record per session, named for the Claude process and its kernel
# start tick. Dispatch reads that record to tell a follower the host runs from
# one a coordinator armed by hand.
def _session_host_record_path() -> Path | None:
    """The calling Claude session's host census record, or None without a host.

    Both the directory and the filename suffix are the host module's own, so the
    name dispatch reads is the name the host wrote rather than a second spelling
    of it, and an override that moves the host's records moves this reader with
    them.
    """
    owner = _session_host_owner()
    if owner is None:
        return None
    from reckon.crew.session_host import RECORD_SUFFIX, _state_dir

    pid, start = owner
    return _state_dir() / f"{pid}-{start}{RECORD_SUFFIX}"


def _session_host_runs_follower(
    project: str, session: str, follower_pid: int | None
) -> bool:
    """Whether the calling session's host runs the follower attached for a pair.

    A session may already be attached when a dispatch arrives -- an earlier
    dispatch asked its host, or the plugin's monitor attached a follower at
    session start. Asking the host again would change nothing, and reporting
    ``monitor`` would hand the caller an arming line for a follower the host
    already consumes. The host's own census names every child it started, so a
    child for this project and session carrying the pid the live registration
    holds is the fact that the follower is the host's rather than one a
    coordinator armed by hand.
    """
    path = _session_host_record_path()
    if path is None:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    for child in payload.get("children") or ():
        if not isinstance(child, Mapping):
            continue
        if str(child.get("project")) != project:
            continue
        if str(child.get("session")) != session:
            continue
        if follower_pid is None:
            continue
        if child.get("pid") == follower_pid:
            return True
    return False


def _ask_session_host_for_follower(project: str, session: str | None) -> bool:
    """Ask this session's host to follow the project, returning whether it did.

    The request line is composed once, in ``_request_host_follower``, rather
    than spelled here too, so a caller that needs only the write -- the follower
    handing over -- shares the one place the line is written. The caller then
    waits, for at most the producer bound, until the session reads as attached
    -- the fact that matters, since the host attaching a follower is what makes
    the finished run reach the session. Without a host, or when it does not
    attach within the caller's bound, this reports False and the caller admits
    exactly as it does today.
    """
    if not _request_host_follower(project, session):
        return False
    deadline = time.monotonic() + WATCHER_LOAD_BOUND_SECONDS
    while time.monotonic() < deadline:
        if watch_state(project, session=session)["session_attached"]:
            return True
        time.sleep(0.1)
    return False


def _request_host_follower(project: str, session: str | None) -> bool:
    """Write one follow request to this session's host FIFO, returning success.

    The request is one JSON line naming the project and the session, written to
    the host's FIFO through the same non-blocking write the fleet spawn uses, so
    a FIFO with no reader means no host and falls back at once rather than
    stalling. This is the write alone; a caller that must see the host attach
    composes it with its own wait, and a caller that is itself the follower
    handing over only needs the request delivered before it exits.
    """
    if not session:
        return False
    fifo = _session_host_fifo()
    if fifo is None:
        return False
    line = json.dumps({"project": project, "session": session}).encode("utf-8") + b"\n"
    try:
        # A deadline already in the past asks ``_write_fleet_request`` for a
        # single attempt: no reader on the FIFO is a fallback, not a wait.
        _write_fleet_request(fifo, line, time.monotonic())
    except CrewError:
        return False
    return True


def _hand_off_to_waiting_host(project: str, session: str | None) -> bool:
    """Hand a hand-armed follower's session to a waiting host, if one is waiting.

    A session whose host is waiting owns its own delivery, so a follower armed
    by hand beside it only duplicates the pane and ends at its lifetime. Writing
    the pair to the host lets the host run its own long-lived follower once the
    hand-armed one is gone. Only a host already waiting on its FIFO is asked: a
    session with no host, or a host that has gone, keeps the Monitor path
    unchanged, so this never turns a working arming into a silent one. The
    request is written through ``_request_host_follower``, the same single
    spelling of the request line the waiting ask uses.
    """
    if not _session_host_waiting():
        return False
    return _request_host_follower(project, session)


def _released_follower_warning(dispatch_watch: Mapping[str, Any]) -> str:
    """Name the released registration and the command that re-arms it.

    The text carries the paste-ready attach command verbatim, because a warning
    that only says delivery stopped leaves the operator to reconstruct the one
    command that restores it.
    """
    attach = str(dispatch_watch.get("attach_line") or "").strip()
    return (
        "this session armed a follower whose registration was released, so this "
        "run's completion will not wake it; re-arm delivery with "
        f"`{attach}`"
    )


def _unmet_follower_conditions(
    project: str,
    dispatch_watch: Mapping[str, Any],
    *,
    session: str | None,
) -> list[str]:
    """Name every reason this session's delivery is not in place, at once.

    The conditions stand alone — no producer, no registration, a follower
    whose lines reach nothing — and a caller can only fix the one a refusal
    names, so a refusal naming one at a time costs a round trip per condition.
    Measured on one worker: three refusals, about twenty-five minutes. The
    list is reported whole, and one command clears every session-side entry on
    it.
    """
    from reckon.crew.runs import follower_state, list_followers

    conditions: list[str] = []
    if not dispatch_watch.get("watcher_live"):
        ensure = str(dispatch_watch.get("ensure_line") or "").strip()
        line = f"no live crew watcher process is reading project {project!r}"
        if ensure:
            line += (
                f"; one is started with `{ensure}`, which is safe to run "
                "against a watcher that is already up"
            )
        conditions.append(line)
    if not session:
        conditions.append(
            "no session was named, so no follower's pane is this run's destination"
        )
        return conditions
    if dispatch_watch.get("session_attached") or dispatch_watch.get(
        "session_follower_released"
    ):
        # An attached session hears the run, and a released one is admitted
        # with the re-arm warning rather than a refusal.
        return conditions
    state = follower_state(project, session)
    # A peer's follower is project-global and feeds the peer, never this
    # session, so naming the sessions that do deliver answers the question the
    # caller is left with when its own delivery is not in place.
    others = sorted(
        str(row.get("session") or "")
        for row in list_followers(project)
        if row.get("live") and str(row.get("session") or "") != session
    )
    peers = (
        "; sessions delivering for this project right now: "
        + ", ".join(repr(name) for name in others)
        if others
        else ""
    )
    if state.get("registered"):
        conditions.append(
            f"session {session!r} has a follower that is not delivering: "
            f"{state.get('not_live_because')}{peers}"
        )
    else:
        conditions.append(f"session {session!r} has no registered follower{peers}")
    return conditions


class _FollowerAdmissionUnmet(WatcherRequired):
    """A dispatch refused for every unmet follower condition in one result.

    The conditions are independent and fixing one reveals the next, so a
    refusal that names a single condition teaches the caller one at a time.
    This refusal names the whole list and the one follower command that clears
    it, and it keeps the error key and exit code :class:`WatcherRequired`
    already answers with so a caller reading the documented channel sees the
    same verdict either way.
    """

    def __init__(
        self,
        project: str,
        watch: Mapping[str, Any],
        *,
        session: str | None,
        conditions: list[str],
    ) -> None:
        self.project = project
        self.watch = dict(watch)
        self.session = session
        self.conditions = list(conditions)
        attach = str(watch.get("attach_line") or "reckon crew follow").strip()
        unmet = "\n".join(f"- {condition}" for condition in self.conditions)
        count = len(self.conditions)
        plural = "condition" if count == 1 else "conditions"
        CrewError.__init__(
            self,
            format_refusal(
                "D13",
                f"session {session!r} would not hear this run finish; "
                f"{count} follower {plural} unmet, named together so one "
                f"dispatch reaches the fix:\n{unmet}\n"
                f"Arm `{attach}` with the harness primitive that reports each "
                "line as it is written -- named for this host in reckon-build "
                "references/orchestrator-harness/<harness>.md -- then dispatch "
                "again. A copied-but-wrongly-armed line is the common case: the "
                "command is right and its lines still end where nothing reads "
                "them. Or pass --no-watch to waive delivery for a synchronous "
                "dispatch",
            ),
        )


def _watcher_delivery_admission(
    project: str,
    dispatch_watch: Mapping[str, Any],
    *,
    session: str,
    launch_kind: str,
    delivery: str = "monitor",
) -> str | None:
    """Decide whether a session's delivery admits the dispatch.

    Three cases, and the whole point is that they are told apart. An attached
    session needs nothing. A session whose registration was released — it armed
    a follower that has since expired — is admitted while the watcher process
    is live, and handed the re-arm warning: the project is still watched, and
    the run's own record keeps the delivery it was missing visible to a later
    reader. Everything else is refused, and every unmet condition is named in
    the one refusal rather than one per round trip.

    ``delivery`` names how the session's follower was sought — ``"host"`` when a
    session host attached it, ``"monitor"`` otherwise. It rides the refusal too,
    so a caller that fell back to the Monitor path is told so rather than left
    to infer it.

    Returns the warning line when a released session proceeds, and ``None``
    when the session is attached or the launch kind carries no delivery.
    Raises :class:`WatcherRequired` for a session that would not hear the run,
    whatever the launch kind, and for any launch kind with no live producer.
    """
    if launch_kind != "cli":
        # A launch that is not a session delivery — an in-harness node preparing
        # a directive — has no follower of its own to judge, so the conditions
        # below do not apply to it. The producer does apply: every launch kind
        # reads the project's watch seat, so its absence is refused here for
        # this kind too, as the call site refused it for all kinds.
        if dispatch_watch.get("watcher_live"):
            return None
        refusal = WatcherRequired(project, dispatch_watch)
        refusal.delivery = delivery
        raise refusal
    if dispatch_watch.get("session_follower_released") and dispatch_watch.get(
        "watcher_live"
    ):
        return _released_follower_warning(dispatch_watch)
    conditions = _unmet_follower_conditions(project, dispatch_watch, session=session)
    if conditions:
        refusal = _FollowerAdmissionUnmet(
            project, dispatch_watch, session=session, conditions=conditions
        )
        refusal.delivery = delivery
        raise refusal
    return None


def _open_request_fifo(fifo: Path, deadline: float) -> int:
    """Open a request FIFO for writing, retrying while no reader holds it.

    Opening a FIFO for writing blocks until a reader holds the other end, so
    the open is non-blocking: a FIFO not yet created, or a reader not yet
    there, is retried until the deadline rather than hung on, and neither
    arriving within it is a refusal. The
    caller owns the returned descriptor and closes it. This is the one spelling
    of the reconnect-or-fall-back open, shared by the batch step's request
    write and by the dry run's read of the session host's liveness. A deadline
    already in the past asks for a single attempt.
    """
    while True:
        try:
            return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ENXIO} and time.monotonic() < deadline:
                time.sleep(FLEET_REQUEST_POLL_SECONDS)
                continue
            raise CrewError(
                f"the fleet's request FIFO {fifo} could not be written: {exc}"
            ) from exc

from .dispatch_accounting import (  # noqa: E402
    _coordinator_runtime,
)

from .dispatch_launch import (  # noqa: E402
    FLEET_REQUEST_POLL_SECONDS,
    FLEET_SPAWN_ACK_NAME,
    _fleet_runtime_dir,
    _fleet_spawn_enabled,
    _read_fleet_record,
    _runs_inside_fleet_allocation,
    _spawn_through_fleet,
    _watch_request_slug,
    _write_fleet_request,
)
