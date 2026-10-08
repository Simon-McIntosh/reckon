import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import click

from reckon._timestamps import parse_utc
from reckon.crew_dispatch_commands import _crew_modules, _emit_crew_result, crew


def _follow_selects(
    event: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str] = (),
    run_ids: tuple[str, ...] = (),
) -> bool:
    """Decide whether one transition belongs to this follower's fleet.

    An event whose owner cannot be established — a line a pre-upgrade producer
    rendered, or a pointer carrying no session — reaches every follower. A
    reader that drops what it cannot attribute converts an unknown into a
    silence, which is the failure this whole surface exists to prevent.

    A follower narrows by the owning ``session`` and, when named, also admits
    the ``observed`` sessions. The two halves are not the same: the owning
    session is registered for delivery so a dispatch guard may trust it, while
    an observed session is admitted for oversight only — nothing is recorded
    for it, and a guard consulted for it still reports no delivery.

    Every transition that belongs to the fleet is delivered; the follower
    carries no state filter. A filter that reports how a run stopped and never
    how it recovered is worse than none, because it hides the all-clear the
    very same reader is waiting for, and a filter matching nothing is
    indistinguishable from a follower that never started.

    A line an older producer wrote names neither a session nor a run, so it is
    attributable to no fleet in particular. Only a fleet-wide reader — one with
    no session and no named runs — can claim it. A follower that has scoped
    itself must not: handing a scoped follower an unattributable row is how a
    session received another session's history as though it were its own, which
    is the leak the ``--session`` and ``--run`` filters exist to close.
    """
    if event.get("legacy"):
        return session is None and not run_ids
    owner = str(event.get("session") or "")
    if session is not None and owner and owner != session and owner not in observed:
        return False
    if run_ids and str(event.get("run_id") or "") not in run_ids:
        return False
    return True



def _follow_render_event(
    event: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str],
) -> Mapping[str, Any]:
    """Prepare one delivered row for the ticker, marking observed rows foreign.

    The ticker draws its owner column — and its foreign-owner glyph, for any
    row that carries a session — only when asked ``with_session``. An observing
    follower asks for it on every row so the grid stays aligned, which means
    the owning session's own rows must reach the ticker without their session:
    they are this reader's own, so the cell reads blank rather than foreign.
    Observed rows keep their session and so carry the glyph. Without observed
    sessions no row is rewritten and nothing about the scoped rendering
    changes.
    """
    if not observed or session is None:
        return event
    if str(event.get("session") or "") == session:
        return {**event, "session": ""}
    return event



def _sweep_lapsed_holds(project: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Resume this project's runs whose provider refusal has lapsed.

    Deferred like every other crew import in this file, and wrapped so a
    recovery that cannot run never takes the follower down with it: the pane a
    reader is watching matters more than any single sweep, and the next cadence
    tick tries again.
    """
    from reckon.crew.resumption import sweep

    return sweep(project, dry_run=dry_run)



_FOLLOWER_CHECKPOINT_ENV = "RECKON_FOLLOWER_CHECKPOINT"



_FOLLOWER_LIFETIME_ENV = "RECKON_FOLLOWER_LIFETIME_DEADLINE"



def _carried_lifetime_deadline() -> float | None:
    """Return the UTC epoch an arming ends at, when a reload handed one over.

    Absent for a fresh arming, which is the command's normal path: the host
    starts it in its own environment and no deadline is inherited. Present only
    in an image the follower re-executed into, so a reload continues the
    original arming rather than beginning a new one. The value is read and
    removed from ``os.environ`` in the same step, so no child this image goes on
    to start inherits a deadline that bounds only the follower.
    """
    raw = os.environ.pop(_FOLLOWER_LIFETIME_ENV, "")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None



def _take_follower_checkpoint(project: str) -> dict[str, Any]:
    """Consume the stream position handed across an in-place process reload."""
    raw = os.environ.pop(_FOLLOWER_CHECKPOINT_ENV, "")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("project") != project:
        return {}
    checkpoint = payload.get("checkpoint")
    return dict(checkpoint) if isinstance(checkpoint, dict) else {}



def _import_root(module) -> Path:
    """Return the sys.path entry that supplied an already-imported module."""
    package_name = module.__name__.partition(".")[0]
    package = sys.modules[package_name]
    module_path = Path(module.__file__).resolve()
    package_paths = [
        Path(location).resolve()
        for location in package.__path__
        if module_path.is_relative_to(Path(location).resolve())
    ]
    for entry in sys.path:
        root = Path(entry or os.getcwd()).resolve()
        if any((root / package_name).resolve() == path for path in package_paths):
            return root
    raise RuntimeError(f"cannot identify the import root for {module.__name__}")



_FOLLOWER_RELOAD_PROBE = (
    "import pathlib, sys\n"
    "root = sys.argv[1]\n"
    "sys.path.insert(0, root)\n"
    "package = pathlib.Path(root, 'reckon')\n"
    "for path in sorted(package.rglob('*.py')):\n"
    "    compile(path.read_bytes(), str(path), 'exec')\n"
    "import reckon.cli, reckon.crew.dispatch, reckon.crew.runs\n"
)



class _FollowerReloader:
    """Replace a stale follower only at a complete stream-record boundary."""

    def __init__(
        self,
        project: str,
        registration,
        *,
        stream=None,
        deadline: float | None = None,
        owner: tuple[int, str] | None = None,
        color: bool = False,
        label: str = "crew follow",
        seat: bool = False,
    ) -> None:
        from reckon.crew import runs

        self.project = project
        self.registration = registration
        self.stream = stream
        self.color = color
        # The command this reloader speaks for, so its deferral and failure
        # lines name the process a reader must cycle rather than always the
        # follower's.
        self.label = label
        # A producer holds the project's watch seat rather than a session
        # registration, so its replacement must be handed the seat descriptor
        # rather than the reader checkpoint.
        self.seat = seat
        # The instant this arming ends, handed to the replacement only through
        # the environment ``os.execve`` passes: it never enters this image's
        # ``os.environ``, so no child started here inherits it.
        self.deadline = deadline
        # The process that armed this follower, fixed before the reload. It is
        # carried to the replacement beside the deadline, so the replacement
        # keeps the owner the original arming had rather than adopting whatever
        # process replaced the image.
        self.owner = owner
        self.code_stamp = runs.follower_code_stamp()
        self.import_root = _import_root(runs)
        self.checked_at: float | None = None
        self.failed = False
        # The stamp whose replacement was proven unimportable and therefore not
        # performed. Kept so the check is not repeated and the deferred line is
        # not reprinted on every tick; a reload retries only once the code
        # changes again.
        self.deferred_stamp: str | None = None

    def _replacement_imports(self) -> tuple[bool, str]:
        """Prove the replacement image loads before re-executing into it.

        Run in a throwaway interpreter against the same import root the exec
        leads with, because this process is the wrong place to discover that the
        new code does not parse: a failed import here would take the live pane
        down with it. Returns whether the image is safe and, when it is not, one
        line naming why.
        """
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _FOLLOWER_RELOAD_PROBE, str(self.import_root)],
                capture_output=True,
                text=True,
                timeout=_FOLLOWER_RELOAD_PROBE_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if completed.returncode == 0:
            return True, ""
        lines = (completed.stderr or completed.stdout or "").strip().splitlines()
        return False, (lines[-1] if lines else f"exit {completed.returncode}")

    def poll(self, checkpoint: Mapping[str, Any]) -> None:
        """Check at a bounded cadence and re-exec with the reader checkpoint."""
        from reckon.crew import runs
        from reckon.crew.dispatch import _export_launched_workers_for_reexec

        if self.failed:
            return
        moment = time.monotonic()
        if (
            self.checked_at is not None
            and moment - self.checked_at < runs.FOLLOWER_FRESHNESS_SECONDS
        ):
            return
        self.checked_at = moment
        current_stamp = runs.follower_code_stamp()
        if current_stamp == self.code_stamp:
            return
        if current_stamp == self.deferred_stamp:
            return
        reloadable, reason = self._replacement_imports()
        if not reloadable:
            self.deferred_stamp = current_stamp
            line = (
                f"reckon {self.label} deferred its reload: the new image does not "
                f"import ({reason}); keeping the current image, retrying when the "
                "code changes"
            )
            if self.color:
                line = _dim_history_line(line)
            _echo_follow_line(line, stream=self.stream)
            return

        # The launched-worker registry does not survive an image replacement,
        # though the process (and therefore the parent-child relationships)
        # does. Hand the outstanding pids over beside the checkpoint so the
        # replacement image continues to own the workers it launched.
        _export_launched_workers_for_reexec()
        if self.registration is not None:
            self.registration.prepare_reexec()
        # A shell commonly supplies only a command name in argv[0], so an
        # absolute console-script guess cannot identify the code now running.
        # Lead with the sys.path root that supplied the imported package.
        launcher = (
            "import sys; "
            f"sys.path.insert(0, {str(self.import_root)!r}); "
            "from reckon.cli import main; main()"
        )
        # The carried deadline travels in the environment this exec passes,
        # not in this image's ``os.environ``: a child the follower starts
        # inherits ``os.environ`` and would otherwise carry a deadline that
        # bounds nothing it runs. Building the mapping here keeps the arming's
        # identity separate from every process but the replacement itself.
        exec_environment = dict(os.environ)
        if not self.seat:
            # The reader's place reaches the replacement image through the
            # mapping this exec passes and never through ``os.environ``: every
            # child this image starts inherits the process environment, and a
            # checkpoint describes one reader's place in one stream rather than
            # anything a child could use.
            exec_environment[_FOLLOWER_CHECKPOINT_ENV] = json.dumps(
                {"project": self.project, "checkpoint": dict(checkpoint)}
            )
        if self.deadline is not None:
            exec_environment[_FOLLOWER_LIFETIME_ENV] = repr(self.deadline)
        if self.owner is not None:
            exec_environment[runs._FOLLOWER_OWNER_ENV] = runs._format_follower_owner(
                self.owner
            )
        if self.seat:
            # The producer's seat is an open advisory lock, which the image
            # replacement would otherwise close and release; naming the held
            # descriptor in the replacement's environment keeps the seat held
            # across the exec rather than re-entered against a race.
            seat_fd = runs.prepare_watch_seat_reexec(self.project)
            if seat_fd is not None:
                exec_environment[runs._WATCH_SEAT_ENV] = str(seat_fd)
                _echo_follow_line(
                    "reckon crew watch started its reload; awaiting replacement",
                    stream=self.stream,
                )
        # Ignoring the alarm before disarming drops one that became pending while
        # the import proof ran. The ignored disposition crosses exec until the
        # replacement installs its handler; a caught disposition would reset to
        # the fatal default there.
        previous_alarm = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        signal.signal(signal.SIGALRM, signal.SIG_IGN)
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            os.execve(  # noqa: S606 - replacement preserves descriptors and stdout
                sys.executable,
                [
                    sys.executable,
                    "-c",
                    launcher,
                    *sys.argv[1:],
                ],
                exec_environment,
            )
        except OSError as exc:
            signal.signal(signal.SIGALRM, previous_alarm)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            if self.seat:
                runs.cancel_watch_seat_reexec(self.project)
            if self.registration is not None:
                self.registration.cancel_reexec()
            self.failed = True
            command = shlex.join(["reckon", *sys.argv[1:]])
            _echo_follow_line(
                f"reckon {self.label} is stale and could not reload itself "
                f"({exc}); cycle it with: {command}",
                stream=self.stream,
            )



class _StampPoll:
    """Poll the producer's code stamp on the follower's cadence.

    A follower reaches its reload check through the per-wait hook its own
    stream loop runs. The producer does not own that loop — ``watch_follow``
    sleeps inside a generator it does not hand ticks to — so it needs its own
    cadence, and a signal timer is the one that keeps the check in the main
    thread: an image replacement is well defined only from the thread that
    performs it, and ``os.execve`` from a helper thread would race the main
    thread's own stream writes.
    """

    def __init__(self, reloader: _FollowerReloader, interval: float) -> None:
        self.reloader = reloader
        self.interval = interval
        self.previous = None

    def _tick(self, signum, frame) -> None:
        # The import proof may outlast the interval. Disarm before entering it so
        # the signal handler cannot recursively start another proof forever.
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            self.reloader.poll({})
        finally:
            if not self.reloader.failed:
                signal.setitimer(signal.ITIMER_REAL, self.interval, self.interval)

    def start(self) -> None:
        self.previous = signal.signal(signal.SIGALRM, self._tick)
        signal.setitimer(signal.ITIMER_REAL, self.interval, self.interval)

    def stop(self) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if self.previous is not None:
            signal.signal(signal.SIGALRM, self.previous)
            self.previous = None



FOLLOWER_END_EVENT = "follower-end"



FOLLOWER_RESUME_EVENT = "follower-resume"



FOLLOWER_FORMAT_EVENT = "follower-format-changed"



FOLLOWER_STALE_PRODUCER_EVENT = "stale-producer"



FOLLOWER_REATTACH_EVENT = "follower-reattached"



def _snapshot_module() -> Any:
    """The snapshot reader, loaded by file path when nothing has imported it."""
    loaded = sys.modules.get("reckon.crew.obligation_snapshot")
    if loaded is not None:
        return loaded
    from importlib.util import module_from_spec, spec_from_file_location

    path = Path(__file__).resolve().parent / "crew" / "obligation_snapshot.py"
    specification = spec_from_file_location("reckon.crew.obligation_snapshot", path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load the snapshot reader from {path}")
    module = module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module



_SNAPSHOT_READER = _snapshot_module()



_FOLLOWER_RELOAD_PROBE_TIMEOUT = _SNAPSHOT_READER.FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS



PRODUCER_POLL_INTERVAL_CAP_SECONDS = _SNAPSHOT_READER.PRODUCER_POLL_INTERVAL_CAP_SECONDS



PRODUCER_RELOAD_WINDOW_SECONDS = _SNAPSHOT_READER.PRODUCER_RELOAD_WINDOW_SECONDS



FOLLOWER_PRODUCER_RELOADING_EVENT = "producer-reloading"



FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT = "producer-reload-failed"



FOLLOWER_PRODUCER_STOPPED_EVENT = "producer-stopped"



def _logged_producer_stop(project: str) -> str | None:
    """Read the producer's final reason without loading its long service log."""
    from reckon.crew import runs

    try:
        with runs.watch_log_path(project).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.strip():
            marker = "reckon crew watch stopped: "
            candidate = line.strip()
            if candidate.startswith("[") and "] " in candidate:
                candidate = candidate.split("] ", 1)[1]
            return candidate if candidate.startswith(marker) else None
    return None



def _ticker_layout_signature() -> str:
    """Fingerprint the ticker's fixed grid geometry, not the code that emits rows.

    A follower reloads onto new code many times an hour on a busy branch, and
    the format marker exists to tell a reader that the rows below it were drawn
    differently from the rows above. Two images that lay a row out in the same
    columns drew it identically, so there is nothing to mark: the marker is
    owed only when the replacement image's grid differs from the one on screen.
    The comparison keys on what the grid fixes -- the ordered widths of the
    fixed cells, the gutters between them, and the minimum width they compose --
    which is what moves when a column is added, resized or reordered, and is
    unchanged when only the rendering of a cell's contents changes.
    """
    from reckon.crew import ticker as ticker_module

    layout = (
        ticker_module.CLOCK,
        ticker_module.MODEL,
        ticker_module.EFFORT,
        ticker_module.ROLE,
        ticker_module.NODE,
        ticker_module.STATE,
        ticker_module.SPEND,
        ticker_module.STATS,
        ticker_module.GAP,
        ticker_module.SPEND_GAP,
        ticker_module.MIN_WIDTH,
    )
    return hashlib.sha256(repr(layout).encode()).hexdigest()[:16]



def _needs_you_runs(project: str, *, session: str | None) -> list[dict[str, str]]:
    """List the owning session's runs whose state needs the coordinator now.

    Read at the moment the follower ends rather than accumulated from the
    stream. The line's job is to name what is outstanding as the pane goes
    dark, and a run falls into a needs-action state whether or not a producer
    ever wrote a transition for it — a run that finished while no producer was
    up would otherwise be named by nobody.
    """
    from reckon.crew import recovery as recovery_module
    from reckon.crew import runs as runs_module

    stall_seconds = recovery_module.parse_duration(
        recovery_module.DEFAULT_WATCH_STALL_WINDOW
    )
    moment = recovery_module._utc_seconds()
    blocked_states = recovery_module.FLEET_BLOCKED_STATES
    unpromoted_states = recovery_module.FLEET_UNPROMOTED_STATES
    wanted = frozenset((*blocked_states, *unpromoted_states))
    rows: list[dict[str, str]] = []
    for pointer in runs_module._list_live_records(project=project):
        owner = str(pointer.get("session") or "")
        # A pointer with no recorded owner stays in the counted set, matching
        # the dispatch fence: absence cannot prove it belongs to a peer.
        if session is not None and owner and owner != session:
            continue
        snapshot = recovery_module._watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds
        )
        state = str(snapshot.get("state") or "")
        if state not in wanted:
            continue
        rows.append(
            {
                "run_id": str(snapshot.get("run_id") or ""),
                "node": str(snapshot.get("node") or ""),
                "state": state,
            }
        )
    return rows



