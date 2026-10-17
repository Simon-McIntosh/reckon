import json
import os
import signal
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
    """
    if not run_id:
        return []
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    wanted_home = Path(run_home)
    own_pid = os.getpid()
    named: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        environment = _process_environment(pid)
        if environment is None or environment.get(RUN_ID_ENV) != run_id:
            continue
        if _crew_home_of(environment) != wanted_home:
            continue
        if started_no_earlier_than is not None:
            start = _start_tick(pid)
            if start is None or start < started_no_earlier_than:
                continue
        named.append(pid)
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
) -> bool:
    """Deliver one signal, never to a group the target does not lead."""
    try:
        group = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    target_is_own_group_leader = group == pid and group > 1
    signalled_as_group = target_is_own_group_leader and group != os.getpgid(0)
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
    for target in targets:
        if target == own_pid:
            continue
        if _signal_one(target, sig, reason=reason, run_dir=run_dir, project=project):
            ended.append(target)
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
