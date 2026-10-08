# ruff: noqa: I001, UP035
from __future__ import annotations
import ctypes
import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import (
    Path,
)
from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
)
from reckon import (
    _backends,
    _store,
)
from reckon.crew.dispatch_claims import (
    CLAIM_GRACE_OBSERVATIONS_NAME,
    CLAIM_GRACE_OBSERVATION_LIMIT,
)
from reckon._timestamps import (
    parse_utc,
)
from reckon.crew.fleet_supervisor import (
    REQUEST_FIFO_NAME,
)
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    CrewError,
)
from reckon.crew.recovery import (
    stream_paths_newest_first,
)
from reckon.crew.routing import (
    _boundary_tree_roots,
    _repository_tree_snapshot,
    signal_worker,
)
from reckon.crew.runs import (
    _mutate_pointer,
    _pointer_lock,
    _process_start_time,
    _utc_now,
    _write_json,
    crew_home,
    placement_job_alive,
    process_alive,
    read_pointer,
    run_dir,
    scheduler_job_reason,
    scheduler_job_state,
    scheduler_kill_class,
)



# Every worker this process has launched but not yet waited on. The waiting is
# the reaper's, not the caller's: an ordinary dispatch CLI is gone before its
# worker finishes, and a long-lived follower that sweeps on a cadence only
# looks again at the next tick, so between a worker finishing and someone
# observing it, it would sit unreaped. A defunct child is still a live pid to
# a zero-signal liveness probe, which is exactly how a finished resume reads
# as running and refuses the next one. The follower reaps what it launched, so
# no corpse exists for that probe to misread. Owned by the process that called
# :func:`_spawn`, because only the parent may wait on a child.
_LAUNCHED_WORKERS: set[int] = set()
# What each launched pid was launched as, so a reap can judge the exit against
# the run it belongs to. A pid is only ever in both this map and the set above
# together; the map is popped with the pid.
_LAUNCHED_WORKER_RUNS: dict[int, dict[str, Any]] = {}
_LAUNCHED_WORKERS_LOCK = threading.Lock()
_LAUNCHED_WORKERS_WAKE = threading.Event()
# The reaper thread, once started, behind the same lock as the set it guards.
# A mutable holder rather than a rebound global so the start-once guard can
# record it without a module-level reassignment.
_LAUNCHED_WORKER_REAPER: dict[str, threading.Thread | None] = {"thread": None}


# The stream file a launch writes its turn records into. A launch that produced
# no byte of it never reached a model: the process is gone, so there is no turn
# to wait for and no session to reuse, and the run is stopped rather than
# retried. Named as a phase so every reader of the pointer sees the same thing.
LAUNCH_FAILED_PHASE = "launch-failed"

# How much of the failed launch's stderr is kept on the run. Enough for a
# traceback or an exec diagnostic, bounded so a chatty backend cannot grow a
# pointer without limit.
_LAUNCH_FAILURE_STDERR_BYTES = 2048


def _launched_worker_record(
    plan: _backends.LaunchPlan, log_path: Path, stderr_path: Path
) -> dict[str, Any] | None:
    """Describe a launched worker, or None when its stream names no run.

    The stream path is ``<run dir>/<turn>.jsonl``, so the run it belongs to is
    the directory's name. A launch outside a run directory — a lane probe —
    hands back None and is reaped exactly as before.
    """
    run_id = Path(log_path).parent.name
    if not run_id or Path(log_path).parent != run_dir(run_id):
        return None
    return {
        "run_id": run_id,
        "stream_path": str(log_path),
        "stderr_path": str(stderr_path),
        "argv": list(plan.argv),
        "backend": plan.backend,
    }


def _placement_job_state(
    placement: Mapping[str, Any] | None, job_id: Any
) -> str | None:
    """The scheduler's state for a placed job, or None when it cannot be read."""
    if not placement:
        return None
    try:
        return scheduler_job_state(placement, job_id)
    except (OSError, CrewError):
        return None


def _placement_job_alive(
    placement: Mapping[str, Any] | None, job_id: Any
) -> bool | None:
    """Whether a placed job is still in the system, or None when unread."""
    if not placement:
        return None
    try:
        return placement_job_alive({"placement": placement, "job_id": job_id})
    except (OSError, CrewError):
        return None


def _placed_record_identity(run_id: str) -> tuple[dict[str, Any] | None, Any]:
    """The placement and job id a run's pointer carries, reading it once.

    A reap holds nothing but the launched plan, so the run's own pointer is the
    only place the placement was ever recorded. A pointer that cannot be read —
    reclaimed between the reap and this question — answers no placement, which
    leaves the reap on its original empty-stream rule.
    """
    try:
        pointer = read_pointer(run_id)
    except CrewError:
        return None, None
    placement = pointer.get("placement")
    if not isinstance(placement, Mapping) or not placement:
        return None, None
    return dict(placement), pointer.get("job_id")


def _wait_status_record(exit_status: int) -> dict[str, Any]:
    """How the launcher's wait on its own child ended, as two distinct cases.

    ``os.waitstatus_to_exitcode`` negates the signal number when the process was
    terminated rather than exited, so the sign carries the whole distinction and
    a reader must not have to know that to act on it: the two cases want
    different responses. A signalled process chose nothing, so it records no
    exit code and names the signal that ended it; an exited one records its code
    and names no signal. A signal outside the named set — a realtime signal, for
    instance — is still recorded by number, so an unfamiliar kill is legible
    rather than dropped.
    """
    record: dict[str, Any] = {"exit_code": None, "signal": None, "signal_name": None}
    if exit_status < 0:
        number = -exit_status
        record["signal"] = number
        try:
            record["signal_name"] = signal.Signals(number).name
        except ValueError:
            record["signal_name"] = f"signal {number}"
    else:
        record["exit_code"] = exit_status
    return record


def _launch_failure_record(
    launched: Mapping[str, Any],
    *,
    exit_status: int,
    placement: Mapping[str, Any] | None = None,
    job_id: Any = None,
) -> dict[str, Any]:
    """The one record written for a launch that exited before any turn.

    A placed launch is charged to a scheduler job, so its exit status is the
    scheduler client's rather than the worker's and the payload log, not that
    status, is what decides whether the work ran; the job's own terminal state
    and reason are recorded beside it. A job the scheduler ended at a time or
    memory limit is named distinctly, because resubmitting it unchanged fails
    the same way.
    """
    stderr_tail = ""
    try:
        raw = Path(str(launched["stderr_path"])).read_bytes()
        stderr_tail = raw[-_LAUNCH_FAILURE_STDERR_BYTES:].decode("utf-8", "replace")
    except OSError:
        stderr_tail = ""
    state = _placement_job_state(placement, job_id)
    reason = scheduler_job_reason(placement, job_id)
    kind = scheduler_kill_class(state, reason) or "launch-failed"
    record = {
        "recorded_at": _utc_now(),
        "kind": kind,
        "backend": str(launched.get("backend") or ""),
        "exit_status": exit_status,
        "argv": list(launched.get("argv") or ()),
        "stderr_tail": stderr_tail,
        "stream_path": str(launched.get("stream_path") or ""),
        **_wait_status_record(exit_status),
    }
    if placement:
        record["scheduler_state"] = state
        record["scheduler_reason"] = reason
    return record