def _population_has_live_work(project: str, *, session: str | None) -> bool:
    """Whether this follower's own fleet still holds a run that has not ended.

    The seat a follower reads is a shared producer that exits when nothing
    renews its ten-minute lease, so its going is the designed idle exit rather
    than an outage whenever the follower's population — its session's runs, or
    the project's when unscoped — holds nothing left to watch. Only a run that
    has not reached a terminal phase is work still owed the pane at that
    moment; a pointer already terminal waits on the coordinator's promotion
    rather than on a producer, and the dispatch guard names an absent producer
    with its remedy to the session that is trying to dispatch.
    """
    from reckon.crew import node as node_module
    from reckon.crew import runs as runs_module

    for pointer in runs_module._list_live_records(project=project):
        owner = str(pointer.get("session") or "")
        # A pointer with no recorded owner stays in the counted set, matching
        # the dispatch fence: absence cannot prove it belongs to a peer.
        if session is not None and owner and owner != session:
            continue
        if str(pointer.get("phase") or "") in node_module._TERMINAL_RUN_PHASES:
            continue
        return True
    return False



def _follower_end_line(
    *,
    attach_line: str,
    needs: list[Mapping[str, str]],
) -> str:
    """Compose the single line a reader acts on as the pane goes dark."""
    if not needs:
        tail = "no runs need you"
    else:
        outstanding = ", ".join(f"{row['run_id']} ({row['node']})" for row in needs)
        if len(needs) == 1:
            tail = f"1 run needs you: {outstanding}"
        else:
            tail = f"{len(needs)} runs need you: {outstanding}"
    return f"follower end: arming lifetime elapsed; re-arm with: {attach_line}; {tail}"



def _follower_end_event(
    project: str,
    *,
    session: str | None,
    armed_seconds: float | None,
    elapsed: float,
) -> dict[str, Any]:
    """Build the follower's own final transition, carrying the line to print."""
    from reckon.crew import runs as runs_module

    attach_line = runs_module._watch_attach_line(project, session=session)
    needs = _needs_you_runs(project, session=session)
    return {
        "event": FOLLOWER_END_EVENT,
        "project": project,
        "session": session or "",
        "run_id": None,
        "armed_seconds": armed_seconds,
        "elapsed_seconds": round(float(elapsed), 3),
        "attach_line": attach_line,
        "needs_you": needs,
        "line": _follower_end_line(attach_line=attach_line, needs=needs),
    }



def _follow_resume_plan(
    project: str,
    session: str | None,
    *,
    stream_path: Path,
    resume_state: Mapping[str, Any],
) -> tuple[str, int, dict[str, str]]:
    """Decide how a follower starts against the stream it is about to read.

    Three modes, one per kind of place an arming can begin from:

    ``baseline`` — a first attachment, which emits the fleet report as it is
    derived. ``continue`` — the stream has only advanced since the recorded
    place, so the recorded offset is the boundary to seek to and nothing before
    it is replayed. ``restart`` — the stream was replaced or truncated, so its
    recorded offset no longer names a boundary: the file is re-read from its
    start and filtered to the runs whose state differs from the checkpoint's.

    The recorded state travels back with the mode either way, so a continuation
    delivers only what changed rather than re-announcing the fleet.
    """
    from reckon.crew import follow_checkpoint

    if resume_state:
        # A checkpoint handed across an in-place reload: the same image is
        # continuing, so the stream path it recorded still governs.
        offset = resume_state.get("offset")
        recorded = {
            str(run_id): str(state)
            for run_id, state in dict(resume_state.get("reported") or {}).items()
        }
        if (
            isinstance(offset, int)
            and not isinstance(offset, bool)
            and resume_state.get("stream_path") == str(stream_path)
        ):
            return "continue", max(0, offset), recorded
        return "baseline", 0, recorded

    if session is None:
        # A session-less follower observes the whole fleet in one pass and has
        # no pane to restore, so it has no durable place. Reading one would find
        # a place a concurrent session-less follower wrote, resume mid-stream,
        # and deliver a resume event and rows the pane already showed.
        return "baseline", 0, {}

    record = follow_checkpoint.read(project, session)
    if not record:
        return "baseline", 0, {}
    recorded = {
        str(run_id): str(state)
        for run_id, state in dict(record.get("reported") or {}).items()
    }
    if follow_checkpoint.continues(record, stream_path):
        return "continue", max(0, int(record["offset"])), recorded
    return "restart", 0, recorded



