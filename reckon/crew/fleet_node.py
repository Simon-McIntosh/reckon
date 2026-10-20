"""The fleet node: one whole-node allocation that hosts the interactive sessions.

The allocation holds one node of the site's CPU partition exclusively and with
no wall clock, so it stays up until it is cancelled or its node fails. Its batch
step runs ``fleet-supervisor`` (:mod:`reckon.crew.fleet_supervisor`), the one
process tree on the node that outlives every login node, and the zellij servers
holding the agent sessions are started from it.

The allocation is found from the queue by the comment token this module writes
into it, never by a remembered job id, so a resubmission, a requeue onto another
node or a cancel-and-hold is picked up by the next reader.

Nothing here runs on its own: ``cx`` holds an allocation through
``reckon fleet-node hold --submit`` when it finds none running, and the operator
reads it with ``reckon fleet-node status``.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any

from reckon.crew.fleet_supervisor import (
    CATCH_ALL_FLEET,
    FLEET_NAME_ENV,
    RECORD_NAME,
    STATE_DIR_ENV,
    fleet_state_directory,
    state_directory,
)

# The size of the allocation, overridable per environment variable. A rigel
# node has 28 cores and 128,259 MB, the site reserves nothing for the system,
# and the GPFS daemon pins about 17 GB, so an idle node offers a job about
# 107 GB. A cgroup limit above that can never be reached: a job that grows past
# what the node holds exhausts the node instead, swap fills, slurmd stops
# answering the controller within SlurmdTimeout (30 s), and the node is declared
# failed with every session on it. The limit therefore sits below what the node
# offers, so the job's own OOM killer acts first, and two cores stay with the
# system so slurmd keeps room to answer.
DEFAULT_PARTITION = "rigel"
DEFAULT_ACCOUNT = "iter"
DEFAULT_CPUS = 26
DEFAULT_MEMORY = "96G"
PARTITION_ENV = "FLEET_PARTITION"
ACCOUNT_ENV = "FLEET_ACCOUNT"
CPUS_ENV = "FLEET_CPUS"
MEMORY_ENV = "FLEET_MEMORY"

# Compute-library thread pools size themselves from the node's core count unless
# the environment caps them, so every heavy process the fleet starts opened a
# pool as wide as the node and the thread count multiplied. Each library
# variable is capped at one declared width unless the submitting environment
# already sets it; a process that needs more threads raises its own variable.
DEFAULT_THREAD_CAP = 8
THREAD_CAP_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)

# The scheduler spells an unbounded wall clock with this token, both in a
# request and in the remaining time it reports.
UNBOUNDED_TIME = "UNLIMITED"

# Scheduler identity of the allocation, carried as its job name and comment so
# a held allocation is found from the queue alone.
FLEET_COMMENT = "reckon-fleet"

# The comment an allocation held before reckon submitted the fleet carries. It
# is still found by that token until it is replaced by a new submission.
EARLIER_FLEET_COMMENTS = ("ambix-fleet",)

# A named fleet's allocation carries the fleet comment and its name, so each
# fleet's allocation is found from the queue apart from every other's.
FLEET_COMMENT_SEPARATOR = ":"


def fleet_comment(fleet: str | None = None) -> str:
    """The scheduler comment and job name one fleet's allocation carries."""
    if not fleet or fleet == CATCH_ALL_FLEET:
        return FLEET_COMMENT
    return f"{FLEET_COMMENT}{FLEET_COMMENT_SEPARATOR}{fleet}"


def fleet_environ(fleet: str | None = None) -> dict[str, str]:
    """An environment naming one fleet, for the readers that resolve its state."""
    if not fleet or fleet == CATCH_ALL_FLEET:
        return {}
    return {
        FLEET_NAME_ENV: fleet,
        STATE_DIR_ENV: str(fleet_state_directory(fleet)),
    }


# Remaining wall clock below which the status warns. Moving sessions off an
# allocation takes minutes of operator work, so the notice has to arrive while
# the allocation still has time left to act in.
REMAINING_WARNING_SECONDS = 30 * 60

# Scheduler node-state stem that means the node is being taken out of service.
# A draining node still runs its jobs and the allocation continues until it
# ends, but the scheduler places no new job there. The spelling varies (DRAIN,
# DRAINED, DRAINING) but always starts with this stem, so the match is a token
# prefix rather than an equality.
_DRAINING_STATE_PREFIX = "DRAIN"

# The queue fields read for each job: id, name, state, elapsed, node or pending
# reason, allocated generic resources, comment, remaining time.
_SQUEUE_FORMAT = "%i|%j|%T|%M|%R|%b|%k|%L"

