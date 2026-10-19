"""One reservation held once, and the workers that run inside it as steps.

A job per worker mints a reservation per worker, so concurrency would mean
submitting more reservations rather than sizing one, and nothing would be
shared between the sessions on this host. The design here is the opposite: a
single reservation is held once through an ensure command, its job id is
published into the shared crew state, and every session places its workers into
that one allocation as steps.

Steps are asked with ``--overlap``, and the reason is what a worker is. A
worker spends nearly all its life blocked on the served model, so reserving
cores exactly for one prices an idle process; ``--exact`` would turn the
allocation into a quota a worker queues behind, while ``--overlap`` makes it a
place to run. Because nothing is then enforced, the concurrency limit is ours
to hold, and it is held in the roster: a cap sized from resident memory per
worker against the reservation's own reserved memory, because memory is the
axis that binds and cores are the cheap one.
"""

from __future__ import annotations

import contextlib
import fcntl
import getpass
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon._store import write_json_atomically
from reckon.crew.refusals import format_refusal
from reckon.crew.runs import CrewError

# The size the lead locked on 2026-09-20. Under --overlap the reservation is a
# place to run rather than a quota, so the numbers are a place to run in and
# the concurrency limit lives in the roster below. Held at 32 cores and 128 GiB
# on a 36-core node: the node still carries headroom, and memory is the axis
# that binds rather than cores.
RESERVATION_CORES = 32
RESERVATION_MEMORY_GB = 128

# The partition the reservation is held on. The `all` partition carries no time
# limit, which is what a reservation that outlives a wave needs; a partition
# with a ceiling would end the allocation under the fleet.
RESERVATION_PARTITION = "all"

# The allocation client and the step client. The allocation is held with no
# shell attached, so it returns a job id and keeps the resources without
# occupying a terminal; the workers then run inside it as steps.
RESERVATION_SCHEDULER = "salloc"
RESERVATION_STEP_SCHEDULER = "srun"
RESERVATION_NO_SHELL_OPTION = "--no-shell"
RESERVATION_JOB_NAME = "reckon-reservation"

# The memory a worker step carries by default, and the ceiling above which a
# step leaves the fleet allocation for a node of its own. The allocation holds
# 96 GB shared by every session and worker, so an undeclared step bounded to a
# small share keeps the OOM killer from reaching the sessions when one grows.
# A step named past the ceiling cannot sit beside them at all: it is placed as
# a job of its own, where a whole node serves it, instead of taking the
# allocation to the limit. The default is overridable per environment.
DEFAULT_STEP_MEMORY_GB = 24
STEP_MEMORY_CEILING_GB = 48
STEP_MEMORY_ENV = "RECKON_STEP_MEMORY"

# A step too large for the allocation is placed on a debug partition, so the
# work is sized for a debug queue rather than a production one. Such a partition
# is a one-hour window with no default: there the scheduler's own maximum is an
# hour and omitting the limit leaves the job pending forever, so a step placed
# on one always states a limit and never states more than the window allows.
DEBUG_PARTITION_SUFFIX = "_debug"
DEBUG_JOB_TIME_CEILING = "01:00:00"

# A step client inherits the allocation's identity from its environment, and
# that inheritance is the only thing that would make it a step of the fleet job
# rather than a job of its own. These are the variables it must not inherit.
SLURM_JOB_ENVIRONMENT_ROOTS = ("SLURM_JOB", "SLURM_STEP")

# The roster cap and the axis it is measured on. Twenty-five is the concurrency
# the serve was observed delivering throughput for; the binding quantity is
# resident memory per worker against the reservation's reserved memory, never a
# core count, so the cap is raised by reading memory across a wave rather than
# by counting cores.
RESERVATION_ROSTER_LIMIT = 25
RESERVATION_ROSTER_BASIS = (
    "resident memory per worker against the reservation's reserved memory, "
    "not a core count"
)

# How the reservation's own job is asked about. The reservation is not a
# backend and carries no schema declaration, so the two reporting verbs it is
# asked with are declared here beside the wrapper they ask.
RESERVATION_STATE_QUERY = ("squeue", "-h", "-j", "{job}", "-o", "%T")
RESERVATION_REASON_QUERY = ("squeue", "-h", "-j", "{job}", "-o", "%R")

# The scheduler question a replacement asks about the job it is pointed at: one
# row carrying the state and the owner of the job named. The job's shape is read
# through the allocation-shape reader the fleet node already owns, so only the
# two fields a replacement decides on are asked for here.
REPLACEMENT_JOB_QUERY = ("squeue", "-h", "-j", "{job}", "-o", "%T|%u")

# What a scheduler prints on stderr when it is asked about a job id it does not
# know. Some clients answer a successful query naming no job; this one reports
# the unknown id as an error and exits non-zero, so the marker is what tells an
# unknown job apart from a scheduler that could not be asked at all — the two
# must not be read as each other.
SCHEDULER_UNKNOWN_JOB_MARKER = "invalid job id"

# The one state a replacement may be pointed at: a job that is there to run
# steps under. A pending job has not started, and every other state names a job
# that has left the queue or never began, so neither is a place to run.
REPLACEMENT_RUNNING_STATE = "running"

_ALLOCATION_TIMEOUT_SECONDS = 60.0

# How long an ensure waits, and how often it asks again, while the liveness
# probe cannot be completed at all. A question that never reached the scheduler
# says nothing about the allocation, so the ensure waits and asks again rather
# than reading the silence as an absent reservation; the bound is what stops an
# unreachable controller from hanging the command, and it ends in a reported
# unknown rather than in a second allocation.
_UNKNOWN_PROBE_INTERVAL_SECONDS = 0.25
_UNKNOWN_PROBE_ATTEMPTS = 4


