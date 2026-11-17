"""The fleet node: one whole-node allocation that hosts the interactive sessions.

The allocation holds one node of the site's CPU partition exclusively and with
no wall clock, so it stays up until it is cancelled or its node fails. Its batch
step runs ``fleet-supervisor`` (:mod:`reckon.crew.fleet_supervisor`), the one
process tree on the node that outlives every login node, and the zellij servers
holding the agent sessions are started from it.

The allocation is found from the queue by the comment token this module writes
into it, never by a remembered job id, so a resubmission, a requeue onto another
node or a cancel-and-hold is picked up by the next reader.

``cx`` holds an allocation through
``reckon fleet-node hold --submit`` when it finds none running, and the operator
reads it with ``reckon fleet-node status``. The hold starts an independent
scheduler reader through ``fleet-health --scheduler``.
"""

from __future__ import annotations

import csv
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
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
from reckon.node_exclusive_lock import LockHeld, exclusive_lock

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

# Terminal allocation states, rather than absence from the live queue, close a
# scheduler series. Accounting may lag the queue and either query may fail.
_TERMINAL_JOB_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "REVOKED",
        "TIMEOUT",
    }
)
_ACTIVE_JOB_STATES = frozenset(
    {
        "CONFIGURING",
        "COMPLETING",
        "PENDING",
        "RUNNING",
        "RESIZING",
        "REQUEUED",
        "REQUEUE_FED",
        "REQUEUE_HOLD",
        "RESV_DEL_HOLD",
        "SIGNALING",
        "SPECIAL_EXIT",
        "STAGE_OUT",
        "STOPPED",
        "SUSPENDED",
    }
)
_SCHEDULER_COLUMNS = [
    "job_id",
    "node",
    "scheduler_state",
    "scheduler_reason",
    "scheduler_running",
    "scheduler_event",
    "source",
]
_HEALTH_COLUMNS = [
    "utc",
    "load1",
    "load5",
    "procs_running",
    "procs_blocked",
    "dstate",
    "mem_avail_mb",
    "swap_free_mb",
    "job_mem_mb",
    "job_mem_max_mb",
    "home_stat_ms",
    "slurmd_ms",
    "ctld_ping",
    "tasks",
    "python_procs",
    "top_parent",
    "writable_interpreters",
    "job_oom_kill",
    *_SCHEDULER_COLUMNS,
    "session_scratch_bytes",
    "test_trees",
    "test_rss_bytes",
    "test_runs",
]


def scheduler_reading(
    job_id: str, node: str, *, runner: Callable[..., Any] = subprocess.run
) -> dict[str, str]:
    """Read one allocation, falling back to accounting only after queue absence."""
    row = {
        "job_id": job_id,
        "node": node,
        "scheduler_state": "UNKNOWN",
        "scheduler_reason": "scheduler unreadable",
        "scheduler_running": "unknown",
        "scheduler_event": "unknown",
        "source": "scheduler",
    }
    queries = (
        ["squeue", "-h", "-j", job_id, "-o", "%i|%N|%T|%r"],
        [
            "sacct",
            "-X",
            "-n",
            "-P",
            "-j",
            job_id,
            "--format=JobIDRaw,NodeList,State%80,Reason%200",
        ],
    )
    for argv in queries:
        try:
            result = runner(
                argv, capture_output=True, text=True, check=False, timeout=10
            )
        except (OSError, subprocess.SubprocessError) as exc:
            row["scheduler_reason"] = str(exc)
            return row
        if result.returncode:
            row["scheduler_reason"] = result.stderr.strip() or f"{argv[0]} failed"
            if argv[0] == "squeue":
                continue
            return row
        if not result.stdout.strip():
            continue
        for line in result.stdout.splitlines():
            fields = line.strip().split("|")
            if len(fields) < 4 or fields[0].strip() != job_id:
                continue
            _, reported_node, state, reason = (field.strip() for field in fields[:4])
            state_word = state.split()[0].rstrip("+") if state else ""
            if state_word not in _ACTIVE_JOB_STATES | _TERMINAL_JOB_STATES:
                return row
            # Accounting retains the cancelling uid in the state string.
            terminal = state_word in _TERMINAL_JOB_STATES
            row.update(
                node=reported_node
                if reported_node not in ("", "None", "(null)")
                else node,
                scheduler_state=state,
                scheduler_reason=reason or "None",
                scheduler_running="true" if state == "RUNNING" else "false",
                scheduler_event="end" if terminal else "sample",
            )
            return row
        return row
    row["scheduler_reason"] = "job absent from queue and accounting"
    return row


