"""Resume runs after a provider hold or declared external wait ends.

A per-request refusal stops a worker without breaking anything: the account is
spent until a moment the refusal names, the session still holds every turn of
the worker's orientation, and the worktree it was oriented against is still on
disk. The only thing missing minutes later is the word *continue* — and until
now a person had to notice the outage and type it, which is the wrong shape for
a failure whose defining property is happening while nobody is watching.

Eligibility needs no provider call. A refusal naming a reset time is dated by
it; one naming none ages out of the shelf life declared in flight
configuration. Both readings come from records already on disk, so the sweep
costs nothing to run and can therefore run often.

An external wait is explicit worker delivery rather than an inferred stop. Its
manifest names the condition, a shell-free probe argument vector, the returned
states that mean terminal, and the brief for the resumed turn. The same sweep
checks that trigger on its cadence and leaves the run alone until it terminates.

Four bounds keep the sweep from becoming its own hazard, and each is checked
before anything is launched:

* **At most one resume per trigger.** A lift is recorded in the append-only
  recovery log with the hold or condition it was resumed for, and the pointer's
  ``auto_resume`` slot is a cache of that record rather than its authority — a
  launch that rewrites the pointer from a snapshot taken before the stamp
  cannot drop a line from the log. A fresh refusal or rewritten waiting manifest
  carries a new identity, so it can be judged independently.
* **Never onto a lane still held.** The resume plan is built through the same
  budget verdict a hand-typed resume passes, so a lane whose hold is in force
  refuses here too and the sweep can never spend the last of a recovering quota.
* **Never into a scope another run owns.** A write path claimed by a live run
  is a collision this would be creating rather than recovering from.
* **Never without a worktree.** A resume has no working directory to start in,
  and papering over that hides the very state a retained tree exists to prevent.

Every decision is reported with its reason, and every sweep that touched
anything is appended to a per-project record, so an operator reading afterwards
sees the recovery happen rather than inferring it from a run that is suddenly
alive again.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _backends, ledger
from reckon import budget as budget_module
from reckon._store import write_atomically, write_json_atomically
from reckon._timestamps import parse_utc
from reckon.crew.dispatch import (
    _WORKER_DONE_MANIFEST_STATUSES,
    BudgetHold,
    LanePaused,
    _actionable_budget_hold,
    _backend_settings,
    _require_fleet_gate_open,
    project_mount_repository,
    record_resumption,
    resolve_project_repository,
    resume_plan,
)
from reckon.crew.node import CrewError
from reckon.crew.process_liveness import _process_start_time as process_start_time
from reckon.crew.recovery import (
    _process_reading,
    _run_exit_record,
    _stream_refusal_block,
    classify_pointer,
    dispatch_awaiting_reviews,
    external_wait,
    live_worker_pid,
    local_liveness,
    resume_window_refusal,
    stream_paths_newest_first,
)
from reckon.crew.runs import (
    _manifest_mtime_ns,
    _pointer_lock,
    _utc_now,
    _write_json,
    crew_home,
    list_live,
    pointer_path,
    process_alive,
    read_pointer,
    record_process_alive,
    run_dir,
)

# What the resumed worker is told. It states the two things the worker cannot
# know from inside its own stopped turn — that the limit which stopped it has
# passed, and that nothing else about its node has changed — and then asks for
# the only thing it needs to do, which is carry on.
CONTINUE_ADVICE = (
    "the provider limit that stopped this run has reset; continue from where "
    "you stopped, keeping the same node, fence and manifest path"
)

# Where a resume that was refused because the run already had a live attempt
# leaves the advice it was carrying. The advice belongs to the run rather than
# to the refused attempt: the attempt that holds the session is the one that
# must receive it, and it will not be re-issued by whoever typed the refusal,
# so the text is kept beside the run's resume records and delivered to the next
# resume of that run. The existing resume records cannot carry it: a
# resume-<n>-advice.txt is named for a turn the refused attempt never reached,
# so its number is unknown at refusal time, and the text needs its own slot.
REFUSED_ADVICE_NAME = "resume-refused-advice.txt"

# The pointer slot recording that a resume of this run has begun and has not
# yet recorded its attempt. The launch is not atomic with the check that
# licenses it — the plan is built and the process spawned before the attempt's
# pid is folded onto the pointer — so without this marker a second door reading
# the pointer inside that window sees a run whose recorded process has ended
# and starts a worker beside one already starting. The slot carries the pid of
# the process doing the launching and the kernel start tick that pid was
# issued with: while that process lives the launch is in flight. The start
# tick is carried because the pid alone cannot answer it — a launcher that
# died without clearing its mark leaves a number the kernel may hand to an
# unrelated process, and read by number alone that stranger would hold every
# resume of the run off for as long as it lived. The refusal then cannot
# outlive the attempt it guarded.
RESUME_LAUNCH_IN_FLIGHT = "resume_launch_in_flight"


def refused_advice_path(run_id: str) -> Path:
    """The path a refused resume leaves its advice at, beside the run records."""
    return run_dir(run_id) / REFUSED_ADVICE_NAME


def store_refused_advice(run_id: str, advice: str) -> Path | None:
    """Keep a refused resume's advice for the run's next resume.

    Returns the path the text was written to, or None when there is no advice
    to keep. The text is what the next resume must deliver, so it is stored
    verbatim and never merged with an earlier refusal's: the most recent
    coordinator's answer is the one the run was last told.
    """
    text = str(advice or "").strip()
    if not text:
        return None
    path = refused_advice_path(run_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def consume_refused_advice(run_id: str) -> str:
    """Read and clear the advice a refusal left for this run, or ''.

    The next resume is the one that delivers it, so the slot is emptied as it
    is read: advice already carried into a resumed turn must not be replayed
    into the one after it as though it were new.
    """
    path = refused_advice_path(run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    with suppress(OSError):
        path.unlink()
    return text.strip()


def launch_in_flight(record: Mapping[str, Any]) -> bool:
    """Whether a resume of this run has begun and not yet recorded an attempt.

    The mark this process wrote is not a refusal owed to this process. A door
    that composed a plan and then chose not to launch — a resume offered and not
    taken, a launcher that raised — would otherwise leave its own mark standing
    for as long as it lives, refusing the very resume that would carry the run
    on. The window this closes is between processes: another door, reading the
    pointer while a launch it cannot see is under way.
    """
    marker = record.get(RESUME_LAUNCH_IN_FLIGHT)
    if not isinstance(marker, Mapping):
        return False
    pid = marker.get("pid")
    if pid is None or pid == os.getpid():
        return False
    return record_process_alive(marker) is True


def mark_resume_launching(run_id: str) -> None:
    """Record that this process has begun a resume of the run.

    A run with no live pointer has nothing to carry the mark and nothing that
    could read it back, so there is nothing to write and no refusal to warn of.
    """
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        current[RESUME_LAUNCH_IN_FLIGHT] = {
            "pid": os.getpid(),
            "pid_start_time": process_start_time(os.getpid()),
            "at": _utc_now(),
        }
        _write_json(pointer_path(run_id), current)


def clear_resume_launching(run_id: str) -> None:
    """Drop the in-flight marker once the attempt's pid has been recorded."""
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        if current.pop(RESUME_LAUNCH_IN_FLIGHT, None) is None:
            return
        _write_json(pointer_path(run_id), current)