def reservation_path() -> Path:
    """Path of the published reservation record, one record for the host.

    One allocation is held for the whole fleet, so its record is unkeyed: a
    project that did not publish it must still resolve it, and every placed run
    occupies the same roster whichever project dispatched it. The record was
    keyed by project while a reservation was held per project; that keying is
    retired because it made a shared allocation unreadable by anyone but its
    publisher.
    """
    from reckon.crew.runs import crew_home

    base = crew_home() / "placement"
    return base / "reservation.json"


def reservation_lock_path() -> Path:
    """Path of the lock that serialises holding the one reservation.

    The lock lives beside the record it guards, in the same crew state every
    dispatch on the workstation reads, because the thing it protects is the one
    shared allocation rather than any single session's copy of it.
    """
    from reckon.crew.runs import crew_home

    return crew_home() / "placement" / "reservation.lock"


@contextlib.contextmanager
def _reservation_hold_lock() -> Iterator[None]:
    """Hold the lock that makes holding the reservation idempotent under a race.

    The claim is a POSIX advisory lock on a file beside the record — an
    operation that is atomic between the separate processes dispatching on this
    workstation, which a lock held inside one process is not. Several dispatches
    starting at once therefore produce one allocation rather than one each:
    exactly one proceeds through the read, the probe and the submission while
    the rest block here, and each of those reads the record it published once
    the lock is released. Without it every racer sees no record, concludes the
    reservation is absent, and mints an allocation of its own — the per-worker
    minting this module exists to remove.
    """
    path = reservation_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def legacy_reservation_path(project: str) -> Path:
    """Path a pre-sharing release keyed by project; read for migration only.

    An existing per-project record must not be orphaned by the change to one
    unkeyed record: a dispatch under the project that holds one still resolves
    it until the ensure command rewrites it unkeyed.
    """
    from reckon.crew.runs import crew_home

    return crew_home() / "placement" / project / "reservation.json"


def _read_record(path: Path) -> dict[str, Any] | None:
    """A record read from one path, or None when absent or unreadable.

    A record that cannot be read answers None rather than raising: an absent
    reservation and an unreadable one both mean no reservation is available to
    place into, and the caller reports that rather than inventing an id.
    """
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def read_reservation(project: str | None = None) -> dict[str, Any] | None:
    """Read the published reservation, or None when none is held.

    A named project reads the one unkeyed record exactly as any other project
    does, because there is one shared allocation and a project that did not
    publish it must still place its workers into it. ``project`` is used only
    for migration: with no unkeyed record present, a legacy per-project record
    for that project is read so an existing reservation is not orphaned.
    """
    record = _read_record(reservation_path())
    if record is not None or project is None:
        return record
    return _read_record(legacy_reservation_path(project))


def publish_reservation(record: Mapping[str, Any], project: str | None = None) -> Path:
    """Write the reservation to the crew state every project's sessions read.

    The published file is the whole point of the design: a job id held in one
    session's memory is invisible to every other session, so a second session
    would hold its own reservation and the fleet would be back to one
    allocation per worker without anyone noticing. ``project`` is accepted so a
    caller can name the project it holds the allocation for; the record is
    written unkeyed, because the allocation is shared and every project reads it.
    """
    path = reservation_path()
    write_json_atomically(
        path, dict(record), indent=2, sort_keys=True, fsync=False, mode=None
    )
    return path


def reservation_options(
    *,
    partition: str = RESERVATION_PARTITION,
    cores: int = RESERVATION_CORES,
    memory_gb: int = RESERVATION_MEMORY_GB,
    job_name: str = RESERVATION_JOB_NAME,
) -> list[str]:
    """The argument vector that holds one allocation of the decided size."""
    return [
        RESERVATION_NO_SHELL_OPTION,
        f"--job-name={job_name}",
        f"--partition={partition}",
        f"--cpus-per-task={int(cores)}",
        f"--mem={int(memory_gb)}G",
    ]