# The queue fields read for an allocation's declared shape: partition, the core
# count the scheduler allocated, and the memory it reports for the job. Read
# from the scheduler rather than from this module's defaults, so an allocation
# held before reckon knew about it is described by the size it was submitted
# with.
_SHAPE_FORMAT = "%P|%C|%m"


class FleetNodeError(RuntimeError):
    """A scheduler call about the fleet node failed."""


@dataclass(frozen=True)
class FleetSize:
    """What the allocation asks the scheduler for."""

    partition: str
    account: str
    cpus: int
    memory: str


def fleet_size(environ: Mapping[str, str] | None = None) -> FleetSize:
    """The allocation's size: the defaults above, each overridable."""
    environ = os.environ if environ is None else environ
    return FleetSize(
        partition=environ.get(PARTITION_ENV) or DEFAULT_PARTITION,
        account=environ.get(ACCOUNT_ENV) or DEFAULT_ACCOUNT,
        cpus=int(environ.get(CPUS_ENV) or DEFAULT_CPUS),
        memory=environ.get(MEMORY_ENV) or DEFAULT_MEMORY,
    )


def batch_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the batch step's own output goes, beside the fleet's record.

    A requeue truncates it, so it holds the current node's run only; the node
    sampler's files beside it are what survive a lost node.
    """
    return state_directory(environ) / "fleet-%j.log"


def generate_hold_script(
    size: FleetSize,
    *,
    log_path: str | os.PathLike[str],
    standby: bool = False,
    fleet: str | None = None,
) -> str:
    """The whole-node allocation that hosts the interactive sessions.

    ``TMPDIR`` is pointed at ``/tmp`` because a compute node cannot write the
    per-user runtime directory, and the batch step runs the installed
    supervisor when there is one and otherwise only holds the node. A named
    fleet's allocation carries its name in its comment and exports it, so its
    supervisor publishes into that fleet's own state.
    """
    comment = fleet_comment(fleet)
    headers = [
        "#!/bin/bash",
        f"#SBATCH --job-name={comment}",
        f"#SBATCH --partition={size.partition}",
        f"#SBATCH --account={size.account}",
        f"#SBATCH --cpus-per-task={size.cpus}",
        f"#SBATCH --mem={size.memory}",
        f"#SBATCH --time={UNBOUNDED_TIME}",
        f"#SBATCH --output={log_path}",
        "#SBATCH --nodes=1",
        "#SBATCH --exclusive",
        f"#SBATCH --comment={comment}",
    ]
    thread_caps = "\n".join(
        f'export {name}="${{{name}:-{DEFAULT_THREAD_CAP}}}"'
        for name in THREAD_CAP_VARIABLES
    )
    supervisor_args = " --standby" if standby else ""
    body = (
        dedent(
            """
        set -euo pipefail

        export TMPDIR=/tmp
        {fleet_exports}
        # Cap each compute-library thread pool at the declared width, keeping a
        # value the submitting environment already set. A heavy process
        # otherwise opens a pool as wide as the node's cores, and the thread
        # count multiplies across every process the allocation starts. A
        # process that needs more threads raises its own variable.
        {thread_caps}

        # The batch step is the one process tree on the node that outlives every
        # login node: a step ends when its login-side client does. The fleet
        # supervisor runs here when installed, so the zellij servers holding the
        # agent sessions are started from it and survive a lost login node.
        if [ -x "$HOME/.local/bin/fleet-supervisor" ]; then
            exec "$HOME/.local/bin/fleet-supervisor"{supervisor_args}
        fi

        echo "[$(date)] Holding $(hostname) for the interactive agent fleet"
        exec sleep infinity
        """
        )
        .strip()
        .format(
            thread_caps=thread_caps,
            supervisor_args=supervisor_args,
            fleet_exports="".join(
                f"export {name}={value}\n"
                for name, value in fleet_environ(fleet).items()
            ),
        )
    )
    return "\n".join([*headers, "", body, ""])


def submit(script: str) -> str:
    """Submit a generated allocation and return its job id."""
    result = subprocess.run(
        ["sbatch", "--parsable"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "sbatch failed"
        raise FleetNodeError(message)
    return result.stdout.strip().split(";", maxsplit=1)[0]


def query_jobs(
    account: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[dict[str, str]]:
    """This user's jobs charged to ``account``, one field mapping per row.

    A job charged to another account is invisible to a query filtered on this
    one, so the caller passes the account the allocation is billed to. A failed
    query raises rather than reading as an empty queue. ``runner`` overrides the
    process call, which is how a caller that already holds a scheduler seam
    asks this question through it rather than reaching a real client.
    """
    user = os.environ.get("USER") or getpass.getuser()
    run = subprocess.run if runner is None else runner
    result = run(
        ["squeue", "-h", "-u", user, "-A", account, "-o", _SQUEUE_FORMAT],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise FleetNodeError(result.stderr.strip() or "squeue failed")
    jobs: list[dict[str, str]] = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) not in (7, 8):
            continue
        job = {
            "jobid": parts[0].strip(),
            "name": parts[1].strip(),
            "state": parts[2].strip(),
            "time": parts[3].strip(),
            "node": parts[4].strip(),
            "gres": parts[5].strip(),
            "comment": parts[6].strip(),
        }
        if len(parts) == 8:
            job["timeleft"] = parts[7].strip()
        jobs.append(job)
    return jobs


def find_allocations(
    jobs: Iterable[Mapping[str, str]], *, fleet: str | None = None
) -> list[Mapping[str, str]]:
    """One fleet's held allocations among scheduler rows, found by its comment."""
    if not fleet or fleet == CATCH_ALL_FLEET:
        tokens = {FLEET_COMMENT, *EARLIER_FLEET_COMMENTS}
    else:
        tokens = {fleet_comment(fleet)}
    return [job for job in jobs if job.get("comment", "").strip() in tokens]