def follower_row_path(
    *,
    session: str | None = None,
    observed: Iterable[str] = (),
    run_ids: Iterable[str] = (),
    reported: Mapping[str, str] | None = None,
):
    """The row path a follower runs on, for a reader that has to measure one.

    A replay that drove the policy directly would count rows the follower never
    offered it. This builds the very object the follower feeds — the same
    selection function, the same policy, the same memory — so a measurement
    taken through it is the pane's own answer rather than a second opinion.
    """
    from reckon.crew import ticker as ticker_module

    observed_sessions = frozenset(observed)
    selected_runs = tuple(run_ids)
    return ticker_module.PaneRowPath(
        selects=lambda event: _follow_selects(
            event, session=session, observed=observed_sessions, run_ids=selected_runs
        ),
        reported=reported,
    )



def _follow_boundary(stream_path: Path) -> int:
    """The byte after the last complete record when the boundary is taken.

    The producer appends one JSON record per line, so a line boundary is a
    record boundary. The size captured from the file can fall inside a record
    the producer is still writing, and a boundary taken there would leave the
    replay reading a half-written line it cannot parse and the follow loop
    opening in the middle of that record, so the record would be delivered in
    halves or not at all. So the guard snaps the captured stream offset back to
    the byte after the last newline at or before it: the boundary always lands
    on a newline, the replay reads only whole records, and the loop opens at the
    start of any record still being written, which it then reads whole and once.
    """
    from reckon.crew import runs

    return runs.line_boundary(stream_path)



def _stream_events_upto(stream_path: Path, *, offset: int, boundary: int) -> list[dict]:
    """Read a stream's events from one byte offset up to a fixed boundary.

    The boundary is captured before the read rather than taken from the file
    afterwards, so a line appended while the read is in progress belongs to the
    read that follows rather than to this one. That is what makes a transition
    written between the replay and the read loop arrive exactly once: the replay
    stops at the boundary, and the loop opens at the same boundary.
    """
    from reckon.crew import runs as runs_module

    events: list[dict] = []
    if boundary <= offset:
        return events
    try:
        stream = Path(stream_path).open(encoding="utf-8")
    except OSError:
        return events
    with stream:
        stream.seek(offset)
        while stream.tell() < boundary:
            line = stream.readline()
            if not line:
                break
            if stream.tell() > boundary:
                # A line that runs past the boundary is not this read's to
                # deliver: the reader that follows opens at the boundary and
                # reads it whole, so it arrives once rather than in halves.
                break
            event = runs_module.parse_stream_line(line)
            if event is not None:
                events.append(event)
    return events



def _recorded_fleet_times(
    stream_path: Path, *, boundary: int
) -> dict[str, tuple[str, str]]:
    """The state each run is in, and the time it entered that state.

    A row that re-announces a run should carry the clock the run's own record
    gave it rather than the moment the pane happened to attach, so the time is
    read from the stream the producer wrote. A run's recorded time is the first
    event in the unbroken trailing run of events whose ``to_state`` equals that
    state, read as the earliest stamp that the run carries for the state: a
    re-emitted baseline names a state the run already sits in and is stamped
    when it is emitted, so it extends the run without moving the time, while a
    genuine change of state opens a new run and stamps it at the change. The
    read stops at the boundary the replay was given so the answer describes the
    same span the rows do.
    """
    latest: dict[str, tuple[str, str]] = {}
    for event in _stream_events_upto(stream_path, offset=0, boundary=boundary):
        if event.get("legacy"):
            continue
        run_id = str(event.get("run_id") or "")
        to_state = event.get("to_state")
        if not run_id or not to_state:
            continue
        state = str(to_state)
        stamp = str(event.get("observed_at") or "")
        recorded = latest.get(run_id)
        if recorded is not None and recorded[1] == state:
            # The same state again: this event continues the trailing run, so
            # the run's recorded time stays the earlier stamp it already holds.
            held = parse_utc(recorded[0])
            moment = parse_utc(stamp)
            if moment is not None and (held is None or moment < held):
                latest[run_id] = (stamp, state)
            continue
        latest[run_id] = (stamp, state)
    return latest



def _fleet_replay(
    path,
    *,
    baseline,
    stream_path: Path,
    clock,
    reannounce: bool,
    resume_from: int,
    boundary: int,
) -> list[dict]:
    """One row per live run, under its own recorded clock and in time order.

    The rows come from two places and neither may deliver a run twice. A run
    whose record moved between the place this arming resumes from and the
    boundary the replay read to is news: the transitions in that span are handed
    to the row path in recorded order, so the policy that holds a noise pair,
    resolves one silently, or withholds a row until its window closes applies to
    a gap exactly as it does to a live read. A run with no news of its own in
    that span is re-announced instead — one row carrying the state the live
    fleet gives it, stamped with the time its own record entered that state.

    The recorded time is taken only where the stream's last word for the run
    agrees with the live classification. Where they disagree the live fleet is
    the authority, because it is the current pointer while the stream is
    history, so the row keeps the live state and the fleet's own clock rather
    than a state the live classification contradicts: a row never announces a
    state the fleet denies.

    The rows are ordered by their own stamp, so a re-arm reads as a timeline and
    an old transition leads a run that has sat at its state since.

    Only an arming that is re-announcing the fleet marks its rows
    ``reannounced``: the stored history is the pane's record of what it was
    handed, so a first arming that draws the fleet records it, while a re-arm
    that draws the same fleet leaves the history it restores alone rather than
    doubling it. A gap transition is news and is recorded.
    """
    recorded = _recorded_fleet_times(stream_path, boundary=boundary)
    rows: list[dict] = []
    announced: set[str] = set()
    for event in _stream_events_upto(
        stream_path, offset=resume_from, boundary=boundary
    ):
        run_id = str(event.get("run_id") or "")
        if run_id:
            announced.add(run_id)
        rows.append(dict(event))
    for event in baseline:
        row = dict(event)
        run_id = str(row.get("run_id") or "")
        if run_id and run_id in announced:
            continue
        latest = recorded.get(run_id)
        if latest is not None:
            stamp, state = latest
            if stamp and state == str(row.get("to_state") or ""):
                row["observed_at"] = stamp
        if reannounce:
            row["reannounced"] = True
        rows.append(row)
    rows.sort(key=_follow_row_stamp)
    printed: list[dict] = []
    for row in rows:
        printed.extend(path.feed(row, now=clock()))
    return printed



_FOLLOW_REATTACH_FRAME = (
    "── re-attached · {live} live runs · {changed} changed since {when} ──"
)



def _follow_reattach_line(live: int, changed: int, recorded_at: str) -> str:
    """Compose the framing line of a re-attach, stamped with the record's time."""
    from reckon.crew import ticker as ticker_module

    return _FOLLOW_REATTACH_FRAME.format(
        live=live, changed=changed, when=ticker_module.local_clock(recorded_at)
    )