def _empty_stream_launch_failure(
    record: Mapping[str, Any], data: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The launch-failure entry for a run that died before writing a turn.

    A stream of zero bytes means the worker exited before its first event, so
    nothing in the stream explains the exit and the reason sits in the run's
    stderr, where no reader looked. The entry is built only when stderr carries
    something to quote: a process that exited with nothing in either file is an
    orphan with no reason to attach, and claiming a launch failure there would
    name an end that was never recorded. The shape is the one the launcher's
    reap writes, so a reader of ``launch_failures`` sees one vocabulary however
    the failure was found.
    """
    stream = Path(str(record.get("log_path") or ""))
    try:
        if stream.stat().st_size:
            return None
    except OSError:
        # A stream never created is the same fact as an empty one.
        pass
    try:
        raw = Path(str(record.get("stderr_path") or "")).read_bytes()
    except OSError:
        return None
    tail = raw[-_LAUNCH_FAILURE_STDERR_BYTES:].decode("utf-8", "replace").strip()
    if not tail:
        return None
    entry: dict[str, Any] = {
        "recorded_at": _utc_now(),
        "kind": LAUNCH_FAILED_PHASE,
        "backend": str(record.get("backend") or ""),
        "argv": list(record.get("argv") or ()),
        "stderr_tail": tail,
        "stream_path": str(stream),
    }
    exit_status = data.get("exit_status")
    if isinstance(exit_status, int) and not isinstance(exit_status, bool):
        entry["exit_status"] = exit_status
        entry.update(_wait_status_record(exit_status))
    return entry


def _record_launch_failure(launched: Mapping[str, Any], *, exit_status: int) -> None:
    """Record how a launched worker's process ended, on its run.

    Every reap records the exit the launcher observed, except where the payload log
    tells us the status is not the worker's to report. A run whose stream is
    non-empty otherwise reads as still working with no trace of its process
    having gone, which is how a worker ended by a signal stays
    indistinguishable from a live one. The payload log, not the step's exit
    status, still decides whether a worker turn ran: a placed launch's exit
    status belongs to the scheduler client, and a step the scheduler reports
    COMPLETED can still have aborted before reaching a model. A non-empty
    stream is a turn that ran whatever the status says, so an unplaced run
    keeps its phase and gains the wait status.

    A launch that wrote no byte at all reached no model: there is nothing to
    resume from and nothing the lift loop can usefully retry. That one is a
    launch failure as well — it records the failure and sets the phase, which
    stops a further lift until a person resumes or completes the run, the
    measured loop of one 0-byte stream every two minutes.
    """
    stream = Path(str(launched.get("stream_path") or ""))
    try:
        size = stream.stat().st_size
    except OSError:
        # A stream that was never created is the same fact as an empty one.
        size = 0
    run_id = str(launched.get("run_id") or "")
    if not run_id:
        return
    placement, job_id = _placed_record_identity(run_id)
    # The pid that finished is the scheduler client, not the worker, so a job
    # still in the system means the worker has not ended and there is no failure
    # to record yet. Only a job that has left the queue is judged.
    if placement and _placement_job_alive(placement, job_id) is True:
        return
    if size and placement:
        # A placed launch's wait status is the scheduler client's, not the
        # worker's, so a run whose payload log shows a turn ran has nothing
        # here that describes the worker: the client's status says nothing
        # about a process the scheduler still owns. A placed run that wrote no
        # byte is judged on the payload log, exactly as before.
        return
    wait_status = _wait_status_record(exit_status)
    failure = (
        None
        if size
        else _launch_failure_record(
            launched,
            exit_status=exit_status,
            placement=placement,
            job_id=job_id,
        )
    )

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        phase = str(pointer.get("phase") or "")
        if phase == LAUNCH_FAILED_PHASE or phase in _TERMINAL_RUN_PHASES:
            return pointer
        pointer["wait_status"] = wait_status
        if failure is None:
            return pointer
        pointer["phase"] = LAUNCH_FAILED_PHASE
        failures = list(pointer.get("launch_failures") or ())
        failures.append(failure)
        pointer["launch_failures"] = failures
        return pointer

    try:
        _mutate_pointer(run_id, mutation)
    except CrewError:
        # The pointer was reclaimed between the reap and the write; there is
        # no run left to mark, which is not this reaper's failure to report.
        return


def _reap_launched_workers() -> None:
    """Wait on every finished worker this process launched, without blocking.

    Only the pids written here are touched. A child a caller manages
    synchronously — a condition probe, a lane probe — is never a member, so
    this cannot reap it out from under its own ``wait``. A pid that no longer
    names a child has already been reaped or never was one; it is dropped so
    the loop does not probe a reused identifier forever.
    """
    with _LAUNCHED_WORKERS_LOCK:
        pending = list(_LAUNCHED_WORKERS)
    for pid in pending:
        try:
            got, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            with _LAUNCHED_WORKERS_LOCK:
                _LAUNCHED_WORKERS.discard(pid)
            continue
        except OSError:
            continue
        if got:
            with _LAUNCHED_WORKERS_LOCK:
                _LAUNCHED_WORKERS.discard(pid)
                launched = _LAUNCHED_WORKER_RUNS.pop(pid, None)
            if launched is not None:
                _record_launch_failure(
                    launched, exit_status=os.waitstatus_to_exitcode(status)
                )


def _worker_reaper_loop() -> None:
    """Poll the launched-worker set until the process dies.

    A daemon thread so it lives exactly as long as the process that owns the
    sweep — a short-lived dispatch CLI exits and takes it with it, a long-lived
    follower keeps it for the life of the session. It wakes promptly while work
    is outstanding and slows to a heartbeat when there is nothing to wait on.
    """
    while True:
        _LAUNCHED_WORKERS_WAKE.clear()
        with _LAUNCHED_WORKERS_LOCK:
            outstanding = bool(_LAUNCHED_WORKERS)
        _reap_launched_workers()
        # A registration then wakes this loop immediately, so a worker that
        # finishes seconds after launch is reaped seconds after registration
        # rather than on the next idle heartbeat. The slow branch only runs
        # when nothing is outstanding, and only decides how long to nap.
        _LAUNCHED_WORKERS_WAKE.wait(0.1 if outstanding else 2.0)


def _ensure_launched_worker_reaper() -> None:
    """Start the process's reaper whenever no live reaper is running.

    The recorded thread object is not evidence that the thread is running: a
    thread that ended for any reason — an uncaught exception in the poll loop
    — leaves its object in the holder, and a start-once guard keyed to the
    object would then silently stop reaping for the life of the process.
    """
    with _LAUNCHED_WORKERS_LOCK:
        recorded = _LAUNCHED_WORKER_REAPER["thread"]
        if recorded is not None and recorded.is_alive():
            return
        thread = threading.Thread(
            target=_worker_reaper_loop,
            name="reckon-worker-reaper",
            daemon=True,
        )
        thread.start()
        _LAUNCHED_WORKER_REAPER["thread"] = thread


# The launched-worker set crosses a follower's in-place process reload in the
# environment, which a process image replacement preserves. A file would
# survive too, but a stale file from a replacement that never happened could
# be adopted by an unrelated later process; the env var dies with the process
# and only this handover consumes it. A pipe survives as well and offers
# nothing over an env var, and module state is the carrier the replacement
# destroys, which is the defect the handover exists to fix.
_LAUNCHED_WORKERS_HANDOVER_ENV = "RECKON_FOLLOWER_LAUNCHED_PIDS"


def _export_launched_workers_for_reexec() -> None:
    """Hand the outstanding launched pids to the follower's replacement image.

    A follower that adopts newly installed code replaces its own process image
    with ``os.execv``. The replacement keeps the pid and every parent-child
    relationship and destroys all threads and module state, so a reaper thread
    and the launched-worker set built before the swap vanish even though the
    process is still the parent of the children it launched. Those children
    can then never be collected by anyone: their parent is alive, so they are
    not reparented to the init process that would collect them, and the new
    image has forgotten them. The reloader already carries its reader
    checkpoint across the boundary in the environment, so the pids cross the
    same way, at the same moment. Only outstanding pids are exported: a
    follower that launched nothing hands over nothing and the new image starts
    clean.
    """
    with _LAUNCHED_WORKERS_LOCK:
        outstanding = sorted(_LAUNCHED_WORKERS)
        carried = {
            str(pid): dict(_LAUNCHED_WORKER_RUNS[pid])
            for pid in outstanding
            if pid in _LAUNCHED_WORKER_RUNS
        }
    if outstanding:
        os.environ[_LAUNCHED_WORKERS_HANDOVER_ENV] = json.dumps(
            {"pids": outstanding, "runs": carried}
        )
    else:
        os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, None)


def _adopt_launched_workers_from_reexec() -> None:
    """Register pids handed across a process image replacement, or start clean.

    The new follower image after an ``os.execv`` owns the same process, so the
    pids its previous image launched are still its children; registering them
    exactly as a launch would makes the reaper their owner again, because it
    is the same process and remains their parent. A carrier that is absent,
    empty or unparseable leaves the registry empty and does not raise, because
    a follower that refuses to start is worse than one that misses a reap.
    """
    raw = os.environ.pop(_LAUNCHED_WORKERS_HANDOVER_ENV, "")
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return
    # A bare pid list is the older carrier; the mapping is the current one.
    # Any other parsed type — a scalar, a string — is unparseable as a carrier
    # and starts the new image clean rather than raising from the iteration.
    if isinstance(payload, dict):
        pids = [int(pid) for pid in payload.get("pids") or ()]
        runs_carried = payload.get("runs") or {}
    elif isinstance(payload, (list, tuple)):
        pids = [int(pid) for pid in payload]
        runs_carried = {}
    else:
        return
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.update(pid for pid in pids)
        for pid in pids:
            entry = runs_carried.get(str(pid))
            if isinstance(entry, dict):
                _LAUNCHED_WORKER_RUNS[pid] = dict(entry)
        adopted = bool(_LAUNCHED_WORKERS)
        if adopted:
            _LAUNCHED_WORKERS_WAKE.set()
    if adopted:
        _ensure_launched_worker_reaper()


def _launched_prior_session(plan: _backends.LaunchPlan | None) -> str | None:
    """Return the prior session this launch carried into argv, or None.

    A backend's ``session_reuse`` setting says the lane permits a run to
    continue an earlier session; only the command line says whether this run
    did. The plan records the session it was handed, and the answer is read
    back off the launch plan argv so a plan naming a session in its command line
    does not carry is not reported as a resumption.
    """
    if plan is None:
        return None
    session = plan.resumed_session
    if not session:
        return None
    carried = str(session)
    if str(session) not in [str(token) for token in plan.argv]:
        return None
    return carried


class LaunchResolutionError(CrewError):
    """A backend command could not be resolved against the launching PATH."""


def _current_host_facts() -> Any:
    """Read placement once at the decision that consumes it."""
    from reckon import host

    return host.host_facts()


def _worker_shim_directory() -> Path:
    """Return the scheduler-shim directory from its owning module."""
    from reckon.nested_launch import shim_directory

    return shim_directory()


def _worker_git_shim_directory() -> Path:
    """Return the git-shim directory from its owning module."""
    from reckon.worker_git_shim import worker_shim_directory

    return worker_shim_directory()


def _worker_host_line(facts: Any, run_directory: str | Path) -> str:
    """State the compute-host contract in one worker-prompt line."""
    if not facts.in_allocation:
        return ""
    node = facts.node or "unknown"
    job = facts.job_id or "unknown"
    scratch = "/tmp"  # noqa: S108 — the host probe's explicit scratch path
    tmp_clause = (
        f"{scratch} is node-local"
        if facts.tmp_is_node_local
        else f"{scratch} is not node-local"
    )
    return (
        f"HOST — ALLOCATION: node {node}; job {job}; {tmp_clause}; the work "
        "runs in place — do not open an srun or salloc step in this job or an "
        "sbatch into it; a separate partition job (for example betelgeuse or a "
        "*_debug partition) is submitted with RECKON_ALLOW_NESTED_LAUNCH=1 when "
        "the done-when names one; logs a later reader needs go under "
        f"{run_directory}."
    )


def launch_search_path(
    environment: Mapping[str, str] | None = None,
    *,
    facts: Any | None = None,
) -> str:
    """Return the effective worker PATH for one launch.

    The git shim is first on every worker's path, inside an allocation or out
    of one: it refuses a mutating verb aimed at a repository other than the
    run's worktree, which is a hazard wherever the worker runs. The scheduler
    shims follow it only inside an allocation, because there is no scheduler
    out of one to refuse.

    An inherited entry holding a reckon shim for any of those tools is dropped,
    whichever checkout it belongs to. A dispatch from a worktree or a copy of
    reckon inherits the main checkout's shims from the worker it runs in, and
    two checkouts' shims on one path resolve to each other rather than to the
    real tool.
    """
    from reckon.shim_lookup import holds_shim

    merged = {**os.environ, **(environment or {})}
    inherited = str(merged.get("PATH") or os.defpath)
    placement = _current_host_facts() if facts is None else facts
    directories = [str(_worker_git_shim_directory())]
    if placement.in_allocation:
        directories.append(str(_worker_shim_directory()))
    resolved = {os.path.realpath(directory) for directory in directories}
    shimmed = sorted(
        {name for directory in directories for name in _shim_names(directory)}
    )
    inherited_entries = [
        entry
        for entry in inherited.split(os.pathsep)
        if entry
        and os.path.realpath(entry) not in resolved
        and not holds_shim(entry, shimmed)
    ]
    return os.pathsep.join([*directories, *inherited_entries])


def _shim_names(directory: str) -> list[str]:
    """The tool names the shim directory stands in for."""
    try:
        return [entry.name for entry in os.scandir(directory) if entry.is_file()]
    except OSError:
        return []


def _persisted_worker_environment(
    environment: Mapping[str, str] | None = None,
    *,
    facts: Any | None = None,
) -> dict[str, str]:
    """Return only the launch overlay safe to persist in a run record."""
    persisted = dict(environment or {})
    for key in (
        "RECKON_RUN_ID",
        "RECKON_MANIFEST",
        "RECKON_ATTEMPT_STARTED_AT",
    ):
        persisted.pop(key, None)
    headers = str(persisted.get("ANTHROPIC_CUSTOM_HEADERS") or "").splitlines()
    if (
        len(headers) >= 2
        and headers[-2].startswith("X-Reckon-Run-Id:")
        and headers[-1].startswith("X-Reckon-Session:")
    ):
        headers = headers[:-2]
        if headers:
            persisted["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
        else:
            persisted.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    placement = _current_host_facts() if facts is None else facts
    persisted["PATH"] = launch_search_path(environment, facts=placement)
    return persisted


# A worker's scratch lives and dies with its run. The host's temp root is
# node-local and shared by every session on the machine, so a run that writes
# there unattended leaves entries nothing owns and nothing removes. Each run is
# therefore handed a private directory beneath a reckon-owned root, named for
# its run id, and pointed at it by TMPDIR; promotion and discard remove that
# directory. Release also removes direct siblings bearing that run's timestamp
# stem, because worker-created basetemps may sit beside TMPDIR. Other run ids
# remain outside the release's reach.
WORKER_SCRATCH_ROOT_ENV = "RECKON_WORKER_SCRATCH_ROOT"
WORKER_SCRATCH_ROOT_NAME = "reckon-crew-scratch"
# The node's own tmp, pinned rather than taken from TMPDIR, the way the fleet
# supervisor pins the runtime sockets it owns: the allocator may point TMPDIR at
# shared storage, and a scratch root that followed TMPDIR would then place every
# run's scratch on GPFS and — worse — resolve differently in the dispatcher and
# in the promotion or discard that later removes it, so the removal would miss
# and the directory would leak.
WORKER_SCRATCH_ROOT_DEFAULT = "/tmp"  # noqa: S108 - the node's own tmp, never shared
# The size above which a run's scratch is reported at the moment it lands, so a
# run that wrote gigabytes is visible on its own ledger row rather than only to
# whoever later finds the temp directory full. Node-local disk is a scarce shared
# allocation, so the figure is a report and never a refusal: a run that legitimately
# needs more space is not stopped, it is named.
WORKER_SCRATCH_BUDGET_BYTES = 2 * 1024**3


class TmpHeadroomError(CrewError):
    """The scratch filesystem cannot admit another worker."""

    def __init__(self, free_bytes: int, floor_bytes: int):
        self.free_bytes = free_bytes
        self.floor_bytes = floor_bytes
        super().__init__(
            f"tmp-headroom-refusal: {free_bytes} bytes free, floor "
            f"{floor_bytes} bytes; run `reckon crew gc --project <project>` "
            "to inspect reclaimable worker scratch"
        )


def require_worker_scratch_headroom(config: Mapping[str, Any]) -> dict[str, int]:
    """Check the filesystem that will hold run scratch before worktree creation."""
    root = worker_scratch_root()
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stat = os.statvfs(probe)
    free = stat.f_bavail * stat.f_frsize
    total = stat.f_blocks * stat.f_frsize
    worktree = config.get("worktree") or {}
    floor = max(
        int(worktree.get("scratch_min_free_bytes", 5 * 1024**3)),
        total * int(worktree.get("scratch_min_free_pct", 10)) // 100,
    )
    if free < floor:
        raise TmpHeadroomError(free, floor)
    return {"free_bytes": free, "floor_bytes": floor}


def worker_scratch_root() -> Path:
    """The node-local root every run's scratch directory sits beneath.

    ``RECKON_WORKER_SCRATCH_ROOT`` overrides it, so a test can synthesise a root
    without writing to the host's real temp directory and a caller can place the
    whole fleet's scratch somewhere it would rather own. Without an override the
    root is pinned to the node's own tmp rather than read from ``TMPDIR``: a
    shared-storage ``TMPDIR`` would otherwise move every run's scratch onto GPFS,
    and a ``TMPDIR`` that differs between the dispatcher and the promotion or
    discard that removes the directory would make each resolve a different root,
    so the removal would report the directory absent and leak it.
    """
    override = os.environ.get(WORKER_SCRATCH_ROOT_ENV)
    if override:
        return Path(override)
    return Path(WORKER_SCRATCH_ROOT_DEFAULT) / WORKER_SCRATCH_ROOT_NAME


def worker_scratch_dir(run_id: str) -> Path:
    """The scratch directory one run owns, named for its run id."""
    return worker_scratch_root() / str(run_id)


def tree_size_bytes(path: Path, *, limit: int | None = None) -> int:
    """Total bytes of the regular files beneath ``path``, symlinks excluded.

    Taken while the directory still exists, because the size is recorded on the
    run's terminal row at the moment the directory dies and there is no second
    chance to measure it afterwards. A file that cannot be stat'd is skipped
    rather than failing the walk: a size a few bytes short still names the tree
    that filled the disk, while raising here would leave the directory in place.
    ``limit`` bounds the walk, so a caller measuring a directory it does not own
    on a shared temp root cannot be made to traverse a whole corpus; the sum is
    then a floor rather than an exact figure, which is all a report needs.
    """
    total = 0
    seen = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if limit is not None and seen > limit:
                return total
            candidate = Path(root) / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def ensure_worker_scratch(run_id: str) -> Path:
    """Create the run's scratch directory if it is absent and return it."""
    path = worker_scratch_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _removable_scratch_target(
    run_id: str, recorded_path: str | os.PathLike[str] | None
) -> tuple[Path | None, str]:
    """The directory a run may remove, or ``None`` and the reason it may not.

    Every check the removal depends on lives here so it applies whatever path a
    caller offers. A ``recorded_path`` gets no more reach than the path computed
    from the run id: it must resolve to a direct child of the resolved scratch
    root, its final component must equal the validated run id, and it must be a
    real directory rather than a symlink. A recorded path is therefore only ever
    allowed to equal ``<scratch root>/<run id>`` beneath the root that resolves
    now, so it narrows what may be removed rather than widening it. A record made
    under a scratch root that has since moved names a directory no longer a child
    of the resolved root; it is withheld and left in place, never removed.
    """
    name = str(run_id or "").strip()
    # A run id is a single path component and is never a parent reference. A
    # separator, an absolute path, or a bare "." / ".." names no scratch
    # directory this function may remove: ".." would otherwise resolve to the
    # temp directory itself and be removed.
    if not name or name in {".", ".."} or Path(name).name != name:
        return None, "run id names no scratch directory"
    root = worker_scratch_root()
    path = Path(recorded_path) if recorded_path else root / name
    # Checked before the resolve below, because a symlink resolves to its
    # target and would let a recorded path point anywhere under the root.
    if path.is_symlink():
        return None, "scratch path is a symlink"
    resolved_root = root.resolve()
    try:
        resolved_path = path.resolve()
    except OSError:
        resolved_path = path
    if resolved_path.parent != resolved_root or resolved_path.name != name:
        return None, "scratch path is not the directory this run owns"
    if not path.is_dir():
        return None, "scratch directory is no longer present"
    return path, ""


def remove_worker_scratch(
    run_id: str,
    *,
    recorded_path: str | os.PathLike[str] | None = None,
    budget_bytes: int | None = None,
) -> dict[str, Any]:
    """Remove a run's scratch directories, printing each path and size.

    The path removed is ``<scratch root>/<run id>``, or the ``recorded_path`` a
    dispatch recorded when it created the directory — which is accepted only
    when it names exactly that directory beneath the root that resolves now (see
    ``_removable_scratch_target``), so a record whose scratch field names a
    different run's directory removes nothing. A record made under a scratch
    root that has since moved is therefore withheld and left in place, never
    removed. An absent directory is reported rather than raised: a run whose
    scratch was already reclaimed has nothing left to remove.

    Direct siblings whose names begin with the run's timestamp stem are also
    removed. The size of each removed directory is retained in
    ``scratch_removed_paths``, while ``scratch_bytes`` reports their sum. A
    total above ``budget_bytes`` adds a warning but never refuses removal.
    """
    name = str(run_id or "").strip()
    attempted = (
        Path(recorded_path)
        if recorded_path
        else (worker_scratch_root() / name if name else None)
    )
    path, reason = _removable_scratch_target(run_id, recorded_path)
    result: dict[str, Any] = {
        "scratch_removed": False,
        "scratch_path": str(path or attempted) if (path or attempted) else None,
        "scratch_withheld": reason,
        "scratch_bytes": None,
        "scratch_removed_paths": [],
    }
    if path is None and reason != "scratch directory is no longer present":
        return result
    targets = [path] if path is not None else []
    # A worker may create siblings named for the timestamp stem instead of
    # placing every temporary file below TMPDIR. That stem is unique to the
    # run; only direct, real directories with the same stem are in its reach.
    stem = re.match(r"^(r-\d{8}T\d{12})(?:-|$)", name)
    root = worker_scratch_root()
    if stem and root.is_dir():
        targets.extend(
            child
            for child in root.iterdir()
            if child != path
            and child.name.startswith(stem.group(1))
            and child.is_dir()
            and not child.is_symlink()
        )
    if not targets:
        return result
    sizes = {target: tree_size_bytes(target) for target in targets}
    result["scratch_bytes"] = sum(sizes.values())
    if budget_bytes is not None and result["scratch_bytes"] > budget_bytes:
        result["scratch_warning"] = (
            f"run scratch for {name} is {result['scratch_bytes']} bytes, "
            f"above the {budget_bytes} byte budget"
        )
        print(
            f"warning: run scratch for {name} is {result['scratch_bytes']} bytes, "
            f"above the {budget_bytes} byte budget"
        )
    failures = []
    for target in targets:
        size = sizes[target]
        print(f"removing worker scratch directory {target} ({size} bytes)")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            failures.append(f"{target}: {exc}")
            continue
        result["scratch_removed_paths"].append({"path": str(target), "bytes": size})
        print(f"removed worker scratch directory {target}")
    result["scratch_removed"] = not failures
    result["scratch_withheld"] = "; ".join(failures)
    return result


def _worker_runtime_environment(
    environment: Mapping[str, str] | None,
    *,
    run_id: str,
    manifest_path: str,
    attempt_started_at: str,
    coordinator_session: str,
    claude_headers: bool,
) -> dict[str, str]:
    """Add one attempt's identity to the environment inherited by its worker."""
    runtime = dict(environment or {})
    runtime.update(
        {
            "RECKON_RUN_ID": run_id,
            "RECKON_MANIFEST": manifest_path,
            "RECKON_ATTEMPT_STARTED_AT": attempt_started_at,
        }
    )
    if run_id:
        runtime["TMPDIR"] = str(ensure_worker_scratch(run_id))
    if claude_headers:
        inherited = str(
            runtime.get("ANTHROPIC_CUSTOM_HEADERS")
            or os.environ.get("ANTHROPIC_CUSTOM_HEADERS")
            or ""
        ).rstrip("\n")
        attribution = (
            f"X-Reckon-Run-Id: {run_id}\nX-Reckon-Session: {coordinator_session}"
        )
        runtime["ANTHROPIC_CUSTOM_HEADERS"] = (
            f"{inherited}\n{attribution}" if inherited else attribution
        )
    return runtime


def _worker_runtime_plan(
    plan: _backends.LaunchPlan,
    *,
    run_id: str,
    manifest_path: str,
    attempt_started_at: str,
    coordinator_session: str,
) -> _backends.LaunchPlan:
    """Return a launch plan carrying the current run and attempt identity."""
    return dataclasses.replace(
        plan,
        environment=_worker_runtime_environment(
            plan.environment,
            run_id=run_id,
            manifest_path=manifest_path,
            attempt_started_at=attempt_started_at,
            coordinator_session=coordinator_session,
            claude_headers=plan.dialect == "claude",
        ),
    )


def _worker_process_environment(
    environment: Mapping[str, str] | None, *, dialect: str
) -> dict[str, str]:
    """Merge inherited launch state while withholding Claude headers from Codex."""
    merged = {**os.environ, **(environment or {})}
    if dialect != "claude":
        merged.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    return merged


def harness_command_index(argv: Any) -> int:
    """Position of a launch argv's harness command.

    A fenced launch is ``<fence> <binds...> -- <harness> ...``, so the harness is
    the first token behind the fence's own ``--`` separator, searched for only
    after the fence element because the fence element is what distinguishes a
    composition from a bare argv. An argv whose first element is not the fence
    binary is not fenced, and its harness is its own first element at index 0.

    Read by the composed-plan rewrite and by the pre-flight resolution, so the
    two cannot disagree about which token is the backend.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return 0
    if Path(str(argv[0])).name != _backends.FENCE_BINARY:
        return 0
    if "--" not in argv[1:]:
        return 0
    return argv.index("--", 1) + 1


def _unresolved_backend_command(binary: str, searched: str) -> str:
    """The one refusal an unresolvable backend command produces."""
    return (
        f"backend command {binary!r} cannot be resolved on the PATH this "
        f"launch would search: {searched} — install it or add its directory "
        "to PATH, then retry; nothing has been launched"
    )


def preflight_launch_command(
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    fence: bool,
    facts: Any | None = None,
) -> str:
    """Resolve a backend's harness command before its plan is composed.

    Composing the plan seeds the run's own harness home, so a refusal that
    waited for the composed argv would leave a half-written run behind on a
    resume and a run directory behind on a dispatch. The harness's position is
    read through the same helper the composed-plan rewrite uses, so the
    pre-flight and that rewrite cannot disagree about which token is the
    backend. Returns the absolute command, or raises
    :class:`LaunchResolutionError`.

    A backend naming no command returns empty rather than refusing here, so the
    missing-command refusal stays where it belongs — at composition, which
    knows the launch kind.
    """
    from reckon.flight import expand_backend_environment

    command = str(backend.get("command") or "")
    if not command:
        return ""
    # The shape the composed launch will name: the harness alone when unfenced,
    # and behind the harness offset by the fence and its separator when fenced.
    # The binds the fence inserts between are irrelevant to the position, which
    # the helper reads from the fence element and the first separator after it.
    shape = [command] if not fence else [_backends.FENCE_BINARY, "--", command]
    binary = shape[harness_command_index(shape)]
    environment = expand_backend_environment(backend_name, backend)
    searched = launch_search_path(environment, facts=facts)
    resolved = shutil.which(binary, path=searched) if binary else None
    if not resolved:
        raise LaunchResolutionError(_unresolved_backend_command(binary, searched))
    return os.path.abspath(resolved)


def resolve_launch_executable(
    plan: _backends.LaunchPlan,
    *,
    environment: Mapping[str, str] | None = None,
    facts: Any | None = None,
) -> _backends.LaunchPlan:
    """Return the plan with the backend's own binary replaced by an absolute path.

    The launch inherits the PATH of whoever started it, so a watcher armed
    without the backend directory execs a bare name, dies at exec and leaves an
    empty stream that reads as a worker turn. Resolving at plan construction
    makes the launch either runnable or an explicit refusal, and the refusal
    names the binary and the PATH that was searched so the repair is a command
    rather than an investigation.

    A fenced composition's first element is the fence binary, not the backend:
    the harness is the command behind the fence's ``--`` separator. Resolving
    the first element would resolve the fence itself, and the fence binary is
    present on every host able to launch at all, so a missing backend would be
    accepted and the launch would die behind the fence with nothing naming why.
    The fence element is therefore never taken as the backend.

    ``environment`` is the overlay the launch will run with; absent, the launch's
    own environment is used, which is what every construction site passes.
    """
    selected_environment = plan.environment if environment is None else environment
    searched = launch_search_path(selected_environment, facts=facts)
    element = harness_command_index(plan.argv)
    binary = str(plan.argv[element]) if element < len(plan.argv) else ""
    resolved = shutil.which(binary, path=searched) if binary else None
    if not resolved:
        raise LaunchResolutionError(_unresolved_backend_command(binary, searched))
    # Absolute, not canonical: a launcher installed as ``bin/codex`` symlinked
    # to ``codex.js`` must still be exec'd under the name the launch was
    # configured with, because that name is how the command's dialect is
    # selected and how the run records what it ran.
    resolved = os.path.abspath(resolved)
    argv = list(plan.argv)
    argv[element] = resolved
    return dataclasses.replace(plan, argv=argv)


def apply_backend_placement(
    plan: _backends.LaunchPlan,
    backend: Mapping[str, Any],
    project: str | None = None,
) -> _backends.LaunchPlan:
    """Prefix an already-resolved launch with its backend's declared placement.

    The resolved argv is carried through unchanged behind the scheduler
    invocation, so the absolute executable, the environment, and the stdin,
    stdout and stderr paths the launch was built with are the ones that run.
    The scheduler executable is resolved against the same PATH the launch
    searches, because an unresolvable wrapper would die at exec and leave an
    empty stream that reads as a worker turn — the failure the launch resolution
    above exists to turn into an explicit refusal.

    A backend declaring no placement is returned untouched, which is what keeps
    an undeclared backend launching as a child of the coordinator exactly as it
    does today.
    """
    from reckon import flight

    placement = flight.placement_for(backend)
    if placement is None:
        return plan
    searched = launch_search_path(plan.environment)
    scheduler = str(placement["scheduler"])
    found = shutil.which(scheduler, path=searched)
    if not found:
        raise LaunchResolutionError(
            f"the declared placement names scheduler {scheduler!r}, which cannot "
            f"be resolved on the PATH this launch would search: {searched} — "
            "install it or add its directory to PATH, then retry; nothing has "
            "been launched"
        )
    from reckon.crew import placement as placement_module

    options = [str(item) for item in placement.get("options") or ()]
    # The one shared reservation, which any project's dispatch resolves: a
    # worker placed into it runs where it was sized to and is counted against
    # the roster that admits it.
    reservation = placement_module.read_reservation(project)
    if reservation and placement_module.reservation_alive(reservation):
        # The reservation is held and its job id is published, so this worker
        # runs inside it as an overlapping step rather than as an allocation of
        # its own. The id is resolved from the shared state rather than taken on
        # the command line, which is what makes one reservation shared by every
        # session instead of one per dispatcher.
        prefix = [
            os.path.abspath(found),
            *placement_module.step_prefix(str(reservation["job_id"]), options),
        ]
    else:
        prefix = [os.path.abspath(found), *options]
    return dataclasses.replace(plan, argv=[*prefix, *plan.argv])


def _placement_hold_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    """The hold's report as a dispatch payload carries it.

    A session that arms the reservation through a dispatch reads the run's
    record, not the ensure's return value, so what the hold states about the
    roster cap — its reach, and which projects' placed runs it counts right now
    — is copied onto the payload where that session reads it. The job id and
    the reason are carried beside it so a reader can tell an allocation this
    dispatch minted from one it joined.
    """
    return {
        "job_id": result.get("job_id"),
        "reason": result.get("reason"),
        "detail": result.get("detail"),
        "roster_reach": result.get("roster_reach"),
    }


def resolve_backend_placement(
    plan: _backends.LaunchPlan,
    backend: Mapping[str, Any],
    project: str | None = None,
    *,
    payload: dict[str, Any] | None = None,
) -> _backends.LaunchPlan:
    """Hold or join the shared reservation, then place the launch inside it.

    A dispatch declaring a placement runs inside the one allocation every worker
    of the host shares. The declared wrapping on its own prefixes a bare step
    client, which carries no job id and publishes nothing for the next dispatch
    to find, so every dispatch mints an allocation of its own; the ensure is
    reached instead so the allocation is created once, by whichever dispatch
    arrives first, and every later dispatch joins it. ``ensure_reservation``
    decides on the liveness probe under a cross-process lock and starts nothing
    when a live reservation is already held, which is what makes a dispatch that
    finds one — or that races another — join the allocation rather than mint a
    second beside it.

    The ensure's result is kept rather than discarded: when a ``payload`` is
    given, the hold's reach statement is carried on it under
    ``placement_reservation``, because dispatch is how most sessions arm the
    reservation and the run's record is the only report they read.

    A backend declaring no placement runs outside any reservation, so it is
    returned untouched: nothing is held and the scheduler is not asked after.
    """
    from reckon import flight

    if flight.placement_for(backend) is None:
        return plan
    from reckon.crew import placement as placement_module

    hold = placement_module.ensure_reservation(project=project)
    if payload is not None:
        payload["placement_reservation"] = _placement_hold_payload(hold)
    return apply_backend_placement(plan, backend, project)


# How long a declared job-id probe is given to answer, and how many times it is
# retried. The scheduler assigns the identifier as the job is admitted, so a
# probe read in the same instant as the spawn can precede the assignment.
_PLACEMENT_PROBE_TIMEOUT_SECONDS = 10
_PLACEMENT_PROBE_ATTEMPTS = 3


def placement_job_id(
    placement: Mapping[str, Any] | None,
    *,
    run_id: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str | None, str]:
    """Ask the scheduler which job a placed launch became.

    Returns the identifier and a status naming what happened, because a
    placement that reaches a job always identifies it while one that cannot
    must say so rather than record a fabricated id. ``{run}`` in the probe
    argument vector is replaced by the run id, which is how a probe addresses
    the job it is asking about without reckon knowing any scheduler's own
    vocabulary.
    """
    if not placement:
        return None, "no-placement"
    probe = placement.get("job_id_probe")
    if not probe:
        return None, "no-probe-declared"
    argv = [str(token).replace("{run}", run_id) for token in probe]
    run = runner or subprocess.run
    last = ""
    for attempt in range(_PLACEMENT_PROBE_ATTEMPTS):
        try:
            completed = run(
                argv,
                capture_output=True,
                text=True,
                timeout=_PLACEMENT_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last = f"probe failed to run — {exc}"
        else:
            for token in str(completed.stdout or "").split():
                if token.isdigit():
                    return token, "recorded"
            last = (
                "probe answered no identifier "
                f"(exit {completed.returncode})"
            )
        if attempt + 1 < _PLACEMENT_PROBE_ATTEMPTS:
            time.sleep(1.0)
    return None, last or "probe answered no identifier"


def assert_routable_backends_resolvable(
    project: str,
    config: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Refuse when any backend this project can route to has no executable.

    A watcher that cannot resolve a backend it may be asked to lift is a
    watcher that reads as armed and silently loses every park it lifts, so the
    check runs before the registration is taken rather than at the first lift.
    """
    resolved: list[dict[str, str]] = []
    from reckon import flight

    for name in sorted((config.get("backends") or {}), key=str):
        backend = (config.get("backends") or {})[name] or {}
        if backend.get("launch") != "cli":
            continue
        command = str(backend.get("command") or "")
        if not command:
            continue
        environment = flight.expand_backend_environment(str(name), backend)
        path = launch_search_path(environment)
        found = shutil.which(command, path=path)
        if not found:
            raise LaunchResolutionError(
                f"project {project!r} routes to backend {name!r} whose command "
                f"{command!r} cannot be resolved on the PATH the launch would "
                f"search: {path} — install it or add its directory to PATH, "
                "then arm the watcher again; it is not armed"
            )
        resolved.append(
            {
                "backend": str(name),
                "command": command,
                "executable": os.path.abspath(found),
            }
        )
    return resolved


def _spawn(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start one backend attempt, supervising continuations by run identity.

    Fresh dispatches already construct their supervisor before reaching this
    compatibility seam. The two callers that execute a later attempt name that
    attempt in its stream path: ``resume-*`` for a same-lane continuation and
    ``lane-change-*`` for a redispatch. Those attempts must leave through the
    same supervisor as the first one so the pointer names the supervisor and a
    worker exit is collected after the caller returns.

    Other callers retain the detached worker helper below. They are internal
    launch probes with no continuation identity; treating an arbitrary stream
    filename as a run attempt would make its parent directory into a run by
    accident.
    """
    _require_fleet_gate_open()
    stream_name = Path(log_path).name
    if stream_name.startswith(("resume-", "lane-change-")):
        directory = Path(log_path).parent
        record = read_pointer(directory.name)
        worktree = str(record.get("worktree") or "")
        return supervised_launch(
            plan,
            run_directory=directory,
            repo_root=Path(str(record.get("repo") or directory)),
            worktree=Path(worktree) if worktree else directory,
            log_path=Path(log_path),
            stderr_path=Path(stderr_path),
            prompt_path=Path(prompt_path),
        )
    return _spawn_detached_worker(
        plan,
        log_path=log_path,
        stderr_path=stderr_path,
        prompt_path=prompt_path,
    )


# bwrap refuses a launch whose read-only bind source cannot be found, naming
# the source in its refusal — one phrase when the source is gone before the
# mount is attempted, another when the kernel refuses it. The fence composes
# those binds from the paths that exist when it is built, and a writer that
# replaces a file by rename leaves the name missing for an instant, so a spawn
# landing in that instant dies before the worker starts while a launch a moment
# later would have begun normally. A spawn that dies this way is retried rather
# than recorded as a launch failure.
_VANISHED_BIND_REFUSAL_PHRASES = ("Can't bind mount", "Can't find source path")

# How long a freshly spawned worker is given to prove it started, and how often
# its exit is looked for inside that window. bwrap refuses a missing bind source
# before it execs the harness, so the window only has to cover bwrap's own
# startup — but that startup competes with every other process on the host, so
# the window is sized for a loaded host rather than for the refusal itself: a
# child still running at the end of it is a launch that began.
SANDBOX_STARTUP_WINDOW_SECONDS = 0.5
VANISHED_BIND_STARTUP_POLL_SECONDS = 0.01


def _read_only_bind_sources(argv: Iterable[str]) -> list[str]:
    """The read-only bind sources a launch argv would mount."""
    words = list(argv)
    sources: list[str] = []
    for index, word in enumerate(words):
        if str(word) == "--ro-bind" and index + 2 < len(words):
            sources.append(str(words[index + 1]))
    return sources


def _refusal_names_a_bind_source(refusal: str, sources: Iterable[str]) -> bool:
    """Whether bwrap's refusal is the missing-source one for this launch."""
    if not any(phrase in refusal for phrase in _VANISHED_BIND_REFUSAL_PHRASES):
        return False
    return any(source and source in refusal for source in sources)


def _died_over_a_vanished_bind_source(
    process: subprocess.Popen,
    *,
    argv: Iterable[str],
    stderr_path: Path,
) -> bool:
    """Whether a just-spawned worker already died over a missing bind source.

    The window a rename leaves is momentary, so this only answers true for the
    failure that window causes: the child exited inside the startup poll, and
    bwrap's refusal names one of the sources this launch asked it to mount. A
    child still running at the end of the window started, and a child that
    exited over anything else is the launch failure its caller records.
    """
    sources = _read_only_bind_sources(argv)
    if not sources:
        return False
    # A caller may hand the launch a stand-in rather than a real process; such a
    # launch cannot be observed starting, and the refusal is proved by the
    # child's own exit, so there is nothing the retry could key on.
    poll = getattr(process, "poll", None)
    if poll is None:
        return False
    deadline = time.monotonic() + SANDBOX_STARTUP_WINDOW_SECONDS
    while poll() is None and time.monotonic() < deadline:
        time.sleep(VANISHED_BIND_STARTUP_POLL_SECONDS)
    if poll() is None or process.returncode == 0:
        return False
    try:
        refusal = stderr_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _refusal_names_a_bind_source(refusal, sources)


def _spawn_worker_retrying_a_vanished_bind(
    attempt: Callable[[], subprocess.Popen],
    *,
    argv: Iterable[str],
    stderr_path: Path,
) -> subprocess.Popen:
    """Spawn a worker, retrying while the fence's own bind source is missing.

    The retry is inside one attempt: a worker that starts on a later try is the
    attempt that launched, so nothing is recorded as a failure for the tries a
    momentary rename defeated. The tries are bounded by the constants the
    composition's own wait uses. A spawn that failed for any other reason is
    returned at once for its caller to record as the launch failure it is.
    """
    process = attempt()
    tries = 1
    limit = max(1, _backends.PROTECTED_BIND_WAIT_ATTEMPTS)
    while tries < limit and _died_over_a_vanished_bind_source(
        process, argv=argv, stderr_path=stderr_path
    ):
        # The dead try is reaped here so it cannot be mistaken later for the
        # worker's own exit by whoever collects the run's children.
        process.wait()
        time.sleep(_backends.PROTECTED_BIND_WAIT_INTERVAL_SECONDS)
        tries += 1
        process = attempt()
    return process


def _spawn_detached_worker(
    plan: _backends.LaunchPlan,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start a backend process detached, with its event stream landing on disk.

    The prompt is fed from a file rather than a pipe so the caller never blocks
    on a full pipe buffer, and so the exact prompt stays recoverable beside the
    stream it produced. ``start_new_session`` detaches the worker from the
    caller's process group: a dispatching agent that ends its turn must not take
    its workers down with it.

    Every launched pid is registered with the reaper, which owns the wait the
    caller will never get around to making. The worker is still the caller's
    child — it has to be, for the pid to mean anything — but it is a child the
    caller does not owe a wait on.
    """
    argv = [str(word) for word in plan.argv]

    def attempt() -> subprocess.Popen:
        with (
            open(prompt_path, "rb") as stdin,
            open(log_path, "wb") as stdout,
            open(stderr_path, "wb") as stderr,
        ):
            return subprocess.Popen(
                argv,
                cwd=plan.cwd,
                env=_worker_process_environment(
                    plan.environment,
                    dialect=plan.dialect,
                ),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )

    process = _spawn_worker_retrying_a_vanished_bind(
        attempt, argv=argv, stderr_path=Path(stderr_path)
    )
    launched = _launched_worker_record(plan, log_path, stderr_path)
    # A poll of a child that has already exited collects its status, and the
    # retry's startup check polls the child. A worker that died inside that
    # window is therefore no longer waitable: registering the pid would hand
    # the reaper a child it cannot wait, and both the failure record and the
    # registered pid would be lost. The exit the check collected is recorded
    # here exactly as a reap would record it.
    exit_status = getattr(process, "returncode", None)
    if exit_status is not None:
        if launched is not None:
            _record_launch_failure(launched, exit_status=exit_status)
        return process.pid
    with _LAUNCHED_WORKERS_LOCK:
        _LAUNCHED_WORKERS.add(process.pid)
        if launched is not None:
            _LAUNCHED_WORKER_RUNS[process.pid] = launched
    _LAUNCHED_WORKERS_WAKE.set()
    _ensure_launched_worker_reaper()
    return process.pid


# ── The per-run supervisor ──────────────────────────────────────────────────
#
# Dispatch must return as soon as a worker exists, but two things must happen
# after it returns and neither can be left to a process that is gone: the
# boundary tree snapshot — a walk whose cost grows with the repository's
# worktree count — and collecting the worker's exit, which a reparented worker
# hands to init and reckon never sees. Both move to one small process, started
# per run in its own session, that outlives the dispatch command. Dispatch
# writes the run's pointer naming that process and exits; the supervisor takes
# the snapshot, spawns the worker, waits on it and writes the exit to the run
# directory, then exits itself.
#
# The supervisor never writes the pointer and never creates the run directory.
# Everything it produces lands beside the worker's stream under the run
# directory, so a discard that removes only the pointer leaves the exit record
# findable, and a discard that removes the whole directory drops the write
# rather than bringing the run back.
SUPERVISOR_SPEC_NAME = "supervisor.json"
TREE_SNAPSHOT_NAME = "tree-snapshot.json"
WORKER_RECORD_NAME = "worker.json"
EXIT_RECORD_NAME = "exit.json"
ATTEMPT_RECORD_NAME = "attempt.json"

# The argv token that runs this module as the per-run supervisor. Named with
# underscores so it can never collide with a real crew subcommand.
SUPERVISOR_ENTRY = "__supervise__"


def _supervisor_write(path: Path, payload: Mapping[str, Any]) -> bool:
    """Write JSON into a run directory without ever creating that directory.

    The run directory is the supervisor's only durable home, and it may be
    removed while the supervisor is mid-run — a discard takes it. A write that
    recreated the directory would bring a discarded run back into existence, so
    a write whose directory has gone is dropped and reported by its return
    value rather than by raising.
    """
    if not path.parent.is_dir():
        return False
    try:
        _store.write_json_atomically(path, payload, create_parents=False)
    except OSError:
        return False
    return True


def _stream_record_facts(paths: Iterable[Path]) -> tuple[int, Any]:
    """Count a run's stream records and name the newest one's type.

    Streams arrive newest write first, so the newest record is the last
    parseable line of the first stream that holds any. A line that is not a
    JSON object is not a record, so a truncated tail cannot masquerade as one.
    """
    total = 0
    newest_type: Any = None
    for path in paths:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        count = 0
        last_type: Any = None
        with handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                count += 1
                last_type = event.get("type")
        total += count
        if count and newest_type is None:
            newest_type = last_type
    return total, newest_type


def _attempt_artifact_path(run_directory: Path, name: str, attempt: int) -> Path:
    """Return the immutable path carrying one attempt's worker or exit record."""
    return run_directory / f"attempt-{attempt}-{name}"


def _prepare_attempt_records(
    run_directory: Path,
    *,
    run_id: str,
    attempt: int,
    attempt_kind: str,
    attempt_started_at: str,
) -> None:
    """Publish current attempt identity and retire prior canonical records.

    The immutable attempt files retain every worker and exit receipt. The two
    canonical names remain the classifier's current-attempt surface, so they
    are cleared before a new supervisor can start. Publishing the marker first
    prevents a late exit from the previous supervisor from reclaiming those
    canonical names while the replacement is launching.
    """
    _supervisor_write(
        run_directory / ATTEMPT_RECORD_NAME,
        {
            "run_id": run_id,
            "attempt": attempt,
            "attempt_kind": attempt_kind,
            "attempt_started_at": attempt_started_at,
        },
    )
    for name in (WORKER_RECORD_NAME, EXIT_RECORD_NAME):
        current = run_directory / name
        try:
            payload = json.loads(current.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            archived = dict(payload)
            try:
                recorded_attempt = int(archived.get("attempt") or attempt - 1)
            except (TypeError, ValueError):
                recorded_attempt = max(1, attempt - 1)
            archived["attempt"] = recorded_attempt
            _supervisor_write(
                _attempt_artifact_path(run_directory, name, recorded_attempt),
                archived,
            )
        current.unlink(missing_ok=True)


def _attempt_is_current(run_directory: Path, attempt: int) -> bool:
    """Whether the run directory still names this supervisor's attempt."""
    try:
        marker = json.loads(
            (run_directory / ATTEMPT_RECORD_NAME).read_text(encoding="utf-8")
        )
        return isinstance(marker, Mapping) and int(marker.get("attempt")) == attempt
    except (OSError, TypeError, ValueError):
        return False


def _write_attempt_artifact(
    run_directory: Path,
    name: str,
    payload: Mapping[str, Any],
    *,
    attempt: int,
) -> bool:
    """Write an immutable receipt and expose it only while its attempt is current."""
    record = {**payload, "attempt": attempt}
    written = _supervisor_write(
        _attempt_artifact_path(run_directory, name, attempt), record
    )
    if _attempt_is_current(run_directory, attempt):
        written = _supervisor_write(run_directory / name, record) and written
    return written


# The environment variables the supervisor must inherit to find the run it was
# asked to hold. A per-run supervisor resolves the run through reckon's crew
# home, so one started with the fleet batch step's own environment reads the
# node's default home, finds no run, and exits at once -- while the dispatch
# that asked for it records a live pid and returns success. Carrying these
# values in the spec is what lets the batch step start the supervisor under the
# dispatcher's configuration rather than its own.
#
#   RECKON_HOME           the crew home: live pointers, manifests, worktrees
#   RECKON_STATE_ROOT     the state root the crew home resolves under
#   RECKON_MOUNTS_PATH    the mounts.json naming each project's docs tree
#   RECKON_FLIGHT_CONFIG  the routing and lane configuration
#   RECKON_RUN_STORE      the durable run store's database path
CREW_STATE_ENVIRONMENT = (
    "RECKON_HOME",
    "RECKON_STATE_ROOT",
    "RECKON_MOUNTS_PATH",
    "RECKON_FLIGHT_CONFIG",
    "RECKON_RUN_STORE",
)

# How long dispatch waits, after the supervisor has been started, to confirm it
# is still running before reporting the run directory's pid as a live worker. A
# supervisor that cannot find its run exits within milliseconds of starting, so
# a short window tells a supervisor that reached its run from one that did not
# without holding a healthy dispatch open.
SUPERVISOR_SURVIVAL_SECONDS = 2.0
SUPERVISOR_SURVIVAL_POLL_SECONDS = 0.05


def _carried_crew_environment() -> dict[str, str]:
    """The dispatcher's values of every variable that relocates crew state.

    Only variables that are set are carried: an unset one must arrive at the
    supervisor as absent rather than as an empty string, which resolves
    differently from absent in every reader that falls back on a default.
    """
    return {
        name: os.environ[name]
        for name in CREW_STATE_ENVIRONMENT
        if os.environ.get(name)
    }


def _supervisor_exit_detail(run_directory: Path) -> str | None:
    """The detail from the supervisor's exit record, or None while none exists.

    Each attempt clears the canonical exit record before its supervisor starts,
    so the one read here belongs to the supervisor this dispatch just started
    rather than to a predecessor's.
    """
    try:
        record = json.loads(
            (run_directory / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    detail = str(record.get("detail") or "").strip()
    return detail or "the supervisor exited without recording a detail"


def _supervisor_launched_worker(run_directory: Path) -> bool:
    """Whether the supervisor's exit record names a worker it spawned.

    A supervisor that reached its run spawns the worker and writes the exit
    record with the worker's pid; a launch failure or a pre-spawn stop writes
    the same record with no worker pid and a detail. So a record naming a worker
    is the receipt of a launch that happened, whatever the worker then did. Each
    attempt clears the canonical record before its supervisor starts, so the one
    read here belongs to the supervisor this dispatch just started.
    """
    try:
        record = json.loads(
            (run_directory / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if not isinstance(record, Mapping):
        return False
    return record.get("worker_pid") is not None


def _confirm_supervisor_survived(pid: int, run_directory: Path, run_id: str) -> str:
    """Confirm the supervisor reached its run, and name the run on success.

    A dispatch that launched a worker must never be refused, so a supervisor
    whose exit record names the worker it spawned counts as having reached its
    run: the launch happened and its outcome belongs to the worker, not to the
    dispatch. That is the case a fast launch produces — the supervisor scans,
    spawns, collects and records inside this window — and it must return, not
    refuse.

    The refusal is reserved for a supervisor that exits inside the window
    without such a record: one that could not find its run, because it started
    under the wrong crew home, or one whose spawn failed. The batch step
    acknowledges a spawn as soon as the child exists, so a dispatch that
    returned such a pid would report a live worker that never started. The
    supervisor writes its exit record before it ends and ``process_alive`` reads
    a zombie as dead, so a short bounded wait tells a supervisor that reached
    its run from one that did not.
    """
    deadline = time.monotonic() + SUPERVISOR_SURVIVAL_SECONDS
    while True:
        if _supervisor_launched_worker(run_directory):
            return run_id
        detail = _supervisor_exit_detail(run_directory)
        if detail is not None:
            raise CrewError(
                f"the supervisor for {run_id} exited within "
                f"{SUPERVISOR_SURVIVAL_SECONDS:g}s of starting: {detail}"
            )
        if process_alive(pid) is False:
            raise CrewError(
                f"the supervisor for {run_id} exited within "
                f"{SUPERVISOR_SURVIVAL_SECONDS:g}s of starting without writing "
                "an exit record"
            )
        if time.monotonic() >= deadline:
            return run_id
        time.sleep(SUPERVISOR_SURVIVAL_POLL_SECONDS)


def _supervisor_spec(
    *,
    run_id: str,
    run_directory: Path,
    repo_root: Path,
    worktree: Path,
    plan: _backends.LaunchPlan,
    fenced: bool = False,
    prompt_path: Path,
    log_path: Path,
    stderr_path: Path,
    facts: Any | None = None,
    attempt: int = 1,
    attempt_kind: str = "dispatch",
    attempt_started_at: str = "",
) -> dict[str, Any]:
    """Describe everything the supervisor needs to take over the launch.

    The plan is written out in full — argv, working directory and environment —
    because the supervisor is a fresh process that never sees the resolved
    launch plan otherwise. Everything else names a path the supervisor writes
    to or reads from.
    """
    return {
        "run_id": run_id,
        "run_directory": str(run_directory),
        "repo": str(repo_root),
        # The variables that relocate crew state, carried so the batch step
        # starts this supervisor under the dispatcher's home rather than its own.
        "environment": _carried_crew_environment(),
        "worktree": str(worktree),
        # Whether this launch was composed inside the fence, carried so the
        # supervisor's boundary baseline reads the same two trees the dispatch
        # record names.
        "fenced": fenced,
        "prompt_path": str(prompt_path),
        "log_path": str(log_path),
        "stderr_path": str(stderr_path),
        "attempt": attempt,
        "attempt_kind": attempt_kind,
        "attempt_started_at": attempt_started_at or _utc_now(),
        # The argv the fleet's batch step runs verbatim when it is asked to
        # spawn this run, carried in the spec so the batch step stays free of
        # any knowledge of reckon's module layout.
        "argv": _supervisor_argv(spec_path=run_directory / SUPERVISOR_SPEC_NAME),
        "plan": {
            "argv": list(plan.argv),
            "cwd": plan.cwd,
            "environment": _persisted_worker_environment(plan.environment, facts=facts),
            "dialect": plan.dialect,
            "backend": plan.backend,
        },
    }


def _supervisor_argv(*, spec_path: Path) -> list[str]:
    """The argv that starts the per-run supervisor for one spec path.

    Kept in one place because the same vector is both forked here and written
    into the spec for the fleet's batch step to run verbatim, and the two must
    agree or a fleet spawn would start something other than what this process
    would have forked.

    The entry module is ``reckon.crew.supervisor_main`` and not dispatch itself:
    the crew package imports dispatch when it loads, so running dispatch as the
    main module re-executes an already-imported module and opens the
    supervisor's stderr with runpy's duplicate-import warning. Nothing imports
    the entry module, so the launch is quiet.
    """
    return [
        sys.executable,
        "-m",
        "reckon.crew.supervisor_main",
        SUPERVISOR_ENTRY,
        "--spec",
        str(spec_path),
    ]


# ── Launching through the fleet's batch step ────────────────────────────────
#
# On the SLURM fleet node every session runs as a step of one allocation, and a
# ``setsid``-detached child is reaped when its step ends — so a supervisor
# forked by a session dies with the session and takes its worker with it. The
# only process tree that outlives every step is the allocation's batch step,
# which reads one request per line from a FIFO in the runtime directory the
# fleet record publishes. When this process runs inside that allocation, the
# spawn is therefore delegated to the batch step through the FIFO rather than
# forked here, and the pid the batch step acknowledges is recorded on the run
# exactly as a forked supervisor's pid would be.
FLEET_RECORD_PATH_ENV = "RECKON_FLEET_RECORD"
FLEET_SPAWN_ENV = "RECKON_FLEET_SPAWN"
FLEET_FIFO_NAME = REQUEST_FIFO_NAME
FLEET_SPAWN_ACK_NAME = "spawned.json"
FLEET_SPAWN_ACK_BOUND_SECONDS = 10.0
FLEET_REQUEST_POLL_SECONDS = 0.02


def _fleet_record_path() -> Path:
    """Where the fleet record that publishes the batch step's location lives."""
    override = os.environ.get(FLEET_RECORD_PATH_ENV, "").strip()
    if override:
        return Path(override)
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "fleet" / "record.json"


def _read_fleet_record() -> dict[str, Any] | None:
    """The published fleet record, or None when none is readable."""
    try:
        payload = json.loads(_fleet_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _fleet_spawn_enabled() -> bool:
    """Whether a launch may be delegated to the fleet's batch step.

    The lane is opt-in rather than inferred from the record alone. The batch
    step's ``spawn`` verb and this half land separately, and this workstation's
    fleet node publishes a record for every session running on it — so keying
    on the record would route every dispatch on the node into a FIFO whose
    reader may not yet know the verb, turning an ordinary dispatch into a
    ten-second refusal. The allocation that carries the verb enables the lane;
    everywhere else, and in every test that does not opt in, launches fork as
    they always have.
    """
    return os.environ.get(FLEET_SPAWN_ENV, "").strip().lower() in {
        "on",
        "1",
        "true",
        "yes",
    }


def _runs_inside_fleet_allocation(record: Mapping[str, Any]) -> bool:
    """Whether this process runs inside the allocation the record names.

    Two local proofs, either sufficient: the runtime directory the record
    publishes is the one this process was given, which only the allocation
    itself sets, or the job id matches this process's SLURM job. Anything else
    — no record, a stale record, a session on the login node — is a dispatch
    that forks as it always has.
    """
    runtime = str(record.get("runtime_dir") or "").strip()
    if runtime and os.environ.get("XDG_RUNTIME_DIR", "").strip() == runtime:
        return True
    job_id = str(record.get("job_id") or "").strip()
    return bool(job_id) and os.environ.get("SLURM_JOB_ID", "").strip() == job_id


def _fleet_runtime_dir(record: Mapping[str, Any]) -> Path | None:
    runtime = str(record.get("runtime_dir") or "").strip()
    return Path(runtime) if runtime else None


def _watch_request_slug(project: str) -> str:
    """A request id for a project's producer, free of the line's separator.

    The spawn request is one line of space-separated fields, so a project name
    that carries a space would otherwise split the id and shift the spec path
    into the wrong field.
    """
    return re.sub(r"\s+", "-", project.strip()) or "project"


def _write_fleet_request(fifo: Path, line: bytes, deadline: float) -> None:
    """Write one request line to the batch step's FIFO within the deadline.

    No reader within the deadline is a refusal, not a wait; the non-blocking
    open with its retry lives in :func:`_open_request_fifo`.
    """
    descriptor = _open_request_fifo(fifo, deadline)
    try:
        os.write(descriptor, line)
    except OSError as exc:
        raise CrewError(
            f"the fleet's request FIFO {fifo} refused the request: {exc}"
        ) from exc
    finally:
        os.close(descriptor)


def _read_spawn_ack(ack_path: Path) -> int | None:
    """The pid the batch step acknowledged, or None while none is written."""
    try:
        payload = json.loads(ack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def _spawn_through_fleet(
    runtime_dir: Path, request_id: str, spec_path: Path, ack_path: Path
) -> int:
    """Ask the batch step to start one spec and return the acknowledged pid.

    One line of three space-separated fields — the word ``spawn``, the run id
    and the spec path — is written to the runtime directory's FIFO, and the
    batch step answers by writing the started pid to the run directory. A
    stale acknowledgement is removed first so a previous attempt's file can
    never stand in for this one's.
    """
    deadline = time.monotonic() + FLEET_SPAWN_ACK_BOUND_SECONDS
    ack_path.unlink(missing_ok=True)
    line = f"spawn {request_id} {spec_path}\n".encode()
    _write_fleet_request(runtime_dir / FLEET_FIFO_NAME, line, deadline)
    while time.monotonic() < deadline:
        pid = _read_spawn_ack(ack_path)
        if pid is not None:
            return pid
        time.sleep(FLEET_REQUEST_POLL_SECONDS)
    raise CrewError(
        f"the fleet's batch step did not acknowledge the spawn of {request_id} "
        f"within {FLEET_SPAWN_ACK_BOUND_SECONDS:g}s"
    )


def supervised_launch(
    plan: _backends.LaunchPlan,
    *,
    run_directory: Path,
    repo_root: Path,
    worktree: Path,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
) -> int:
    """Start a run's supervisor for an already-built plan and return its pid.

    A resumption and a review dispatch reach the supervisor through here rather
    than through an in-process ``_spawn`` call: a worker started inline is a
    child of whoever is sweeping, so on the fleet it dies with that step, and
    off it the run's exit is never recorded because nothing waits on it. The
    spec is written before the supervisor is asked for, in the same order the
    dispatch path writes it, so a fleet spawn always finds its stderr path on
    disk.
    """
    _require_fleet_gate_open()
    record = read_pointer(run_directory.name)
    attempt = int(record.get("attempt") or 1) + 1
    stream_name = Path(log_path).name
    attempt_kind = "lane-change" if stream_name.startswith("lane-change-") else "resume"
    attempt_started_at = _utc_now()
    _prepare_attempt_records(
        run_directory,
        run_id=run_directory.name,
        attempt=attempt,
        attempt_kind=attempt_kind,
        attempt_started_at=attempt_started_at,
    )
    spec_path = run_directory / SUPERVISOR_SPEC_NAME
    _write_json(
        spec_path,
        _supervisor_spec(
            run_id=run_directory.name,
            run_directory=run_directory,
            repo_root=repo_root,
            worktree=worktree,
            plan=plan,
            fenced=bool(record.get("fenced")),
            prompt_path=prompt_path,
            log_path=log_path,
            stderr_path=stderr_path,
            attempt=attempt,
            attempt_kind=attempt_kind,
            attempt_started_at=attempt_started_at,
        ),
    )
    return _start_supervisor(spec_path, run_directory, run_directory.name)


def _start_supervisor(spec_path: Path, run_directory: Path, run_id: str) -> int:
    """Start the per-run supervisor and return the pid that runs it.

    Off the fleet this starts the supervisor through a short-lived intermediate
    that puts it in a session of its own and exits at once, so the supervisor's
    parent is never the launching process and it outlives that launcher. Its pid
    is a process group ``crew stop`` can signal, since the worker is spawned
    inside that group. On the fleet node a spawned supervisor would be reaped
    with the session step that made it, so the same argv is handed to the
    allocation's batch step instead and the caller waits, bounded, for the pid
    the step acknowledges.
    """
    _require_fleet_gate_open()
    fleet = _read_fleet_record()
    if (
        _fleet_spawn_enabled()
        and fleet is not None
        and _runs_inside_fleet_allocation(fleet)
    ):
        runtime_dir = _fleet_runtime_dir(fleet)
        if runtime_dir is not None:
            pid = _spawn_through_fleet(
                runtime_dir,
                run_id,
                spec_path,
                run_directory / FLEET_SPAWN_ACK_NAME,
            )
            _confirm_supervisor_survived(pid, run_directory, run_id)
            return pid
    argv = _supervisor_argv(spec_path=spec_path)
    pid = _spawn_detached_supervisor(argv, run_directory / "supervisor.stderr.log")
    _confirm_supervisor_survived(pid, run_directory, run_id)
    return pid


def _spawn_detached_supervisor(argv: list[str], stderr_path: Path) -> int:
    """Start the supervisor under an intermediate that exits at once.

    A single ``Popen`` makes the supervisor the launcher's child, so the
    launcher's own end — a coordinator turn, a shell that returns — takes the
    supervisor with it. A short-lived intermediate runs here instead: it starts
    the supervisor in a session of its own, hands its pid back and exits, so the
    supervisor is reparented away from the launching process before it does any
    work. The returned pid leads its own session and process group — the group
    ``crew stop`` signals, with the worker spawned inside it.

    The intermediate is a fresh interpreter rather than an in-process ``fork``:
    the launcher may be threaded (the MCP server is one), where a forked child
    that runs Python is not safe. Every ``Popen`` here keeps ``close_fds=True``,
    so neither the supervisor nor the intermediate inherits the launcher's
    descriptors.
    """
    payload = json.dumps({"argv": list(argv), "stderr": str(stderr_path)})
    intermediate = subprocess.Popen(
        [sys.executable, "-c", _SUPERVISOR_LAUNCHER_SOURCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )
    stdout, stderr = intermediate.communicate(f"{payload}\n")
    reported = stdout.strip()
    if not reported:
        raise CrewError(
            "the supervisor's launcher exited without reporting a pid"
            f"{_intermediate_failure_detail(stderr, intermediate.returncode)}"
        )
    return int(reported.splitlines()[0])


def _intermediate_failure_detail(stderr: str, returncode: int | None) -> str:
    """The exit status and stderr tail that explain a silent intermediate.

    An intermediate that prints no pid failed before it could start a
    supervisor, and its own stderr — which a caller cannot see once this
    recursive launch returns — is the only account of why. The last few lines
    carry the exception or the refusal.
    """
    status = "unknown" if returncode is None else str(returncode)
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    tail = " | ".join(lines[-3:])
    if tail:
        return f" (exit status {status}; stderr: {tail})"
    return f" (exit status {status})"


# The intermediate: start the supervisor from the argv it is handed and report
# the pid, then exit. Kept as a source string because it is run by a fresh
# interpreter, which is what keeps the launcher's own process image — and any
# lock a threaded launcher holds — out of the child.
_SUPERVISOR_LAUNCHER_SOURCE = """
import json
import subprocess
import sys

payload = json.loads(sys.stdin.readline())
with open(payload["stderr"], "ab") as errors:
    process = subprocess.Popen(
        payload["argv"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=errors,
        start_new_session=True,
        close_fds=True,
    )
print(process.pid, flush=True)
"""


def _worker_default_signals() -> None:
    """Give the worker the default signal dispositions the supervisor changed.

    The supervisor installs its own handlers for SIGTERM and SIGHUP, and the
    worker is spawned into the supervisor's signal environment. The worker must
    end on the group signal that stops it rather than carry a handler the
    supervisor needed for itself, so it starts with the defaults its launch
    would otherwise have had. The mask is cleared as well: the spawn runs with
    those signals blocked so a stop inside the window cannot be missed, and the
    child inherits the mask across the fork, so a worker that kept it would
    never see the stop the group sends it.
    """
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGHUP})
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def _wait_status_from_returncode(returncode: int) -> int:
    """The wait status a ``Popen`` return code stands for.

    ``subprocess`` reports a signalled child as the negated signal number and an
    exited one as its code, while the supervisor's exit record is composed from
    the wait statuses ``os.waitpid`` returns. A status the startup poll
    collected is a ``Popen`` return code, so it is converted back at this
    boundary and the record is composed by the same path a reaped wait uses.
    """
    if returncode < 0:
        return -returncode
    return (returncode & 0xFF) << 8


class _WorkerPid(int):
    """A spawned worker's pid, carrying any exit its startup poll collected.

    The vanished-bind retry polls the worker it has just spawned, and a poll of
    a child that has exited collects its status. That status is then the only
    copy — the pid is no longer waitable — so it is carried rather than dropped:
    a supervisor waiting on such a pid gets ECHILD and writes an exit record
    naming neither a code nor a signal. The carrier stays an int because every
    other reader wants only the pid, and it must keep comparing and serialising
    as one.
    """

    collected_wait_status: int | None

    def __new__(cls, pid: int, collected_wait_status: int | None = None) -> _WorkerPid:
        worker_pid = super().__new__(cls, pid)
        worker_pid.collected_wait_status = collected_wait_status
        return worker_pid


def _supervisor_spawn_worker(spec: Mapping[str, Any]) -> int:
    """Spawn the worker inside the supervisor's own process group.

    The answer names the pid and, when the spawn retry's startup poll already
    collected the worker's exit, carries that exit — as an ``_WorkerPid`` — so
    the supervisor writes it instead of waiting on a pid that has been reaped.
    """
    plan = spec["plan"]
    record = read_pointer(str(spec["run_id"]))
    environment = _worker_runtime_environment(
        plan.get("environment") or {},
        run_id=str(spec["run_id"]),
        manifest_path=_recorded_manifest_path(record, str(spec["run_id"])),
        attempt_started_at=_utc_now(),
        coordinator_session=str(record.get("session") or ""),
        claude_headers=str(plan.get("dialect") or "") == "claude",
    )
    argv = [str(word) for word in plan["argv"]]

    def attempt() -> subprocess.Popen:
        with (
            open(str(spec["prompt_path"]), "rb") as stdin,
            open(str(spec["log_path"]), "wb") as stdout,
            open(str(spec["stderr_path"]), "wb") as stderr,
        ):
            return subprocess.Popen(
                argv,
                cwd=plan.get("cwd"),
                env=_worker_process_environment(
                    environment,
                    dialect=str(plan.get("dialect") or ""),
                ),
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=False,
                # The worker must start with the default signal dispositions
                # rather than the supervisor's own handlers, so a stop aimed at
                # the group ends the worker. This runs in the supervisor's
                # single-threaded child as it starts.
                preexec_fn=_worker_default_signals,  # noqa: PLW1509
            )

    process = _spawn_worker_retrying_a_vanished_bind(
        attempt,
        argv=argv,
        stderr_path=Path(str(spec["stderr_path"])),
    )
    # A poll of a child that has already exited collects its status, and the
    # retry's startup check is such a poll. The status is carried on the pid
    # from here: the child is no longer waitable, so a supervisor that only
    # held the pid would record neither its code nor its signal.
    collected = getattr(process, "returncode", None)
    return _WorkerPid(
        process.pid,
        None if collected is None else _wait_status_from_returncode(collected),
    )


def _plan_composed_the_fence(plan: _backends.LaunchPlan | None) -> bool:
    """Whether a launch plan's argv carries the fence wrapper.

    The fence is composed inside the plan builder and only for a cli launch
    that asked for it, so the fact is read from the composed argv rather than
    from the request to compose one. A launch that composed no fence — an
    in-harness lane, whose plan is absent here, or any lane whose argv is a
    bare harness — is not fenced, and its boundary check keeps the full scan of
    every registered worktree, because nothing stopped it writing elsewhere.
    """
    return plan is not None and bool(_harness_behind_the_fence(plan.argv))


def _boundary_snapshot_roots(
    repo_root: Path, worktree: Path | None, *, fenced: bool
) -> list[Path] | None:
    """The trees a boundary snapshot reads, or None for the full registry.

    A fenced run reads only its own worktree and the main checkout. A run that
    was not fenced keeps the full registry scan, because nothing stopped it
    from writing elsewhere.
    """
    if not fenced or worktree is None:
        return None
    return _boundary_tree_roots(repo_root, worktree)


def _write_boundary_tree_snapshot(
    run_directory: Path,
    repo_root: Path,
    *,
    worktree: Path | None = None,
    fenced: bool = False,
) -> dict[str, Any]:
    """Write the boundary baseline into the run directory and return it.

    The snapshot is a boundary baseline: it must follow dispatch's own writes
    and predate any write the worker makes in another tree. A snapshot that
    raises is recorded and the launch continues — a scan failure must never
    kill a worker nor turn a launch into a refusal. The write never creates the
    run directory, so a baseline racing a discard is dropped rather than
    bringing a discarded run back into existence.

    Both launch lanes write this one artifact. A spawned run's supervisor takes
    it after dispatch's writes and before the worker's spawn; a delegated run
    spawns nothing, so dispatch takes it inline, in the same last
    repository-facing step. Promotion reads it from the run directory either
    way, so a stray uncommitted edit in another tree is refused for both.

    A fenced launch reads only the run's own worktree and the main checkout;
    the rest of the worktree registry is a write the fence already refused.
    """
    try:
        snapshot: dict[str, Any] = _repository_tree_snapshot(
            repo_root,
            roots=_boundary_snapshot_roots(repo_root, worktree, fenced=fenced),
        )
    except Exception as exc:  # noqa: BLE001 - a scan failure never kills a launch
        snapshot = {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    _supervisor_write(run_directory / TREE_SNAPSHOT_NAME, snapshot)
    return snapshot


def _supervisor_tree_snapshot(spec: Mapping[str, Any]) -> None:
    """Write the boundary snapshot, or its failure, into the run directory."""
    worktree_value = str(spec.get("worktree") or "").strip()
    _write_boundary_tree_snapshot(
        Path(str(spec["run_directory"])),
        Path(str(spec["repo"])),
        worktree=Path(worktree_value) if worktree_value else None,
        fenced=bool(spec.get("fenced")),
    )


def _supervisor_exit_record(
    *,
    run_id: str,
    attempt: int,
    worker_pid: int | None,
    launched_at: str,
    status: int | None,
    run_directory: Path,
) -> dict[str, Any]:
    """Compose the one record written for a worker's exit or launch failure."""
    paths = stream_paths_newest_first(run_directory)
    count, last_type = _stream_record_facts(paths)
    record: dict[str, Any] = {
        "run_id": run_id,
        "attempt": attempt,
        "recorded_by": "supervisor",
        "worker_pid": worker_pid,
        "launched_at": launched_at,
        "exited_at": _utc_now(),
        "stream_records_seen": count,
        "last_record_type": last_type,
        # A worker that wrote no byte of its stream reached no model: that is a
        # launch failure, and it wants a different recovery from a death while
        # working, so the two are distinguished on the record itself.
        "ended_during": "launch" if count == 0 else "working",
    }
    if status is None:
        record.update({"exit_code": None, "signal": None, "signal_name": None})
    else:
        record.update(_wait_status_record(os.waitstatus_to_exitcode(status)))
    return record


def _record_launch_abandoned_before_spawn(
    spec: Mapping[str, Any],
    run_directory: Path,
    *,
    attempt: int,
    launched_at: str,
) -> None:
    """Write the one launch-failure record for a stop that ended the launch.

    A stop that arrives before the worker exists abandons the launch, so none
    is spawned. The record is still written, because the run directory outlives
    the pointer a discard removes and a reader needs to tell this launch from a
    worker that started and died.
    """
    exit_record = _supervisor_exit_record(
        run_id=str(spec.get("run_id") or ""),
        attempt=attempt,
        worker_pid=None,
        launched_at=launched_at,
        status=None,
        run_directory=run_directory,
    ) | {"detail": "crew stop arrived before the worker was spawned"}
    _write_attempt_artifact(
        run_directory,
        EXIT_RECORD_NAME,
        exit_record,
        attempt=attempt,
    )
    _publish_stored_phase(spec, ended=True, exit_record=exit_record)


def _stop_is_requested(flag: threading.Event, blocked: set[int]) -> bool:
    """Whether a stop has arrived, including one the signal mask is holding.

    A blocked SIGTERM or SIGHUP is not delivered, so its handler cannot run and
    the flag cannot be set; the signal stays pending and is the only observable
    a stop leaves while the mask is applied. Both are read here so a stop
    delivered either before the mask or inside the window it guards is seen.
    """
    return flag.is_set() or bool(signal.sigpending() & blocked)


def _record_stop_before_spawn() -> threading.Event:
    """Install the supervisor's stop handler and return the flag it sets.

    The supervisor starts in its own session, so ``crew stop`` reaches it as a
    group signal — the same signal that must end the worker. It cannot take the
    default disposition for SIGTERM or SIGHUP, or a stop delivered after the
    spawn would take the supervisor down before it recorded the exit the stop
    caused. It cannot ignore them either: an ignored stop is reported as done
    while the supervisor goes on to spawn that stop's worker. The handler
    records the request and lets the supervisor finish its snapshot, and the
    pre-spawn launch is abandoned when the flag is set.
    """
    requested = threading.Event()

    def record(_signum: int, _frame: Any) -> None:
        requested.set()

    signal.signal(signal.SIGTERM, record)
    signal.signal(signal.SIGHUP, record)
    return requested


def _record_is_this_attempt(record: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    """Whether a live pointer still names the attempt this supervisor runs as."""
    try:
        return int(record.get("attempt") or 1) == int(spec.get("attempt") or 1)
    except (TypeError, ValueError):
        return False


def _started_phase(record: Mapping[str, Any]) -> str:
    """``working`` for a pointer the launcher left at a pre-spawn label, else ""."""
    from reckon.crew.recovery import _PRE_SPAWN_PHASES

    return "working" if str(record.get("phase") or "") in _PRE_SPAWN_PHASES else ""


def _delivered_phase(
    record: Mapping[str, Any], exit_record: Mapping[str, Any] | None
) -> str:
    """The phase a finished worker's own delivery supports, or "" when none does.

    A delivered manifest is the worker's own verdict, so its terminal status is
    the phase the pointer should carry. A worker that reached no model at all —
    the exit record says it ended during launch — is a launch failure rather
    than a run that stopped mid-turn. Anything else, including a worker that
    exited after only a partial report, keeps the phase the spawn wrote.
    """
    from reckon.crew.reports import TERMINAL_MANIFEST_STATUSES, parse_manifest

    manifest = Path(str(record.get("manifest_path") or ""))
    try:
        status = str(
            parse_manifest(
                manifest.read_text(encoding="utf-8"), path=str(manifest)
            ).get("status")
            or ""
        )
    except (OSError, ValueError):
        status = ""
    if status in TERMINAL_MANIFEST_STATUSES:
        return status
    if (
        exit_record is not None
        and str(exit_record.get("ended_during") or "") == "launch"
    ):
        return LAUNCH_FAILED_PHASE
    return ""


def _publish_stored_phase(
    spec: Mapping[str, Any],
    *,
    ended: bool,
    exit_record: Mapping[str, Any] | None = None,
) -> None:
    """Rewrite the live pointer's stored phase from the run's own evidence.

    The launcher writes ``starting`` and nothing else, so without this the
    stored phase a reader sees — the follower's re-arm replay, the MCP views, a
    peer's tooling — stays the pre-spawn label for the whole life of the run.
    The supervisor is the only process that touches the pointer while the run is
    alive, so the advance is written here: ``working`` once the worker is
    spawned, and the delivered phase once the worker has exited.

    A pointer that is gone — a discard took it — is left gone rather than
    recreated, and a pointer whose attempt has moved on belongs to that attempt.
    """
    run_id = str(spec.get("run_id") or "")
    if not run_id:
        return

    with _pointer_lock(run_id):
        try:
            record = read_pointer(run_id)
        except CrewError:
            return
        if not _record_is_this_attempt(record, spec):
            return
        phase = (
            _delivered_phase(record, exit_record) if ended else _started_phase(record)
        )
        if phase:
            record["phase"] = phase
        if not ended:
            try:
                worker = json.loads(
                    (run_dir(run_id) / WORKER_RECORD_NAME).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                worker = {}
            if isinstance(worker, Mapping):
                for key in ("claim_registered_at", "launched_at"):
                    if worker.get(key):
                        record[key] = worker[key]
        _write_existing_pointer(run_id, record)


# A worker whose manifest reaches one of these has delivered its verdict and
# will do no more work, so the supervisor stops waiting for it and ends it. The
# terminal ``blocked`` is excluded on purpose: a blocked run is resumed in
# place, and its process and disposable identity are kept for that resume.
# ``in-progress`` is non-terminal and is waited on as before.
_WORKER_DONE_MANIFEST_STATUSES = frozenset({"complete", "failed"})

# The env var a test (or an operator) shortens the grace with. The default
# bounds how long a finished worker may hold its slot before the supervisor
# ends it; a worker whose manifest is complete or failed should exit at once,
# so the grace covers only the flush between the manifest write and the exit.
TERMINAL_MANIFEST_GRACE_ENV = "RECKON_WORKER_TERMINAL_GRACE_SECONDS"
TERMINAL_MANIFEST_GRACE_DEFAULT = 300.0
# How often the supervisor rechecks a manifest while it waits for the worker.
# Coarse on purpose: the file lives on shared storage, and a manifest the
# worker has not written again cannot have changed its status, so poll often
# enough to notice a delivery against a multi-minute grace without reading the
# manifest across the whole life of every worker.
_WORKER_MANIFEST_POLL_SECONDS = 3.0
# How long a worker gets to end on the grace signal before the supervisor
# escalates to SIGKILL, so a worker ignoring SIGTERM cannot hold the slot.
_WORKER_GRACE_KILL_SECONDS = 10.0
# How long a worker gets to end after ``crew stop`` reaches its process group
# before the supervisor escalates to SIGKILL. The stop signal is already
# delivered to the worker; the grace only covers the flush between receiving it
# and exiting. Without a bound here, a worker that ignores the stop holds the
# supervisor for as long as the worker itself lives.
STOP_GRACE_ENV = "RECKON_WORKER_STOP_GRACE_SECONDS"
STOP_GRACE_DEFAULT = _WORKER_GRACE_KILL_SECONDS
# A manifest's mtime must rest for this long before its terminal status counts
# as a delivery. A worker writes its manifest line by line, so it passes through
# states where the status line already reads terminal while the list fields
# below it are still being written; signalling on such a read is what cut a
# manifest off mid-path near the deadline — wrote complete, then kept writing,
# and the grace expired against the half-written file.
_WORKER_MANIFEST_QUIET_SECONDS = 3.0
# The overall ceiling on deferring a signal. A whole terminal manifest that
# will not rest — its mtime keeps advancing, so the quiet period is never met —
# may defer its signal for at most this long; past it the whole read the
# supervisor holds is taken as the delivery. An incomplete manifest is never
# signalled on, at the ceiling or before it.
_WORKER_MANIFEST_CEILING_SECONDS = 600.0


def _terminal_manifest_grace_seconds() -> float:
    """The grace a finished worker is given to exit on its own, in seconds.

    Read from the environment so a test can shorten it, and floored at zero so
    a nonsensical value degrades to an immediate end rather than to no bound.
    """
    raw = os.environ.get(TERMINAL_MANIFEST_GRACE_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return TERMINAL_MANIFEST_GRACE_DEFAULT


def _stop_grace_seconds() -> float:
    """The grace a stopped worker is given to exit before it is killed.

    Read from the environment so a test can shorten it, and floored at zero so
    a nonsensical value degrades to an immediate end rather than to no bound.
    """
    raw = os.environ.get(STOP_GRACE_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = None
        if value is not None and value >= 0:
            return value
    return STOP_GRACE_DEFAULT


def _supervisor_manifest_path(run_id: str) -> Path:
    """The manifest the supervisor watches for its run's terminal verdict.

    Read from the live pointer, whose manifest path the launcher wrote; a
    pointer already gone (a discard took it) falls back to the run directory,
    so the watch names a path rather than raising.
    """
    try:
        record: Mapping[str, Any] = read_pointer(run_id)
    except CrewError:
        record = {}
    return Path(_recorded_manifest_path(record, run_id))


def _worker_manifest_done_status(manifest_path: Path) -> str:
    """The done status a delivered manifest carries, or "" for none yet.

    A manifest that is absent, unreadable, still carries the dispatch
    template's placeholder or names a status the supervisor waits on all read
    as "" — the worker is still working, or has declared a wait, and is left
    to its own exit.
    """
    from reckon.crew.reports import manifest_status_is_template, parse_manifest

    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        status = str(parse_manifest(text, path=str(manifest_path)).get("status") or "")
    except ValueError:
        return ""
    status = status.strip().lower()
    if not status or manifest_status_is_template(status):
        return ""
    return status if status in _WORKER_DONE_MANIFEST_STATUSES else ""


def _worker_manifest_is_whole(manifest_path: Path) -> bool:
    """Whether every list field on the manifest closes its bracket.

    The tolerant reader accepts a value and splits it, so a ``changed_paths:
    [a, b, c`` line cut off mid-path still parses to a done manifest with a
    plausible list — the shape a worker at its pen passes through. A list field
    whose raw value opens a ``[`` must close it before the manifest counts as
    delivered; an unbalanced bracket is a write in progress, not a delivery.

    A field written in the block form (a bare key over ``- item`` lines) carries
    no bracket to check; the quiet period, not this check, is what covers it.
    """
    from reckon.crew.reports import _MANIFEST_LIST_KEYS

    keys = frozenset((*_MANIFEST_LIST_KEYS, "orientation_write_paths"))
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(
            r"^(?:[-*]\s+)?(?:\*\*)?(?P<key>[a-z][a-z0-9_-]*)\s*:\s*(?P<value>.*)$",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue
        if match.group("key").lower().replace("-", "_") not in keys:
            continue
        value = match.group("value").strip().strip("*").strip()
        if "[" in value and value.count("[") != value.count("]"):
            return False
    return True


def _iso_stamp_to_ns(stamp: str) -> int | None:
    """An ISO-8601 instant as epoch nanoseconds, or None for an unreadable one."""
    parsed = parse_utc(stamp)
    if parsed is None:
        return None
    return int(parsed.timestamp() * 1_000_000_000)


def _attempt_started_ns(
    spec: Mapping[str, Any], record: Mapping[str, Any], supervisor_started_at: str
) -> int | None:
    """The current attempt's own start, as epoch nanoseconds.

    The spec is authoritative — it is written by the launch that started this
    supervisor — and the pointer is the fallback for a spec that omits the
    field. The supervisor's own launch instant is the last resort before the
    clock is unreadable.
    """
    for stamp in (
        spec.get("attempt_started_at"),
        record.get("attempt_started_at"),
        supervisor_started_at,
    ):
        if not stamp:
            continue
        parsed = _iso_stamp_to_ns(str(stamp))
        if parsed is not None:
            return parsed
    return None


def _supervisor_manifest_baseline_ns(
    run_id: str, spec: Mapping[str, Any], *, supervisor_started_at: str = ""
) -> int:
    """The manifest generation this attempt may call its own.

    A resumed run keeps its manifest path, so the previous attempt's delivered
    manifest is still on disk when the new worker starts, carrying an mtime from
    before this attempt began. A generation recorded on the pointer at dispatch
    time predates that manifest, so honouring it alone would read the stale
    manifest as this attempt's delivery. The baseline is therefore never earlier
    than the attempt's own start: a recorded generation counts only when it is
    at or after that start, which is the floor. A manifest written before this
    attempt began cannot count as its delivery.

    When the attempt clock is unreadable everywhere, the recorded generation is
    taken as before — a manifest is then treated as delivery, the pre-existing
    behaviour rather than a regression.
    """
    try:
        record: Mapping[str, Any] = read_pointer(run_id)
    except CrewError:
        record = {}
    baseline: int | None = None
    recorded = record.get("manifest_baseline_mtime_ns")
    if recorded is not None:
        try:
            baseline = int(recorded)
        except (TypeError, ValueError):
            baseline = None
    started_ns = _attempt_started_ns(spec, record, supervisor_started_at)
    if started_ns is None:
        return baseline if baseline is not None else 0
    if baseline is None:
        return started_ns
    return max(baseline, started_ns)


def _reap_worker_on_its_terminal_manifest(
    pid: int,
    *,
    run_directory: Path,
    manifest_path: Path,
    grace_seconds: float,
    baseline_ns: int,
    stop_requested: threading.Event | None = None,
    stop_grace_seconds: float = 0.0,
) -> int | None:
    """Collect a worker's exit, ending it once its manifest is delivered or a stop arrives.

    A worker that delivered (a complete or failed manifest) and then kept
    running holds its run's slot long after its work was finished. The
    supervisor notices the delivery, gives the worker the grace period to exit
    on its own, and then ends its process — writing the sender record before it
    signals, so the signal is attributable to the run's own directory.

    A delivery is a manifest this attempt wrote that reads as a done status,
    parses in full with every list field closed, and has rested without a write
    for the quiet period. A manifest that is still being written — its mtime
    advancing, or a list field whose bracket is not yet closed — is a worker at
    its pen, and the supervisor defers to the next poll rather than signal a
    half-written record. A manifest that reads non-terminal, or blocked (kept
    for resume), is waited on as before, and so is a worker that rewrites a done
    manifest back to a non-done status: withdrawing the delivery clears the
    deadline it had set. A done manifest that reads whole but will not rest
    cannot defer its signal forever: past the overall ceiling the supervisor
    takes the whole read it holds as the delivery. An incomplete manifest is
    never signalled on, at the ceiling or before it. Returns the wait status, or
    ``None`` when the child was already reaped elsewhere.

    A recorded stop bounds the wait as well. ``crew stop`` delivers its signal
    to the worker's whole process group, so the worker has already been asked to
    end; the supervisor gives it ``stop_grace_seconds`` to do so, then kills it.
    Without this the supervisor would block on ``waitpid`` for as long as the
    worker lives, so a worker that ignores the stop would hold the supervisor,
    and its slot, past any bound. A stop does not wait on a manifest: it ends
    the run outright.

    The manifest is stat'd each poll; its status and wholeness are read only
    when its mtime has advanced past the attempt's baseline and changed since
    the last read, so an unchanged manifest is never reparsed and a manifest
    left by a previous attempt — a resumed run's own complete record — is not
    mistaken for this attempt's delivery.
    """
    seen_mtime_ns: int | None = None
    written_at: float | None = None
    terminal_at: float | None = None
    is_done = False
    is_whole = False
    deadline: float | None = None
    signalled_at: float | None = None
    stop_deadline: float | None = None
    stop_killed = False
    while True:
        try:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return None
        if waited_pid == pid:
            return status
        now = time.monotonic()
        if (
            stop_requested is not None
            and stop_deadline is None
            and not stop_killed
            and stop_requested.is_set()
        ):
            # A stop ends the run: the group signal has already reached the
            # worker, and re-signalling it here names the sender in the run's
            # worker record. The grace covers only the flush between receiving
            # the stop and exiting.
            signal_worker(
                pid,
                signal.SIGTERM,
                reason="run-stop",
                run_dir=run_directory,
            )
            stop_deadline = now + stop_grace_seconds
        if stop_deadline is not None and now >= stop_deadline:
            # The worker outlived the stop grace. SIGKILL cannot be ignored, so
            # the supervisor ends it rather than waiting on it further.
            signal_worker(
                pid,
                signal.SIGKILL,
                reason="worker-ignored-the-stop-grace-signal",
                run_dir=run_directory,
            )
            stop_deadline = None
            stop_killed = True
        try:
            mtime_ns = manifest_path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if mtime_ns is not None and mtime_ns != seen_mtime_ns:
            seen_mtime_ns = mtime_ns
            written_at = now
            fresh = mtime_ns > baseline_ns
            is_done = fresh and bool(_worker_manifest_done_status(manifest_path))
            is_whole = is_done and _worker_manifest_is_whole(manifest_path)
            if is_done:
                if terminal_at is None:
                    terminal_at = now
            else:
                terminal_at = None
        # Delivered: a done manifest this attempt wrote, whole, and rested past
        # the quiet period. The grace is measured from the manifest's own write,
        # so a delivery noticed late still ends on schedule; the age is
        # subtracted unclamped, so once it reaches the grace the deadline is
        # already in the past and the worker is ended at once.
        settled = (
            mtime_ns is not None
            and is_done
            and is_whole
            and written_at is not None
            and now - written_at >= _WORKER_MANIFEST_QUIET_SECONDS
        )
        # A whole done manifest that will not rest cannot defer its signal past
        # the overall ceiling: the whole read the supervisor holds is the
        # delivery. An unwhole manifest never reaches this branch.
        ceiling_due = (
            mtime_ns is not None
            and is_done
            and is_whole
            and terminal_at is not None
            and now - terminal_at >= _WORKER_MANIFEST_CEILING_SECONDS
        )
        if settled or ceiling_due:
            age = time.time() - (mtime_ns / 1_000_000_000)
            deadline = now + grace_seconds - age
        else:
            # Nothing whole, quiet and terminal is on disk: either the delivery
            # was withdrawn (the worker is working again, or has declared a wait
            # for resume) or a done manifest is still being written, so any
            # deadline an earlier read set is cleared and the worker is waited
            # on as before rather than ended on a withdrawn or half-written
            # delivery's clock.
            deadline = None
        if deadline is not None and signalled_at is None and now >= deadline:
            signal_worker(
                pid,
                signal.SIGTERM,
                reason="worker-lingered-after-terminal-manifest",
                run_dir=run_directory,
            )
            signalled_at = now
        elif signalled_at is not None and now - signalled_at >= _WORKER_GRACE_KILL_SECONDS:
            # The worker ignored the grace signal. SIGKILL cannot be ignored,
            # and the record names this second, harder signal.
            signal_worker(
                pid,
                signal.SIGKILL,
                reason="worker-ignored-the-terminal-grace-signal",
                run_dir=run_directory,
            )
            signalled_at = now
        time.sleep(_WORKER_MANIFEST_POLL_SECONDS)


# A supervisor's argv is not always the worker itself: some lanes start the
# worker through an intermediate, which exits while the worker runs on. The
# worker is then an orphan, and without this the kernel reparents it to init,
# where its exit cannot be collected and the attempt would be recorded as ended
# while its worker still works.
_PR_SET_CHILD_SUBREAPER = 36


def _become_child_subreaper() -> None:
    """Have the kernel reparent an orphaned worker to this supervisor.

    A worker is collectable only where its parent waits for it, and a launcher
    that exits ahead of its worker would otherwise hand the worker to init. As
    a child subreaper the supervisor receives it as its own child, so the wait
    for the worker and the exit record that ends the attempt both stay here.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise CrewError(
            f"prctl(PR_SET_CHILD_SUBREAPER) failed: {os.strerror(ctypes.get_errno())}"
        )


def _child_processes_of(pid: int) -> list[int]:
    """The pids the kernel currently parents to a process.

    Read from /proc rather than from a record, because a reparented worker is
    named nowhere: it was parented to a launcher that is gone by the time the
    wait for it begins. A child that already exited shows here too, as the
    zombie its parent has yet to reap.
    """
    children: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            parent = int(stat[stat.rindex(")") + 2 :].split()[1])
        except (ValueError, IndexError):
            continue
        if parent == pid:
            children.append(int(entry))
    return sorted(children)


def _reap_the_launched_worker(
    pid: int,
    status: int | None,
    *,
    run_directory: Path,
    manifest_path: Path,
    grace_seconds: float,
    baseline_ns: int,
    stop_requested: threading.Event | None = None,
    stop_grace_seconds: float = 0.0,
) -> tuple[int, int | None]:
    """Collect the exit of the worker a launcher started and left behind.

    Returns the pid and wait status the attempt's exit record should name. When
    the immediate child was the worker they are unchanged. When it was an
    intermediate that exited first, the worker it started was reparented to
    this supervisor as a child subreaper, and it is waited on exactly as a
    directly spawned worker is — under the same terminal-manifest grace and the
    same stop bound — so the record names the exit that ended the attempt's
    worker rather than a launcher's exit taken while the worker still runs.
    """
    worker_pid, worker_status = pid, status
    if status is not None and os.WIFSIGNALED(status):
        # A launcher hands the attempt to its worker by exiting; it is not
        # killed. A spawned child that ended by signal ended the attempt's own
        # worker, and any descendants it leaves behind are leftovers of that
        # worker rather than a worker still running the attempt. The signal is
        # the fact the exit record carries, so it is written now rather than
        # after a leftover ends.
        return worker_pid, worker_status
    while True:
        children = _child_processes_of(os.getpid())
        if not children:
            return worker_pid, worker_status
        for child in children:
            child_status = _reap_worker_on_its_terminal_manifest(
                child,
                run_directory=run_directory,
                manifest_path=manifest_path,
                grace_seconds=grace_seconds,
                baseline_ns=baseline_ns,
                stop_requested=stop_requested,
                stop_grace_seconds=stop_grace_seconds,
            )
            if child_status is not None:
                # The exit that ended the wait is the attempt's exit; a child
                # reaped elsewhere leaves the one already held standing.
                worker_pid, worker_status = child, child_status


def _run_supervisor(spec_path: Path) -> int:
    """Take the snapshot, launch the worker, collect its exit, and stop.

    The supervisor holds the worker's parentage for the whole run, so it is the
    only process that can collect the exit a reparented worker would otherwise
    hand to init. A stop is recorded rather than ignored or acted on directly:
    the supervisor survives one so it can write the exit a post-spawn stop
    caused, and it abandons the launch when the stop arrived before the worker
    existed.
    """
    try:
        spec = json.loads(spec_path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(spec, Mapping):
        return 0
    run_directory = Path(str(spec.get("run_directory") or ""))
    run_id = str(spec.get("run_id") or "")
    try:
        attempt = int(spec.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    stop_requested = _record_stop_before_spawn()
    _supervisor_tree_snapshot(spec)
    launched_at = _utc_now()
    if stop_requested.is_set():
        # The stop arrived while the snapshot ran, before any worker existed,
        # so none is spawned and the launch is over before it began.
        _record_launch_abandoned_before_spawn(
            spec, run_directory, attempt=attempt, launched_at=launched_at
        )
        return 0
    # SIGTERM and SIGHUP stay blocked across the stop read and the spawn, so a
    # stop delivered between them is deferred rather than running its handler
    # and being missed. It is read again here, immediately before the spawn: a
    # stop delivered inside this window means no worker is ever spawned.
    blocked = {signal.SIGTERM, signal.SIGHUP}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        if _stop_is_requested(stop_requested, blocked):
            _record_launch_abandoned_before_spawn(
                spec, run_directory, attempt=attempt, launched_at=launched_at
            )
            return 0
        try:
            # Before the worker exists, so an intermediate that exits ahead of
            # the worker it starts leaves that worker parented here rather than
            # to init, where its exit would be uncollectable.
            _become_child_subreaper()
            pid = _supervisor_spawn_worker(spec)
        except (OSError, ValueError, KeyError, CrewError) as exc:
            failure_record = _supervisor_exit_record(
                run_id=str(spec.get("run_id") or ""),
                attempt=attempt,
                worker_pid=None,
                launched_at=launched_at,
                status=None,
                run_directory=run_directory,
            ) | {"detail": f"worker did not spawn: {type(exc).__name__}: {exc}"}
            _write_attempt_artifact(
                run_directory,
                EXIT_RECORD_NAME,
                failure_record,
                attempt=attempt,
            )
            _publish_stored_phase(spec, ended=True, exit_record=failure_record)
            return 0
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    registered_at = _claim_registration_for_worker(run_directory, run_id)
    _write_attempt_artifact(
        run_directory,
        WORKER_RECORD_NAME,
        {
            "run_id": str(spec.get("run_id") or ""),
            "attempt": attempt,
            "pid": pid,
            "pid_start_time": _process_start_time(pid),
            "launched_at": launched_at,
            "claim_registered_at": registered_at,
            "backend": str(spec["plan"].get("backend") or ""),
            "argv": list(spec["plan"].get("argv") or ()),
        },
        attempt=attempt,
    )
    if attempt == 1 and registered_at:
        try:
            _remember_claim_launch_interval(run_id, registered_at, launched_at)
        except (OSError, ValueError, TypeError) as exc:
            print(f"claim grace observation unavailable: {exc}", file=sys.stderr)
    _publish_stored_phase(spec, ended=False)
    # The spawn retry's startup poll reaps a worker that exits inside its
    # window, and the pid it hands back then carries that exit: waiting on the
    # pid would get ECHILD and the record would name neither a code nor a
    # signal, so the carried exit is the attempt's exit. A worker that outlived
    # the poll carries none and is supervised exactly as before.
    status = getattr(pid, "collected_wait_status", None)
    if status is not None:
        # The exit was collected from the worker itself, so it stands as the
        # attempt's exit on its own: the worker has been reaped, so no child of
        # this supervisor is a still-running worker whose later exit could be
        # the one that ended the attempt.
        worker_pid = pid
    else:
        status = _reap_worker_on_its_terminal_manifest(
            pid,
            run_directory=run_directory,
            manifest_path=_supervisor_manifest_path(run_id),
            grace_seconds=_terminal_manifest_grace_seconds(),
            baseline_ns=_supervisor_manifest_baseline_ns(
                run_id, spec, supervisor_started_at=launched_at
            ),
            stop_requested=stop_requested,
            stop_grace_seconds=_stop_grace_seconds(),
        )
        worker_pid, status = _reap_the_launched_worker(
            pid,
            status,
            run_directory=run_directory,
            manifest_path=_supervisor_manifest_path(run_id),
            grace_seconds=_terminal_manifest_grace_seconds(),
            baseline_ns=_supervisor_manifest_baseline_ns(
                run_id, spec, supervisor_started_at=launched_at
            ),
            stop_requested=stop_requested,
            stop_grace_seconds=_stop_grace_seconds(),
        )
    exit_record = _supervisor_exit_record(
        run_id=str(spec.get("run_id") or ""),
        attempt=attempt,
        worker_pid=worker_pid,
        launched_at=launched_at,
        status=status,
        run_directory=run_directory,
    )
    _write_attempt_artifact(
        run_directory,
        EXIT_RECORD_NAME,
        exit_record,
        attempt=attempt,
    )
    _publish_stored_phase(spec, ended=True, exit_record=exit_record)
    return 0


def _claim_registration_for_worker(run_directory: Path, run_id: str) -> str:
    """Read this run's registration instant for its worker receipt."""
    try:
        claim = json.loads((run_directory / "claim.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(claim, Mapping) or claim.get("run_id") != run_id:
        return ""
    return str(claim.get("claim_registered_at") or "")


def _remember_claim_launch_interval(
    run_id: str, registered_at: str, launched_at: str
) -> None:
    """Keep a bounded, process independent index of durable worker receipts."""
    registered = parse_utc(registered_at)
    launched = parse_utc(launched_at)
    if registered is None or launched is None:
        return
    seconds = (launched - registered).total_seconds()
    if seconds < 0:
        return
    path = crew_home() / CLAIM_GRACE_OBSERVATIONS_NAME
    with _pointer_lock("claim-grace-observations"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        rows = payload.get("intervals") if isinstance(payload, Mapping) else None
        observations = (
            [
                row
                for row in rows
                if isinstance(row, Mapping) and row.get("run_id") != run_id
            ]
            if isinstance(rows, list)
            else []
        )
        observations.append({"run_id": run_id, "seconds": seconds})
        observations.sort(key=lambda row: str(row.get("run_id") or ""))
        _write_json(path, {"intervals": observations[-CLAIM_GRACE_OBSERVATION_LIMIT:]})


from .dispatch_admission import (  # noqa: E402
    _require_fleet_gate_open,
)

from .dispatch_picker import (  # noqa: E402
    _write_existing_pointer,
)

from .dispatch_sessions import (  # noqa: E402
    _harness_behind_the_fence,
    _recorded_manifest_path,
)

from .dispatch_watch import (  # noqa: E402
    _open_request_fifo,
)
