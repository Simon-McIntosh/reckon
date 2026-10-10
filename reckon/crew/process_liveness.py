# ruff: noqa: I001
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any



def process_alive(pid: Any) -> bool | None:
    """Report whether a pid is still running; None when there is no pid.

    A dead process with no terminal event in its log is a recoverable orphan
    rather than a completed run, which is why liveness is recorded beside the
    stream rather than inferred from it. A PermissionError from a zero signal
    means the process exists but belongs to another user — proof of life, not
    death. On a shared workstation carrying several fleets that is the normal
    condition for a peer worker, so reporting it as dead would classify a live
    run as abandoned.
    """
    if not pid:
        return None
    # A zombie is a process-table entry whose process has exited and whose
    # exit status the parent has not yet collected, so the kernel accepts a
    # zero signal against it and the probe below would report the finished
    # run as running: its slot stays held and its own resume is refused while
    # it lingers. Every caller asking whether work is still running wants
    # "no" for a zombie, because the process it was launched to run has
    # finished either way. Reading the state from per-process stat is what
    # tells that apart from the liveness proof above; an unreadable record is
    # not proof of death, so it falls through to the signal probe unchanged.
    if _process_state(pid) == "Z":
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return None
    return True


# The scheduler states that mean a placed job is still in the system: the job
# exists and its work has not ended. Any other readable state means the job has
# left the queue, which is terminal whatever the scheduler calls it. A state the
# scheduler cannot be asked for is None, not False, so a silent scheduler never
# reads as a stopped worker.
_JOB_LIVE_STATES = frozenset(
    {"running", "pending", "configuring", "completing", "suspended"}
)

# A scheduler query is on the liveness path of every read, so it is bounded:
# a controller that hangs must not make a fleet listing hang with it.
_SCHEDULER_QUERY_TIMEOUT_SECONDS = 5.0

# The token a query vector carries where the job id is substituted, so a probe
# spells its own argument order rather than reckon guessing one.
_JOB_STATE_PLACEHOLDER = "{job}"


def _scheduler_query_argv(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    field: str,
) -> list[str] | None:
    """The argument vector for one field of one job, or None when unknowable.

    The query is read from the placement's own declaration rather than from a
    table keyed on the wrapper's name, so which reporting verb answers a given
    scheduler is configuration. A placement that declares the wrapper without
    declaring how to ask it answers None here and falls through to the pid
    probe, having been refused before launch for exactly that omission.
    """
    if not placement or not job_id:
        return None
    query = placement.get(field)
    if not isinstance(query, Iterable) or isinstance(query, (str, bytes)) or not query:
        return None
    token = str(job_id)
    return [
        token if str(item) == _JOB_STATE_PLACEHOLDER else str(item) for item in query
    ]


def _ask_scheduler(
    argv: list[str] | None, runner: Callable[[list[str]], str | None] | None
) -> str | None:
    """One scheduler question, or None when the question could not be asked.

    The two failures are kept apart because they mean opposite things. A query
    that could not be run — no such scheduler, a non-zero exit, a timeout —
    answers None, and the caller falls back to another instrument. A query that
    ran and printed nothing answers the empty string, which for a job-state
    question is a statement in its own right: the scheduler knows no such job,
    so the job has left the queue. Collapsing the empty answer into None would
    read every ordinary completion as unreadable and send the caller back to a
    pid that belongs to another host.
    """
    if argv is None:
        return None
    probe = _run_scheduler_query if runner is None else runner
    try:
        output = probe(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    if output is None:
        return None
    lines = str(output).strip().splitlines()
    return lines[-1].strip() if lines else ""


def scheduler_job_reason(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    runner: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    """The scheduler's own reason string for a placed job, or None.

    A job that never started reports why here rather than through an exit
    status, so a launch-failure record quotes the scheduler's reason instead
    of fabricating one.
    """
    return _ask_scheduler(
        _scheduler_query_argv(placement, job_id, "reason_query"), runner
    )


# A job the scheduler ended for its own reason is not a worker whose work
# failed: the remedy differs, since a resubmission with the same resources fails
# the same way. The two classes a placement plan already names are a time limit
# and a memory limit, matched on the scheduler's own words so a different
# spelling adds a class rather than being read as a failed worker.
_SCHEDULER_KILL_CLASSES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"timeout", "timelimit", "time limit", "deadline"}), "job-timeout"),
    (
        frozenset(
            {
                "out_of_memory",
                "outofmemory",
                "out of memory",
                "oom",
                "memory limit",
            }
        ),
        "job-out-of-memory",
    ),
)