def fleet_of_allocation(job: Mapping[str, str]) -> str | None:
    """The fleet an allocation row belongs to, or None when it is no fleet's."""
    comment = job.get("comment", "").strip()
    if comment in {FLEET_COMMENT, *EARLIER_FLEET_COMMENTS}:
        return CATCH_ALL_FLEET
    prefix = FLEET_COMMENT + FLEET_COMMENT_SEPARATOR
    if comment.startswith(prefix) and comment[len(prefix) :]:
        return comment[len(prefix) :]
    return None


def parse_memory_gb(memory: str) -> int | None:
    """Whole gigabytes a scheduler memory string names, or None.

    The scheduler reports memory in the unit the job was requested with —
    ``96G``, ``98304M`` — so the string is parsed rather than assumed, and a
    value carrying no unit is read as the megabytes the scheduler defaults to.
    A per-node or per-cpu suffix (``96Gn``) is dropped before the unit is read.
    None means the value could not be read, which the caller treats as no shape
    rather than as a zero-memory allocation.

    The figure itself is read by the one parser of a scheduler memory string,
    :func:`reckon.crew.placement.memory_figure_bytes`; this function only states
    the answer in the unit its caller publishes, whole gigabytes, so a figure
    below one floors to zero.
    """
    from reckon.crew import placement

    raw = memory.strip().upper()
    while raw and raw[-1] in "NC":
        raw = raw[:-1]
    total = placement.memory_figure_bytes(raw)
    return None if total is None else total // (1024**3)


