import json
import os
import shutil
import signal
import subprocess
import sys

from reckon._store import _config_home
from reckon.crew.routing import (
    UTC,
    Any,
    CrewError,
    Mapping,
    Path,
    datetime,
)
from reckon.crew.runs import (
    _process_start_time,
    _process_stat_fields,
)

# The run directory's own record of who signalled it. One line per signal,
# appended rather than rewritten, because a run may be signalled more than once
# and the order is part of the fact. The name is fixed so a reader — and the
# attribution scan — looks in one place.
SENDER_RECORD_NAME = "senders.jsonl"


def _write_sender_record(
    run_dir: str | Path | None,
    *,
    target_pid: int,
    reason: str,
    sig: int | None = None,
    target_pgid: int | None = None,
    outcome: str | None = None,
    detail: str | None = None,
    project: str | None = None,
) -> Path | None:
    """Append one sender record into the target run's directory.

    This is the one writer every signalling path funnels through, so the next
    unattributed SIGTERM is read from the run's own directory rather than
    reconstructed from the survivors. A signal leaves exactly one attribution
    record and one outcome record: the attribution record is written BEFORE the
    signal is delivered, because a signal that ends the sender too must still
    leave the attribution behind, which a write ordered after the signal cannot
    promise. The outcome of the attempt is appended as a second record once the
    attempt returns, because before it runs the outcome is unknowable and a
    record that names a SIGTERM that was never sent is worse than none.

    ``outcome`` is ``"delivered"``, ``"refused"`` or ``"failed"``; ``detail``
    carries the guard's reason for a refusal or the operating system's message
    for a failure. A record written before the attempt leaves both unset.

    ``project`` is the watched project a sender record's target belongs to. It
    is written as its own field rather than folded into ``detail`` so a reader
    can act on the project without parsing a free-text message; a watcher's
    record, which lands in a shared watch directory rather than a run
    directory, is the case it exists for.

    The target's process group is recorded as well as its pid, because the two
    answer different questions — a group signal that reached unrelated work is
    only visible from the group id. A path that cannot be written (a run
    directory already removed by a rollback) is not raised: the signal is the
    safety mechanism and the record is the attribution, so a failed write must
    never withhold the signal. Returns the path written, or ``None``.
    """
    if run_dir is None:
        return None
    try:
        pid = int(target_pid)
    except (TypeError, ValueError):
        return None
    if target_pgid is None:
        try:
            target_pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            target_pgid = None
    if sig is None:
        sig = signal.SIGTERM
    try:
        signal_name = signal.Signals(sig).name
    except (ValueError, TypeError):
        signal_name = str(sig)
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    record = {
        "sender_pid": os.getpid(),
        "sender_argv0": sys.argv[0] if sys.argv else "",
        "target_pid": pid,
        "target_pgid": int(target_pgid) if target_pgid is not None else None,
        "reason": str(reason or ""),
        "signal": signal_name,
        "time": now,
    }
    if outcome is not None:
        record["outcome"] = str(outcome)
    if detail is not None:
        record["outcome_detail"] = str(detail)
    if project is not None:
        record["project"] = str(project)
    path = Path(run_dir) / SENDER_RECORD_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


# The environment mark a launcher writes into every process it starts for one
# run. It is the only handle on a worker the kernel reparented away from its
# supervisor, which the descendant walk cannot cross and the working-directory
# scan does not read.
RUN_ID_ENV = "RECKON_RUN_ID"


# The run directory's own record of the worker it started.
_RUN_WORKER_RECORD = "worker.json"


