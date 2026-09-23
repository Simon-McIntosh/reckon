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
import os
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

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


def reservation_path(project: str | None = None) -> Path:
    """Path of the published reservation record, per project.

    A reservation is held by one project and its roster bounds that project's
    workers, so the record is keyed by project rather than by host. Keying it
    by host made a per-project decision carry a fleet-wide ceiling: a project
    declaring a placement published one record, and every other project's
    in-flight workers were then counted against it. Measured 2026-09-23, forty
    five runs across four repositories against a ceiling of twenty five, while
    one step ran inside the allocation the ceiling exists to protect.

    ``project`` of None is the pre-project host-global path. It is read so an
    existing record is not orphaned, and it belongs to NO project rather than
    to all of them — see ``read_reservation``.
    """
    from reckon.crew.runs import crew_home

    base = crew_home() / "placement"
    if project is None:
        return base / "reservation.json"
    return base / project / "reservation.json"


def read_reservation(project: str | None = None) -> dict[str, Any] | None:
    """Read a project's published reservation, or None when none is held.

    A record that cannot be read answers None rather than raising: an absent
    reservation and an unreadable one both mean no reservation is available to
    place into, and the caller reports that rather than inventing an id.

    A named project reads ONLY its own record. It deliberately does not fall
    back to the host-global one, because inheriting a reservation another
    project holds is the defect this keying exists to remove: the fallback
    would place this project's workers into an allocation it does not own and
    count them against a roster it did not arm.
    """
    path = reservation_path(project)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def publish_reservation(record: Mapping[str, Any], project: str | None = None) -> Path:
    """Write a project's reservation to the crew state its sessions read.

    The published file is the whole point of the design: a job id held in one
    session's memory is invisible to every other session, so a second session
    would hold its own reservation and the fleet would be back to one
    allocation per worker without anyone noticing. Keying it by project is what
    keeps that sharing inside the project that holds the allocation.
    """
    path = reservation_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(dict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def clear_reservation(project: str | None = None) -> None:
    """Remove a project's published reservation record, if one is there."""
    try:
        reservation_path(project).unlink()
    except FileNotFoundError:
        return


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


def reservation_alive(
    record: Mapping[str, Any] | None,
    runner: Callable[[list[str]], str | None] | None = None,
) -> bool:
    """Whether the reservation a record names is still held.

    Answered from the scheduler rather than from a pid, because the pid a
    holding client leaves behind belongs to whoever submitted the reservation.
    A question that cannot be asked at all answers False — the caller then
    holds a fresh reservation rather than reporting an id nothing answers for.
    """
    from reckon.crew.runs import scheduler_job_state

    if not record:
        return False
    job_id = record.get("job_id")
    if not job_id:
        return False
    state = scheduler_job_state(_reservation_query_placement(), job_id, runner)
    if state is None:
        return False
    return bool(state.strip())


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
    """Hold the project's reservation if absent, and report it if present.

    Idempotent in the sense that decides whether a second call disturbs a live
    reservation: a reservation already held and still in the system is
    reported and nothing is submitted, so the command every session is told to
    run does not mint a second allocation. A record whose job has left the
    system is replaced, because reporting a finished allocation as held would
    send workers into nothing.

    Idempotence is per project, which is what lets two projects each hold one
    allocation rather than the first to ask holding the only one.
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
    pointers: Iterable[Mapping[str, Any]], project: str | None
) -> list[Mapping[str, Any]]:
    """The subset of live pointers a project's reservation roster counts.

    A reservation belongs to one project and admits that project's workers, so
    only those occupy its roster. Counting every project's workers against it
    is what turned one project's placement decision into a ceiling over the
    whole host: measured 2026-09-23, forty five runs across four repositories
    counted against a limit of twenty five while one step ran inside the
    allocation.

    This is deliberately NOT the same population as the lane's own concurrency
    bound. A served lane is shared, so its ceiling is rightly counted across
    every project; an allocation is not shared, so its roster is not. The two
    bounds read the same pointers and want different scopes, and conflating
    them is the defect rather than an optimisation.
    """
    if project is None:
        return list(pointers)
    return [p for p in pointers if str(p.get("project") or "") == project]


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