def _follow_gap_rows(
    baseline: Iterable[Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    session: str | None,
    observed: Iterable[str],
    run_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    """The rows a re-attach replays: one per owned run whose state moved.

    The record holds the state each of the session's runs was last shown at, so
    a run still at the state the record names carries no news and draws nothing:
    the gap between two armings is replayed rather than a whole fleet
    re-announced. A run the record does not name was dispatched while nothing
    was attached, so it is replayed from ``dispatched``, the state every launch
    passes through. Each row keeps the remembered state as its left half, so it
    reads as the ordinary transition the run made while the pane was away, and
    takes its right half and its clock from the live classification rather than
    from any stored phase.
    """
    states = {
        str(run_id): str(state)
        for run_id, state in dict(record.get("states") or {}).items()
    }
    rows: list[dict[str, Any]] = []
    for row in baseline:
        if not _follow_selects(
            row, session=session, observed=observed, run_ids=run_ids
        ):
            continue
        run_id = str(row.get("run_id") or "")
        event = dict(row)
        current = str(event.get("to_state") or "")
        recorded = states.get(run_id)
        if recorded == current:
            continue
        event["from_state"] = recorded or "dispatched"
        rows.append(event)
    return rows



def _follow_watch_lines(
    project: str,
    *,
    session: str | None = None,
    observed: Iterable[str] = (),
    run_ids: Iterable[str] = (),
    poll_interval: float = 0.1,
    sleeper=time.sleep,
    stop=None,
    on_poll=None,
    sweep=_sweep_lapsed_holds,
    sweep_interval: float | None = None,
    clock=time.monotonic,
    resume: Mapping[str, Any] | None = None,
    lifetime: float | None = None,
    lifetime_deadline: float | None = None,
    registration=None,
    producer_reload_window: float | None = None,
    reloaded_in_place: bool = False,
):
    """Yield this follower's transitions for as long as its session lives.

    Two properties matter more than anything else here, and both were learned
    from measured silence:

    A follower outlives the producer it reads. The seat is released when a wave
    drains, so a follower that ended there covered exactly one wave — the next
    wave armed a fresh producer while the session's monitor had already exited.
    Waiting for the next producer instead makes one arming cover the session.

    A follower replays the fleet, and a re-arm's gap is news. Every arming of a
    fresh image — a first attach and a re-arm alike — draws one row for each
    live run, stamped with the recorded time that run entered its current state
    and ordered by it. A reader that re-arms therefore sees the whole fleet with
    its own clocks rather than a blank pane, and never a burst of rows sharing
    the moment the arming attached.

    A re-arm did have a place in the stream, and what the stream gained between
    that place and the arming's own start is delivered as news ahead of the
    fleet: those transitions go through the row path in recorded order, so the
    same policy that governs a live read governs them — a noise pair still
    prints nothing, a held row is still held — and a run that moved while the
    pane was away appears once, in its new state, at its recorded transition
    time, rather than being drawn twice. A first arming, having no place in the
    stream, derives the fleet as it stands. Only an in-place reload, which
    replaces the process image with the grid still on screen, continues from its
    recorded offset and delivers just what moved.

    The byte the replay reads to is captured once, before it reads, and the read
    loop then opens at exactly that byte, so a transition appended in between
    falls to one side or the other and is delivered once.

    ``observed`` names sessions delivered for oversight alongside the owning
    ``session``. They carry no registration: the attachment and its dispatch
    guard stay the owning session's alone, so observing never vouches for
    delivery of the observed session's runs.

    ``lifetime`` bounds the arming itself. The host ends a Monitor at thirty
    minutes and announces it only in its own words, so a follower armed for
    slightly less reaches its own deadline first: it prints one line a reader
    can act on and releases its registration, rather than leaving the pane to
    stop silently. That line is the one deliberate exception to this stream's
    fleet-only vocabulary, because the end of the stream is the one fact about
    the follower a reader must act on.
    """
    from reckon.crew import follow_checkpoint, runs
    from reckon.flight import FlightConfigError

    selected_runs = tuple(run_ids)
    observed_sessions = frozenset(observed)
    resume_state = dict(resume or {})
    # The follower's own row path: the filter that decides which events are
    # this follower's, the policy that holds a row opening a noise pair, and
    # the memory of the states the pane was actually shown. It is the object
    # the checkpoint stores and a re-arm restores, so a row it withholds is not
    # in that memory and a re-arm re-evaluates it instead of believing it was
    # delivered.
    path = follower_row_path(
        session=session,
        observed=observed_sessions,
        run_ids=selected_runs,
        reported=resume_state.get("reported"),
    )

    started_at = clock()
    if lifetime_deadline is None and lifetime is not None:
        # A caller that hands only a duration is granted it from this arming's
        # own start; a caller that hands the absolute instant in keeps the
        # deadline the arming was granted, which a replacement image must not
        # re-anchor.
        lifetime_deadline = started_at + max(0.0, float(lifetime))
    lifetime_elapsed = False
    consumer_gone = False

    def _check_consumer() -> None:
        """End this arming when the process it reports to has gone.

        The follower delivers to the session that armed it, and that owner is
        fixed once, at the follower's first start, as a pid plus the kernel
        start time that tells it apart from an unrelated process reusing the
        pid. It is read from the owner identity rather than from
        ``os.getppid()``, because once the arming session dies the parent names
        init or a subreaper; and rather than from the registration record,
        because a second follower that takes the registration over rewrites
        that record from its own parent, so a record read back here could name
        the wrong process. The owner having gone means the lines reach nobody,
        so holding the registration only denies the session its next follower:
        the orphan keeps the advisory lock, and every dispatch from the session
        is refused ``watcher-required`` until someone kills it by hand.

        This runs on every wait pass, whether or not the follower holds the
        registration: a read-only follower holds no lock and still outlives its
        owner, and it is the read-only ones that accumulate, one per re-arm. An
        owner of pid 1 or below has already been re-parented to init or a
        subreaper, so it is gone rather than a reason to skip the check. Nothing
        is printed on this path: it is the one path whose whole point is that no
        reader is left.
        """
        nonlocal consumer_gone
        if consumer_gone:
            return
        owner_pid, owner_start = runs.follower_owner()
        if owner_pid <= 1 or runs.process_alive(owner_pid) is not True:
            consumer_gone = True
            return
        if owner_start and runs._process_start_time(owner_pid) != owner_start:
            consumer_gone = True

    def _check_lifetime() -> None:
        """Mark this arming ended when its own deadline has passed.

        The deadline is the follower's own, so it is read on the wait pass
        rather than on any stream event: a follower with no producer up must
        still reach its deadline, which is exactly the case where a reader
        would otherwise see nothing at all and assume the pane is quiet.
        """
        nonlocal lifetime_elapsed
        if lifetime_deadline is not None and clock() >= lifetime_deadline:
            lifetime_elapsed = True

    def _stopped() -> bool:
        return stop is not None and stop.is_set()

    # The place this arming last wrote, so a poll that changes nothing does not
    # rewrite it. Declared beside the writer that owns it.
    written_place: tuple[Any, ...] | None = None

    def _record_checkpoint(
        stream_path: Path,
        offset: int,
        *,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist this follower's place, so its next arming continues here.

        Written as lines are delivered, so the place advances with the stream
        rather than with the arming's end: an arming that dies without reaching
        its own teardown has still left behind everything it delivered. A
        checkpoint that cannot be written costs a later re-arm its place and
        must never cost this arming its stream, so it is not allowed to raise.

        ``identity`` is the open stream's own identity when the offset was read
        from that handle, so a replacement landing between the read and this
        write cannot pair the old offset with the new file's inode.

        A write is performed only when the place has moved — the stream, the
        offset, the stream's identity or the reported map — or when the file
        this place names is gone. The per-tick hook runs on every wait pass, so
        an arming sitting against a quiet stream would otherwise rewrite the
        same record once a pass, two fsyncs and a rename each, for as long as it
        sat there. The existence half is what makes a checkpoint survive a
        deletion: a file removed while the pane sat idle is rewritten on the
        next poll rather than staying absent until the place moves, so a reload
        in that window still finds the pane's memory.
        """
        from reckon.crew import follow_checkpoint

        nonlocal written_place
        if session is None:
            # A session-less follower has no pane to restore, so it keeps no
            # durable place: writing one would hand a concurrent session-less
            # follower a spot to resume from, which is a place it never had.
            return
        try:
            resolved_identity: Mapping[str, Any] | None = (
                identity
                if identity is not None
                else follow_checkpoint.stream_identity(stream_path)
            )
        except OSError:
            resolved_identity = None
        place = (
            str(stream_path),
            int(offset),
            None
            if resolved_identity is None
            else (resolved_identity.get("dev"), resolved_identity.get("ino")),
            tuple(
                sorted((str(key), str(value)) for key, value in path.reported.items())
            ),
        )
        if place == written_place and follow_checkpoint.exists(project, session):
            return
        try:
            follow_checkpoint.write(
                project,
                session,
                stream_path=stream_path,
                offset=offset,
                reported=path.reported,
                identity=identity,
            )
        except OSError:
            return
        written_place = place

    def _renew_producer_lease() -> None:
        """Push this live follower's project producer's lease forward.

        A producer ends one lease interval after the last renewal, so a live
        follower must renew repeatedly for as long as it lives — a single
        renewal at attach would let the producer die under a follower that is
        still reading it. The writer throttles itself to half the interval and
        refuses when no producer is live, so this pass never resurrects the
        registration of a producer that has already exited into the lease. A
        failure to write must never end the pane: the renewal is best-effort and
        a missed one merely ages the lease.
        """
        try:
            runs.renew_producer_lease(project)
        except OSError:
            return

    def _tick(
        *,
        stream_path: Path | None = None,
        offset: int = 0,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        """Run the caller's per-wait work — reclaiming a registration, say."""
        if on_poll is not None:
            on_poll(
                {
                    "reported": dict(path.reported),
                    "stream_path": str(stream_path) if stream_path else "",
                    "offset": offset,
                    # The grid this image draws with, carried to the replacement
                    # so a reload that leaves every column where it was does not
                    # claim the drawing style changed.
                    "layout": _ticker_layout_signature(),
                }
            )
        if stream_path is not None:
            _record_checkpoint(stream_path, offset, identity=identity)
        _check_lifetime()
        _check_consumer()
        _renew_producer_lease()

    from reckon.crew.resumption import DEFAULT_SWEEP_SECONDS

    cadence = DEFAULT_SWEEP_SECONDS if sweep_interval is None else float(sweep_interval)
    swept_at: float | None = None

    def _sweep_quietly() -> None:
        try:
            sweep(project)
        except Exception:  # noqa: BLE001 - a failed recovery must not end the pane
            return

    def _sweep_on_cadence() -> None:
        """Run the recovery sweep at most once per cadence.

        The follower polls in a tight loop, so the cadence is what stops a
        cheap sweep from becoming a hot path. It is time since the last sweep
        rather than a count of iterations, because the loop's own rate depends
        on whether a producer is up.

        An arming's deadline bounds the poll itself, not only the gap between
        two of them. A sweep walks every registered worktree and can run for
        hours, and while it ran the check that ends an arming waited for it to
        return — a 29 m arming held open for 3 h 36 m. A sweep on its own
        thread is waited for only as long as the arming has left; one still
        walking at the deadline is abandoned and the follower ends.
        """
        nonlocal swept_at, lifetime_elapsed
        if sweep is None:
            return
        moment = clock()
        if swept_at is not None and moment - swept_at < cadence:
            return
        swept_at = moment
        if lifetime_deadline is None:
            _sweep_quietly()
            return
        remaining = lifetime_deadline - clock()
        if remaining <= 0:
            # A poll begun now would outlive the arming before it did any
            # work: the arming is over and the passes that follow take the
            # end path.
            lifetime_elapsed = True
            return
        poll = threading.Thread(target=_sweep_quietly, daemon=True)
        poll.start()
        poll.join(remaining)
        if poll.is_alive():
            lifetime_elapsed = True

    # A resume handed in the environment is an image replacing itself, which
    # already has the pane's rows on screen and needs only the format switch
    # marked. A resume read from the durable checkpoint is a re-arm, whose pane
    # is empty and must be given the stored history before anything else.
    reloading = bool(resume_state)
    # Only the first pass through the loop is an attachment. A later pass is the
    # stream still not existing, and the pane has already been told what it
    # needed to know; re-announcing it would replay the history on every poll.
    first_attach = True

    # A producer whose recorded stamp differs from this follower's is either
    # mid-reload or a seat genuinely left behind, and the record does not say
    # which. An in-place reload proves the code moved moments ago, so a mismatch
    # first seen on that path is granted the producer's reload window before it
    # is reported as staleness; a mismatch that outlasts the window is reported
    # with the cycle advice. A fresh arming has no such proof, so it is reported
    # at once. The window is only consulted while a decision is pending, so the
    # stamp comparison is not paid on every idle poll.
    reload_window = (
        PRODUCER_RELOAD_WINDOW_SECONDS
        if producer_reload_window is None
        else float(producer_reload_window)
    )
    producer_stale_since: float | None = None
    producer_reload_deferred = False
    producer_stale_advised = False
    producer_reload_failure_reported = False
    producer_stop_reported = False

    def _producer_stale_event(identity: Mapping[str, Any]) -> dict[str, Any]:
        """Name a producer running old code and the command that cycles it."""
        remedy = runs.watch_cycle_line(project)
        return {
            "event": FOLLOWER_STALE_PRODUCER_EVENT,
            "project": project,
            "session": session or "",
            "run_id": None,
            "code_stamp": identity.get("code_stamp"),
            "current_stamp": identity.get("current_stamp"),
            "remedy": remedy,
            "line": (
                f"producer {project} runs older code than this follower "
                f"({_short_code_stamp(identity.get('code_stamp'))} vs "
                f"{_short_code_stamp(identity.get('current_stamp'))}); "
                f"cycle it with: {remedy}"
            ),
        }

    def _producer_reloading_event(
        identity: Mapping[str, Any], *, pane_line: bool
    ) -> dict[str, Any]:
        """Name a producer still catching up.

        ``pane_line`` is the reload having overrun its window: while the window
        is still open the producer may catch up on its own at any poll, and a
        follower reloading onto the same new code many times an hour must not
        put a line on the pane for each -- the event still reaches the JSON
        stream, but only an overrun, which is the case that needs a reader, is
        echoed to the pane.
        """
        return {
            "event": FOLLOWER_PRODUCER_RELOADING_EVENT,
            "project": project,
            "session": session or "",
            "run_id": None,
            "pane_line": pane_line,
            "code_stamp": identity.get("code_stamp"),
            "current_stamp": identity.get("current_stamp"),
            "line": (
                f"producer {project} runs older code than this follower "
                f"({_short_code_stamp(identity.get('code_stamp'))} vs "
                f"{_short_code_stamp(identity.get('current_stamp'))}); "
                "it is reloading itself, waiting for it to catch up"
            ),
        }

    def _producer_code_events(identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Lines about a producer whose code the follower has outrun.

        A mismatch first seen after this image reloaded is deferred for the
        reload window: while the window is open the producer may catch up on
        its own, so the note that it is reloading reaches the JSON stream but
        stays off the pane, which otherwise carries a line for every reload on
        a branch that moves many times an hour. Only a mismatch that outlasts
        the window earns a pane line -- the reloading note once, then the cycle
        advice that says the seat needs cycling by hand. A fresh arming has no
        such window to grant: it never saw the code move and cannot vouch that
        the mismatch is fresh, so the advice is owed at once. The deferral keys
        on ``reloaded_in_place`` rather than on a non-empty checkpoint: the
        fact that matters is that this image replaced another, and a reload
        whose checkpoint was empty is still a reload.
        """
        nonlocal producer_stale_since, producer_reload_deferred, producer_stale_advised
        if not identity.get("stale"):
            producer_stale_since = None
            producer_reload_deferred = False
            producer_stale_advised = False
            return []
        if producer_stale_advised:
            return []
        moment = clock()
        if producer_stale_since is None:
            producer_stale_since = moment
            if reloaded_in_place:
                producer_reload_deferred = True
                # Inside the window: the event is kept for the JSON stream but
                # not echoed to the pane.
                return [_producer_reloading_event(identity, pane_line=False)]
        if not producer_reload_deferred:
            producer_stale_advised = True
            return [_producer_stale_event(identity)]
        if moment - producer_stale_since < reload_window:
            return []
        # The reload overran its window: the note reaches the pane once, then
        # the remedy, because a producer still behind after the window is one a
        # reader has to cycle rather than wait out.
        producer_stale_advised = True
        return [
            _producer_reloading_event(identity, pane_line=True),
            _producer_stale_event(identity),
        ]

    def _producer_reload_pending() -> bool:
        return producer_stale_since is not None and not producer_stale_advised

    # A config layer a merge left momentarily malformed must cost this arming a
    # tick, never the stream. The reader is called every tick; without this it
    # would raise out of the loop and end the pane, so a transient config error
    # would take down a follower that has nothing to do with the file that broke
    # it. The line is printed once per contiguous deferral rather than once per
    # tick: a misconfigured layer is not news a reader needs refreshed every
    # poll, and repeating it would bury the fleet rows with the same sentence.
    deferred_config = False

    def _defer_config_tick(exc: Exception) -> None:
        nonlocal deferred_config
        if deferred_config:
            return
        deferred_config = True
        line = _dim_history_line(
            "reckon crew follow deferred a tick: the flight configuration could "
            f"not be read ({exc}); keeping this image and retrying on the next tick"
        )
        _echo_follow_line(line)

    while not _stopped() and not lifetime_elapsed and not consumer_gone:
        identity = runs.watch_producer_identity(project)
        if not runs.producer_live(project):
            reason = _logged_producer_stop(project)
            # A producer that stops while this follower's fleet still holds
            # live work is a silent fleet the pane must name; one that stops
            # with nothing left to watch -- the measured idle exit that armed
            # no run -- keeps the pane quiet, and the event still reaches the
            # JSON consumer and the session's record. The predicate is read
            # once, at the moment the stop is observed, and only when a line is
            # actually owed: this branch spins every poll while the seat is
            # down, so a scan on each pass would tax a follower that has
            # nothing to say.
            report_stop = bool(reason) and not producer_stop_reported
            report_reload = (
                bool(identity.get("reload_started_at"))
                and not producer_reload_failure_reported
            )
            if report_stop or report_reload:
                watched = _population_has_live_work(project, session=session)
            if report_stop:
                producer_stop_reported = True
                yield {
                    "event": FOLLOWER_PRODUCER_STOPPED_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "pane_line": watched,
                    "line": f"producer {project} is gone; {reason}",
                }
            if report_reload:
                producer_reload_failure_reported = True
                yield {
                    "event": FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "pane_line": watched,
                    "line": (
                        f"producer {project} stopped during its reload begun "
                        f"{identity['reload_started_at']}; last output: "
                        f"{identity.get('log_path') or 'not recorded'}; re-arm with: "
                        f"{runs.watcher_ensure_line(project)}"
                    ),
                }
            # The sweep runs here rather than at the top of the loop: a pane
            # must show its rows before the recovery sweep's cost is paid,
            # because the sweep is the long call and the rows are what the
            # reader is waiting for.
            _sweep_on_cadence()
            _tick()
            sleeper(poll_interval)
            continue
        producer_reload_failure_reported = False
        producer_stop_reported = False

        try:
            cursor = runs.watch_stream_cursor(project)
        except FlightConfigError as exc:
            _defer_config_tick(exc)
            sleeper(poll_interval)
            continue
        deferred_config = False
        producer = cursor["producer"]
        # A producer whose code this follower has outrun is named once: a
        # reloading line inside its reload window, or the cycle advice a fresh
        # arming owes at once — see ``_producer_code_events``. It travels as an
        # event rather than being printed here so a JSON reader receives an
        # object like every other line, and the caller decides how it renders.
        for event in _producer_code_events(producer):
            yield event
        # The baseline is the fleet report: one transition per live run, in the
        # ticker's own vocabulary. Nothing about the follower itself goes on this
        # stream — a reader wants worker transitions and the fleet posture, not
        # two streams interleaved into one pane.
        stream_path = Path(cursor["stream_path"])
        mode, offset, recorded = _follow_resume_plan(
            project,
            session,
            stream_path=stream_path,
            resume_state=resume_state,
        )
        if reloading and mode != "continue":
            # An in-place reload continues only where its recorded place still
            # names this stream. A resume whose checkpoint names a replaced
            # stream has no place here to continue from, so it is treated as a
            # fresh arm: the fleet baseline is re-derived from the live runs and
            # fed through the row path on this same first pass, rather than
            # reading a stream whose recorded offset never described it. Reading
            # instead defers the owed rows to a pass gated on this arming's
            # remaining lifetime, which a slow fleet snapshot can exhaust first,
            # leaving the pane with none of the rows it was owed.
            reloading = False
        if reloading:
            # An in-place reload replaces the process image with the grid still
            # on screen, so it picks the stream up where the previous image left
            # it: at the recorded offset for a file that has only advanced, or
            # at the file's own start when it was replaced or truncated — with
            # the states the pane already showed restored, so only what moved is
            # delivered and nothing is re-announced.
            cursor["offset"] = offset
            path.reseed(recorded)
            if first_attach:
                # Continue each run's chain from what the pane last showed it,
                # for the runs the checkpoint does not name. The checkpoint is
                # the primary carrier and is read first; the log is the memory
                # for a reload whose checkpoint is gone, so a run renders
                # ``abandoned → working`` rather than restarting from a state
                # the reader never saw. A state the checkpoint already names
                # keeps the checkpoint's word, so the log cannot overwrite it.
                # The replacement image's grid starts empty, so it needs the
                # same memory a re-arm does, or the first row it draws falls
                # back to the producer's own ``from_state``.
                for run_id, state in follow_checkpoint.seed_states(
                    follow_checkpoint.read_history(project, session)
                ).items():
                    path.remember(run_id, state)
            # The pane's own line, before the rows': the format switch, never a
            # run's row. It carries the remembered states so the renderer can
            # seed its grid from the same map, which is what keeps a row's left
            # side on the state the pane last showed. The line is owed only when
            # the replacement image's grid differs from the one the rows already
            # on screen were drawn with; a reload that left every column where
            # it was is not news, so the event still reaches the JSON stream but
            # the pane stays quiet. ``pane_line`` carries that decision to the
            # consumer, the way the producer's stop and reload-failed lines do.
            if first_attach:
                layout_changed = resume_state.get("layout") != _ticker_layout_signature()
                yield {
                    "event": FOLLOWER_FORMAT_EVENT,
                    "project": project,
                    "session": session or "",
                    "reported": dict(path.reported),
                    "layout_changed": layout_changed,
                    "pane_line": layout_changed,
                }
        else:
            # Every arming of a fresh image — a first attach and a re-arm alike
            # — replays the fleet: one row per live run, each stamped with the
            # recorded time the run entered its current state, in ascending time
            # order. A re-arm is therefore never blank and never a burst of rows
            # all stamped with the moment it attached. A re-arm whose pane is a
            # terminal restores the stored history first, under its frame; the
            # remembered states travel on that event rather than through the row
            # path, so the replay below is not suppressed by the very states it
            # is about to re-announce.
            #
            # The boundary is read from the file once, before the replay reads
            # anything, and the read loop then opens at exactly that byte. A
            # transition appended in between therefore falls on one side of the
            # boundary or the other — the replay's list or the loop's read — and
            # arrives once, where taking the size afterwards would drop it from
            # both. The capture also snaps back to the byte after the last
            # newline, so a record the producer is still writing is not split
            # across the two reads: a line boundary is a record boundary.
            boundary = _follow_boundary(stream_path)
            # What this session's pane was last shown, one state per run. It is
            # consulted only by an attach that has no place to resume from. A
            # re-arm whose recorded offset still names this stream continues
            # from it, and what its pane missed is exactly what the stream
            # gained — the fleet replay's job, as it was. An attach with no
            # place — one whose checkpoint is gone, or whose recorded offset
            # names a stream that no longer exists — has no such gap to read,
            # and re-deriving the fleet would stamp a run that has been working
            # all along with the moment it attached and drop the runs that
            # finished. The record replays that gap as a diff instead: one row
            # per run that moved, from the state the record names, under a
            # header that says how many runs were found and how many changed.
            record = (
                runs.read_delivered(project, session)
                if first_attach and not reloaded_in_place and mode in ("baseline", "restart")
                else {}
            )
            gap_rows = (
                _follow_gap_rows(
                    cursor["baseline"],
                    record,
                    session=session,
                    observed=observed_sessions,
                    run_ids=selected_runs,
                )
                if record
                else []
            )
            if record:
                live_runs = sum(
                    1
                    for row in cursor["baseline"]
                    if _follow_selects(
                        row,
                        session=session,
                        observed=observed_sessions,
                        run_ids=selected_runs,
                    )
                )
                yield {
                    "event": FOLLOWER_REATTACH_EVENT,
                    "project": project,
                    "session": session or "",
                    "run_id": None,
                    "line": _follow_reattach_line(
                        live_runs, len(gap_rows), str(record.get("recorded_at") or "")
                    ),
                }
            if mode != "baseline" and first_attach:
                # The event restores the pane's stored history for a terminal,
                # and carries no remembered states: the replay below draws the
                # runs that moved, and seeding the pane's memory with the very
                # states it is about to re-announce would suppress every row of
                # it. The rows themselves restore the memory as they are drawn.
                yield {
                    "event": FOLLOWER_RESUME_EVENT,
                    "project": project,
                    "session": session or "",
                    "reported": {},
                }
            if record:
                # The diff covers the whole gap, so the read loop opens at the
                # boundary and the rows above are the only rows for the runs
                # they name.
                for row in gap_rows:
                    for printed in path.feed(row, now=clock()):
                        yield printed
            else:
                # A first arming has no place in this stream, so it has no gap
                # to deliver and derives the fleet as it stands: the place the
                # replay resumes from is the boundary itself. A re-arm did have
                # a place, and everything the stream gained between it and the
                # boundary is what the pane missed.
                resume_from = boundary if mode == "baseline" else offset
                for printed in _fleet_replay(
                    path,
                    baseline=cursor["baseline"],
                    stream_path=stream_path,
                    clock=clock,
                    reannounce=mode != "baseline",
                    resume_from=resume_from,
                    boundary=boundary,
                ):
                    yield printed
            # A re-arm's replay delivered its gap as news up to the boundary, so
            # the read loop opens there: the gap it already covered is not read
            # again, which is what stops a run that moved from being drawn twice
            # — as a replay row and then as a transition. The record's diff
            # covers the same ground from the pane's own memory, so an attach
            # that drew it opens at the boundary too. A first arming read
            # nothing to the boundary — it derives the fleet — so it leaves every
            # line already in the stream to the read loop, which opens at the
            # cursor's own offset and delivers them. Opening a first arming at
            # the boundary would drop the lines it never replayed.
            if record or mode != "baseline":
                cursor["offset"] = boundary
        # Left behind before the first read rather than after the first line:
        # an arming that starts against a quiet stream and then ends has still
        # established its place. Without this the baseline's own arming wrote
        # nothing, so a re-arm before the fleet next moved found no checkpoint
        # and replayed the baseline — the defect this removes, on the quiet path.
        _record_checkpoint(stream_path, cursor["offset"])
        resume_state = {}
        reloading = False
        first_attach = False
        # After the first row is on screen, not before it: on a first arming
        # that is the baseline, on a re-arm the history burst. Deferring the
        # sweep past them is what stops a long recovery from holding an empty
        # pane, and the cadence is unchanged because the pass after the first
        # row gates the same as any other.
        _sweep_on_cadence()

        while not stream_path.exists() and not consumer_gone:
            if not runs.producer_live(project):
                break
            if _stopped():
                return
            _tick()
            if _producer_reload_pending():
                # A deferred mismatch is re-read on the wait pass rather than
                # only on attach: the producer's reload window can close while
                # the follower waits, and the same pass that renews the lease is
                # where the escalation is owed.
                for event in _producer_code_events(
                    runs.watch_producer_identity(project)
                ):
                    yield event
            if lifetime_elapsed or consumer_gone:
                break
            sleeper(poll_interval)
        if not stream_path.exists():
            continue

        with stream_path.open(encoding="utf-8") as stream:
            stream.seek(cursor["offset"])
            # The open file's identity, taken once here and carried with every
            # offset read from it: a replacement that lands while this handle is
            # open changes the path's inode but not this file's, and the recorded
            # place must describe the stream this reader is actually reading.
            stream_file_identity = follow_checkpoint.identity_of(stream)
            while True:
                # A half-written record is held, not parsed: readline returns
                # the producer's bytes so far without their newline, and
                # admitting that fragment would deliver the record truncated
                # while its completion arrived as a second one. read_whole_line
                # leaves the handle at the line's start until the fuller line
                # arrives, so the record is delivered once and whole.
                line = runs.read_whole_line(stream)
                if line:
                    event = runs.parse_stream_line(line)
                    for printed in path.feed(event, now=clock()):
                        yield printed
                    _tick(
                        stream_path=stream_path,
                        offset=stream.tell(),
                        identity=stream_file_identity,
                    )
                    if lifetime_elapsed or consumer_gone:
                        break
                    _sweep_on_cadence()
                    continue
                # Stopping and losing what is already written would be the same
                # defect at the other end of the pipe, so both endings drain
                # the stream first.
                if _stopped():
                    for printed in path.flush(now=float("inf")):
                        yield printed
                    return
                if not runs.producer_live(project):
                    break
                _tick(
                    stream_path=stream_path,
                    offset=stream.tell(),
                    identity=stream_file_identity,
                )
                if _producer_reload_pending():
                    # The idle wait pass is where a deferred mismatch is
                    # re-read: the producer's reload window can close while the
                    # stream is quiet, and the follower is not otherwise looking
                    # at the seat on a busy stream.
                    for event in _producer_code_events(
                        runs.watch_producer_identity(project)
                    ):
                        yield event
                # A held opener is released on elapsed time, not on the next
                # row, so the wait pass is where its window can close while the
                # fleet is quiet. Without this a held row would wait for a
                # later transition that may never come.
                for printed in path.flush(now=clock()):
                    yield printed
                if lifetime_elapsed or consumer_gone:
                    break
                # The gate is time-based, so calling it from the wait pass as
                # well as the line pass runs the recovery on elapsed time while
                # a producer is up; the outer loop only iterates after attach.
                _sweep_on_cadence()
                sleeper(poll_interval)

    for printed in path.flush(now=float("inf")):
        yield printed

    if lifetime_elapsed:
        yield _follower_end_event(
            project,
            session=session,
            armed_seconds=lifetime,
            elapsed=clock() - started_at,
        )



def _echo_follow_line(line: str, *, stream=None) -> None:
    """Write and flush one ticker line so pipe readers receive it immediately.

    The stream this writes to is almost never a terminal — it is a pipe into
    a pane or a log file — so Click's default auto-detection would strip any
    escape codes the ticker painted before a single reader ever saw them.
    Whether the line carries colour at all was already decided when it was
    painted (``Ticker.color``, honouring ``--no-color``/``NO_COLOR``); once
    painted, those bytes must reach the reader unstripped.
    """
    output = stream or click.get_text_stream("stdout")
    click.echo(line, file=output, color=True)
    output.flush()



HISTORY_DIM = "\x1b[2m"



HISTORY_RESET = "\x1b[0m"



def _short_code_stamp(stamp: Any) -> str:
    """Name a code stamp in the width a one-line report can carry.

    The full digest is 64 characters and two of them in one sentence push the
    remedy off the pane's width. Eight is what `git log --oneline` uses for the
    same reason, and the two sides of a comparison are read against each other
    rather than resolved by a reader.
    """
    text = str(stamp or "")
    return text[:8] if text else "none"



def _dim_history_line(text: str) -> str:
    """Wrap one replayed row in the dim styling, leaving its own bytes intact.

    The row's text is exactly what it was first rendered as; the mark is applied
    around it, never by rewriting the row, so a reader comparing a replayed row
    with the original sees the same columns and the same clock.
    """
    return f"{HISTORY_DIM}{text}{HISTORY_RESET}"



def _follow_history_caps(project: str) -> tuple[int, float]:
    """The pane-history caps for this project, from flight config or shipped.

    Both caps are configuration: rows and a wall-clock window, applied together
    so whichever admits fewer governs. A config that cannot be read falls back
    to the shipped defaults rather than silencing the pane — the misconfigured
    layer already surfaces on ``reckon flight``, and a reader watching a pane is
    not the person to tell about it.
    """
    from reckon.crew.follow_checkpoint import (
        DEFAULT_HISTORY_ROWS,
        DEFAULT_HISTORY_SECONDS,
    )

    fallback = (DEFAULT_HISTORY_ROWS, DEFAULT_HISTORY_SECONDS)
    try:
        from reckon import flight as flight_module
        from reckon.crew.node import parse_duration

        config = flight_module.resolve(project).config
        ticker = config.get("ticker") or {}
        rows = int(ticker.get("history_rows", DEFAULT_HISTORY_ROWS))
        window = str(ticker.get("history_window") or "").strip()
        seconds = float(parse_duration(window)) if window else DEFAULT_HISTORY_SECONDS
    except Exception:  # noqa: BLE001 - a pane must not die for its caps
        return fallback
    if rows < 1 or seconds <= 0:
        return fallback
    return rows, seconds



def _follow_row_stamp(event: Mapping[str, Any]) -> float:
    """The epoch a delivered row was drawn under, read from its own stamp.

    The history window is measured against the time each row carried, not the
    time the log was written, so a re-arm after a long outage replays the rows
    that are still inside the window rather than the ones written most recently.
    A row whose stamp cannot be read is placed at the moment of reading. Only a
    stamp that states a time of day is read; a bare calendar date carries no
    hour to place the row at, so it is placed at the moment of reading too.
    """
    text = str(event.get("observed_at") or "")
    if text != text.strip() or ("T" not in text and " " not in text):
        return time.time()
    moment = parse_utc(text)
    if moment is None:
        return time.time()
    return moment.timestamp()



def _follow_replay_visible() -> bool:
    """Whether this follower's reader is watching a live pane, not a pipe.

    A re-arm restores the pane a reader has been watching, so the rows land
    above whatever the terminal still shows. A pipe has no scrollback: the
    host's line-batching Monitor is one, and every line it carries is news to
    the transcript it feeds. Handing that reader the history makes rows it has
    already acted on arrive again as transitions, which is the replay this
    removes. Only a terminal is handed the history burst.
    """
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False



_HISTORY_FRAME = "── history · {count} rows ──"



def _follow_history_burst(rows, *, dim=_dim_history_line) -> str:
    """Compose a re-arm's history replay as one string, to be written once.

    One string rather than one write per row: a consumer that batches the stream
    into notifications then sees the whole replay as a single event.

    The rows carry their own timestamps, but a clock alone does not say a row
    is *old*: a reader skimming the pane cannot tell a restored row from a
    fresh one by its time. So the replay opens with one dim frame line that
    names it as earlier history, and every row under it reads as something the
    pane has already shown. The line is furniture only for the reader that
    needs it: this composer is reached only when a terminal is watching.
    """
    if not rows:
        return ""
    frame = dim(_HISTORY_FRAME.format(count=len(rows)))
    return "\n".join([frame, *(dim(row["text"]) for row in rows)])



TICKER_THEMES = ("dark", "light")



def _row_is_stale_inventory(event) -> bool:
    """Whether a text reader is better served by this row's absence.

    A follower emits one baseline per live run the moment it attaches, so a
    restart prints a burst of rows for work that already finished, each reading
    exactly like a landing that just happened. Only the inventory that asks the
    reader for nothing is withheld: a run waiting to be promoted, one that
    failed and one that was abandoned are finished work with a duty still
    outstanding, and their rows are exactly what a reader attaching must see.
    The machine stream still carries every one of them — a consumer
    reconstructing the fleet needs the inventory — so the judgement is made
    here, on the human path only.
    """
    from reckon.crew import ticker as ticker_module

    return ticker_module.hidden_at_attach(event)



def _ticker_grid(width, theme, no_color):
    """Build the reader's grid, deferring the import that owns its defaults."""
    from reckon.crew import ticker as ticker_module

    return ticker_module.Ticker(
        width=ticker_module.resolve_terminal_width() if width is None else width,
        theme=ticker_module.DEFAULT_THEME if theme is None else theme,
        color=not no_color,
    )



def _seed_ticker_memory(grid, states) -> None:
    """Give the grid the states the pane already showed, before its next row.

    A grid remembers the state it last put on screen for each run, and reads a
    row's left side from that memory rather than from the producer's own record.
    A replacement image builds a fresh grid, so the memory has to be handed to
    it: without this a reloaded pane draws the producer's ``from_state`` and
    shows a bare state where the reader had a transition. The map travels on the
    attach event, which is where the follower has already merged the checkpoint
    and the stored history into one remembered set.
    """
    if not states:
        return
    grid._reported.update({str(key): str(value) for key, value in dict(states).items()})



def _ticker_options(command):
    """Attach the reader's grid choices to a command that prints ticker lines.

    The follower's own stdout is a pipe, so ``isatty`` is false and ``COLUMNS``
    is unset where the stream is read; the pane it fills is owned by a terminal
    further up the process tree, and width is resolved from that ancestor's
    window so it stays current across a resize. Colour is stated, not
    inferred. ``--width`` is the override, and the fallback for a detached
    follower whose ancestry holds no terminal.
    """
    for option in reversed(
        (
            click.option(
                "--width",
                type=int,
                default=None,
                help=(
                    "Column the right-aligned fleet counts end on. Raised to "
                    "whatever the fixed columns need, so no line ever wraps."
                ),
            ),
            click.option(
                "--theme",
                type=click.Choice(TICKER_THEMES),
                default=None,
                help="Colour set to read against a dark or light pane.",
            ),
            click.option(
                "--no-color",
                "no_color",
                is_flag=True,
                help="Print plain lines. NO_COLOR in the environment does the same.",
            ),
        )
    ):
        command = option(command)
    return command



_ATTENTION_REMOVED_AFTER = "2027-06-30"



_ATTENTION_DEPRECATION = (
    "Warning: --attention no longer filters; the follower now delivers every "
    "transition. The flag is deprecated and stops being accepted after "
    f"{_ATTENTION_REMOVED_AFTER}; remove it from any arming line before then."
)



@crew.command(name="host", hidden=True)
@click.option(
    "--owner-pid",
    type=int,
    default=None,
    help="The Claude process this host lives for; defaults to the parent."
)
@click.option(
    "--owner-start",
    default=None,
    help="The owner's kernel start time, so a reused pid is not mistaken for it.",
)
@click.option(
    "--follower-command",
    default=None,
    hidden=True,
    help="JSON argv run per request in place of this checkout's crew follow.",
)
@click.option(
    "--fifo",
    "fifo_path",
    default=None,
    hidden=True,
    help="Read requests from this FIFO path rather than the inherited stdin.",
)
@click.option(
    "--fd",
    "fd_number",
    type=int,
    default=None,
    hidden=True,
    help="Read requests from this inherited descriptor.",
)
@click.option(
    "--first-request",
    "first_request",
    default=None,
    hidden=True,
    help="A request line the caller already read off the descriptor.",
)
def crew_host(
    owner_pid, owner_start, follower_command, fifo_path, fd_number, first_request
):
    """Supervise one live session's crew followers for as long as it lives.

    Hidden from help because only the plugin entry point runs it. It reads one
    JSON request per line naming a ``project`` and a ``session``, and runs one
    ``crew follow`` per pair, restarting a child that exits while the host does.
    Its stdout is the pane, so it writes nothing there and lets its children
    speak.
    """
    import json as json_module
    import os as os_module

    from reckon.crew import session_host as session_host_module

    owner: dict[str, object] | None = None
    if owner_pid is not None:
        owner = {
            "pid": owner_pid,
            "start_time": owner_start
            or (session_host_module.process_start_time(owner_pid) or ""),
        }
    follower_argv = (
        json_module.loads(follower_command) if follower_command else None
    )
    requests = None
    if fd_number is not None:
        requests = os_module.fdopen(fd_number, "r", encoding="utf-8", errors="replace")
    elif fifo_path:
        descriptor = os_module.open(fifo_path, os_module.O_RDWR)
        requests = os_module.fdopen(descriptor, "r", encoding="utf-8", errors="replace")
    return session_host_module.run(
        owner=owner,
        follower_argv=follower_argv,
        requests=requests,
        first_request=first_request,
    )



@crew.command(name="follow")
@click.option("--project", required=True, help="Project whose watch stream to follow.")
@click.option(
    "--session",
    default=None,
    help=(
        "Deliver only the runs this session dispatched, and register the "
        "session as attached so dispatch can verify delivery."
    ),
)
@click.option(
    "--observe-session",
    "observe_sessions",
    multiple=True,
    help=(
        "Deliver another session's transitions to this pane as well, to keep "
        "older work in view beside your own. Repeat for several. Observing "
        "registers nothing for the named session: it does not vouch for "
        "delivery, and that session's own dispatch still requires its own "
        "follower."
    ),
)
@click.option(
    "--run",
    "run_ids",
    multiple=True,
    help="Deliver only these run ids. Repeat for several.",
)
@click.option(
    "--attention",
    is_flag=True,
    hidden=True,
    help=(
        "Deprecated no-op, accepted so a follower armed before the removal "
        "reconnects instead of failing to parse. It no longer filters; its "
        f"passing is announced on stderr. Removed after {_ATTENTION_REMOVED_AFTER}."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
@click.option(
    "--lifetime",
    default=None,
    metavar="DURATION",
    help=(
        "End this follower after DURATION (an integer plus s, m or h, e.g. 29m), "
        "printing one final line that names how to re-arm and this session's "
        "runs that need the coordinator. The host ends a Monitor at thirty "
        "minutes, so arm slightly less and re-arm on the final line instead of "
        "discovering the end when the host kills the pane."
    ),
)
@_ticker_options
def crew_follow(
    project,
    session,
    observe_sessions,
    run_ids,
    attention,
    json_output,
    pretty,
    lifetime,
    width,
    theme,
    no_color,
):
    """Follow one session's live runs without acquiring the project watcher seat.

    The seat is project-global and this delivery is session-local, so a
    follower is what a coordinator arms to be woken about its own fleet. Arm it
    through the host harness's per-line notification primitive: this command
    produces lines and does not exit, so a mechanism that reports only on exit
    delivers nothing at all.

    Every transition in the fleet is delivered, starts and recoveries
    included; there is deliberately no option to narrow to the action states,
    because a filter that hides a run's recovery hides the news the reader is
    waiting for.

    ``observe_sessions`` names other sessions delivered for oversight beside
    the owning one. Only the owning session is registered — an observed
    session's dispatch guard still sees no delivery, so observing vouches for
    hearing its rows, never for hearing every run it might finish.
    """
    if attention:
        click.echo(_ATTENTION_DEPRECATION, err=True)
    from reckon.crew import runs as runs_module
    from reckon.crew.node import parse_duration
    from reckon.crew.recovery import format_watch_transition

    lifetime_seconds: float | None = None
    if lifetime is not None:
        try:
            lifetime_seconds = float(parse_duration(lifetime))
        except runs_module.CrewError as exc:
            raise click.ClickException(str(exc)) from exc
    # The absolute instant this arming ends, in UTC epoch. Fixed here and never
    # rewritten, so a reload continues the original arming rather than granting
    # a fresh lifetime. Held in a local rather than ``os.environ``: the only
    # process allowed to see it is the replacement this follower execs into,
    # which the reloader below carries it to.
    carried_deadline = _carried_lifetime_deadline()
    deadline_epoch: float | None = None
    if carried_deadline is not None:
        # This image replaced one already armed with a lifetime, so it spends
        # what is left of that arming rather than arming its own. A reload that
        # arrived past the deadline yields a lifetime already elapsed, which
        # ends the follower at once — a reload never extends an arming.
        deadline_epoch = carried_deadline
        lifetime_seconds = max(0.0, carried_deadline - time.time())
    elif lifetime_seconds is not None:
        deadline_epoch = time.time() + lifetime_seconds

    # The same instant on the clock the watch loop reads, taken once, here. The
    # replacement image imports, resolves its delivery and replays the fleet
    # before its first wait pass, so a deadline re-anchored at that pass
    # charges the arming for all of it — measured at up to 2.6 s past a
    # six-second grant, and more when the first sweep is slow.
    lifetime_deadline: float | None = None
    if deadline_epoch is not None:
        lifetime_deadline = time.monotonic() + max(0.0, deadline_epoch - time.time())

    delivery = runs_module.delivery_mode()
    grid = _ticker_grid(width, theme, no_color)
    # Whether this image replaced a previous follower in place. The reloader
    # sets the checkpoint variable for exactly that replacement, so its presence
    # -- not a non-empty checkpoint -- is what says so: a reload whose checkpoint
    # came through empty is still a reload, and the staleness deferral keys on
    # the reload rather than on the checkpoint's contents.
    reloaded_in_place = _FOLLOWER_CHECKPOINT_ENV in os.environ
    resume = _take_follower_checkpoint(project)
    from reckon.crew.dispatch import (
        WATCH_ARMING_ENV,
        _adopt_launched_workers_from_reexec,
    )

    # The variable that lets a child under a throwaway home arm a producer is
    # an instruction to the process doing the arming, and this follower is not
    # that process: a child of an armed follower — the recovery sweep's probes
    # among them — would inherit the instruction and act on it, exactly as the
    # lifetime deadline did before it was carried to the replacement alone.
    os.environ.pop(WATCH_ARMING_ENV, None)

    # The follower may have just replaced its own process image; the launched
    # workers it spawned before the replacement are still its children, so it
    # adopts them exactly as if it had launched them and collects them as they
    # end.
    _adopt_launched_workers_from_reexec()

    # Fix the process this follower reports to, once, before anything claims a
    # registration. A registration taken over later records this owner rather
    # than whatever process happens to be the parent by then, and the reloader
    # carries the same value to a replacement image.
    owner = runs_module.follower_owner()

    def stream_events(registration):
        reloader = _FollowerReloader(
            project,
            registration,
            deadline=deadline_epoch,
            owner=owner,
            color=getattr(grid, "color", False),
        )

        def poll(checkpoint) -> None:
            """Take over a registration whose holder has gone, while streaming.

            Silently. A second follower streaming read-only, and its later
            takeover, are facts about the followers rather than about the fleet.
            The session that needs telling is one trying to dispatch, and the
            dispatch guard tells it there, with the remedy.
            """
            if registration is not None and not registration.held:
                registration.acquire()
            reloader.poll(checkpoint)

        from reckon.crew import follow_checkpoint as history_module

        # The pane's memory of what it drew, for a session that will re-arm.
        # A session-less follower exists to observe the whole fleet in one
        # pass, so it has no pane to restore and keeps no log.
        history_caps = _follow_history_caps(project) if session is not None else None
        replay_dim = _dim_history_line if getattr(grid, "color", False) else str

        # The state each run was last handed to this reader at, so the session's
        # next attach can replay the gap rather than re-announce the fleet. It
        # is written as rows are delivered, because a row the pane never
        # received is not one the reader saw and must not subtract a run from
        # the next replay. The record this attach inherited is carried forward:
        # a run drawing nothing here keeps the state its last row carried, and
        # a write that dropped it would replay the run from `dispatched` as soon
        # as one other run moved.
        delivered_states: dict[str, str] = {
            str(run_id): str(state)
            for run_id, state in (
                runs_module.read_delivered(project, session).get("states") or {}
            ).items()
        }

        def note_delivered(row) -> None:
            run_id = str(row.get("run_id") or "")
            state = str(row.get("to_state") or "")
            if not run_id or not state or delivered_states.get(run_id) == state:
                return
            delivered_states[run_id] = state
            runs_module.write_delivered(project, session, delivered_states)

        for event in _follow_watch_lines(
            project,
            session=session,
            observed=observe_sessions,
            run_ids=run_ids,
            on_poll=poll,
            resume=resume,
            lifetime=lifetime_seconds,
            lifetime_deadline=lifetime_deadline,
            registration=registration,
            reloaded_in_place=reloaded_in_place,
        ):
            # An attach event carries the states the pane already showed, so the
            # grid is seeded from the same remembered map the follower filtered
            # against — before any fresh row is consumed. The map is the
            # follower's own bookkeeping rather than part of the pane's event,
            # so it is taken off before the event goes on to the reader.
            _seed_ticker_memory(grid, event.pop("reported", None))
            # A row the arming re-announced rather than drew as news: it is a
            # fresh line for the pane, but the run's row is already the one the
            # stored history carries, so recording it again would replay the
            # same run twice on the next burst.
            reannounced = bool(event.pop("reannounced", False))
            if event.get("event") == FOLLOWER_END_EVENT:
                # This one line is about the follower, not the fleet, so it is
                # printed as it was written rather than rendered as a fleet row.
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                else:
                    _echo_follow_line(str(event.get("line") or ""))
                continue
            if event.get("event") == FOLLOWER_FORMAT_EVENT:
                # The follower reloaded onto a new image mid-stream. The rows it
                # drew before the reload were drawn by the old format and are
                # replayed verbatim from the log, so the marker stands between
                # them and the new ones; the DIM row is the reader's signal that
                # the drawing style changed here. A reload whose grid did not
                # move is not news: its event still reaches the JSON stream, but
                # the pane is handed no marker and the log records none, so a
                # later re-arm does not replay a switch that never happened.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line and session is not None:
                    _echo_follow_line(replay_dim(history_module.FORMAT_CHANGED_TEXT))
                    history_module.append_history(
                        project,
                        session,
                        text=history_module.FORMAT_CHANGED_TEXT,
                        at=time.time(),
                        kind=history_module.FORMAT_CHANGED_KIND,
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )
                continue
            if event.get("event") == FOLLOWER_REATTACH_EVENT:
                # The pane's own header, written before any fleet row: it says
                # the follower re-attached, how many live runs it found and how
                # many the replay below will draw. Like the other framing lines
                # it is about the pane rather than the fleet, so it is never
                # rendered as a run's row.
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                else:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if event.get("event") == FOLLOWER_RESUME_EVENT:
                # A re-arm, before any fresh row: restore the pane exactly as
                # the reader last saw it, in one write so the whole view lands
                # as a single event, then let the live rows follow.
                #
                # Only a terminal is handed the history. A pipe reader — the
                # host's line-batching Monitor is one — has no scrollback to
                # fill and would read the restored rows as fresh transitions,
                # which is precisely the replay this removes. When there is a
                # terminal, the burst opens under one dim frame line that names
                # it as earlier history, so a restored row is never acted on.
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif session is not None and _follow_replay_visible():
                    restored = history_module.cap_history(
                        history_module.read_history(project, session),
                        now=time.time(),
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )
                    burst = _follow_history_burst(restored, dim=replay_dim)
                    if burst:
                        _echo_follow_line(burst)
                continue
            if event.get("event") in (
                FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT,
                FOLLOWER_PRODUCER_STOPPED_EVENT,
            ):
                # A producer stop reaches the JSON stream in every case and
                # the pane only when the follower's own fleet still holds live
                # work. The emission site marks that predicate on the event as
                # ``pane_line``; stripping it here keeps the emitted object the
                # shape every other JSON row carries, and a quiet pane is the
                # whole point of the mark rather than a dropped event.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if event.get("event") in (
                FOLLOWER_STALE_PRODUCER_EVENT,
                FOLLOWER_PRODUCER_RELOADING_EVENT,
            ):
                # The seat's producer runs older code than this follower — with
                # the cycle remedy once it is confirmed stale, or a line saying
                # it is still catching up inside its reload window. Like the
                # format marker it is about the pane rather than the fleet, so
                # it is never rendered as a run's row: JSON mode emits the
                # object with the stamps (and the remedy when there is one), and
                # text mode prints the one dim line. A reloading note marks its
                # own pane reach on ``pane_line``: while the window is still
                # open it is kept off the pane, and only an overrun is echoed.
                pane_line = bool(event.pop("pane_line", True))
                if json_output:
                    _emit_crew_result(event, pretty, observation=True)
                elif pane_line:
                    _echo_follow_line(replay_dim(str(event.get("line") or "")))
                continue
            if json_output:
                _emit_crew_result(event, pretty, observation=True)
                # The record is the pane's memory, and there is one per session
                # rather than one per output mode: a row only the JSON consumer
                # received is not one the pane in front of the session drew, and
                # counting it would subtract the run from that pane's next
                # re-attach.
                if not _row_is_stale_inventory(event):
                    note_delivered(event)
            elif not _row_is_stale_inventory(event):
                # An observing follower draws the owner column on every row so
                # the grid stays aligned, and the owning session's own rows are
                # blanked in it; only the observed rows carry the foreign glyph.
                with_session = session is None or bool(observe_sessions)
                rendered = format_watch_transition(
                    _follow_render_event(
                        event, session=session, observed=observe_sessions
                    ),
                    with_session=with_session,
                    ticker=grid,
                    session=session,
                )
                _echo_follow_line(rendered)
                note_delivered(event)
                if history_caps is not None and not reannounced:
                    # The row's bytes as drawn, and the stamp it carried, so a
                    # later re-arm replays the pane rather than a re-derivation
                    # of it: the clock a reader saw is the clock that returns.
                    history_module.append_history(
                        project,
                        session,
                        text=rendered,
                        at=_follow_row_stamp(event),
                        run_id=str(event.get("run_id") or ""),
                        state=str(event.get("to_state") or ""),
                        max_rows=history_caps[0],
                        max_seconds=history_caps[1],
                    )

    try:
        if session is None:
            stream_events(None)
            return
        with runs_module.follower_registration(
            project,
            session,
            delivery=delivery,
            scope={"runs": list(run_ids)},
        ) as registration:
            stream_events(registration)
    except runs_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc



@crew.command(name="watch")
@click.option("--project", required=True, help="Project whose live fleet to watch.")
@click.option(
    "--stall-window",
    default="15m",
    show_default=True,
    help="Exit when a non-terminal run's stream stays quiet this long.",
)
@click.option(
    "--exit-on-empty",
    is_flag=True,
    help=(
        "With --once, exit when no live pointers remain instead of "
        "waiting for the first one."
    ),
)
@click.option(
    "--once",
    is_flag=True,
    help=(
        "Return after a single fleet event instead of following. The seat is "
        "released on return, so the caller must re-arm before its next dispatch."
    ),
)
@click.option(
    "--ensure",
    "ensure_service",
    is_flag=True,
    help=(
        "Start or restart this project's watcher user service and return, "
        "instead of watching here. Idempotent: a second call on a live, "
        "unchanged unit reports it and starts nothing."
    ),
)
@click.option(
    "--follow",
    is_flag=True,
    hidden=True,
    help=(
        "Accepted for compatibility; following is now the default, and this "
        "flag still wins over --once."
    ),
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    help="Emit machine-readable transition objects rather than ticker lines.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
@_ticker_options
def crew_watch(
    project,
    stall_window,
    exit_on_empty,
    once,
    ensure_service,
    follow,
    json_output,
    pretty,
    width,
    theme,
    no_color,
):
    """Watch project-wide live runs as the single producer through reconciliation.

    Following is the default because the watcher seat is what
    ``reckon crew dispatch`` requires. A seat released after every landing has
    to be re-armed before each dispatch, and a coordinator that forgets meets
    ``watcher-required`` instead of a worker. Following holds the seat from an
    empty project, through every landing, until the fleet drains.
    """
    crew_module, _ = _crew_modules()
    if ensure_service:
        # The service is the durable seat: it survives the shell that ensured
        # it, carries the backend directory on its PATH, and is restarted onto
        # a rewritten unit. Nothing here holds the seat in the caller's name.
        from reckon.crew import runs as runs_module

        try:
            result = runs_module.ensure_watcher_service(project)
        except crew_module.CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(result, pretty)
        return
    # --exit-on-empty only means anything to the single-event mode, so asking
    # for it selects that mode rather than being silently ignored.
    single_event = once or exit_on_empty
    try:
        if follow or not single_event:
            from reckon.crew import runs as runs_module
            from reckon.crew.recovery import format_watch_transition, watch_follow

            grid = _ticker_grid(width, theme, no_color)
            # The producer holds the seat for the life of its stream, so it
            # takes changed follower-side modules the way the follower does:
            # the same content-hash stamp gates the check, the same throwaway
            # import proves the replacement loads, and the seat descriptor
            # rides the exec so the seat is never released.
            reloader = _FollowerReloader(
                project,
                None,
                label="crew watch",
                seat=True,
                color=getattr(grid, "color", False),
            )
            poller = _StampPoll(reloader, runs_module.FOLLOWER_FRESHNESS_SECONDS)
            stopped_by_signal: int | None = None

            def stop_on_signal(signum, frame) -> None:
                nonlocal stopped_by_signal
                stopped_by_signal = signum
                raise SystemExit(128 + signum)

            previous_handlers = {
                signum: signal.signal(signum, stop_on_signal)
                for signum in (signal.SIGTERM, signal.SIGHUP)
            }
            stop_reason = "watch stream ended"
            try:
                poller.start()
                try:
                    for result in watch_follow(
                        project, stall_window=stall_window, transitions=True
                    ):
                        if json_output or result.get("event") not in {
                            "baseline",
                            "transition",
                        }:
                            _emit_crew_result(result, pretty, observation=True)
                        elif not _row_is_stale_inventory(result):
                            click.echo(
                                format_watch_transition(result, ticker=grid), color=True
                            )
                finally:
                    poller.stop()
                renewed = runs_module.watch_lease_renewed_at(project)
                if (
                    renewed is not None
                    and time.time() - renewed >= runs_module.producer_lease_seconds()
                ):
                    stop_reason = "producer lease expired"
            except BaseException as exc:
                if stopped_by_signal is not None:
                    stop_reason = f"received {signal.Signals(stopped_by_signal).name}"
                else:
                    stop_reason = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                click.echo(f"reckon crew watch stopped: {stop_reason}", err=True)
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
            return
        result = crew_module.watch(
            project,
            stall_window=stall_window,
            exit_on_empty=exit_on_empty,
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)