def _process_environment(pid: int) -> dict[str, str] | None:
    """One process's environment, or None when it cannot be read.

    Read-only and best-effort: a process may exit mid-scan, and one owned by
    another user is unreadable. None is "could not read", never "carries no
    mark", so a caller deciding what to signal treats it as unknown rather than
    as a process it may end.
    """
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    environment: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if b"=" not in entry:
            continue
        key, _, value = entry.partition(b"=")
        environment[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environment


def _start_tick(pid: Any) -> int | None:
    """The kernel start tick of a process, or None when it cannot be read."""
    try:
        return int(_process_start_time(pid))
    except (TypeError, ValueError):
        return None


# One reading of the process table, shared by every run answered in a beat.
#
# A process's environment changes only when it execs, and an exec keeps the pid
# and the kernel start tick while replacing the program. So a reading is keyed
# by the pid, its start tick and its executable identity, and a scan reuses
# every reading whose key still holds. That is what lets one beat answer every
# run from one walk of the table rather than one walk per run: the first run
# reads each process's environment, and every other run in the beat — and every
# later beat, while the process has neither execd nor been replaced — is
# answered from the index without opening an environment at all.
#
# The executable identity is the program's path AND the base of its stack. The
# path alone is unchanged when a process re-execs the same binary with a new
# environment, which the in-place re-execs in this repository do (a service
# re-execs its own interpreter to pick up the code on disk); the kernel
# establishes the stack base afresh on every exec, so such a re-exec is read
# afresh rather than answered from its pre-exec reading.
_ENVIRON_INDEX: dict[
    int, tuple[tuple[str | None, str | None, str | None], dict[str, str] | None]
] = {}


def _process_executable(pid: int) -> str | None:
    """The program a process is running, or None when it cannot be read."""
    try:
        return os.readlink(Path("/proc") / str(pid) / "exe")
    except OSError:
        return None


def _process_identity(pid: int) -> tuple[str | None, str | None, str | None]:
    """What a cached environment cannot survive: a reused pid or an exec.

    Three reads, all cheaper than the environment they guard, and none changes
    for any reason but recycling the pid or replacing the image. The start tick
    (stat field 22, index 19 after the parenthesised command) moves when the
    kernel recycles a pid. The executable path moves when the process execs a
    different program. The stack base (field 28, index 25) is established by the
    kernel at exec and left alone for the process's life, so it moves on every
    exec — including a re-exec of the same binary with the same arguments, which
    a path alone cannot see. A kernel that does not report it leaves it None,
    and the two fields before it still detect every other change.
    """
    fields = _process_stat_fields(pid)
    start = fields[19] if len(fields) > 19 else None
    stack = fields[25] if len(fields) > 25 else None
    return (start, _process_executable(pid), stack)


def _process_owner(pid: int) -> int | None:
    """The uid owning a process's ``/proc`` entry, or None when unreadable."""
    try:
        return os.stat(Path("/proc") / str(pid)).st_uid
    except OSError:
        return None


def _indexed_environment(pid: int) -> dict[str, str] | None:
    """``pid``'s environment from the index, read afresh only when it changed.

    The owner of the ``/proc`` entry is read first: another user's process — or
    a root process — cannot be read at all, so it is skipped without opening an
    environment, which is where most of a full scan's opens used to go. A
    reading already taken is returned while the pid still names a process with
    the same start tick and executable; a pid the kernel recycled, or a process
    that execd, is read afresh and re-indexed. None is "could not read", never
    "carries no mark".
    """
    if _process_owner(pid) != os.geteuid():
        _ENVIRON_INDEX.pop(pid, None)
        return None
    identity = _process_identity(pid)
    indexed = _ENVIRON_INDEX.get(pid)
    if indexed is not None and indexed[0] == identity:
        return indexed[1]
    environment = _process_environment(pid)
    _ENVIRON_INDEX[pid] = (identity, environment)
    return environment


def _forget_absent_pids(present: set[int]) -> None:
    """Drop readings for pids that have left the process table.

    A reading kept past its process's exit is a claim about a pid the kernel
    may hand to something else; the index is trimmed on every walk so it holds
    only pids the table still shows.
    """
    for pid in [pid for pid in _ENVIRON_INDEX if pid not in present]:
        del _ENVIRON_INDEX[pid]


def _crew_home_of(environment: Mapping[str, str]) -> Path:
    """The crew home a process's own environment places it in.

    The home a run holds is resolved by ``_config_home`` from the reader's
    environment; a scanned process's home is resolved by that same function
    from its own environment, so the two are compared as one resolver reads
    them. Comparing the raw ``RECKON_HOME`` strings instead would let a
    trailing slash or a symlinked spelling name one home for the run and a
    different one for the process carrying the same place on disk.
    """
    return _config_home(env=environment) / "crew"


def _processes_owning_run(
    run_id: str,
    run_home: str | Path,
    *,
    started_no_earlier_than: int | None = None,
) -> list[int]:
    """Pids of this user's processes that ``run_id`` names in ``run_home``.

    The only reach for a worker the kernel reparented away from its supervisor:
    the descendant walk cannot cross that boundary, and ``_processes_holding``
    reads working directories and open files rather than environments.

    ``run_home`` is the crew home the run belongs to, as its reader resolved
    it. A process qualifies on its environment naming the run id AND placing it
    in that same crew home — the process's home read from its own environment
    by the one config-home resolver, never against a raw ``RECKON_HOME`` string
    — and, when a floor is known, a start tick no earlier than it, so a
    recycled pid cannot be claimed. Without the home comparison a process
    carrying another crew home's run id reads as this run's worker, which is
    how a liveness read could count it and a stop could signal it. A process
    whose environment cannot be read is never named, because the absence of a
    mark must not read as ownership. Read-only; the caller decides what to
    signal.

    Each process's environment is taken from the shared index rather than read
    directly, so a run answered in the same beat as another — or one answered
    after a beat in which the process has not execd — costs no environment open.
    """
    if not run_id:
        return []
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    wanted_home = Path(run_home)
    own_pid = os.getpid()
    named: list[int] = []
    present: set[int] = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        present.add(pid)
        if pid == own_pid:
            continue
        environment = _indexed_environment(pid)
        if environment is None or environment.get(RUN_ID_ENV) != run_id:
            continue
        if _crew_home_of(environment) != wanted_home:
            continue
        if started_no_earlier_than is not None:
            start = _start_tick(pid)
            if start is None or start < started_no_earlier_than:
                continue
        named.append(pid)
    _forget_absent_pids(present)
    return named


def _running_descendants(pid: int) -> list[int]:
    """Every live process under ``pid``, including ``pid`` itself.

    A worker that runs a command leaves the command as its child, so reaching
    the worker alone leaves the command working. The walk reads the kernel's
    child list at each step; a process that exits mid-walk simply contributes
    no children.
    """
    from reckon.crew.recovery_liveness import _process_children

    seen: list[int] = []
    frontier = [pid]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.append(current)
        children = _process_children(current)
        if children:
            frontier.extend(children)
    return seen


def _worker_record(run_dir: str | Path | None) -> Mapping[str, Any] | None:
    """The run directory's own worker record, or None when it cannot be read."""
    if run_dir is None:
        return None
    try:
        data = json.loads((Path(run_dir) / _RUN_WORKER_RECORD).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _recorded_worker_pid(run_dir: str | Path | None) -> int | None:
    """The worker pid the run directory's own record names, or None."""
    data = _worker_record(run_dir)
    if data is None:
        return None
    try:
        return int(data.get("pid"))
    except (TypeError, ValueError):
        return None


def _run_launch_floor(run_dir: str | Path | None) -> int | None:
    """The start tick at or after which the run's own processes began.

    Read from the worker record's recorded start tick, not from whatever
    process holds that pid now: the number in the record is the tick the
    registered worker began at, so a pid the kernel has since recycled cannot
    raise the floor above the run's real processes and hide them from the scan.
    The worker is launched after the supervisor's snapshot, so a process naming
    the run that began no earlier than it is the run's. Absent a record there is
    no floor to apply, and the scan falls back to the run id alone.
    """
    data = _worker_record(run_dir)
    if data is None:
        return None
    try:
        return int(data.get("pid_start_time"))
    except (TypeError, ValueError):
        return None


def _root_names_the_recorded_process(pid: int, recorded_start: Any) -> bool:
    """Whether ``pid`` still names the process a record registered.

    A recorded start tick that differs from the running process's own rules the
    root out: the pid was recycled, and walking from it would descend into an
    unrelated process's children. An absent or unreadable recorded tick is not
    proof of reuse — the same stance the run record's own liveness read takes —
    so the root stands unless the two are both readable and differ.
    """
    if recorded_start is None:
        return True
    actual = _process_start_time(pid)
    return actual is None or actual == str(recorded_start)


def _run_process_pids(
    pid: int | None,
    *,
    run_id: str | None,
    run_dir: str | Path | None,
    expected_start_time: Any = None,
) -> list[int]:
    """Every process the run owns, by descent or by its environment mark.

    Two sources, unioned: the descent from the run's recorded supervisor and
    worker pids, which follows a live tree while the kernel still parenthesises
    it; and the environment scan for a worker the kernel has since reparented.
    Each descent root is checked against the start tick its own record carries
    before it is walked — the handed pid against the record the caller read it
    from, the worker-record pid against the worker record's own tick — so a pid
    the kernel has recycled contributes neither itself nor its children. The
    caller's own pid is dropped, because ending the process asking to end a run
    is never what the request means.
    """
    roots: list[tuple[int, Any]] = []
    if pid:
        roots.append((int(pid), expected_start_time))
    worker_pid = _recorded_worker_pid(run_dir)
    if worker_pid is not None:
        roots.append(
            (worker_pid, (_worker_record(run_dir) or {}).get("pid_start_time"))
        )
    collected: list[int] = []
    for root, recorded_start in roots:
        if not _root_names_the_recorded_process(root, recorded_start):
            continue
        collected.extend(_running_descendants(root))
    if run_id:
        from reckon.crew.runs import crew_home

        floor = _run_launch_floor(run_dir)
        for candidate in _processes_owning_run(
            run_id, crew_home(), started_no_earlier_than=floor
        ):
            collected.extend(_running_descendants(candidate))
    own_pid = os.getpid()
    ordered: list[int] = []
    for candidate in collected:
        if candidate != own_pid and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _signal_one(
    pid: int,
    sig: int,
    *,
    reason: str = "",
    run_dir: str | Path | None = None,
    project: str | None = None,
    allow_group: bool = True,
) -> bool:
    """Deliver one signal, using a group only when permitted and led by the target."""
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    target_is_own_group_leader = group == pid and group > 1
    signalled_as_group = (
        allow_group and target_is_own_group_leader and group != os.getpgid(0)
    )
    if run_dir is not None:
        _write_sender_record(
            run_dir,
            target_pid=pid,
            target_pgid=group,
            reason=reason,
            sig=sig,
            project=project,
        )
    try:
        if signalled_as_group:
            os.killpg(group, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError) as exc:
        if run_dir is not None:
            _write_sender_record(
                run_dir,
                target_pid=pid,
                target_pgid=group,
                reason=reason,
                sig=sig,
                outcome="failed",
                detail=str(exc) or type(exc).__name__,
                project=project,
            )
        return False
    if run_dir is not None:
        _write_sender_record(
            run_dir,
            target_pid=pid,
            target_pgid=group,
            reason=reason,
            sig=sig,
            outcome="delivered",
            project=project,
        )
    return True


def _process_comm(pid: int) -> str | None:
    try:
        return (Path("/proc") / str(pid) / "comm").read_text().strip()
    except OSError:
        return None


def _step_argv(run_dir: str | Path | None) -> list[str]:
    """The placed launch argv, including before its worker record is written."""
    worker = _worker_record(run_dir)
    argv = worker.get("argv") if worker else None
    if not isinstance(argv, list) and run_dir is not None:
        try:
            spec = json.loads((Path(run_dir) / "supervisor.json").read_text())
            argv = spec.get("plan", {}).get("argv")
        except (OSError, ValueError, AttributeError):
            pass
    return [str(word) for word in argv] if isinstance(argv, list) else []


def _run_uses_step(run_dir: str | Path | None) -> bool:
    argv = _step_argv(run_dir)
    return bool(
        argv
        and Path(argv[0]).name == "srun"
        and any(word.startswith("--jobid=") for word in argv)
    )


def _step_worker_identity(run_dir: str | Path | None) -> tuple[str, int, str] | None:
    """The live step task recorded by a placed worker, or None until it starts."""
    argv = _step_argv(run_dir)
    if not _run_uses_step(run_dir):
        return None
    job_ids = [
        str(arg).removeprefix("--jobid=")
        for arg in argv
        if str(arg).startswith("--jobid=")
    ]
    if len(job_ids) != 1 or not job_ids[0].isdigit():
        return None
    try:
        job, step, task_pid, start_tick = (
            (Path(run_dir) / "step-task.txt").read_text().split()
        )
        if (
            job == job_ids[0]
            and step.isdigit()
            and task_pid.isdigit()
            and start_tick.isdigit()
        ):
            return f"{job}.{step}", int(task_pid), start_tick
    except (OSError, ValueError, TypeError):
        pass
    return None


def _step_worker_client(run_dir: str | Path | None) -> int | None:
    worker = _worker_record(run_dir)
    if worker and _run_uses_step(run_dir):
        return _recorded_worker_pid(run_dir)
    return None


def signal_pending_step_client(pid: int, run_dir: str | Path) -> str | None:
    """End a pending srun only when Slurm confirms its named step is absent."""
    argv = _step_argv(run_dir)
    if not _run_uses_step(run_dir) or (Path(run_dir) / "step-task.txt").exists():
        return None
    jobs = [
        word.removeprefix("--jobid=") for word in argv if word.startswith("--jobid=")
    ]
    names = [
        word.removeprefix("--job-name=")
        for word in argv
        if word.startswith("--job-name=")
    ]
    if len(jobs) != 1 or not jobs[0].isdigit() or names != [Path(run_dir).name]:
        return None
    scheduler = shutil.which("squeue")
    if scheduler is None:
        return None
    try:
        answer = subprocess.run(
            [
                scheduler,
                "--steps",
                "--jobs",
                jobs[0],
                "--noheader",
                "--format=%i %j",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if answer.returncode != 0:
        return None
    steps = [line.split(maxsplit=1) for line in answer.stdout.splitlines()]
    if not steps or any(
        len(parts) != 2 or not parts[0].startswith(jobs[0] + ".") for parts in steps
    ):
        return None
    named = next(
        (step for step, name in steps if name == names[0] or names[0].startswith(name)),
        None,
    )
    if named:
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason="stop-before-step-unverified",
            sig=signal.SIGTERM,
            outcome="refused",
            detail=f"scheduler lists step {named} named {names[0]!r}",
        )
        return "refused"
    worker = _worker_record(run_dir)
    if (Path(run_dir) / "step-task.txt").exists():
        return None
    if (
        not worker
        or worker.get("pid") != pid
        or not worker.get("pid_start_time")
        or _process_comm(pid) != "srun"
        or _process_start_time(pid) != str(worker["pid_start_time"])
    ):
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason="stop-before-step-started",
            sig=signal.SIGTERM,
            outcome="refused",
            detail="client identity differs",
        )
        return "refused"
    return (
        "delivered"
        if _signal_one(
            pid,
            signal.SIGTERM,
            reason="stop-before-step-started",
            run_dir=run_dir,
            allow_group=False,
        )
        else None
    )


def _cgroup_names_step(cgroup: str, required: tuple[str, str]) -> bool:
    """Recognise the job and step components in v1 or v2 cgroup paths."""
    return any(
        all(part in path.split("/") for part in required)
        for line in cgroup.splitlines()
        if (path := line.partition(":")[2].partition(":")[2])
    )


def _signal_step_task(
    run_dir: str | Path,
    step: str,
    task_pid: int,
    start_tick: str,
    sig: int,
    reason: str,
    project: str | None,
) -> list[int]:
    """Signal only the sandboxed processes beneath a verified step task."""
    try:
        cgroup = (Path("/proc") / str(task_pid) / "cgroup").read_text()
        job, step_number = step.split(".", 1)
        required = (f"job_{job}", f"step_{step_number}")
        in_step = _cgroup_names_step(cgroup, required)
        actual_start = _process_start_time(task_pid)
        if not in_step or actual_start != start_tick:
            _write_sender_record(
                run_dir,
                target_pid=task_pid,
                reason=reason,
                sig=sig,
                outcome="refused",
                detail=(
                    f"task identity differs for step {step}: "
                    f"cgroup={cgroup.strip()!r}, "
                    f"start={actual_start!r}, expected_start={start_tick!r}"
                ),
                project=project,
            )
            return []
        delivered: list[int] = []
        for inner_pid in _running_descendants(task_pid):
            if inner_pid == task_pid:
                continue
            try:
                if _process_comm(inner_pid) == "bwrap":
                    continue
                inner_cgroup = (Path("/proc") / str(inner_pid) / "cgroup").read_text()
                inner_start = _process_start_time(inner_pid)
                inside = _cgroup_names_step(inner_cgroup, required)
                if (
                    not inside
                    or inner_start is None
                    or int(inner_start) < int(start_tick)
                ):
                    continue
                _write_sender_record(
                    run_dir,
                    target_pid=inner_pid,
                    reason=reason,
                    sig=sig,
                    project=project,
                )
                os.kill(inner_pid, sig)
                _write_sender_record(
                    run_dir,
                    target_pid=inner_pid,
                    reason=reason,
                    sig=sig,
                    outcome="delivered",
                    project=project,
                )
                delivered.append(inner_pid)
            except (OSError, ValueError) as exc:
                _write_sender_record(
                    run_dir,
                    target_pid=inner_pid,
                    reason=reason,
                    sig=sig,
                    outcome="failed",
                    detail=str(exc),
                    project=project,
                )
        return delivered
    except OSError as exc:
        _write_sender_record(
            run_dir,
            target_pid=task_pid,
            reason=reason,
            sig=sig,
            outcome="failed",
            detail=str(exc),
            project=project,
        )
        return []


def signal_worker(
    pid: int,
    sig: int = signal.SIGTERM,
    *,
    reason: str = "",
    run_dir: str | Path | None = None,
    project: str | None = None,
    run_id: str | None = None,
    reach_run: bool = False,
    expected_start_time: Any = None,
) -> bool | list[int]:
    """Signal a spawned process, and the rest of its run when asked.

    ``os.killpg`` takes a process GROUP id, and ``killpg(1, ...)`` is
    ``kill(-1, ...)`` — every process the caller is permitted to signal. A pid
    taken from a scan or a run record can report a group it does not lead: one
    reparented to init, or started without a session of its own, shares its
    group with unrelated work. Signalling that group reaches the whole account
    across every control group and session, which no caller here intends, and
    the reach is invisible at the call site because the argument looks like one
    process.

    The group is therefore signalled only when the process leads it, which is
    true of anything started detached on purpose and is the case the group
    signal exists for. Otherwise the process alone is signalled.

    With ``reach_run`` the signal is delivered to every process the run owns,
    not the one pid alone: a supervisor's worker and that worker's own children,
    found by descent from the run's recorded pids and by the run id in their
    environment — so a worker the kernel reparented to init is ended too, and a
    direct worker's children are not left working. A process whose environment
    cannot be read is not signalled, and a process that merely holds the run's
    worktree as its working directory carries no mark and is therefore never
    signalled.

    Without ``reach_run`` the single pid is signalled and the result is the
    old boolean, so a caller that is not ending a run keeps the contract it
    was written against. With ``reach_run`` the result is the list of pids the
    signal was delivered to, which is what a caller recording an ended run
    needs. ``expected_start_time`` is the start tick the caller read ``pid``
    from: the descent from that pid runs only while the pid still names that
    process, so a recycled pid contributes neither itself nor its children.

    This is the only function in the crew that calls ``os.kill`` or
    ``os.killpg``, so the attribution scan has one home to point at. A caller
    that names the target's run directory gets a sender record written before
    the signal goes out, and a matching outcome record after; a caller
    signalling something that is not a run passes no directory and writes
    nothing.
    """
    if not reach_run:
        return _signal_one(
            int(pid), sig, reason=reason, run_dir=run_dir, project=project
        )
    targets = _run_process_pids(
        pid,
        run_id=run_id,
        run_dir=run_dir,
        expected_start_time=expected_start_time,
    )
    own_pid = os.getpid()
    ended: list[int] = []
    step_placed = _run_uses_step(run_dir)
    client_pid = _step_worker_client(run_dir) if step_placed else None
    client_tree = set(_running_descendants(client_pid)) if client_pid else set()
    identity = _step_worker_identity(run_dir) if client_pid is not None else None
    task_tree = set(_running_descendants(identity[1])) if identity else set()
    for target in targets:
        if target == own_pid or target in client_tree or target in task_tree:
            continue
        # Before worker.json exists, the spec still identifies a placed run.
        # Its supervisor may share a group with srun, so only its PID is safe.
        if step_placed and target != pid:
            continue
        if step_placed and _process_comm(target) in (None, "srun", "bwrap"):
            continue
        if _signal_one(
            target,
            sig,
            reason=reason,
            run_dir=run_dir,
            project=project,
            allow_group=not step_placed,
        ):
            ended.append(target)
    if identity is not None and run_dir is not None:
        step, task_pid, start_tick = identity
        ended.extend(
            _signal_step_task(run_dir, step, task_pid, start_tick, sig, reason, project)
        )
    return ended


def _signal_process_group(
    pid: int,
    expected_start_time: str | None,
    *,
    reason: str = "",
    run_dir: str | Path | None = None,
    project: str | None = None,
) -> list[int]:
    """Signal a worker only while its pid still names the spawned process.

    A recorded pid is data from a run record, not a fact about who is calling
    this function — a test double, a stale carry-forward, or any record whose
    pid happens to equal the caller's own is otherwise indistinguishable from
    a genuine spawned worker. os.killpg signals the whole process group, so
    signalling one's own group takes the caller down with it. Refuse before
    that lookup rather than let the OS enforce it as a self-inflicted SIGTERM.

    A caller that names the target's run directory and reason gets its
    attribution and outcome records written by this one writer through
    :func:`signal_worker`, so a kill read from that run's own directory names
    the caller rather than only the victim. Callers do not write an attribution
    of their own: a pre-write beside this one would leave two attribution
    records for one signal.

    A guard that refuses the signal writes its own record carrying the
    ``refused`` outcome and the guard's reason, so a refusal is not read later
    as a SIGTERM that went out.

    Every caller that is ending a run hands over that run's directory, and the
    reach then covers the whole run rather than the handed pid alone: the run id
    is the directory's own name, so a worker reparented to init and outside the
    handed pid's group is still reached. The identity and self-group checks stay
    on the handed pid — they protect the caller, not the run. A caller that only
    holds a pid to stop, with no run of its own, passes no directory and keeps
    the single-pid behaviour.

    It returns the pids the signal was delivered to, which is what a caller
    recording an ended run needs; the empty list means nothing was signalled.
    """
    own_pid = os.getpid()
    if pid == own_pid or os.getpgid(pid) == os.getpgid(own_pid):
        detail = (
            f"refusing to signal pid {pid}: it is this process's own pid or "
            "shares this process's own process group, and killpg would "
            "terminate the caller doing the releasing"
        )
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason=reason,
            sig=signal.SIGTERM,
            outcome="refused",
            detail=detail,
            project=project,
        )
        raise CrewError(detail)
    actual_start_time = _process_start_time(pid)
    if not expected_start_time or actual_start_time != expected_start_time:
        detail = (
            f"refusing to signal pid {pid}: process identity changed "
            f"from {expected_start_time!r} to {actual_start_time!r}"
        )
        _write_sender_record(
            run_dir,
            target_pid=pid,
            reason=reason,
            sig=signal.SIGTERM,
            outcome="refused",
            detail=detail,
            project=project,
        )
        raise CrewError(detail)
    if run_dir is None:
        delivered = signal_worker(pid, signal.SIGTERM, reason=reason, project=project)
        return [int(pid)] if delivered else []
    run_id = Path(run_dir).name
    ended = signal_worker(
        pid,
        signal.SIGTERM,
        reason=reason,
        run_dir=run_dir,
        project=project,
        run_id=run_id,
        reach_run=True,
        expected_start_time=expected_start_time,
    )
    return list(ended) if isinstance(ended, list) else []
