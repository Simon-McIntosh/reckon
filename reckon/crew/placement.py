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

import json
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
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

_ALLOCATION_TIMEOUT_SECONDS = 60.0


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


def clear_reservation(project: str | None = None) -> None:
    """Remove the published reservation record, and a legacy one if named.

    The unkeyed record is always removed. A legacy per-project record is
    removed too when the caller names the project, so releasing a reservation
    does not leave a stale record for migration to read back.
    """
    paths = [reservation_path()]
    if project is not None:
        paths.append(legacy_reservation_path(project))
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


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


def step_prefix(job_id: str, options: list[str]) -> list[str]:
    """The options that place a step inside a held allocation.

    ``--overlap`` is what makes the allocation a place to run rather than a
    quota to queue behind, and the job id is resolved from the published
    reservation rather than taken on the command line, so every session's
    workers land in the one allocation.
    """
    return ["--overlap", f"--jobid={job_id}", *options]


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

    A record the scheduler cannot be asked about is reported rather than
    replaced, for the same reason a held one is: a query that did not run says
    nothing about the job, and minting an id for it spends a second allocation
    on a reservation that may well be held.
    """
    existing = read_reservation(project)
    probe = alive_probe or reservation_alive
    if existing and probe(existing):
        return {
            "job_id": existing.get("job_id"),
            "held": True,
            "started": False,
            "record": existing,
            "detail": (
                f"the reservation {existing.get('job_id')} is held; started nothing"
            ),
            "reason": "already-held",
        }

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
        # Carried in the record as well as in the path, so a reader holding the
        # record alone can still say whose allocation it is.
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
            f"{memory_gb} GB; roster cap {RESERVATION_ROSTER_LIMIT}"
        ),
        "reason": "held",
    }


def occupying_the_reservation(
    pointers: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """The live pointers that occupy the shared reservation's roster.

    A run that was placed is a step inside the one allocation, so it occupies
    the roster whichever project dispatched it; a run whose record names no
    placement was never placed and runs outside the reservation, so it holds no
    seat. The count is taken from the recorded fact that a run was placed,
    rather than inferred from project or backend membership: scoping it to the
    reading project let a project that never held the allocation be charged for
    runs that were never placed at all, and counting the whole backend is the
    same error pointed the other way.

    This is deliberately NOT the same population as the lane's own concurrency
    bound. A served lane is consumed by every caller that sends it a request,
    placed or not, so its ceiling is rightly counted across them all; an
    allocation is consumed only by the workers running inside it as steps, so
    its roster counts those and nothing else.
    """
    return [p for p in pointers if p.get("placement")]


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


def reservation_step_argv(
    reservation: Mapping[str, Any] | None,
    *,
    scheduler: str,
    options: list[str],
) -> list[str] | None:
    """The prefix that places one worker inside the held reservation, or None.

    None means no live reservation is published, and the caller falls back to
    the backend's own declared wrapping exactly as before — a host that has not
    run the ensure command is not silently given an allocation it never asked
    for.
    """
    if not reservation or not reservation.get("job_id"):
        return None
    return [scheduler, *step_prefix(str(reservation["job_id"]), options)]