def live_attempt_refusal(run_id: str, record: Mapping[str, Any]) -> CrewError | None:
    """The refusal a resume owes a run that already has an attempt starting.

    Composed here rather than at each door, so the launcher and the prediction
    that mirrors it refuse an in-flight run with one message, exactly as they do
    for the process-table guard beside it. The refusal names the pid the launch
    is running under and the file the refused advice is kept in, so a
    coordinator receiving it can check the process table and find the text the
    run's next resume will carry.
    """
    if not launch_in_flight(record):
        return None
    marker = record.get(RESUME_LAUNCH_IN_FLIGHT)
    return _in_flight_refusal(run_id, marker if isinstance(marker, Mapping) else {})


def _in_flight_refusal(run_id: str, marker: Mapping[str, Any]) -> CrewError:
    """The one wording every door refuses an in-flight launch with."""
    pid = marker.get("pid")
    named = f" (pid {pid})" if pid is not None else ""
    return CrewError(
        f"run {run_id!r} already has a resume launching{named}: a launch has "
        "begun and has not yet recorded its attempt, so a second resume would "
        "start a worker beside it and leave both writing one tree; the advice "
        f"is kept at {refused_advice_path(run_id)}"
    )


# The runs whose launch this process has begun and not yet finished. The pointer
# mark separates one process from another; it cannot separate two passes of one
# sweep, which share this process, and whose mark therefore necessarily names
# it. This registry is the same fact kept where the two passes can be told
# apart.
_LAUNCHES_IN_THIS_PROCESS: set[str] = set()


def _refuse_a_launch_already_running(run_id: str) -> None:
    """Refuse a second launch of this run begun inside this process.

    The reading above exempts a process from its own mark, because a door that
    composed a plan and then chose not to launch must not hold off its own next
    resume. A sweep is the door that exemption leaves open: its passes share the
    one long-lived process that follows a fleet, so two wait-lifts of one run
    would be separated by nothing but the re-read a launch performs before it
    retires an attempt's records. The window a launch opens is per run in this
    process, so it is kept here rather than read back off the pointer: a pointer
    mark naming this process cannot say whether it is a live launch or the
    residue of one that was built and then abandoned.
    """
    if run_id not in _LAUNCHES_IN_THIS_PROCESS:
        return
    raise _in_flight_refusal(run_id, {})


# How often the follower may sweep. Long enough that a tight poll loop cannot
# turn the sweep into a hot path, short enough that a lane returning minutes
# later is picked up while the wave still matters.
DEFAULT_SWEEP_SECONDS = 120.0

# A served request is current availability evidence, but only briefly. The
# cache exists to turn a wave of callers into one provider request; it never
# turns a historical observation into a long-lived verdict.
DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS = 60.0
AVAILABILITY_PROBE_PROMPT = "Reply with OK."

# What a sweep writer can call itself, so the status record can tell a
# reader's own follower apart from a hand-run pass without a process lookup.
# A follower keeps its own entry; the other kinds share one slot each, because
# they have no durable identity a reader could name.
SWEEP_WRITER_KINDS = frozenset({"follower", "command", "mcp", "other"})

# The status record keeps at most this many writer entries. Hand-run, MCP and
# other callers each collapse to a single slot, so the only unbounded growth is
# distinct follower sessions; every session that ends leaves one stale entry,
# and this cap evicts the least recently swept so the file stays bounded while
# a reader who saw many sessions keeps their history.
MAX_SWEEP_WRITERS = 8


def _parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, returning None for anything unreadable."""
    return parse_utc(str(value))


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=UTC)


def _safe_project(project: str) -> str:
    return (
        "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in str(project)
        ).strip("-")
        or "project"
    )


def lane_probe_cache_path(project: str, backend_name: str) -> Path:
    """Return one backend's durable availability observation cache."""
    return (
        crew_home()
        / "recovery"
        / "lane-probes"
        / _safe_project(project)
        / f"{_safe_project(backend_name)}.json"
    )