def append_scheduler_reading(directory: Path, row: Mapping[str, str]) -> bool:
    """Append to the node's TSV, serialising with its sampler and deduplicating end."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{row['job_id']}-{row['node']}.tsv"
    with (
        exclusive_lock(Path(f"{path}.lock")),
        path.open("a+", encoding="utf-8", newline="") as stream,
    ):
        stream.seek(0)
        header = next(csv.reader(stream, delimiter="\t"), [])
        if not header:
            header = _HEALTH_COLUMNS
            csv.writer(stream, delimiter="\t", lineterminator="\n").writerow(header)
        if not set(_SCHEDULER_COLUMNS).issubset(header):
            raise FleetNodeError(f"health series lacks scheduler columns: {path}")
        # Only the tail is read, keeping each tick independent of series age.
        stream.flush()
        with path.open("rb") as reader:
            reader.seek(0, os.SEEK_END)
            reader.seek(max(0, reader.tell() - 65536))
            tail = reader.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            values = line.split("\t")
            if len(values) == len(header):
                prior = dict(zip(header, values, strict=True))
                if prior.get("source") == "scheduler":
                    if prior.get("scheduler_event") == "end":
                        return False
                    break
        stream.seek(0, os.SEEK_END)
        csv.writer(stream, delimiter="\t", lineterminator="\n").writerow(
            row.get(column, "-")
            .replace("\t", " ")
            .replace("\r", " ")
            .replace("\n", " ")
            for column in header
        )
        stream.flush()
        os.fsync(stream.fileno())
    return True


def watch_scheduler(
    job_id: str,
    node: str,
    *,
    directory: Path,
    interval: float = 60,
    passes: int = 0,
    runner: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Sample outside the watched allocation until its confirmed terminal row."""
    if job_id in (os.environ.get("SLURM_JOB_ID"), os.environ.get("SLURM_JOBID")):
        raise FleetNodeError("scheduler reader must run outside the watched allocation")
    if (
        not job_id.isdigit()
        or not node
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for c in node
        )
    ):
        raise FleetNodeError("scheduler reader needs a numeric job id and a node name")
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_lock(directory / f"{job_id}-scheduler.lock", blocking=False):
            count = 0
            while True:
                started = time.monotonic()
                row = scheduler_reading(job_id, node, runner=runner)
                row["utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                if not append_scheduler_reading(directory, row):
                    return
                count += 1
                if row["scheduler_event"] == "end" or (passes and count >= passes):
                    return
                sleep(max(0, interval - (time.monotonic() - started)))
    except LockHeld:
        return


def _scheduler_reader_launch(account: str) -> str:
    """A separate, small scheduler job on another node survives the fleet's loss."""
    return dedent(f"""
        if [ -x "$HOME/.local/bin/fleet-health" ]; then
            health_state="${{FLEET_STATE_DIR:-$HOME/.local/state/fleet}}"
            mkdir -p "$health_state/health"
            reader_command=$(printf '%q ' "$HOME/.local/bin/fleet-health" --scheduler "$SLURM_JOB_ID" "$(hostname -s)")
            env -u SLURM_JOB_ID -u SLURM_JOBID sbatch --parsable \\
                --partition=rigel --account={shlex.quote(account)} --nodes=1 \\
                --cpus-per-task=1 --mem=512M --time=UNLIMITED \\
                --exclude="$(hostname -s)" --job-name=fleet-scheduler \\
                --output="$health_state/health/scheduler-%j.log" \\
                --wrap="exec $reader_command" || echo "fleet scheduler reader submission failed" >&2
        fi
    """).strip()


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


def declared_backend_directories(
    backends: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Every directory the flight's backends declare for finding their commands.

    The hold's batch step inherits the environment of whatever shell submitted
    it, and a non-interactive shell never runs the startup files that add a
    backend's installation directory, so a directory the flight declares is the
    only reliable statement that it exists. Entries are home-expanded and
    de-duplicated in declaration order.
    """
    if backends is None:
        from reckon import flight

        backends = flight.resolve().config.get("backends") or {}
    from reckon._backends import backend_search_directories

    directories: list[str] = []
    for entry in backends.values():
        for directory in backend_search_directories(entry):
            if directory not in directories:
                directories.append(directory)
    return directories


def hold_path(environ: Mapping[str, str] | None = None) -> str:
    """The PATH the hold's sessions and services start with.

    The batch step's environment is the submitting shell's, so a hold submitted
    from a non-interactive shell starts every agent session without the
    directories its startup files would have added. Every directory the flight
    declares is appended, after the inherited entries so an explicit entry the
    submitter already holds still wins.
    """
    environ = os.environ if environ is None else environ
    entries = [
        entry
        for entry in str(environ.get("PATH") or os.defpath).split(os.pathsep)
        if entry
    ]
    for directory in declared_backend_directories():
        if directory not in entries:
            entries.append(directory)
    return os.pathsep.join(entries)


def batch_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Where the batch step's own output goes, beside the fleet's record.

    A requeue truncates it, so it holds the current node's run only; the node
    sampler's files beside it are what survive a lost node.
    """
    return state_directory(environ) / "fleet-%j.log"


def _supervisor_lifetime_script(*, standby: bool) -> str:
    """Keep the batch shell alive while its replaceable interpreter is down.

    The service policy supplies the delays at submission time, so recovery needs
    no working Python. Only the reader's explicit retirement marker ends the
    holder; a clean unrequested exit follows the same backoff as a crash.
    """
    from reckon.crew import fleet_supervisor

    backoff_setup, backoff_transition = fleet_supervisor.shell_backoff_transition()
    arguments = " --standby" if standby else ""
    return dedent(
        f'''
        FLEET_LIFETIME_DIRECTORY=$(mktemp -d "$TMPDIR/fleet-lifetime.XXXXXXXX")
        export FLEET_LIFETIME_DIRECTORY
        trap 'rm -fv -- "$FLEET_LIFETIME_DIRECTORY/stop" "$FLEET_LIFETIME_DIRECTORY/active"; rmdir -v -- "$FLEET_LIFETIME_DIRECTORY"' EXIT
        {backoff_setup}
        while [ ! -f "$FLEET_LIFETIME_DIRECTORY/stop" ]; do
            started=$SECONDS
            echo "[$(date -u +%FT%TZ)] fleet supervisor starting"
            if (
                if [ -f "$FLEET_LIFETIME_DIRECTORY/active" ]; then
                    exec "$HOME/.local/bin/fleet-supervisor"
                else
                    exec "$HOME/.local/bin/fleet-supervisor"{arguments}
                fi
            ); then
                result=0
            else
                result=$?
            fi
            [ ! -f "$FLEET_LIFETIME_DIRECTORY/stop" ] || break
            ran=$((SECONDS - started))
            {backoff_transition}
            echo "[$(date -u +%FT%TZ)] fleet supervisor exited ($result); restart in ${{delay}}s"
            sleep "$delay"
        done
        exit 0
        '''
    ).strip()


def generate_hold_script(
    size: FleetSize,
    *,
    log_path: str | os.PathLike[str],
    standby: bool = False,
    fleet: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The whole-node allocation that hosts the interactive sessions.

    ``TMPDIR`` is pointed at ``/tmp`` because a compute node cannot write the
    per-user runtime directory, and the batch step runs the installed
    supervisor when there is one and otherwise only holds the node. A named
    fleet's allocation carries its name in its comment and exports it, so its
    supervisor publishes into that fleet's own state. ``PATH`` is composed from
    the submitting environment plus every directory the flight declares for a
    backend, so the sessions and services the hold starts resolve the commands
    the flight names whatever shell submitted the hold.
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
    body = (
        dedent(
            """
        set -euo pipefail

        export TMPDIR=/tmp
        export PATH={hold_path}
        {fleet_exports}
        {scheduler_reader}
        # Cap each compute-library thread pool at the declared width, keeping a
        # value the submitting environment already set. A heavy process
        # otherwise opens a pool as wide as the node's cores, and the thread
        # count multiplies across every process the allocation starts. A
        # process that needs more threads raises its own variable.
        {thread_caps}

        # The batch step is the one process tree on the node that outlives every
        # login node: a step ends when its login-side client does. The fleet
        # shell holds the allocation independently of the supervisor's Python.
        # Sessions retain their process identities while the reader restarts.
        if [ -x "$HOME/.local/bin/fleet-supervisor" ]; then
            {supervisor_lifetime}
        fi

        echo "[$(date)] Holding $(hostname) for the interactive agent fleet"
        exec sleep infinity
        """
        )
        .strip()
        .format(
            hold_path=shlex.quote(hold_path(environ)),
            thread_caps=thread_caps,
            supervisor_lifetime=_supervisor_lifetime_script(standby=standby),
            scheduler_reader=_scheduler_reader_launch(size.account),
            fleet_exports="".join(
                f"export {name}={value}\n"
                for name, value in fleet_environ(fleet).items()
            ),
        )
    )
    return "\n".join([*headers, "", body, ""])


def submit(
    script: str,
    *,
    partition: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Submit a generated allocation and return its job id.

    ``partition`` names the partition on the argv as well as in the script.
    A submission made from inside an existing allocation is refused as an
    implicit nested job unless its argv names a partition of its own, so a
    caller that submits from one states it here rather than relying on the
    script's own directive. ``runner`` overrides the process call, which is
    how a caller that already holds a scheduler seam asks this question
    through it rather than reaching a real client.
    """
    argv = ["sbatch", "--parsable"]
    if partition is not None:
        argv.append(f"--partition={partition}")
    run = subprocess.run if runner is None else runner
    result = run(
        argv,
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


def append_node_reading(path: Path, values: str = "") -> None:
    """Share the file-server lock and schema with the outside scheduler reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(Path(f"{path}.lock")):
        if path.exists():
            with path.open() as stream:
                header = stream.readline().rstrip("\r\n").split("\t")
            if header != _HEALTH_COLUMNS:
                archived = path.with_suffix(
                    "." + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".tsv"
                )
                path.rename(archived)
        with path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
            if not stream.tell():
                writer.writerow(_HEALTH_COLUMNS)
            if values:
                row = values.rstrip("\n").split("\t")
                if len(row) != len(_HEALTH_COLUMNS):
                    raise FleetNodeError("node health row does not match its schema")
                writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Record fleet job health")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--scheduler", metavar="JOB")
    modes.add_argument("--append-node-row", type=Path, metavar="TSV")
    modes.add_argument("--initialise-series", type=Path, metavar="TSV")
    parser.add_argument("node", nargs="?")
    parser.add_argument("--passes", type=int, default=0)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    try:
        if args.append_node_row:
            append_node_reading(args.append_node_row, sys.stdin.read())
        elif args.initialise_series:
            append_node_reading(args.initialise_series)
        elif args.node:
            watch_scheduler(
                args.scheduler,
                args.node,
                directory=state_directory() / "health",
                interval=args.interval,
                passes=args.passes,
            )
        else:
            parser.error("--scheduler requires a node name")
    except FleetNodeError as exc:
        parser.exit(1, f"{exc}\n")