def step_memory_gb(
    declared: int | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """Whole gigabytes a worker step is bounded to.

    A declared value wins; then the environment override; then the default. The
    value is a whole number of gigabytes because that is the unit both the
    scheduler option and the reservation's own memory are stated in. A declared
    or environment value that cannot be read as a whole number falls back rather
    than raising, so a malformed override never leaves a step unbounded.
    """
    if declared is not None:
        return int(declared)
    env = os.environ if environ is None else environ
    raw = str(env.get(STEP_MEMORY_ENV) or "").strip()
    if raw:
        try:
            return int(raw.rstrip("Gg"))
        except ValueError:
            pass
    return DEFAULT_STEP_MEMORY_GB


def step_memory_option(memory_gb: int) -> str:
    """The scheduler option that bounds a step to ``memory_gb`` gigabytes."""
    return f"--mem={int(memory_gb)}G"


def step_memory_above_ceiling(memory_gb: int) -> bool:
    """Whether a step of this size must leave the fleet allocation."""
    return int(memory_gb) > STEP_MEMORY_CEILING_GB


_MEMORY_UNIT_BYTES: dict[str, int] = {
    "": 1024**2,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
}

_MEMORY_OPTION_NAMES = ("--mem", "--mem-per-cpu")
_CPU_OPTION_NAMES = ("--cpus-per-task", "-c")


def option_values(
    options: Sequence[str], names: Sequence[str]
) -> list[tuple[str, str]]:
    """Every ``names`` option stated among ``options``, paired with its value.

    A scheduler resource request is a plain option string written either
    ``--name=value`` or ``--name value``, so both spellings are read. Anything
    else is not this option and is skipped, so a placement's unrelated flags
    never parse as a request.

    This is the one reader of a placement's declared resources; an admission
    summary that priced a share and a launch that placed no step for it were
    reading two spellings of one declaration and disagreeing about the second.
    """
    found: list[tuple[str, str]] = []
    tokens = [str(option) for option in options or ()]
    for index, token in enumerate(tokens):
        for name in names:
            if token.startswith(f"{name}="):
                found.append((name, token[len(name) + 1 :].strip()))
            elif token == name and index + 1 < len(tokens):
                found.append((name, tokens[index + 1].strip()))
    return [(name, value) for name, value in found if value]


def memory_figure_bytes(text: str) -> int | None:
    """A scheduler memory figure in bytes, or None for anything else.

    A figure is a bare number of mebibytes or a number with a KiB/MiB/GiB/TiB
    suffix. An unreadable figure is None rather than zero: zero would read as a
    bound of nothing, and a bound of nothing places no step at all.

    This is the one reader of a scheduler memory figure in either direction — a
    bound a placement states, and a size the scheduler reports back — so the two
    can never disagree about what ``96G`` or a bare ``98304`` means. A caller
    that publishes the answer in another unit converts from here rather than
    parsing the string a second time.
    """
    match = re.fullmatch(r"\s*(\d+)\s*([KMGTkmgt]?)[Bb]?\s*", str(text))
    if not match:
        return None
    return int(match.group(1)) * _MEMORY_UNIT_BYTES[match.group(2).upper()]


def declared_task_count(options: Sequence[str]) -> int | None:
    """The task count a placement declares, or None when it declares none."""
    for _name, value in option_values(options, _CPU_OPTION_NAMES):
        try:
            count = int(value)
        except ValueError:
            return None
        return None if count < 1 else count
    return None


def declared_memory_bytes(options: Sequence[str]) -> int | None:
    """The whole bound a placement declares, in bytes, or None.

    ``--mem`` states the step's total directly. ``--mem-per-cpu`` states it per
    task, so it is read as its product with the task count the placement
    declares beside it; a per-task figure with no task count is a bound whose
    total is unknown, and None is returned rather than a figure that would
    price the step as a single task.

    None means no whole bound was read — either no bound was declared at all,
    or the one declared cannot be totalled. A caller that must tell those apart
    asks :func:`unresolved_memory_declaration`, which names the option that
    could not be totalled.
    """
    declared = option_values(options, _MEMORY_OPTION_NAMES)
    for name, value in declared:
        if name == "--mem":
            return memory_figure_bytes(value)
    per_task = [
        memory_figure_bytes(value)
        for name, value in declared
        if name == "--mem-per-cpu"
    ]
    if not per_task or per_task[0] is None:
        return None
    count = declared_task_count(options)
    return None if count is None else per_task[0] * count


def unresolved_memory_declaration(options: Sequence[str]) -> str | None:
    """A declared bound whose total cannot be read, or None.

    ``--mem-per-cpu`` is refused rather than approximated: without a task count
    beside it nothing says how much memory the step may actually take. A
    refusal is the safe answer because the alternative — pricing the step as
    one task — understates it by the task count, which is the direction that
    overfills the allocation.
    """
    count = declared_task_count(options)
    for name, value in option_values(options, _MEMORY_OPTION_NAMES):
        if (
            name == "--mem-per-cpu"
            and memory_figure_bytes(value) is not None
            and count is None
        ):
            return f"--mem-per-cpu={value}"
    return None


def declared_memory_gb(options: Sequence[str]) -> int | None:
    """The bound a placement declares, rounded up to whole gigabytes, or None.

    A placement declares a step's memory the way it declares any other resource
    request, as a scheduler option string, so the value is read back from the
    options rather than from a key of its own. A figure below a whole gigabyte
    counts as a gigabyte so that a bound is never read as smaller than it is.
    """
    total = declared_memory_bytes(options)
    if total is None:
        return None
    return -(-total // (1024**3))


def debug_partition(partition: str = RESERVATION_PARTITION) -> str:
    """The debug partition a step too large for the allocation is placed on."""
    return f"{partition}{DEBUG_PARTITION_SUFFIX}"


def step_prefix(job_id: str, options: list[str]) -> list[str]:
    """The options that place a step inside a held allocation.

    ``--overlap`` is what makes the allocation a place to run rather than a
    quota to queue behind, and the job id is resolved from the published
    reservation rather than taken on the command line, so every session's
    workers land in the one allocation.

    Every step is bounded with ``--mem`` so that an undeclared worker cannot
    grow to the allocation's limit, where the scheduler's OOM killer would pick
    the largest process anywhere in the job. The default bound is stated here,
    in the one owner of a step's options; a caller that already states a bound
    keeps it, in whichever spelling it was written and whether it is a total or
    a per-task figure, so no step is given two conflicting memory requests.
    """
    placed = list(options)
    if not option_values(placed, _MEMORY_OPTION_NAMES):
        placed = [step_memory_option(step_memory_gb()), *placed]
    return ["--overlap", f"--jobid={job_id}", *placed]


def own_job_prefix(
    command: Sequence[str],
    *,
    memory_gb: int,
    partition: str = RESERVATION_PARTITION,
    time_limit: str = DEBUG_JOB_TIME_CEILING,
    job_name: str | None = None,
) -> list[str]:
    """The scheduler options that run a step as a job of its own.

    A step sized past the allocation's ceiling is placed on a debug partition
    rather than inside the allocation, where a whole node serves it. The command
    is carried as arguments rather than as a wrapped script, so the standard
    streams the caller composed stay the ones that run and the worker's output
    reaches the supervisor's pipe as it does for a step of the allocation.

    These are options only and never the scheduler's own name: the caller has
    already resolved that executable and prepends it, so the invocation names
    the client exactly once.
    """
    argv = [
        f"--partition={debug_partition(partition)}",
        step_memory_option(memory_gb),
        f"--time={time_limit}",
    ]
    if job_name:
        argv.append(f"--job-name={job_name}")

    argv.extend(str(item) for item in command)
    return argv


def debug_job_time_limit(
    time_budget: str | None, *, ceiling: str = DEBUG_JOB_TIME_CEILING
) -> str:
    """The time limit a step of its own is submitted under, capped at the ceiling.

    A debug partition here is a one-hour window: the scheduler's own maximum is
    an hour and its default is none, so an omitted limit leaves the job pending
    rather than bounding it. A declared budget is honoured up to that window and
    stated as the window when it asks for more, because asking for more on such
    a partition pends the submission instead of shortening the fence. A budget
    that cannot be read is treated as the window rather than as no limit.
    """
    ceiling_seconds = _duration_seconds(ceiling)
    stated = _duration_seconds(time_budget) if time_budget else None
    seconds = ceiling_seconds if stated is None else min(stated, ceiling_seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _duration_seconds(value: str) -> int | None:
    """A budget written as an ``h/m/s`` suffix pair, in seconds, or None.

    The scheduler states its own limits as ``HH:MM:SS``; a budget may also be
    written as the number-and-suffix form a dispatch declares. Both are read so
    the smaller of the two can be taken, and anything else is None.
    """
    text = str(value).strip()
    clock = re.fullmatch(r"(\d+):([0-5]\d)(?::([0-5]\d))?", text)
    if clock:
        hours, minutes, secs = clock.group(1), clock.group(2), clock.group(3) or "0"
        return int(hours) * 3600 + int(minutes) * 60 + int(secs)
    suffix = re.fullmatch(r"(\d+)([hms])", text, re.IGNORECASE)
    if not suffix:
        return None
    return int(suffix.group(1)) * {"h": 3600, "m": 60, "s": 1}[suffix.group(2).lower()]


def without_slurm_job_identity(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    """A copy of ``environment`` without the allocation's job or step identity.

    A step client inherits the held allocation from its environment, and that
    inheritance is the whole reason a client started inside the allocation runs
    as a step of it: with the identity gone the client asks for an allocation of
    its own. Without this the job the caller placed for a step too large for the
    allocation would be that same allocation's step, on the wrong partition and
    against the wrong bound.
    """
    kept: dict[str, str] = {}
    for key, value in (environment or {}).items():
        name = str(key)
        if name.upper().startswith(SLURM_JOB_ENVIRONMENT_ROOTS):
            continue
        kept[key] = value
    return kept


def _reservation_query_placement() -> dict[str, Any]:
    return {
        "scheduler": RESERVATION_SCHEDULER,
        "state_query": list(RESERVATION_STATE_QUERY),
        "reason_query": list(RESERVATION_REASON_QUERY),
    }


def reservation_state(
    record: Mapping[str, Any] | None,
    runner: Callable[[list[str]], str | None] | None = None,
) -> bool | None:
    """What the scheduler answers about the record's job, and whether it answered.

    True the scheduler reports the job in the system, False it ran and named no
    such job, None the question could not be asked at all. The last two are
    kept apart because they mean opposite things: a job the scheduler has
    answered for has left the queue, while a query that never ran has said
    nothing about the job, and a caller that collapses them reads every
    unreadable scheduler as a released reservation.
    """
    from reckon.crew.runs import scheduler_job_state

    if not record or not record.get("job_id"):
        return None
    state = scheduler_job_state(
        _reservation_query_placement(), record.get("job_id"), runner
    )
    if state is None:
        return None
    return bool(state.strip())


def reservation_alive(
    record: Mapping[str, Any] | None,
    runner: Callable[[list[str]], str | None] | None = None,
) -> bool:
    """Whether the reservation a record names may be placed into.

    Answered from the scheduler rather than from a pid, because the pid a
    holding client leaves behind belongs to whoever submitted the reservation.

    Only an answer releases the reservation. A question that could not be asked
    at all — no reporting client on the PATH, a non-zero exit, a controller
    that did not answer inside the query's bound — leaves a record naming a job
    id standing, because reading an unreadable probe as an absent reservation
    hands every worker of the placement the caller's fallback path, and there
    the declared wrapping mints an allocation per worker: one reservation each,
    silently, for as long as the query stays unreadable.

    A record naming no job id is not a reservation at all, and answers False.
    """
    if not record or not record.get("job_id"):
        return False
    return reservation_state(record, runner) is not False


def _parse_job_id(completed: subprocess.CompletedProcess[str]) -> str | None:
    """Read the allocation's job id out of the submitter's own answer.

    The id is read rather than guessed: a submitter that answered no
    identifier records why instead of a fabricated id, the way a backend's
    declared job-id probe does.
    """
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    for token in reversed(text.split()):
        stripped = token.strip(".,:;")
        if stripped.isdigit():
            return stripped
    return None


def _probe_answer(
    record: Mapping[str, Any],
    alive_probe: Callable[[Mapping[str, Any] | None], bool] | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> bool | None:
    """What the probe says about a recorded allocation: held, gone, or unasked.

    Three answers, and the last two mean opposite things. True the allocation
    may be placed into, False the scheduler ran and named no such job so it has
    left, None the question could not be asked at all. The ensure decides on
    that distinction, never on the presence of a record: a record naming no job
    is not a reservation and answers False, while a question that never reached
    the scheduler is unknown and must not read as a released allocation.
    """
    if alive_probe is not None:
        return bool(alive_probe(record))
    if not record.get("job_id"):
        return False
    return reservation_state(record, runner)


def _await_probe(
    record: Mapping[str, Any],
    alive_probe: Callable[[Mapping[str, Any] | None], bool] | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> bool | None:
    """The probe's answer after retrying while it stays unknown, or None.

    The unknown case is the one a dispatch must not turn into a second
    allocation: the record names a job, the scheduler could not be asked about
    it, and the allocation may well be alive and merely invisible, so the ensure
    waits and asks again and never submits while the answer stays unknown. The
    wait is bounded so an unreachable controller ends in a reported unknown
    rather than a hang; a definite answer reached meanwhile is returned as it is.
    """
    for _ in range(_UNKNOWN_PROBE_ATTEMPTS - 1):
        time.sleep(_UNKNOWN_PROBE_INTERVAL_SECONDS)
        answer = _probe_answer(record, alive_probe, runner)
        if answer is not None:
            return answer
    return None


def _held_result(record: Mapping[str, Any], *, unknown: bool = False) -> dict[str, Any]:
    """The report for a reservation that is already held and not replaced.

    The read-back states the same reach the arming hold states: the cap and the
    projects it counts right now. A session that finds the reservation held is
    still about to run inside it, so the cap's blast radius is the reservation's
    own fact at the moment it is read rather than only an arming-time detail.
    """
    job_id = record.get("job_id")
    reach = _roster_reach()
    if unknown:
        detail = (
            f"the reservation {job_id} is recorded but the scheduler could not "
            f"be asked about it; started nothing; {reach['statement']}"
        )
    else:
        detail = (
            f"the reservation {job_id} is held; started nothing; {reach['statement']}"
        )
    result: dict[str, Any] = {
        "job_id": job_id,
        "held": True,
        "started": False,
        "record": record,
        "detail": detail,
        "reason": "already-held",
        "roster_reach": reach,
    }
    if unknown:
        result["probe"] = "unknown"
    return result


def _adopted_result(record: Mapping[str, Any]) -> dict[str, Any]:
    """The report for a live fleet allocation adopted rather than held anew.

    ``started`` is False because nothing was submitted: the allocation was
    already up and is published, not obtained. ``adopted`` marks the record as
    one that names a job the fleet was already running, which a reader can tell
    apart from a reservation this command minted.
    """
    size = record.get("size") or {}
    return {
        "job_id": record.get("job_id"),
        "held": True,
        "started": False,
        "adopted": True,
        "record": record,
        "detail": (
            f"adopted the live supervisor-running allocation "
            f"{record.get('job_id')} on {record.get('partition')} with "
            f"{size.get('cores')} cores and {size.get('memory_gb')} GB; "
            "submitted nothing; "
            f"{record['roster_reach']['statement']}"
        ),
        "reason": "adopted",
        "roster_reach": record.get("roster_reach"),
    }


def _target_job_argv(job_id: str) -> list[str]:
    """The argument vector that asks about one named job's state and owner."""
    return [part.replace("{job}", job_id) for part in REPLACEMENT_JOB_QUERY]


def _replacement_owner() -> str:
    """This user, as the scheduler would name the owner of a job of theirs."""
    return os.environ.get("USER") or getpass.getuser()


def _read_target_job(
    job_id: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> dict[str, Any]:
    """The state and owner the scheduler reports for a named job.

    Raises the refusal to raise when the job cannot be read. A question that
    could not be asked is kept apart from a scheduler that answered for a job
    and did not name it: the first says nothing about the job and must not be
    read as its absence, while the second is the scheduler's own statement that
    no such job is in the queue. This client states that absence as an error
    carrying the unknown-job marker, so both an empty successful answer and a
    marked error are read as a job the scheduler does not know.

    ``state`` and ``owner`` are returned as the scheduler printed them; the
    caller compares them against its own vocabulary rather than trusting the
    spelling here.
    """
    run = runner or subprocess.run
    try:
        completed = run(
            _target_job_argv(job_id),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrewError(
            "cannot replace the placement reservation: the scheduler could not "
            f"be asked about job {job_id} — {exc}; the record is unchanged"
        ) from exc
    if completed.returncode != 0:
        stderr = completed.stderr or ""
        if SCHEDULER_UNKNOWN_JOB_MARKER in stderr.casefold():
            # This cluster's client reports a job id it does not know as an
            # error rather than as a successful query naming no job, so the
            # marker is what names the job unknown instead of reading a
            # scheduler that answered as one that could not be asked.
            raise CrewError(
                "cannot replace the placement reservation: the scheduler does not "
                f"know job {job_id}; the record is unchanged"
            )
        reason = stderr.strip()
        raise CrewError(
            "cannot replace the placement reservation: the scheduler could not "
            f"be asked about job {job_id}"
            + (f" — {reason}" if reason else "")
            + "; the record is unchanged"
        )
    lines = (completed.stdout or "").strip().splitlines()
    if not lines:
        raise CrewError(
            "cannot replace the placement reservation: the scheduler does not "
            f"know job {job_id}; the record is unchanged"
        )
    parts = lines[0].split("|")
    state = parts[0].strip()
    owner = parts[1].strip() if len(parts) > 1 else ""
    if not state or not owner:
        raise CrewError(
            "cannot replace the placement reservation: the scheduler named no "
            f"state and owner for job {job_id}; the record is unchanged"
        )
    return {"state": state, "owner": owner}


def _replacement_refusal(job_id: str, target: Mapping[str, Any]) -> str | None:
    """Why a target job may not be pointed at, or None when it may.

    Each refusal names the state it found, because the remedy differs: a pending
    job will run but has not started, another user's job is not ours to place
    workers into, and a job that is not running has left the queue or never
    began.
    """
    state = str(target.get("state") or "")
    if state.casefold() == "pending":
        return (
            f"job {job_id} is pending, not running; point the reservation at a "
            "job that has started, or wait for it to run"
        )
    if state.casefold() != REPLACEMENT_RUNNING_STATE:
        return f"job {job_id} is {state}, not running; the record is unchanged"
    me = _replacement_owner()
    owner = str(target.get("owner") or "")
    if owner != me:
        return (
            f"job {job_id} belongs to {owner}, not this user ({me}); the record "
            "is unchanged"
        )
    return None


def replace_reservation(
    *,
    job_id: str,
    project: str | None = None,
    session: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Point the reservation record at an explicit, already-running job.

    The automatic ensure replaces a record only once its own job has left the
    queue, so a fleet that must move to a new allocation before the old one
    drains has no way to say so. This is that way: it names the job the record
    is to hold, refuses a target the scheduler cannot show is running under this
    user, and records the job it replaced beside the new one.

    Refusals are distinct because the remedy differs. Steps already running in
    the replaced job are untouched — only the record later dispatches and
    resumes read changes, so every worker placed after this call lands in the
    new job while those already running finish where they are.
    """
    if not job_id or not str(job_id).strip():
        raise CrewError("cannot replace the placement reservation: no job id was named")
    job_id = str(job_id).strip()
    with _reservation_hold_lock():
        target = _read_target_job(job_id, runner)
        refusal = _replacement_refusal(job_id, target)
        if refusal is not None:
            raise CrewError(f"cannot replace the placement reservation: {refusal}")
        previous = read_reservation(project)
        previous_job = previous.get("job_id") if previous else None
        # The shape is read through the allocation-shape reader the fleet node
        # already owns, so the published record describes the target's real
        # allocation rather than a second parse of the same scheduler fields. A
        # shape that cannot be read falls back to the declared defaults: the
        # boundary checks have already admitted the job, and a record naming the
        # real job id is still what a dispatch resolves.
        from reckon.crew import fleet_node

        shape = fleet_node.allocation_shape(job_id, runner=runner) or {}
        partition = str(shape.get("partition") or RESERVATION_PARTITION)
        cores = int(shape.get("cores") or RESERVATION_CORES)
        memory_gb = shape.get("memory_gb")
        memory_gb = int(memory_gb) if memory_gb is not None else RESERVATION_MEMORY_GB
        reach = _roster_reach()
        record = {
            "job_id": job_id,
            "scheduler": RESERVATION_SCHEDULER,
            "step_scheduler": RESERVATION_STEP_SCHEDULER,
            "options": reservation_options(
                partition=partition, cores=cores, memory_gb=memory_gb
            ),
            "size": {"cores": int(cores), "memory_gb": int(memory_gb)},
            "partition": partition,
            "roster_limit": RESERVATION_ROSTER_LIMIT,
            "roster_basis": RESERVATION_ROSTER_BASIS,
            "roster_reach": reach,
            "held_for_project": project,
            "held_by_session": session,
            "held_at": time.time() if now is None else now,
            "replaced": previous_job,
        }
        publish_reservation(record, project)
        if previous_job:
            detail = (
                f"replaced reservation {previous_job} with {job_id} on "
                f"{partition} with {cores} cores and {memory_gb} GB; "
                f"{reach['statement']}"
            )
        else:
            detail = (
                f"pointed the reservation at running job {job_id} on {partition} "
                f"with {cores} cores and {memory_gb} GB; there was no record to "
                f"replace; {reach['statement']}"
            )
        return {
            "job_id": job_id,
            "replaced": previous_job,
            "held": True,
            "started": False,
            "record": record,
            "detail": detail,
            "reason": "replaced",
            "roster_reach": reach,
        }


def ensure_reservation(
    *,
    project: str | None = None,
    session: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    alive_probe: Callable[[Mapping[str, Any] | None], bool] | None = None,
    partition: str = RESERVATION_PARTITION,
    cores: int = RESERVATION_CORES,
    memory_gb: int = RESERVATION_MEMORY_GB,
    now: float | None = None,
) -> dict[str, Any]:
    """Hold the reservation if absent, and report it if present.

    Idempotent in the sense that decides whether a second call disturbs a live
    reservation: a reservation already held and still in the system is
    reported and nothing is submitted, so the command every session is told to
    run does not mint a second allocation. A record whose job has left the
    system is replaced, because reporting a finished allocation as held would
    send workers into nothing.

    Idempotence is fleet-wide, because the allocation is: the first session to
    ask holds it and every later session, whatever project it dispatches for,
    finds it already held and starts nothing.

    With no record and a live supervisor-running allocation already held, that
    allocation is adopted instead of a second one minted beside it: its job is
    found from the queue through the fleet node's comment search, its declared
    shape is read from the scheduler, and the record is published naming it with
    nothing submitted. Only a job the fleet record names is adopted, so a job
    carrying the comment whose batch step does not run the supervisor is not.

    It is also idempotent under a race, because several dispatches start at
    once and the allocation is shared: the whole read-decide-hold-publish
    sequence runs under a single cross-process lock, so exactly one of the
    racers asks the scheduler for an allocation and the rest read the record it
    published. Idempotence rests on the liveness probe, never on the absence of
    a record, which is equally true while another dispatch is mid-hold.

    A record the scheduler cannot be asked about is reported rather than
    replaced, for the same reason a held one is: a query that did not run says
    nothing about the job, and minting an id for it spends a second allocation
    on a reservation that may well be held. Where the probe stays unanswerable
    the ensure waits and retries, submitting nothing, and reports the unknown
    once its bound is reached.
    """
    with _reservation_hold_lock():
        return _ensure_reservation_locked(
            project=project,
            session=session,
            runner=runner,
            alive_probe=alive_probe,
            partition=partition,
            cores=cores,
            memory_gb=memory_gb,
            now=now,
        )


def _unanswerable_queue(exc: BaseException) -> CrewError:
    """The refusal when a queue cannot be asked about a recorded fleet job.

    A recorded job whose allocation cannot be queried is unknown, not absent:
    the ensure refuses and submits nothing rather than falling through to mint
    a second allocation beside the one the record names.
    """
    return CrewError(
        "cannot hold the placement reservation: squeue could not be run — "
        f"{exc}; nothing was submitted"
    )


def _adopt_live_fleet_allocation(
    *,
    project: str | None,
    session: str | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    now: float | None,
) -> dict[str, Any] | None:
    """The supervisor-running allocation already held for the fleet, or None.

    An allocation this command did not hold may still be the fleet's: the
    supervisor-running kind — the whole-node job whose batch step runs
    ``fleet-supervisor`` and publishes the fleet record — is exactly what every
    session should place into, so it is adopted rather than a second allocation
    minted beside it. The job is found from the queue through the fleet node's
    own comment search, and only the job the fleet record names is adopted: that
    record is published by the allocation's own batch step, so it is what tells
    the supervisor-running allocation apart from any other job carrying the
    comment. The shape is read from the scheduler, so the published record
    describes the size the adopted job was actually submitted with, and nothing
    is submitted here. A recorded job the scheduler cannot be asked about — a
    client that will not run, or a query that fails — is unknown rather than
    absent: adoption refuses with a CrewError and submits nothing, never falling
    through to mint a second allocation beside the one the record names; only a
    record naming no job, or a queue that answers without it, is an answered
    absence and returns None.
    """
    from reckon.crew import fleet_node

    recorded = fleet_node.recorded_job_id()
    if not recorded:
        return None
    try:
        jobs = fleet_node.query_jobs(fleet_node.fleet_size().account, runner=runner)
    except (fleet_node.FleetNodeError, OSError, subprocess.SubprocessError) as exc:
        raise _unanswerable_queue(exc) from exc
    job = fleet_node.find_allocation(jobs, preferred=recorded)
    if job is None or str(job.get("jobid", "")).strip() != recorded:
        return None
    try:
        shape = fleet_node.allocation_shape(recorded, runner=runner)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _unanswerable_queue(exc) from exc
    if shape is None or shape.get("memory_gb") is None:
        return None
    partition = str(shape["partition"])
    cores = int(shape["cores"])
    memory_gb = int(shape["memory_gb"])
    return {
        "job_id": recorded,
        "scheduler": "sbatch",
        "step_scheduler": RESERVATION_STEP_SCHEDULER,
        "options": reservation_options(
            partition=partition, cores=cores, memory_gb=memory_gb
        ),
        "size": {"cores": cores, "memory_gb": memory_gb},
        "partition": partition,
        "memory": str(shape.get("memory") or ""),
        "roster_limit": RESERVATION_ROSTER_LIMIT,
        "roster_basis": RESERVATION_ROSTER_BASIS,
        "roster_reach": _roster_reach(),
        "held_for_project": project,
        "held_by_session": session,
        "held_at": time.time() if now is None else now,
        "replaced": None,
        "adopted": True,
    }


def _ensure_reservation_locked(
    *,
    project: str | None,
    session: str | None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
    alive_probe: Callable[[Mapping[str, Any] | None], bool] | None,
    partition: str,
    cores: int,
    memory_gb: int,
    now: float | None,
) -> dict[str, Any]:
    """Hold the reservation, called with the cross-process lock already held."""
    existing = read_reservation(project)
    if existing:
        answer = _probe_answer(existing, alive_probe, runner)
        if answer is None:
            answer = _await_probe(existing, alive_probe, runner)
        if answer is not False:
            return _held_result(existing, unknown=answer is None)
    else:
        adopted = _adopt_live_fleet_allocation(
            project=project, session=session, runner=runner, now=now
        )
        if adopted is not None:
            publish_reservation(adopted, project)
            return _adopted_result(adopted)

    argv = [
        RESERVATION_SCHEDULER,
        *reservation_options(partition=partition, cores=cores, memory_gb=memory_gb),
    ]
    run = runner or subprocess.run
    try:
        completed = run(
            argv,
            capture_output=True,
            text=True,
            timeout=_ALLOCATION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CrewError(
            f"cannot hold the placement reservation: {RESERVATION_SCHEDULER} "
            f"could not be run — {exc}"
        ) from exc
    job_id = _parse_job_id(completed)
    if job_id is None:
        raise CrewError(
            "cannot hold the placement reservation: "
            f"{RESERVATION_SCHEDULER} answered no job identifier "
            f"(exit {completed.returncode})"
        )
    reach = _roster_reach()
    record = {
        "job_id": job_id,
        "scheduler": RESERVATION_SCHEDULER,
        "step_scheduler": RESERVATION_STEP_SCHEDULER,
        "options": reservation_options(
            partition=partition, cores=cores, memory_gb=memory_gb
        ),
        "size": {"cores": int(cores), "memory_gb": int(memory_gb)},
        "partition": partition,
        "roster_limit": RESERVATION_ROSTER_LIMIT,
        "roster_basis": RESERVATION_ROSTER_BASIS,
        "roster_reach": reach,
        # The project that held it is named here and carried nowhere else: the
        # record's path is unkeyed, because one shared allocation has one record
        # every project reads.
        "held_for_project": project,
        "held_by_session": session,
        "held_at": time.time() if now is None else now,
        "replaced": existing.get("job_id") if existing else None,
    }
    publish_reservation(record, project)
    return {
        "job_id": job_id,
        "held": True,
        "started": True,
        "record": record,
        "detail": (
            f"held reservation {job_id} on {partition} with {cores} cores and "
            f"{memory_gb} GB; {reach['statement']}"
        ),
        "reason": "held",
        "roster_reach": reach,
    }


def _live_classifier_process_alive(pid: Any) -> bool | None:
    """The process-liveness reader the live classifier consults, by module.

    Read through the owning module at call time rather than bound by an
    import-time snapshot, so a caller that substitutes the reader on its own
    module — which is how the classifier itself reaches liveness — is not
    bypassed here.
    """
    from reckon.crew import runs as runs_module

    return runs_module.process_alive(pid)


def _still_launching(record: Mapping[str, Any], moment: float) -> bool:
    """Whether a placed run with no recorded worker is inside its launch window.

    Measured from the current attempt's start, resolved the same way the live
    classifier resolves it — the pointer's own attempt clock, falling back to
    the attempt record a supervisor publishes beside it, and then to the
    dispatch moment — against the window a fresh dispatch gets. A run that has
    named no process and whose attempt began outside that window is not still
    arriving and holds no seat.

    An attempt clock that cannot be read at all is not evidence that the run has
    stopped launching, so it holds its seat: the roster guards against
    oversubscription, and freeing a seat on an unreadable clock would admit work
    on no evidence.
    """
    from reckon._timestamps import parse_utc
    from reckon.crew import recovery as recovery_module

    started = recovery_module._attempt_started_seconds(record)
    if started is None:
        dispatched = parse_utc(str(record.get("created_at") or ""))
        started = dispatched.timestamp() if dispatched is not None else None
    if started is None:
        return True
    elapsed = moment - started
    return 0 <= elapsed < recovery_module.LAUNCH_WINDOW_SECONDS


def _placed_worker_can_hold_memory(
    record: Mapping[str, Any],
    alive: Callable[[Any], bool | None],
    moment: float,
) -> bool:
    """Whether a placed run's worker can still hold memory in the allocation.

    A recorded pid is asked directly: a live one holds the seat and an exited
    one frees it. A pid the probe cannot answer for at all is counted as
    holding, for the same reason an unreadable launch clock is — an unreadable
    probe is not evidence of an exit, and the roster may only free a seat on
    evidence that the worker is gone. A pointer that has not recorded its worker
    yet holds its seat only while it is still launching, so a run that never
    names a process does not hold a seat indefinitely.
    """
    pid = record.get("pid")
    if pid is not None:
        return alive(pid) is not False
    return _still_launching(record, moment)


def occupying_the_reservation(
    pointers: Iterable[Mapping[str, Any]],
    *,
    alive: Callable[[Any], bool | None] | None = None,
    now: float | None = None,
) -> list[Mapping[str, Any]]:
    """The pointers that occupy the shared reservation's roster.

    A run that was placed is a step inside the one allocation, so it occupies
    the roster whichever project dispatched it; a run whose record names no
    placement was never placed and runs outside the reservation, so it holds no
    seat. The count is taken from the recorded fact that a run was placed,
    rather than inferred from project or backend membership: scoping it to the
    reading project let a project that never held the allocation be charged for
    runs that were never placed at all, and counting the whole backend is the
    same error pointed the other way.

    A placement is a seat only while the placed worker can still hold memory in
    the allocation. A placed run whose worker has exited holds no seat, whatever
    its manifest says and whether or not it has been promoted: a
    complete-but-unpromoted run, a blocked run awaiting resume, and a run
    waiting on an external condition with no live process all hold none, so the
    cap counts the workers resident rather than every run ever placed. A seat is
    held by a placed pointer whose recorded worker or supervisor pid is alive,
    or one still launching — no worker pid recorded yet and its current attempt
    inside the window a fresh dispatch gets — because a run that has not yet
    named its process is still on its way in.

    Liveness is the same read the live classifier takes: the process-liveness
    reader is :func:`reckon.crew.runs.process_alive`, reached through its own
    module so a substituted probe is honoured. ``alive`` and ``now`` are the
    seam a caller hands its own probe and clock through, so a test reaches this
    without a process table.

    This is deliberately NOT the same population as the lane's own concurrency
    bound. A served lane is consumed by every caller that sends it a request,
    placed or not, so its ceiling is rightly counted across them all; an
    allocation is consumed only by the workers running inside it as steps, so
    its roster counts those and nothing else.
    """
    probe = _live_classifier_process_alive if alive is None else alive
    moment = time.time() if now is None else now
    return [
        pointer
        for pointer in pointers
        if pointer.get("placement")
        and _placed_worker_can_hold_memory(pointer, probe, moment)
    ]


def roster_occupants(
    pointers: Iterable[Mapping[str, Any]],
    *,
    alive: Callable[[Any], bool | None] | None = None,
    now: float | None = None,
) -> list[Mapping[str, Any]]:
    """The pointers holding a seat in the reservation's roster.

    One selection for the two readers that must agree: the reach statement a
    hold carries and the dispatch guard that refuses past the cap. A pointer
    holds a seat when it is still live — a terminal phase describes a run that
    has stopped, whatever its stored record still names — it recorded a
    placement, and its placed worker can still hold memory in the allocation.
    The reservation is host-global and one unkeyed record holds it, so the
    population is every backend's and every project's, never one of each.
    """
    from reckon.crew.node import _TERMINAL_RUN_PHASES

    return occupying_the_reservation(
        [
            pointer
            for pointer in pointers
            if str(pointer.get("phase") or "") not in _TERMINAL_RUN_PHASES
        ],
        alive=alive,
        now=now,
    )


def _roster_reach() -> dict[str, Any]:
    """The roster cap's reach, and the projects it counts against it right now.

    One unkeyed record holds the host's allocation and the cap is summed over
    every project's placed runs, so a project that arms a reservation throttles
    the whole host's fleet. That reach is invisible in the arming project's own
    flight configuration, so the hold reads the live pointers and states it: the
    projects whose placed runs the cap counts at this moment, resolved through
    the same population and the same liveness reading the dispatch guard counts
    with, because both call :func:`roster_occupants`. The projects are named
    rather than merely counted, because a reader at the point of the decision
    needs to see whose work the cap reaches, not just how far it extends.
    """
    from reckon.crew import runs as runs_module

    counted = roster_occupants(runs_module.list_live())
    projects = sorted({str(pointer.get("project") or "unknown") for pointer in counted})
    named = ", ".join(projects) if projects else "none"
    return {
        "scope": "host",
        "projects": projects,
        "statement": (
            f"the roster cap {RESERVATION_ROSTER_LIMIT} counts every project's "
            "placed live runs on this host, not only the holding project's; "
            f"placed live runs counted now from: {named}"
        ),
    }


def reservation_roster_refusal(
    occupancy: int,
    *,
    limit: int = RESERVATION_ROSTER_LIMIT,
) -> str | None:
    """The refusal text for a dispatch past the roster cap, or None.

    The cap is the only concurrency authority once nothing about a step is
    enforced, so the refusal names the limit and the quantity it was sized
    against — resident memory per worker against the reservation's reserved
    memory — rather than a core count. Naming a core count would invite raising
    it by counting cores, which is the axis that does not bind.
    """
    if occupancy < limit:
        return None
    return format_refusal(
        "D09",
        (
            f"{occupancy} workers already occupy the placement reservation's "
            f"roster, whose cap is {limit}. The cap is sized on "
            f"{RESERVATION_ROSTER_BASIS}: raise it by reading resident memory "
            "across a wave, not by counting cores."
        ),
    )