@contextmanager
def _lane_probe_lock(project: str, backend_name: str):
    """Serialize cache refreshes so concurrent callers issue one request."""
    path = lane_probe_cache_path(project, backend_name).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_lane_probe_cache(project: str, backend_name: str) -> dict[str, Any]:
    path = lane_probe_cache_path(project, backend_name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_lane_probe_cache(
    project: str, backend_name: str, observation: Mapping[str, Any]
) -> None:
    path = lane_probe_cache_path(project, backend_name)
    write_json_atomically(
        path,
        dict(observation),
        indent=2,
        sort_keys=True,
        mode=None,
        fsync=False,
        create_parents=True,
    )


def _normalise_lane_probe(
    backend_name: str,
    result: Mapping[str, Any] | None,
    *,
    observed_at: str,
) -> dict[str, Any]:
    """Return the closed availability observation shape used by the fence."""
    result = result if isinstance(result, Mapping) else {}
    status = str(result.get("status") or "unavailable")
    if status not in {"served", "refused", "unavailable"}:
        status = "unavailable"
    return {
        "backend": backend_name,
        "status": status,
        "observed_at": str(result.get("observed_at") or observed_at),
        "detail": str(
            result.get("detail")
            or (
                f"the minimal request was {status}"
                if status != "unavailable"
                else "availability probe returned no result"
            )
        ),
        "budget": dict(result.get("budget") or {}),
        "cached": False,
    }


def _request_lane_availability(
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    root: str | Path | None,
) -> dict[str, Any]:
    """Issue the smallest supported model request and classify its stream."""
    if backend.get("launch") != "cli":
        return {
            "status": "unavailable",
            "detail": "the host cannot spawn an in-harness backend for a probe",
        }
    worktree = Path(root or ".").resolve()
    if not worktree.is_dir():
        return {
            "status": "unavailable",
            "detail": f"the probe working directory {worktree} is unavailable",
        }
    directory = crew_home() / "recovery" / "lane-probes" / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    safe_backend = _safe_project(backend_name)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%f")
    log_path = directory / f"{safe_backend}-{stamp}.jsonl"
    stderr_path = directory / f"{safe_backend}-{stamp}.stderr.log"
    final_path = directory / f"{safe_backend}-{stamp}.final.txt"
    try:
        plan = _backends.launch_plan(
            backend_name=backend_name,
            backend=backend,
            prompt=AVAILABILITY_PROBE_PROMPT,
            worktree=worktree,
            final_message_path=final_path,
            fence=False,
        )
        completed = subprocess.run(
            plan.argv,
            cwd=plan.cwd,
            env={**os.environ, **plan.environment},
            input=plan.stdin_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        log_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        observation = _backends.observe_log(
            backend_name=backend_name,
            backend=backend,
            log_path=log_path,
        )
    except (_backends.BackendError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "detail": f"lane probe could not run: {exc}"}
    if observation.budget.get("refusal"):
        return {
            "status": "refused",
            "detail": observation.detail or "the minimal request was refused",
            "budget": observation.budget,
        }
    if completed.returncode == 0:
        return {
            "status": "served",
            "detail": observation.detail or "the minimal request was served",
            "budget": observation.budget,
        }
    return {
        "status": "unavailable",
        "detail": f"lane probe exited {completed.returncode} without availability evidence",
        "budget": observation.budget,
    }


def probe_lane_availability(
    project: str,
    backend_name: str,
    backend: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    cache_seconds: float = DEFAULT_AVAILABILITY_PROBE_CACHE_SECONDS,
    now: datetime | None = None,
    runner: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Return one bounded, serialized serving observation for a backend."""
    moment = _now(now)
    with _lane_probe_lock(project, backend_name):
        previous = _read_lane_probe_cache(project, backend_name)
        if previous and cache_seconds > 0:
            observed = _parse_stamp(previous.get("observed_at"))
            if observed is not None:
                age = max(0.0, (moment - observed).total_seconds())
                if age <= cache_seconds:
                    return {**dict(previous), "cached": True, "cache_age_seconds": age}
        result = (
            runner(backend_name, backend)
            if runner is not None
            else _request_lane_availability(backend_name, backend, root=root)
        )
        observation = _normalise_lane_probe(
            backend_name,
            result,
            observed_at=moment.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        _write_lane_probe_cache(project, backend_name, observation)
        return observation


def recovery_log_path(project: str) -> Path:
    """Where one project's recovery record accumulates."""
    readable = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(project)
    ).strip("-")
    return crew_home() / "recovery" / f"{readable or 'project'}.jsonl"


def _lifted_holds(project: str) -> set[tuple[str, str]]:
    """The (run id, signature) pairs the recovery log already records as lifted.

    The sweep's append-only recovery log is the authority on what it has already
    resumed: one line per sweep that judged anything, and its ``resumed`` list
    names each lift with the run's id and the hold or condition signature it was
    lifted for. A whole-pointer rewrite cannot drop a line from it, which is what
    the pointer's own ``auto_resume`` slot cannot promise — a resume's launch
    rewrites the pointer from a snapshot taken before the stamp, so the slot can
    read absent for a lift that happened.
    """
    lifted: set[tuple[str, str]] = set()
    try:
        lines = recovery_log_path(project).read_text(encoding="utf-8").splitlines()
    except OSError:
        return lifted
    for line in lines:
        if not line.strip():
            continue
        try:
            report = json.loads(line)
        except ValueError:
            continue
        if not isinstance(report, Mapping):
            continue
        # A dry run appends its own line and lists what it *would* resume, so a
        # preview is not a lift and must not retire a hold the sweep never took.
        if report.get("dry_run"):
            continue
        for entry in report.get("resumed") or ():
            if not isinstance(entry, Mapping):
                continue
            run_id = str(entry.get("run_id") or "")
            signature = str(entry.get("hold") or "")
            if run_id and signature:
                lifted.add((run_id, signature))
    return lifted


def _refusal_observed_at(record: Mapping[str, Any]) -> datetime | None:
    """When the refusal was recorded, read from the stream that carries it.

    The budget block states what the account said, never when this side heard
    it, so the stream's own mtime is the observation time — the same reading
    the classifier already uses to age a quiet run.
    """
    log = Path(str(record.get("log_path") or ""))
    try:
        return datetime.fromtimestamp(log.stat().st_mtime, tz=UTC)
    except OSError:
        return _parse_stamp(
            record.get("attempt_started_at") or record.get("created_at")
        )


def hold_state(
    record: Mapping[str, Any],
    refusal: Mapping[str, Any],
    *,
    policy_block: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Whether this run's own hold is still in force, and what identifies it.

    A hold that names a reset is dated by it. One that names none is aged
    against the declared shelf life, from the moment the refusal was observed —
    the same bound the pre-flight applies, read from the same policy block, so
    a project that lengthens its shelf life lengthens both together.
    """
    moment = _now(now)
    resets_at = _parse_stamp(refusal.get("resets_at"))
    if resets_at is not None:
        return {
            "signature": f"resets:{resets_at.isoformat()}",
            "in_force": resets_at > moment,
            "detail": f"the refusal states a reset at {refusal['resets_at']}",
        }
    bound = float(
        policy_block.get(
            "evidence_shelf_life_minutes", budget_module.DEFAULT_SHELF_LIFE_MINUTES
        )
    )
    observed = _refusal_observed_at(record)
    if observed is None:
        return {
            "signature": "",
            "in_force": True,
            "detail": "the refusal states no reset and carries no readable observation time",
        }
    minutes = int((moment - observed).total_seconds() // 60)
    return {
        "signature": f"observed:{observed.isoformat()}",
        "in_force": bound > 0 and minutes <= bound,
        "detail": (
            f"the refusal states no reset and is {minutes} minutes old against "
            f"a {bound:g} minute shelf life"
        ),
    }


# The three places a run's session can be, in the order a resolver must read
# them: the cheap authoritative one, the one that answers when the cheap one has
# simply not been folded in yet, and the one that answers after promotion has
# taken the pointer away.
SESSION_SOURCES = ("pointer", "stream", "ledger")


def _pointer_session(record: Mapping[str, Any]) -> str:
    return str(record.get("session_id") or "").strip()


def _stream_session(record: Mapping[str, Any]) -> str:
    """The session id the run's newest non-empty stream carries, or empty.

    This is the source the incident turned on. A pointer carrying no session id
    means only that nothing has folded the stream in yet — resume has re-read
    it for months — so reading the pointer alone reports an absence that is not
    one. The streams are read newest first, because a run that has resumed or
    changed lane writes to a newer file than its first stream; a newer stream
    that carries no id yet therefore falls back to an earlier one rather than
    answering an absence it cannot support.
    """
    from reckon import _backends

    if record.get("launch") != "cli":
        return ""
    run_id = str(record.get("run_id") or "")
    directory = (
        Path(run_dir(run_id))
        if run_id
        else Path(str(record.get("log_path") or ".")).parent
    )
    for log in stream_paths_newest_first(directory, include=(record.get("log_path"),)):
        try:
            backend = _backend_settings(record, None)
            observation = _backends.observe_log(
                backend_name=str(record.get("backend") or ""),
                backend=backend,
                log_path=log,
            )
        except (CrewError, OSError, ValueError):
            continue
        found = str(observation.session_id or "").strip()
        if found:
            return found
    return ""


def _ledger_session(run_id: str, *, project: str, root: Any) -> str:
    """The session the promoted row recorded, or empty.

    A promoted run has no pointer and no live stream to consult, and its
    session may still be resumable — so the committed row is the third source
    rather than the end of the search.
    """
    if not project:
        return ""
    try:
        data, _version = ledger.load(project, root=root)
    except Exception:  # noqa: BLE001 - an unreadable ledger answers nothing
        return ""
    for row in reversed(data.get("runs") or []):
        if str(row.get("run_id") or "") == run_id:
            return str(row.get("session_id") or "").strip()
    return ""


def resolve_session(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    project: str = "",
    root: Any = None,
) -> dict[str, Any]:
    """Answer the session for one run, and say which source answered.

    Every surface that reports a session asks this, so two surfaces asked about
    the same run cannot disagree. The sources are consulted in order — live
    pointer, the run's own stream, the promoted ledger row — and the answer
    carries the one that supplied it.

    Where none has an id the answer is a stated absence naming all three as
    consulted, because *not yet observed* and *genuinely absent* are different
    facts and nothing expressed the difference. A coordinator read a bare null
    as a verdict on five runs and promoted them; the ids were in their streams.
    """
    pointer: Mapping[str, Any] = {}
    if record is not None:
        pointer = record
    else:
        try:
            pointer = read_pointer(run_id)
        except (CrewError, OSError):
            pointer = {}
    # A promoted run has no pointer to name its project, so a caller asking
    # about one states it; otherwise the pointer does.
    project = str(project or pointer.get("project") or "")
    answer = {
        "run_id": run_id,
        "session_id": None,
        "source": None,
        "resolved": False,
        "consulted": list(SESSION_SOURCES),
    }
    found = _pointer_session(pointer)
    if found:
        return {**answer, "session_id": found, "source": "pointer", "resolved": True}
    found = _stream_session(pointer)
    if found:
        return {**answer, "session_id": found, "source": "stream", "resolved": True}
    found = _ledger_session(
        run_id, project=project, root=root if root is not None else pointer.get("repo")
    )
    if found:
        return {**answer, "session_id": found, "source": "ledger", "resolved": True}
    return {
        **answer,
        "detail": (
            "no session id in the live pointer, the run's own stream or the "
            "promoted ledger row; all three were consulted, so this is an "
            "absence rather than a reading nobody has taken yet"
        ),
    }


def _claimed_write_paths(record: Mapping[str, Any]) -> list[str]:
    """Write paths this run declared that a different live run now claims.

    Only a run that is still working can hold a scope: a pointer sitting in its
    own terminal state is what the coordinator is about to reconcile, and
    treating it as an owner would make every stopped wave permanently
    unrecoverable.
    """
    node = record.get("node") or {}
    mine = {str(path) for path in (node.get("write_paths") or ())}
    if not mine:
        return []
    run_id = str(record.get("run_id") or "")
    repo = str(record.get("repo") or "")
    claimed: set[str] = set()
    for other in list_live():
        if str(other.get("run_id") or "") == run_id:
            continue
        if str(other.get("repo") or "") != repo:
            continue
        if record_process_alive(other, process_alive) is not True:
            continue
        other_node = other.get("node") or {}
        claimed |= mine & {str(path) for path in (other_node.get("write_paths") or ())}
    return sorted(claimed)


def _readonly_budget_verdict(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The lane's budget verdict for a resume, with none of the durable writes.

    The launcher judges a resume through the recorded readings and the lane's own
    account surface, then files the check it made into the ledger. A prediction
    must see the same evidence — the guards must not be able to disagree about
    the lane — but it must not record a check for an attempt that never happens,
    because a hold check is the durable thing a later wave reads to decide
    whether a lane is open.
    """
    project = str(record.get("project") or "")
    root = record.get("repo")
    backend_name = str(record.get("backend") or "")
    backend = _backend_settings(record, config)
    recorded = budget_module.latest_recorded(project, root=root, config=config)
    state = budget_module.state_for(
        backend_name,
        backend,
        recorded=recorded.get(backend_name),
        unattributed=recorded.unattributed,
    )
    return budget_module.decide(state, budget_module.policy(config), purpose="resume")


def _launcher_refusal(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> BaseException | None:
    """The refusal the launcher would return for this run, or None.

    A dry run exists to predict the real action, so it reports what the launcher
    would do rather than that the launcher would be asked: this consults the
    guards the launcher applies before it builds a resume plan, without an
    attempt and without the writes an attempt leaves behind. The guards return
    the same exception the launcher raises, so one formatter names a refusal
    identically whichever path met it.

    A change here that the launcher does not make, or the launcher guarding
    something this does not, is caught by the parity tests beside this: for
    every guard, `resume_plan` and this function must refuse the same record
    with the same message.
    """
    run_id = str(record.get("run_id") or "")
    if record.get("launch") != "cli":
        return CrewError(f"run {run_id!r} is not a spawned run; resume it in-harness")
    if record_process_alive(record, process_alive) is True:
        # This guard's message is the launcher's own, held word for word by the
        # prediction-parity test beside it, so it is not restated here. The pid
        # naming lives on the observed-end gate below, whose reading is this
        # module's and which is where the run's own worker record answers.
        return CrewError(
            f"run {run_id!r} still has a live process; observe or stop it before resuming"
        )
    # The prediction must meet the in-flight guard too, or a preview would offer
    # a resume the real launcher refuses while a launch of this run is in
    # flight. It is composed by the same helper the launcher uses, so the two
    # refuse with one message.
    in_flight = live_attempt_refusal(run_id, record)
    if in_flight is not None:
        return in_flight
    try:
        _require_fleet_gate_open()
    except LanePaused as exc:
        return exc
    verdict = _readonly_budget_verdict(record, config=config)
    if verdict["held"]:
        return _actionable_budget_hold(verdict, config=config)
    # The window gate is the launcher's own, so the prediction must consult it
    # rather than report a resume the real call refuses. Consulted here, ahead
    # of the sweep's observed-end gate, so both doors report the same reason
    # for a run that fails several.
    return resume_window_refusal(
        run_id, record, backend=_backend_settings(record, config), config=config
    )


def _observed_end_refusal(record: Mapping[str, Any]) -> CrewError | None:
    """The refusal a lift owes a run whose process end nothing observed.

    The sweep acts on nothing but a lane returning or a declared condition
    terminating, and it is the only actor that starts a worker without a person
    deciding to. A run whose process still holds the run is refused upstream,
    but "not known to be alive" is not the same reading as "observed to have
    ended": a worker on another machine and a pointer that recorded no process
    at all are both readings in which the worker may still be writing, and a
    second worker started on one of those collides with the classifier's own
    promise that the lift waits for an end.

    So the lift requires an observation, and there are exactly two: a pid
    checked on this host and found dead, or the supervisor's exit record. The
    reading is composed here through the same three-way vocabulary the row
    states (``local_liveness`` under ``_process_reading``), so the sweep cannot
    act on a verdict the reader was never shown, and the reason names the
    reading it is holding.
    """
    alive, liveness_proven = local_liveness(record)
    exit_record = _run_exit_record(record)
    # The supervisor's record is an observation of the end, and it is the one a
    # prompt reader can never have: it outlives a pointer nobody updated and it
    # stays readable on a machine that never launched the worker. Folding it in
    # before the reading is what the classifier does for the same evidence, so a
    # recorded exit reads as an ended process on both surfaces rather than as
    # the unknown a bare reading of it composes. Where the process table says
    # the worker is still there the record is an earlier attempt's and does not
    # apply, which is why the fold is conditional on the living answer.
    ended_exit = exit_record if exit_record is not None and alive is not True else None
    if ended_exit is not None:
        alive = False
    reading = _process_reading(
        alive, liveness_proven=liveness_proven, exit_record=ended_exit
    )
    if reading == "process gone":
        return None
    run_id = str(record.get("run_id") or "")
    if reading == "alive":
        # The bare pid probe above can answer not-alive while the work itself is
        # still running — a supervisor that exits before its worker takes the
        # pointer's pid with it — so the host-gated reading is asked as well and
        # can still answer alive here. The refusal names the pid that supplied
        # the reading, so a coordinator can check it against the process table
        # rather than take the refusal on faith. Where the reading came from a
        # stored answer carrying no process, there is no pid to name and the
        # refusal stands without one.
        pid = live_worker_pid(record)
        named = f" (pid {pid})" if pid is not None else ""
        return CrewError(
            f"run {run_id!r} reads 'alive' for a lift: the run's own liveness "
            f"reading has not observed the worker's end{named}, and resuming "
            "under a living worker collides with the worker that holds it"
        )
    return CrewError(
        f"run {run_id!r} is not resumed: the process reading is "
        f"{reading!r} — no pid was checked dead on this host and no supervisor "
        "exit was recorded — so nothing observed the end the lift would rest on"
    )


def _refusal_entry(entry: Mapping[str, Any], exc: BaseException) -> dict[str, Any]:
    """The skip record a launcher refusal produces, whichever path met it.

    Shared so a dry run's report and a real sweep's report name the same
    refusal with the same reason and the same detail — a prediction that reads
    differently from the thing it predicts is not a prediction.
    """
    if isinstance(exc, LanePaused):
        return {
            **entry,
            "reason": "lane-paused",
            "detail": str(exc),
            "lane_gate": dict(exc.gate),
        }
    if isinstance(exc, BudgetHold):
        return {
            **entry,
            "reason": "hold-in-force",
            "detail": (
                "the lane's own budget verdict still holds it: "
                + str(exc.verdict.get("reason") or exc)
            ),
        }
    return {**entry, "reason": "resume-refused", "detail": str(exc)}


def _launch_failure_block(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Describe the launch failure that stops this run's lift, or None.

    Only a hand-typed resume or a completion clears the phase, so the lift loop
    is where the refusal belongs: a watcher lifting this run would repeat an
    exec that already failed, once per tick, which is the loop the phase exists
    to stop. The last recorded failure supplies the cause and the remedy.
    """
    phase = str(record.get("phase") or "")
    if phase != "launch-failed":
        return None
    failures = list(record.get("launch_failures") or ())
    latest = failures[-1] if failures else {}
    exit_status = latest.get("exit_status")
    backend = str(latest.get("backend") or record.get("backend") or "the backend")
    tail = str(latest.get("stderr_tail") or "").strip().splitlines()
    cause = tail[-1] if tail else "the process exited before writing any turn"
    return {
        "phase": phase,
        "detail": (
            f"a {backend} launch exited with status {exit_status} before "
            f"writing any stream record ({cause}); {len(failures)} failure"
            f"{'s' if len(failures) != 1 else ''} recorded"
        ),
        "remedy": f"fix the command for backend {backend!r} and its PATH",
    }


def _spawn(plan: Any, *, log_path: Path, stderr_path: Path, prompt_path: Path) -> int:
    """Start a resumed worker through the run's own supervisor.

    A resumption leaves through the same supervisor a dispatch does, so its
    worker is not a dispatch's grandchild and its exit is recorded. The
    repository and worktree a resume continues in are the run's own recorded
    ones and the log's directory is the run directory, so the details are read
    back from the run rather than threaded through the call.
    """
    from reckon.crew.dispatch import supervised_launch

    _require_fleet_gate_open()
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


def _write_resume_prompt(advice_path: Path, *, plan: Any, advice: str) -> None:
    """Write the prompt the resumed worker reads from ``advice_path``.

    The worker reads this file as its prompt, so it must carry the plan's own
    prompt — the advice restated with the time fence composed for the attempt
    now starting — rather than the bare advice. Writing the advice alone left
    the resumed worker reading the inherited session's original deadline, so it
    aged its work against a clock that had already expired. A backend that
    feeds the prompt on argv rather than stdin carries no plan stdin text, and
    there the advice is the whole prompt and is written unchanged.

    Shared so every door that starts a resumption composes the file the same
    way: a door that writes the bare advice leaves the worker on the first
    attempt's deadline however the launch itself was composed.
    """
    prompt_text = plan.stdin_text or advice
    advice_path.write_text(prompt_text.rstrip("\n") + "\n", encoding="utf-8")


def _reset_delivered_manifest_status(
    run_id: str, record: Mapping[str, Any] | None = None
) -> Path | None:
    """Clear a delivered manifest's verdict before a new attempt rewrites it.

    A resumed or relaunched worker keeps its manifest path, so the previous
    attempt's delivered manifest — reading complete or failed — is still on
    disk when the new worker starts. The launch supervisor ends a worker whose
    manifest reads one of those done words and has rested, so the first edit
    the new worker makes to any other field would make the stale delivery look
    fresh and the supervisor would end it as delivered. Setting the status word
    to a non-terminal value before the new attempt spawns leaves the supervisor
    waiting on it, and the classifier reads the run as working.

    The delivered text is kept beside the manifest under a name carrying the
    attempt it came from, so an earlier copy — such as a status repair's
    ``manifest.md.asdelivered`` — is never overwritten. A status outside the
    supervisor's done set (blocked, waiting, in-progress) is left as it is. The
    manifest already in force is returned so a caller can report where the
    delivery was kept, or None when nothing was replaced.
    """
    if record is None:
        try:
            record = read_pointer(run_id)
        except CrewError:
            record = {}
    named = str(record.get("manifest_path") or "").strip()
    manifest = Path(named) if named else run_dir(run_id) / "manifest.md"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    status = ""
    for line in lines:
        if line.startswith("status:"):
            status = line.split(":", 1)[1].strip().lower()
            break
    if status not in _WORKER_DONE_MANIFEST_STATUSES:
        return None
    attempt = int(record.get("attempt") or 1)
    kept = manifest.with_name(f"{manifest.name}.attempt-{attempt}.asdelivered")
    if not kept.exists():
        kept.write_text(text, encoding="utf-8")
    for index, line in enumerate(lines):
        if line.startswith("status:"):
            lines[index] = "status: in-progress"
            break
    rewritten = "\n".join(lines)
    if text.endswith("\n"):
        rewritten += "\n"
    write_atomically(manifest, lambda handle: handle.write(rewritten), fsync=False)
    return kept


def _resume(
    run_id: str,
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    launcher: Callable[..., int] | None = None,
    advice: str = CONTINUE_ADVICE,
) -> dict[str, Any]:
    """Launch one resumption exactly the way a hand-typed resume does.

    With no launcher the resumption goes through the run's supervisor, exactly
    as a dispatch does, so its worker is not a child of whoever swept and its
    exit is recorded. A caller-supplied launcher is the in-process seam a test
    uses to stand in for that supervisor.
    """
    # The repository a resume continues in is the project's mount, exactly as
    # for the dispatch that created the run. A run recorded in a different
    # repository is refused here, naming both roots, rather than resumed into a
    # checkout the project does not own.
    resume_project = str(record.get("project") or "")
    if resume_project and project_mount_repository(resume_project) is not None:
        resolve_project_repository(
            resume_project,
            record.get("repo"),
            flag="the run's recorded repository",
        )
    failure = _launch_failure_block(record)
    if failure is not None:
        # A launch that produced no stream has no session to continue, so a
        # lift would repeat the failed exec every tick. It waits for a person.
        raise CrewError(
            f"run {run_id!r} is in phase {failure['phase']!r}: "
            f"{failure['detail']} — repair the launch ("
            f"{failure['remedy']}) and resume it by hand, or complete it"
        )
    _refuse_a_launch_already_running(run_id)
    _LAUNCHES_IN_THIS_PROCESS.add(run_id)
    mark_resume_launching(run_id)
    try:
        plan = resume_plan(run_id, advice, config=config)
        directory = run_dir(run_id)
        turn = len(list(directory.glob("resume-*.jsonl"))) + 1
        advice_path = directory / f"resume-{turn}-advice.txt"
        advice_path.parent.mkdir(parents=True, exist_ok=True)
        _write_resume_prompt(advice_path, plan=plan, advice=advice)
        log_path = directory / f"resume-{turn}.jsonl"
        stderr_path = directory / f"resume-{turn}.stderr.log"
        attempt_started_at = _utc_now()
        manifest_baseline_mtime_ns = _manifest_mtime_ns(record.get("manifest_path") or "")
        _require_fleet_gate_open()
        # The previous attempt may have left a done manifest on disk; clear its
        # verdict so the supervisor does not read the new worker's first edit as
        # that delivery.
        _reset_delivered_manifest_status(run_id, record)
        if launcher is None:
            pid = _spawn(
                plan,
                log_path=log_path,
                stderr_path=stderr_path,
                prompt_path=advice_path,
            )
        else:
            pid = launcher(
                plan,
                log_path=log_path,
                stderr_path=stderr_path,
                prompt_path=advice_path,
            )
    except BaseException:
        # A launch that ended before it held the session recorded no attempt of
        # its own, so nothing will drop the in-flight marker this door set.
        # Left there it would refuse the next resume against a launcher already
        # gone, so the marker goes with the launch that raised.
        clear_resume_launching(run_id)
        raise
    finally:
        _LAUNCHES_IN_THIS_PROCESS.discard(run_id)
    record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    return {"pid": pid, "turn": turn, "log_path": str(log_path), "plan": plan}


def _run_condition_probe(
    record: Mapping[str, Any], wait: Mapping[str, Any]
) -> dict[str, Any]:
    """Run a waiting manifest's argument vector without invoking a shell.

    This is the sweep's reader and the only one whose verdict lifts a run: the
    ``terminal`` field it returns is what ``sweep`` branches on before it offers
    a parked run to the resume loop. A file condition reaches it as the vector
    ``reckon/crew/recovery._wait_file_probe`` derives from the declared paths,
    and that vector prints nothing and exits 0 exactly when every path exists,
    so the observed state falls back to ``exit:<code>`` and matches the derived
    ``exit:0`` terminal.

    The classifier's reader, ``reckon/crew/recovery._run_wait_condition_probe``,
    is a different implementation: it answers a file condition by looking for
    the paths themselves, never runs a vector, and returns a tri-state for a
    row rather than a verdict. A change here decides whether a park lifts; a
    change there decides what a row says. They must agree on the answer, and
    nothing makes them agree but the declaration they both read.
    """
    worktree = Path(str(record.get("worktree") or "."))
    try:
        completed = subprocess.run(
            list(wait.get("probe") or ()),
            cwd=worktree if worktree.is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "terminal": False,
            "observed": "unavailable",
            "detail": f"condition probe could not run: {exc}",
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    observed = lines[-1] if lines else f"exit:{completed.returncode}"
    candidates = {observed.casefold()}
    candidates.update(
        line.split(maxsplit=1)[0].rstrip("+").casefold() for line in lines
    )
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    return {
        "terminal": bool(candidates & terminal),
        "observed": observed,
        "detail": (
            f"condition probe reported {observed!r} with exit {completed.returncode}"
        ),
    }


def _condition_observation(value: Any) -> dict[str, Any]:
    """Normalise an injected or subprocess-backed condition observation."""
    if isinstance(value, bool):
        return {
            "terminal": value,
            "observed": "terminal" if value else "pending",
            "detail": "condition test returned a boolean verdict",
        }
    if not isinstance(value, Mapping):
        raise TypeError("condition test must return a mapping or boolean")
    terminal = value.get("terminal")
    if not isinstance(terminal, bool):
        raise TypeError("condition test result must carry a boolean terminal field")
    return {
        "terminal": terminal,
        "observed": str(value.get("observed") or "unknown"),
        "detail": str(value.get("detail") or "condition test returned a verdict"),
    }


def _stamp_resumption_trigger(run_id: str, signature: str) -> None:
    """Record which hold or condition caused a resumption, so it happens once."""
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        current["auto_resume"] = {"trigger": signature, "at": _utc_now()}
        _write_json(pointer_path(run_id), current)


def sweep_status_path(project: str) -> Path:
    """Where one project's sweep status accumulates, one entry per writer."""
    return recovery_log_path(project).with_suffix(".status.json")


def _read_sweep_status(project: str) -> dict[str, Any]:
    """The recorded sweep status for a project, or an empty mapping."""
    try:
        value = json.loads(sweep_status_path(project).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _normalise_writer(writer: Any) -> tuple[str, str]:
    """Return (kind, key) identifying the writer of one sweep.

    A caller declares the kind it is — a follower on its cadence, a hand-run
    sweep from the command line, the MCP surface, or anything else — because a
    reader distinguishing follower health from their own manual pass cannot do
    it from a pid on a shared filesystem. The key is the caller's durable
    identity within its kind: the session id for a follower, empty for a kind
    with no stable identity, which gives every such caller one shared slot.
    """
    kind = "other"
    key = ""
    if isinstance(writer, Mapping):
        kind = str(writer.get("kind") or "other")
        key = str(writer.get("key") or writer.get("session") or writer.get("id") or "")
    elif isinstance(writer, str) and writer:
        kind, _, key = writer.partition(":")
    if kind not in SWEEP_WRITER_KINDS:
        kind = "other"
    return kind, key


def _writer_key(kind: str, key: str) -> str:
    """The stable map key a writer's entry lives under in the status record."""
    return f"follower:{key}" if kind == "follower" else kind


def _review_outcome_counts(reviews: Any) -> dict[str, int]:
    """The review pass's outcome, as counts a status reader can consume.

    Counts rather than lists because this lands in a per-writer status entry
    that answers "did my follower dispatch reviews?" — a reader wanting the
    reports themselves reads the sweep report, and a refusal list folded into
    a status line would be the unreadable object this pass was repaired for.
    """
    if not isinstance(reviews, Mapping):
        return {"dispatched": 0, "refused": 0, "awaiting_lane": 0}
    return {
        "dispatched": len(reviews.get("dispatched") or ()),
        "refused": len(reviews.get("refused") or ()),
        "awaiting_lane": len(reviews.get("awaiting_lane") or ()),
    }


def _sweep_writer_entry(
    report: Mapping[str, Any], kind: str, key: str
) -> dict[str, Any]:
    """One writer's last sweep, carrying enough to answer without a process."""
    return {
        "kind": kind,
        "key": key,
        "swept_at": report.get("swept_at"),
        "dry_run": report.get("dry_run"),
        "checked": report.get("checked"),
        "resumed": len(report.get("resumed") or []),
        "skipped": len(report.get("skipped") or []),
        "reviews": _review_outcome_counts(report.get("reviews")),
        "swept_by_pid": os.getpid(),
    }


@contextmanager
def _sweep_status_lock(project: str):
    """Serialize the read-modify-write on the status record.

    Followers write on a cadence that keeps concurrent writes rare, but the
    record is a merge after this change rather than a blind overwrite, and two
    followers on a shared filesystem must not lose each other's entries.
    """
    path = sweep_status_path(project).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_sweep_status(
    project: str, report: Mapping[str, Any], writer: Any = None
) -> None:
    """Record that a sweep ran, what it found, which process ran it, and who.

    Written per writer rather than per project, because the question a reader
    actually asks is whether THEIR follower is sweeping — a question one global
    slot cannot answer when the file is written by every follower on the
    project, by hand-run sweeps, and from the MCP surface, and the stamp is
    only the last write by any writer. Each writer keeps its own last sweep; a
    kind with no durable identity shares a single slot recording its most
    recent pass. The project-level fields answer the question the file answered
    before — the latest sweep's pid and instant — while ``writers`` holds each
    writer's own, so existing readers keep their answer.

    The map is bounded so a finished session does not accumulate forever: when
    it passes MAX_SWEEP_WRITERS the least recently swept entries are dropped
    until it fits again, the writer that just wrote always kept. Hand-run, MCP
    and other callers already share one slot each, so the only growth is
    distinct follower sessions; the cap keeps the file bounded while retaining
    the session history a reader may still want.
    """
    path = sweep_status_path(project)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _sweep_status_lock(project):
            previous = _read_sweep_status(project)
            kind, key = _normalise_writer(writer)
            writers = dict(previous.get("writers") or {})
            writers[_writer_key(kind, key)] = _sweep_writer_entry(report, kind, key)
            if len(writers) > MAX_SWEEP_WRITERS:
                freshly_written = _writer_key(kind, key)
                # Deterministic: drop by sweep time, ties by key, never the
                # writer that just wrote.
                stale = sorted(
                    writers,
                    key=lambda name: (
                        str(writers[name].get("swept_at") or ""),
                        name,
                    ),
                )
                for name in stale:
                    if name == freshly_written:
                        continue
                    writers.pop(name)
                    if len(writers) <= MAX_SWEEP_WRITERS:
                        break
            path.write_text(
                json.dumps(
                    {
                        "project": project,
                        "swept_at": report.get("swept_at"),
                        "dry_run": report.get("dry_run"),
                        "checked": report.get("checked"),
                        "resumed": len(report.get("resumed") or []),
                        "skipped": len(report.get("skipped") or []),
                        "reviews": _review_outcome_counts(report.get("reviews")),
                        "swept_by_pid": os.getpid(),
                        "writers": writers,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    except OSError:
        # A status that cannot be written must not stop the recovery it
        # describes; the next tick writes it.
        return


def read_sweep_status(project: str) -> dict[str, Any]:
    """The recorded project-level sweep view, or an empty mapping.

    The answer the file was built for — whether a sweep ran and found nothing,
    or never ran — is the project-level view: present with ``checked`` zero
    meaning it ran and found nothing, an empty mapping meaning no sweep has
    ever been recorded.
    """
    return _read_sweep_status(project)


def writer_sweep_status(project: str, writer: Any) -> dict[str, Any] | None:
    """The recorded last sweep by one named writer, or None when it never wrote.

    None means the writer is unknown to the record, which a reader must be able
    to tell apart from a writer that swept and found nothing — that is a
    mapping whose ``checked`` is zero. The answer comes from the record's own
    contents: no process is consulted, because a pid on a shared filesystem is
    meaningless on another host.
    """
    kind, key = _normalise_writer(writer)
    return read_sweep_status(project).get("writers", {}).get(_writer_key(kind, key))


def sweep(
    project: str,
    *,
    config: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    launcher: Callable[..., int] | None = None,
    now: datetime | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
    writer: Any = None,
) -> dict[str, Any]:
    """Resume runs whose provider hold or declared external wait has ended.

    A sweep also dispatches the review a run has already composed for itself
    once it reaches scoring, so the reflex fires from the same mechanism that
    tells a coordinator the run finished rather than from the coordinator
    remembering. That pass is idempotent for the same reason the resume pass
    is: a run whose review is stored or already in flight is left alone.

    Idempotent by construction: a resumed run carries the trigger it was
    resumed for, so a second pass over the same fleet reports nothing to do.
    That is what makes it safe for something already running to call on a
    cadence.

    ``writer`` names who is sweeping — a follower on its cadence, a hand-run
    sweep from the command line or the MCP surface, or anything else — so the
    status record can tell a reader's own follower apart from every other
    writer of the same file without a process lookup. A caller that declares
    nothing is recorded as ``other``.
    """
    launch = launcher
    test_condition = _run_condition_probe if condition_test is None else condition_test
    policy_block = budget_module.policy(config)
    lifted_holds = _lifted_holds(project)
    resumed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    considered = 0

    for pointer in list_live(project=project):
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        moment = _now(now)
        wait = external_wait(pointer, now_seconds=moment.timestamp())
        refusal = _stream_refusal_block(pointer)
        if wait is None and refusal is None:
            continue
        considered += 1
        advice = CONTINUE_ADVICE
        observation: dict[str, Any] = {}
        if wait is not None:
            entry = {
                "run_id": run_id,
                "backend": pointer.get("backend"),
                "trigger": "external-condition",
                "condition": wait.get("condition"),
            }
            if not wait.get("valid"):
                skipped.append(
                    {
                        **entry,
                        "reason": "condition-declaration-invalid",
                        "detail": wait.get("error"),
                    }
                )
                continue
            try:
                observation = _condition_observation(test_condition(pointer, wait))
            except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
                skipped.append(
                    {
                        **entry,
                        "reason": "condition-check-failed",
                        "detail": str(exc),
                    }
                )
                continue
            if not observation["terminal"]:
                reason = (
                    "condition-overdue" if wait.get("overdue") else "condition-pending"
                )
                skipped.append(
                    {
                        **entry,
                        "reason": reason,
                        "observed": observation["observed"],
                        "detail": (
                            f"waiting {wait['age_seconds']}s; {observation['detail']}"
                        ),
                    }
                )
                continue
            signature = str(wait["signature"])
            advice = str(wait["resume_brief"])
            duplicate_reason = "already-resumed-for-this-condition"
            duplicate_detail = (
                "this run was already resumed after this external condition "
                "terminated; a rewritten waiting manifest creates a new condition"
            )
        else:
            if str(classify_pointer(pointer).get("classification") or "") != "blocked":
                continue
            entry = {"run_id": run_id, "backend": refusal.get("backend")}
            hold = hold_state(pointer, refusal, policy_block=policy_block, now=now)
            if hold["in_force"]:
                skipped.append(
                    {**entry, "reason": "hold-in-force", "detail": hold["detail"]}
                )
                continue
            signature = str(hold["signature"])
            duplicate_reason = "already-resumed-for-this-hold"
            duplicate_detail = (
                "this run was already resumed for the hold that lapsed; "
                "a further refusal writes its own hold to wait on"
            )
        # The recovery log is the authority on what has already been lifted, and
        # the pointer's slot is only a cache of it — one a resume's own launch
        # can drop by rewriting the pointer from a snapshot taken before the
        # stamp. A whole-pointer rewrite cannot drop a line from the log, so a
        # hold already lifted there is refused even when the cache is gone.
        previous = pointer.get("auto_resume")
        cached = (
            isinstance(previous, Mapping)
            and signature
            and str(previous.get("trigger") or previous.get("hold") or "") == signature
        )
        if signature and (cached or (run_id, signature) in lifted_holds):
            skipped.append(
                {
                    **entry,
                    "reason": duplicate_reason,
                    "detail": duplicate_detail,
                }
            )
            continue
        session = resolve_session(run_id, record=pointer)
        if not session["resolved"]:
            skipped.append(
                {
                    **entry,
                    "reason": "no-recoverable-session",
                    "detail": session["detail"],
                }
            )
            continue
        worktree = Path(str(pointer.get("worktree") or ""))
        if not str(pointer.get("worktree") or "") or not worktree.is_dir():
            skipped.append(
                {
                    **entry,
                    "reason": "worktree-absent",
                    "detail": (
                        f"the run's worktree {str(worktree) or '(unset)'} is not on "
                        "disk, and a resume has no working directory to start in"
                    ),
                }
            )
            continue
        collisions = _claimed_write_paths(pointer)
        if collisions:
            skipped.append(
                {
                    **entry,
                    "reason": "scope-claimed-elsewhere",
                    "detail": (
                        "another live run now claims "
                        + ", ".join(collisions)
                        + ", so resuming would create the collision"
                    ),
                }
            )
            continue
        # The launcher's own guards are consulted here rather than assumed:
        # `would_resume` claims the launcher would resume, so a record the
        # launcher would refuse is reported as the refusal it would meet, with
        # the reason and detail a real sweep would report for it. They are
        # consulted once, ahead of both branches, and the observed-end gate with
        # them, so a refusal a real sweep would raise is a refusal the preview
        # reports and the two modes cannot drift apart.
        refusal = _launcher_refusal(pointer, config=config)
        if refusal is None:
            refusal = _observed_end_refusal(pointer)
        if refusal is not None:
            skipped.append(_refusal_entry(entry, refusal))
            continue
        if dry_run:
            resumed.append(
                {
                    **entry,
                    "would_resume": True,
                    "session_id": session["session_id"],
                    "session_source": session["source"],
                    "hold": signature,
                    "advice": advice,
                    **({"observed": observation["observed"]} if observation else {}),
                }
            )
            continue
        try:
            launched = _resume(
                run_id,
                pointer,
                config=config,
                launcher=launch,
                advice=advice,
            )
        except (BudgetHold, CrewError, OSError) as exc:
            skipped.append(_refusal_entry(entry, exc))
            continue
        _stamp_resumption_trigger(run_id, signature)
        # The composed plan rides along for the doors that render it. The sweep
        # records the attempt it started rather than the invocation, so the plan
        # is dropped here instead of written into the recovery log.
        launched.pop("plan", None)
        resumed.append(
            {
                **entry,
                "session_id": session["session_id"],
                "session_source": session["source"],
                "hold": signature,
                "advice": advice,
                **({"observed": observation["observed"]} if observation else {}),
                **launched,
            }
        )

    # The review pass, on the same cadence and from the same site as the resume
    # pass. A run that reaches scoring has already had its review dispatch
    # composed; leaving it composed is what left runs waiting on a coordinator
    # to notice and retype the command. Every caller of ``sweep`` therefore
    # gives the reflex its caller at once — the follower's cadence, the
    # resume-ready surface and the MCP sweep action all reach ``sweep``.
    #
    # A dry run reports what recovery would do and must not dispatch reviews
    # either: a preview that dispatched would be a preview that acted.
    reviews: dict[str, Any] = {}
    if not dry_run:
        try:
            reviews = dispatch_awaiting_reviews(
                project=project, config=config, launcher=launch
            )
        except (CrewError, OSError) as exc:
            # A review pass that cannot run must not take the resume pass's
            # record down with it: the recoveries it did perform are already
            # durable on each resumed run.
            reviews = {"error": str(exc)}

    report = {
        "project": project,
        "dry_run": bool(dry_run),
        "checked": considered,
        "resumed": resumed,
        "skipped": skipped,
        "reviews": reviews,
        "swept_at": _utc_now(),
    }
    # Two records, because they answer different questions. The append-only log
    # holds what happened, and only sweeps that judged something write to it —
    # an operator reading it wants recoveries and the refusals to recover, not a
    # heartbeat from every tick of every follower.
    if considered or _review_outcome_counts(reviews)["dispatched"]:
        path = recovery_log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, sort_keys=True) + "\n")
    # The status file holds THAT it happened, and is written every sweep. A
    # sweep that runs and finds nothing and a sweep that never ran are
    # indistinguishable otherwise, which is exactly how a fleet of followers
    # running pre-merge code looked healthy while none of them could recover
    # anything: the capability was absent and the pane said the same thing it
    # says when the fleet is simply quiet. Written per writer now, so a reader
    # can ask about one writer rather than only about the project.
    _write_sweep_status(project, report, writer)
    return report