def allocation_shape(
    job_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any] | None:
    """The partition, cores and memory the scheduler reports for one job.

    The shape is read from the scheduler rather than taken from this module's
    declared defaults: an allocation held before reckon knew about it — the
    supervisor-running kind :func:`find_allocation` returns — was submitted with
    its own size, and publishing the defaults in its place would describe a
    reservation sized differently from the one running. None means the shape
    could not be read, so the caller records no size rather than a guess.
    """
    run = subprocess.run if runner is None else runner
    try:
        result = run(
            ["squeue", "-h", "-j", job_id, "-o", _SHAPE_FORMAT],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    if not lines:
        return None
    parts = lines[0].split("|")
    if len(parts) != 3:
        return None
    partition = parts[0].strip()
    cores_text = parts[1].strip()
    if not partition or not cores_text.isdigit():
        return None
    memory = parts[2].strip()
    return {
        "partition": partition,
        "cores": int(cores_text),
        "memory": memory,
        "memory_gb": parse_memory_gb(memory),
    }


def recorded_job_id(environ: Mapping[str, str] | None = None) -> str | None:
    """The job the fleet's record says hosts the sessions, or None.

    More than one allocation can carry the fleet's comment, and only the one
    whose batch step published the record runs the supervisor the sessions live
    under, so this is what tells them apart.
    """
    path = state_directory(environ) / RECORD_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job = str(record.get("job_id") or "").strip() if isinstance(record, dict) else ""
    return job or None


def find_allocation(
    jobs: Iterable[Mapping[str, str]],
    *,
    preferred: str | None = None,
    fleet: str | None = None,
) -> Mapping[str, str] | None:
    """The allocation to act on: the recorded one when held, else the first."""
    allocations = find_allocations(jobs, fleet=fleet)
    for job in allocations:
        if preferred and job.get("jobid", "").strip() == preferred:
            return job
    return allocations[0] if allocations else None


def placement_argv(job: Mapping[str, str], command: Sequence[str]) -> list[str]:
    """The scheduler invocation that runs ``command`` inside a held allocation.

    The command becomes an overlapping step of the allocation rather than an
    allocation of its own, so it lands on the allocation's node and shares its
    control group. The job id comes from the row the allocation was found by.

    The step's options are composed by :func:`reckon.crew.placement.step_prefix`,
    the one owner of a step's options, so the memory bound a step carries is
    stated once and this prefix is not given one of its own.
    """
    from reckon.crew import placement

    return [
        placement.RESERVATION_STEP_SCHEDULER,
        *placement.step_prefix(job.get("jobid", "").strip(), list(command)),
    ]


def node_state(node: str) -> str | None:
    """Scheduler state of the node an allocation runs on, or None.

    A value that is not a node name (a pending job reports its reason in that
    field) or a failed query yields None, which reads as unknown rather than
    healthy.
    """
    name = node.strip()
    if not name or any(char in name for char in " ()"):
        return None
    result = subprocess.run(
        ["scontrol", "show", "node", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return parse_node_state(result.stdout)


def remaining_seconds(time_left: str) -> int | None:
    """Remaining wall clock in seconds, or None when there is no duration.

    The unbounded token is not a duration, so it maps to None and can never be
    compared against a threshold or read as an allocation that is ending. A
    value not reported as ``[DD-]HH:MM:SS`` is None too, because an unknown
    lifetime must not present as an expiring one.
    """
    raw = time_left.strip()
    if raw == UNBOUNDED_TIME:
        return None
    days, _, hms = raw.partition("-")
    if not hms:
        hms, days = days, ""
    try:
        day_count = int(days) if days else 0
        parts = [int(part) for part in hms.split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        hours, minutes, secs = parts
    elif len(parts) == 2:
        hours, minutes, secs = 0, parts[0], parts[1]
    else:
        return None
    return day_count * 86400 + hours * 3600 + minutes * 60 + secs


def parse_node_state(node_info: str) -> str | None:
    """Upper-cased ``State=`` value from ``scontrol show node`` output, or None.

    The value is a ``+``-separated set of tokens (ALLOCATED, MIXED, DRAIN,
    REBOOT_REQUESTED and so on), returned whole, because every token is a fact
    about the node and the first one is not privileged.
    """
    for token in node_info.split():
        if not token.startswith("State="):
            continue
        value = token.split("=", 1)[1].strip().upper()
        return value or None
    return None


def node_is_draining(state: str | None) -> bool:
    """Whether a scheduler node state means the node is going out of service.

    Every token of the set is inspected: this cluster reports a draining node
    as MIXED+DRAIN+REBOOT_REQUESTED, so a leading-token match misses it.
    """
    if not state:
        return False
    return any(
        token.startswith(_DRAINING_STATE_PREFIX) for token in state.upper().split("+")
    )


def _format_duration(seconds: int) -> str:
    """Render a second count compactly: ``2d03h``, ``1h05m``, ``25m``, ``40s``."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def describe_allocation(
    job: Mapping[str, str], *, state_of_node: str | None = None
) -> list[str]:
    """Operator-readable lifetime for one scheduler row, as plain-text lines.

    An allocation with no wall clock reads as unbounded and never warns on
    time; a finite one below :data:`REMAINING_WARNING_SECONDS` warns. A node
    that is draining warns on its own regardless of any wall clock, which is
    the only way an unbounded allocation is warned about at all.
    """
    job_id = job.get("jobid", "") or "unknown"
    state = job.get("state", "") or "unknown"
    node = job.get("node", "") or "unallocated"
    elapsed = job.get("time", "").strip() or "unknown"
    raw_left = job.get("timeleft", "").strip()
    seconds = remaining_seconds(raw_left)
    if raw_left == UNBOUNDED_TIME:
        remaining = "unbounded (no wall clock)"
    elif seconds is None:
        remaining = "unknown"
    else:
        remaining = _format_duration(seconds)

    lines = [
        f"Fleet allocation {job_id} · {state} · node {node}",
        f"  elapsed    {elapsed}",
        f"  remaining  {remaining}",
    ]
    if node_is_draining(state_of_node):
        lines.append(
            f"WARNING: fleet allocation {job_id} runs on {node}, which the "
            f"scheduler reports {state_of_node}; the scheduler places no new "
            "job on the node, the allocation continues until it ends, and the "
            "node returns only when an administrator resumes it"
        )
    if seconds is not None and seconds < REMAINING_WARNING_SECONDS:
        lines.append(
            f"WARNING: fleet allocation {job_id} has {_format_duration(seconds)} "
            f"remaining on {node}"
        )
    return lines