def scheduler_kill_class(state: Any, reason: Any = None) -> str | None:
    """Name the scheduler's own kill reason, or None when it ended for another.

    Matched against both the state and the scheduler's reason string, because a
    scheduler spells a time or memory end in either place and a reason of
    ``None`` is reported differently depending on which one fired.
    """
    haystack = " ".join(part.strip().casefold() for part in (state, reason) if part)
    if not haystack:
        return None
    for spellings, name in _SCHEDULER_KILL_CLASSES:
        if any(spelling in haystack for spelling in spellings):
            return name
    return None


def _run_scheduler_query(argv: list[str]) -> str | None:
    """Run one scheduler state query, answering None when it cannot be read.

    A query that exits non-zero — an unknown job, an unreachable controller —
    answers None rather than an empty string, because the absence of a state is
    not the statement that the job has ended.
    """
    executable = shutil.which(argv[0])
    if executable is None:
        return None
    completed = subprocess.run(
        [executable, *argv[1:]],
        capture_output=True,
        text=True,
        timeout=_SCHEDULER_QUERY_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _scheduler_state_argv(
    placement: Mapping[str, Any] | None, job_id: str
) -> list[str] | None:
    """The argument vector that asks a scheduler for one job's state, or None.

    A placement declares the reporting verb that answers one job's state beside
    the wrapper it asks, so a placement that declares no query answers None and
    falls through to the pid probe instead of being read as a stopped job.
    """
    return _scheduler_query_argv(placement, job_id, "state_query")


def scheduler_job_state(
    placement: Mapping[str, Any] | None,
    job_id: Any,
    runner: Callable[[list[str]], str | None] | None = None,
) -> str | None:
    """The state a scheduler reports for a placed job, or None when unread.

    Three answers, and the caller must tell the last two apart. A state names
    the job and is read against the in-flight set. The empty string is a
    successful query that named no job: the scheduler knows it not, so it has
    left the queue. None is a question that could not be asked at all — no
    scheduler, no such wrapper, a non-zero exit — and leaves the caller to fall
    back to the pid probe rather than reporting a live run as stopped.
    """
    return _ask_scheduler(_scheduler_state_argv(placement, str(job_id or "")), runner)


def placement_job_alive(
    record: Mapping[str, Any] | None,
    runner: Callable[[list[str]], str | None] | None = None,
) -> bool | None:
    """Whether the job a placed run was charged to is still in the system.

    A placed run's recorded pid names the scheduler client, not the worker, so
    the job is the subject of a liveness read. A state the scheduler reports as
    in-flight answers True. Any other readable answer means the job has left the
    queue and answers False, whatever the scheduler calls it — including the
    empty answer of a successful query that named no job, which is how an
    ordinary completion is reported and must not fall through to the pid. A
    record carrying no placement, or one whose scheduler could not be queried at
    all, answers None so the pid probe decides as it always has.

    ``runner`` is the caller's own scheduler query, handed in the way ``alive``
    is so a test reaches this without a scheduler on the host.
    """
    if not record:
        return None
    placement = record.get("placement")
    if not isinstance(placement, Mapping) or not placement:
        return None
    state = scheduler_job_state(placement, record.get("job_id"), runner)
    if state is None:
        return None
    return state.casefold() in _JOB_LIVE_STATES


def record_process_alive(
    record: Mapping[str, Any] | None,
    alive: Callable[[Any], bool | None] | None = None,
    job_alive: Callable[[Mapping[str, Any] | None], bool | None] | None = None,
    match_start_time: bool = True,
) -> bool | None:
    """Report whether the process a run record names is still running.

    Every liveness decision about a run is taken from the run's own record, so
    the pid lookup lives here in one place and the call site never handles a
    bare pid. A record that names no process answers None, the same shape
    :func:`process_alive` already returns for a missing pid, so a caller cannot
    read "no process recorded yet" as a stopped worker.

    A pid the kernel has since handed to another process is not the one the
    record names, and a bare process-table probe cannot tell the two apart: it
    answers liveness for whatever now holds the number. When the record carries
    the kernel start tick written at registration, the probe's answer is kept
    only if the running process is the registered one, so a reused pid stops
    reading as a survivor on every read rather than only where a caller
    remembered to compare. An unreadable start tick is not proof of reuse — the
    same stance :func:`process_alive` takes toward an unreadable process record
    — so the probe's answer stands, which is also what keeps a peer's live
    process readable.

    A placed run is charged to a scheduler job rather than to the coordinator's
    own login slice, so its recorded pid names the scheduler client rather than
    the worker and a local process-table read answers a different question. The
    job is asked first, and the pid probe is the fallback for a record carrying
    no placement or a scheduler that cannot be queried — which keeps an
    unplaced run answering exactly as it always has.

    ``alive`` is the caller's own probe. A module that keeps the primitive
    bound under its own name — so a test can substitute liveness for that
    module — hands it in rather than having its substitution bypassed. The
    reuse check needs the process table, so it is taken only when that real
    primitive answered: a substituted probe is the whole answer for the read.

    ``match_start_time`` is for the one caller that asks whether the process
    itself is running rather than whether it is the registered one. A seat
    guard needs the process that holds the seat, and a running holder is a
    running holder however its recorded identity reads, so it opts out here
    rather than depending on this check being absent.
    """
    if not record:
        return None
    placed = (placement_job_alive if job_alive is None else job_alive)(record)
    if placed is not None:
        return placed
    probe = process_alive if alive is None else alive
    pid = record.get("pid")
    running = probe(pid)
    # The reuse check reads the process table, which is the only thing that can
    # say whether the pid still names the registered process. A caller that
    # substitutes its own probe owns the whole read — its probe answers for
    # liveness and there is nothing left for a second lookup to decide — so the
    # check is skipped when the real primitive is the one that answered. The
    # callers that pass this module's own ``process_alive`` through still get
    # the check on every read they make, and ``alive=None`` resolves to that
    # same function, so the default path is covered by the same identity.
    if running is True and match_start_time and probe is process_alive:
        expected = record.get("pid_start_time")
        if expected is not None:
            actual = _process_start_time(pid)
            if actual is not None:
                running = actual == expected
    return running


def _process_stat_fields(pid: Any) -> list[str]:
    """The space-separated per-process stat fields, or [] when unreadable.

    The comm field is parenthesised and may itself contain spaces and closing
    parentheses, so the split begins after the final ``)``; the fields are the
    third one (state) onwards, unrenumbered from the parenthesised form.
    """
    try:
        value = int(pid)
        stat = Path(f"/proc/{value}/stat").read_text()
    except (OSError, TypeError, ValueError):
        return []
    return stat[stat.rfind(")") + 2 :].split()


def _process_state(pid: Any) -> str | None:
    """The single-character kernel state from the per-process stat record."""
    fields = _process_stat_fields(pid)
    return fields[0] if fields else None


def _process_start_time(pid: Any) -> str | None:
    """Read the kernel start tick that distinguishes reused process ids."""
    fields = _process_stat_fields(pid)
    return fields[19] if len(fields) > 19 else None
