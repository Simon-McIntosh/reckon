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

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import _backends
from reckon import budget as budget_module
from reckon._backends import _human_age
from reckon._store import write_atomically, write_json_atomically
from reckon._timestamps import parse_utc
from reckon.crew import plan_review, runs
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
    LAUNCH_WINDOW_SECONDS,  # noqa: F401 -- re-exported: resume doors read it here
    WORKER_RECORD_NAME,
    _newest_stream_shows_work,
    _opened_repair_rounds,
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
from reckon.crew.recovery_classification import WAIT_PRODUCER_GONE_SECONDS
from reckon.crew.recovery_liveness import (
    _newest_stream_in_motion,
    _record_is_known_foreign,
)
from reckon.crew.runs import (
    AS_DELIVERED_SUFFIX,
    PointerLockHeldError,
    _manifest_for_repair,
    _manifest_mtime_ns,
    _pointer_lock,
    _status_line_replaced,
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
from reckon.node_exclusive_lock import exclusive_lock

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


def _read_refused_advice(run_id: str, *, consume: bool) -> tuple[str, str]:
    """The advice a refusal kept for this run, and where it waits.

    One reader serves both doors: the resume that delivers the text empties the
    slot as it reads, and the reopen that only reports it leaves the slot for
    that resume. Keeping the read in one place is what stops the two from
    drifting over what an absent or empty slot yields.
    """
    path = refused_advice_path(run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "", ""
    if consume:
        with suppress(OSError):
            path.unlink()
    return text.strip(), str(path)


def consume_refused_advice(run_id: str) -> str:
    """Read and clear the advice a refusal left for this run, or ''.

    The next resume is the one that delivers it, so the slot is emptied as it
    is read: advice already carried into a resumed turn must not be replayed
    into the one after it as though it were new.
    """
    return _read_refused_advice(run_id, consume=True)[0]


def waiting_refused_advice(run_id: str) -> tuple[str, str]:
    """The advice a refused resume left for this run, and where it waits.

    Read without consuming: a reopen only reports the slot, and the resume
    that delivers the text is a separate call which empties it as it reads.
    """
    return _read_refused_advice(run_id, consume=False)


#: Where a reopen records what it granted, beside the run's other records.
REOPEN_RECORD_NAME = "reopened.json"


def _record_reopening(
    run_id: str,
    *,
    granted_paths: Sequence[str],
    reason: str,
    delivered: Path | None,
) -> dict[str, Any]:
    """Record that a run was reopened, and what the reopen granted.

    A reopen is not a verdict, so it is not written as one: the run is still
    working towards a status of its own, and a status word here would be an
    outcome no attempt reached — exactly what left a resumed worker classified
    from its predecessor's delivery. What a reopen does owe a reader is the
    fact itself, its reason, the paths it granted and the delivered text it set
    aside, and those are recorded in the run's own directory where the attempt
    records live.
    """
    record = {
        "run_id": run_id,
        "reason": reason,
        "granted_paths": list(granted_paths),
        "reopened_at": _utc_now(),
    }
    if delivered is not None:
        record["delivered"] = str(delivered)
    _write_json(run_dir(run_id) / REOPEN_RECORD_NAME, record)
    return record


def reopen_run(
    run_id: str,
    write_paths: Sequence[str],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Prepare a halted or completed run, optionally widening its write scope.

    A reopen answers a caller who asked for a wider boundary, not one who asked
    for work: the advice a launch would carry was composed for a state the run
    has left, so launching it spends an attempt on instructions nobody wrote
    for it. The grant is made and reported, and the resume that follows is the
    caller's own separate call — which carries any advice a refused resume left
    waiting, as it always has.
    """
    from reckon.crew.claims import widen_claim

    record = read_pointer(run_id)
    if local_liveness(record)[0] is True:
        raise live_process_refusal(record, reading="alive")
    in_flight = live_attempt_refusal(run_id, record)
    if in_flight is not None:
        raise in_flight
    added: list[str] = []
    widened = record
    if write_paths:
        # Use the pointer returned by the grant so the attempt and fence
        # cannot drift through a second read after widening.
        widened, added = widen_claim(run_id, list(write_paths))
    delivered = _reset_delivered_manifest_status(run_id, widened)
    reopening = _record_reopening(
        run_id,
        granted_paths=list(write_paths),
        reason=reason or "reopening the stopped run",
        delivered=delivered,
    )
    advice, advice_path = waiting_refused_advice(run_id)
    return {
        "run_id": run_id,
        "reopened": True,
        "launched": False,
        "granted_paths": list(write_paths),
        "added_paths": list(added),
        "write_paths": list((widened.get("node") or {}).get("write_paths") or []),
        "reason": reopening["reason"],
        "reopened_at": reopening["reopened_at"],
        "refused_advice": advice,
        "refused_advice_path": advice_path,
    }


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


class PromotionInProgressError(CrewError):
    """A resume met a run that a promotion holds, and refuses rather than waits."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"cannot resume {run_id!r}: it is being promoted, so its pointer "
            "and worktree are the promotion's to rewrite; the resume is "
            "refused rather than waiting, and the run can be resumed once "
            "the promotion finishes"
        )
        self.run_id = run_id


# The runs one context already holds against promotion. A resume plan writes
# the pointer and then marks the launch, and both must happen under one hold;
# without this the inner take would compete with the outer one and refuse the
# process against itself.
_PROMOTION_HELD: ContextVar[frozenset[str]] = ContextVar(
    "promotion_held", default=frozenset()
)


def promotion_lock_name(run_id: str) -> str:
    """The lock a promotion holds for the whole of a run's promotion.

    The name is promotion's own, imported rather than spelled again here, so a
    rename there cannot leave this guard watching a lock nobody takes. What
    keeps the two spellings from diverging silently is the case that drives a
    real promotion lock held over both its writes. The lock named must stay the
    one promotion takes before the run's pointer lock: a resume takes it first
    here, in the same order, and the two can never hold what the other waits
    for.
    """
    from reckon.crew.promotion import _promotion_lease_name

    return _promotion_lease_name(run_id)


@contextmanager
def hold_against_promotion(run_id: str):
    """Hold the run against a promotion, or refuse while one holds it.

    Re-entrant within one context, so a caller that already holds the run (a
    resume plan that writes the pointer and then marks the launch) passes
    through the inner take rather than competing with itself.
    """
    held = _PROMOTION_HELD.get()
    if run_id in held:
        yield
        return
    # The local lock below sees only a promotion on this host. A promotion on
    # another host holds the same run through its cross-host lease, so the lease
    # is read too: a repair round that rides a resume must not start for a run
    # whose promotion has begun anywhere, because the promotion is about to
    # settle the record the resumed worker would commit under.
    from reckon.crew.promotion import promotion_in_flight

    if promotion_in_flight(run_id):
        raise PromotionInProgressError(run_id)
    try:
        with _pointer_lock(promotion_lock_name(run_id), blocking=False):
            token = _PROMOTION_HELD.set(held | {run_id})
            try:
                yield
            finally:
                _PROMOTION_HELD.reset(token)
    except PointerLockHeldError as held_error:
        if held_error.run_id != promotion_lock_name(run_id):
            raise
        raise PromotionInProgressError(run_id) from None


def mark_resume_launching(run_id: str) -> None:
    """Record that this process has begun a resume of the run.

    A run with no live pointer has nothing to carry the mark and nothing that
    could read it back, so there is nothing to write and no refusal to warn of.

    The run is held against promotion first, without waiting. A promotion holds
    that lock while it rewrites and removes the pointer, so a resume that meets
    it refuses instead of writing a pointer under the promotion; a resume that
    takes it first is finished before any promotion can start, because the
    promotion acquires the same lock. Taking it before the pointer lock — the
    order promotion uses — is what keeps the pair from deadlocking.
    """
    with hold_against_promotion(run_id), _pointer_lock(run_id):
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


def _drop_launch_mark_locked(run_id: str) -> None:
    try:
        current = read_pointer(run_id)
    except CrewError:
        return
    if current.pop(RESUME_LAUNCH_IN_FLIGHT, None) is None:
        return
    _write_json(pointer_path(run_id), current)


def clear_resume_launching(run_id: str) -> None:
    """Drop the in-flight marker once the attempt's pid has been recorded."""
    with _pointer_lock(run_id):
        _drop_launch_mark_locked(run_id)


def clear_resume_launching_without_waiting(run_id: str) -> None:
    """Drop the mark only if its pointer is free, so a refusal never waits.

    A resume refused because a promotion holds the run must not queue behind
    that promotion to undo a mark: the pointer is the promotion's to rewrite
    and remove, so a mark left on it is not this refusal's to undo.
    """
    try:
        with _pointer_lock(run_id, blocking=False):
            _drop_launch_mark_locked(run_id)
    except PointerLockHeldError:
        return


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
    from reckon.crew.dispatch_launch import resolve_launch_executable

    try:
        # The probe runs the backend's own command, so it runs the one the
        # backend's resolution found: a composed argv carries the bare name, and
        # a launcher whose PATH omits a declared directory would die here and
        # read as a lane that is down.
        plan = resolve_launch_executable(
            _backends.launch_plan(
                backend_name=backend_name,
                backend=backend,
                prompt=AVAILABILITY_PROBE_PROMPT,
                worktree=worktree,
                final_message_path=final_path,
                fence=False,
            )
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
    lock_path = lane_probe_cache_path(project, backend_name).with_suffix(".lock")
    with exclusive_lock(lock_path):
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
        except (_backends.BackendError, CrewError, OSError, ValueError):
            # A backend whose command has no launch translation carries no
            # dialect to read its stream with, which is the same absence an
            # unreadable stream expresses: this source has no session id to
            # offer, so the resolver falls through to the ledger rather than
            # letting one untranslatable command end the caller's pass.
            continue
        found = str(observation.session_id or "").strip()
        if found:
            return found
    return ""


def _ledger_session(
    run_id: str,
    *,
    project: str,
    root: Any,
    row: Mapping[str, Any] | Callable[[], Mapping[str, Any] | None] | None = None,
) -> str:
    """The session the promoted row recorded, or empty.

    A promoted run has no pointer and no live stream to consult, and its
    session may still be resumable — so the committed row is the third source
    rather than the end of the search.

    A caller that already holds the run's committed row hands it in, so the
    ledger is not loaded again to find the row it just read. A caller holding
    only a run id reads that run's own record through its indexed identity.

    A caller that can find the row but would rather not unless it is needed
    hands in a nullary callable returning it or ``None``; it is called only
    here, so a run whose session the pointer or the stream already answered
    never triggers the lookup. A lookup that finds nothing answers the same
    absence the ledger load did, without paying it.
    """
    if callable(row):
        row = row()
    if row is not None:
        return str(row.get("session_id") or "").strip()
    if not project:
        return ""
    from reckon.crew.open_run_index import row_for_run

    try:
        candidate = row_for_run(project, run_id, root)
    except Exception:  # noqa: BLE001 - an unreadable ledger answers nothing
        return ""
    return str(candidate.get("session_id") or "").strip() if candidate else ""


def resolve_session(
    run_id: str,
    *,
    record: Mapping[str, Any] | None = None,
    ledger_row: Mapping[str, Any]
    | Callable[[], Mapping[str, Any] | None]
    | None = None,
    project: str = "",
    root: Any = None,
) -> dict[str, Any]:
    """Answer the session for one run, and say which source answered.

    Every surface that reports a session asks this, so two surfaces asked about
    the same run cannot disagree. The sources are consulted in order — live
    pointer, then stream, then ledger — and the answer carries the one that
    supplied it.

    ``ledger_row`` is the committed row the caller already holds for this run,
    which the ledger source answers from directly instead of loading the ledger
    to find it again. A caller holding no row states only the run id and the
    ledger source reads that run's own indexed identity. A caller holding a nullary callable has the
    ledger source call it, so a run whose session the pointer or the stream
    answered never pays a ledger read it did not need.

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
        run_id,
        project=project,
        root=root if root is not None else pointer.get("repo"),
        row=ledger_row,
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
    from reckon.crew import budget_group as budget_group_module

    # The launcher judges against the backend's group block with any lift in
    # force, so the prediction must too, or a lift the launcher honours would be
    # reported as a hold here.
    group = budget_group_module.declared_group_for(config, backend_name)
    judged = config
    if group is not None:
        block = budget_group_module.effective_block(
            config, str(group), readings=None, now=None, session=None
        )
        judged = {**(config or {}), "budget": block}
    return budget_module.decide(state, budget_module.policy(judged), purpose="resume")


# The run directory's immutable record of each attempt's worker, named for the
# attempt it describes. The pointer names only the attempt that launched last,
# while the process a refusal finds may belong to any of them, so naming the
# attempt is a read of these files rather than of the pointer alone.
_ATTEMPT_WORKER_PATTERN = re.compile(r"^attempt-(\d+)-worker\.json$")

# The lane-change stream a lane change writes, named for its attempt; a run's
# first attempt writes ``stream.jsonl`` and later same-lane turns write
# ``resume-<turn>.jsonl``, which the pointer dates through its own log path.
_LANE_CHANGE_STREAM_PATTERN = re.compile(r"^lane-change-(\d+)\.jsonl$")

# How each attempt kind reads in a sentence. A kind the vocabulary does not
# name is left out rather than echoed as a bare token.
_ATTEMPT_KIND_PHRASES = {
    "dispatch": "a dispatch",
    "resume": "a resume",
    "lane-change": "a lane change",
}


def _attempt_worker_records(
    record: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]]:
    """Every attempt's worker record the run directory holds, by attempt number.

    Both shapes are read: the immutable ``attempt-<n>-worker.json`` receipt and
    the canonical ``worker.json`` the classifier reads for the attempt running
    now, which carries its own attempt number. A receipt wins for a number both
    hold — they describe the same process, and the receipt is the one written
    when that attempt started. An unreadable file is no record: the refusal
    leaves the detail out rather than guessing it.
    """
    directory = run_dir(str(record.get("run_id") or ""))
    found: dict[int, Mapping[str, Any]] = {}
    try:
        entries = list(directory.glob("attempt-*-worker.json"))
    except OSError:
        entries = []
    for path in entries:
        number = _attempt_worker_number(path.name)
        payload = _read_record(path)
        if number is None or payload is None:
            continue
        found[number] = payload
    canonical = _read_record(directory / WORKER_RECORD_NAME)
    if canonical is not None:
        number = canonical.get("attempt")
        if isinstance(number, int) and number not in found:
            found[number] = canonical
    return found


def _attempt_worker_number(name: str) -> int | None:
    """The attempt an immutable worker receipt's file name gives it, or None."""
    match = _ATTEMPT_WORKER_PATTERN.match(name)
    return int(match.group(1)) if match is not None else None


def _read_record(path: Path) -> Mapping[str, Any] | None:
    """A JSON record on disk, or None when it is absent or unreadable."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _recorded_attempt_number(record: Mapping[str, Any]) -> int | None:
    """The attempt number the run's own records give it, or None."""
    number = record.get("attempt")
    if isinstance(number, int):
        return number
    artifact = _read_record(run_dir(str(record.get("run_id") or "")) / "attempt.json")
    if artifact is None:
        return None
    recorded = artifact.get("attempt")
    return recorded if isinstance(recorded, int) else None


def _live_attempt_number(record: Mapping[str, Any]) -> int | None:
    """The attempt whose recorded worker this host still finds running.

    A pid is meaningful only on the machine that issued it, so a run launched
    elsewhere names no attempt here: the refusal falls back to naming nothing
    rather than pointing a coordinator at a process this host cannot see. The
    newest living attempt answers, which is the one a second worker would
    collide with when more than one receipt still reads alive.
    """
    if _record_is_known_foreign(record):
        return None
    living = [
        number
        for number, payload in _attempt_worker_records(record).items()
        if record_process_alive(payload, process_alive) is True
    ]
    return max(living) if living else None


def _stream_attempt_number(record: Mapping[str, Any]) -> int | None:
    """The attempt whose stream the run is writing now, when a name says so.

    A lane-change stream is named for its attempt and a first attempt's is the
    run's original stream. A resume stream is named for the turn it reached
    rather than for the attempt, so it answers only where the pointer's own log
    path is that file — and a stream whose file no record ties to an attempt
    leaves the attempt unnamed rather than guessed.
    """
    directory = run_dir(str(record.get("run_id") or ""))
    log_path = str(record.get("log_path") or "")
    paths = stream_paths_newest_first(directory, include=(log_path,))
    if not paths:
        return None
    name = paths[0].name
    lane_change = _LANE_CHANGE_STREAM_PATTERN.match(name)
    if lane_change is not None:
        return int(lane_change.group(1))
    if name == "stream.jsonl":
        return 1
    if log_path and Path(log_path).name == name:
        return _recorded_attempt_number(record)
    return None


def _attempt_kind(record: Mapping[str, Any], attempt: int | None) -> str:
    """One attempt's kind, from the record that names that attempt, or empty.

    The run directory's attempt record is written when the attempt begins and
    the pointer carries the kind its launch recorded; either answers only for
    the attempt it names, so a kind is never carried over from a neighbour.
    """
    if attempt is None:
        return ""
    artifact = _read_record(run_dir(str(record.get("run_id") or "")) / "attempt.json")
    if artifact is not None and artifact.get("attempt") == attempt:
        kind = str(artifact.get("attempt_kind") or "").strip()
        if kind:
            return kind
    if _recorded_attempt_number(record) == attempt:
        return str(record.get("attempt_kind") or "").strip()
    return ""


def _attempt_stream_age(record: Mapping[str, Any], attempt: int | None) -> int | None:
    """How long ago one attempt's stream was last written, or None when unread.

    Only streams that belong to the named attempt are candidates: the pointer's
    log path where the pointer names that attempt, the run's original stream for
    a first attempt, and a lane-change stream for the attempt it is named for. A
    run's newest stream is deliberately not a fallback — it may be another
    attempt's, and the message would then date the attempt it names by a
    neighbour's record. An attempt whose own stream cannot be identified answers
    nothing, and the message leaves the age out rather than guessing it.
    """
    if attempt is None:
        return None
    directory = run_dir(str(record.get("run_id") or ""))
    log_path = str(record.get("log_path") or "")
    candidates: list[Path] = []
    if log_path and _recorded_attempt_number(record) == attempt:
        candidates.append(Path(log_path))
    if attempt == 1:
        candidates.append(directory / "stream.jsonl")
    candidates.append(directory / f"lane-change-{attempt}.jsonl")
    moment = time.time()
    for path in candidates:
        try:
            written = path.stat().st_mtime
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        return max(0, int(moment - written))
    return None


def _repair_round_opened(record: Mapping[str, Any]) -> bool:
    """Whether the review reflex opened a repair round for this run.

    The record is the repair dispatcher's own lookup, which reads the live
    pointer afresh rather than the mapping its caller was handed: a round opened
    after a sweep read its pointer must still be visible here. A pointer that is
    gone answers empty, which reads as no round opened.
    """
    opened = _opened_repair_rounds(record)
    return bool(str(opened.get("round_id") or ""))


def live_repair_round(record: Mapping[str, Any]) -> bool:
    """Whether the attempt running now is a repair round the reflex opened.

    A repair round continues the reviewed run rather than composing a second
    node, so a path granted while it runs belongs on the same claim the round's
    next attempt is fenced from. The round is live only while the attempt the
    run's records hold running is the resume the reflex launched: a run whose
    opened round has ended and whose worker is running some other attempt keeps
    its claim fixed, exactly as any other live worker does.
    """
    if not _repair_round_opened(record):
        return False
    attempt = _live_attempt_number(record)
    if attempt is None:
        attempt = _recorded_attempt_number(record)
    return _attempt_kind(record, attempt) == "resume"


def _attempt_detail(
    record: Mapping[str, Any],
    attempt: int | None,
    *,
    in_motion: bool,
    stream_age: int | None,
) -> str:
    """What the run's records hold about the attempt a refusal found, as a clause.

    Every part is optional and a part the records do not carry is left out. The
    clause is empty when no attempt could be named, so a refusal whose live
    process the records do not attribute keeps the sentence it has always given.
    """
    facts: list[str] = []
    worker = (
        _attempt_worker_records(record).get(attempt) if attempt is not None else None
    )
    if isinstance(worker, Mapping):
        pid = worker.get("pid")
        if isinstance(pid, int):
            facts.append(f"pid {pid}")
        launched = str(worker.get("launched_at") or "").strip()
        if launched:
            facts.append(f"launched {launched}")
    kind = _attempt_kind(record, attempt)
    parts: list[str] = []
    if attempt is not None:
        head = f"attempt {attempt}"
        if facts:
            head += " (" + ", ".join(facts) + ")"
        if kind in _ATTEMPT_KIND_PHRASES:
            head += ", " + _ATTEMPT_KIND_PHRASES[kind]
        parts.append(
            f"{head[:1].upper()}{head[1:]}, wrote that stream"
            if in_motion
            else f"That process is {head}"
        )
    if kind == "resume" and _repair_round_opened(record):
        parts.append(
            "the review reflex opened it as a repair round, which delivers by "
            "itself: advice sent after it does will reach it"
        )
    if stream_age is not None:
        parts.append(f"its newest stream record is {_human_age(stream_age)} old")
    if not parts:
        return ""
    return ". " + "; ".join(parts) + "."


def live_process_refusal(record: Mapping[str, Any], *, reading: str) -> CrewError:
    """The one message that refuses a second worker over a run that still has one.

    A refusal that names no attempt reads as a stale process, and a coordinator
    who stops what the refusal did not name ends a repair round that would have
    delivered by itself. So every door that refuses a second worker composes its
    message here, naming the attempt the run's own records hold: its number, pid
    and launch time from the attempt's worker receipt, its kind from the attempt
    record, whether a resume is a repair round the review reflex opened, and the
    age of the attempt's newest stream record. A detail the records do not hold
    is left out rather than guessed, and an attempt no record attributes keeps
    the sentence alone.

    The caller supplies the liveness reading it took, because the two readings
    say different things and must not be confused. ``alive`` is a pid this host
    checked and found running: the message is the live-process refusal, and
    every door that read that pid gives the same words. Anything else is the
    unknown a worker on another machine leaves: no pid could be read here and no
    exit was recorded, so the message says so — it never calls that attempt
    live, and never claims its process was found gone.
    """
    run_id = str(record.get("run_id") or "")
    in_motion = reading != "alive"
    if in_motion:
        attempt = _stream_attempt_number(record)
        opening = (
            f"run {run_id!r} is refused: liveness unknown — no pid of its worker "
            "could be read on this host and no exit was recorded, and its newest "
            "stream is still being written"
        )
        age = _attempt_stream_age(record, attempt)
        detail = _attempt_detail(record, attempt, in_motion=True, stream_age=age)
        if detail:
            return CrewError(
                opening + detail + " Its liveness could not be read on this host."
            )
        return CrewError(opening + ". Its liveness could not be read on this host.")
    attempt = _live_attempt_number(record)
    opening = (
        f"run {run_id!r} still has a live process; observe or stop it before resuming"
    )
    detail = _attempt_detail(
        record,
        attempt,
        in_motion=False,
        stream_age=_attempt_stream_age(record, attempt),
    )
    return CrewError(opening + detail)


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
    if record_process_alive(record, process_alive) is True and not _parked_on_a_wait(
        record
    ):
        # This guard's message is the launcher's own, held word for word by the
        # prediction-parity test beside it, so it is not restated here: both
        # doors compose it through the one refusal below.
        return live_process_refusal(record, reading="alive")
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
    refusal = resume_window_refusal(
        run_id, record, backend=_backend_settings(record, config), config=config
    )
    if refusal is not None:
        return refusal
    # The launcher's last guard reads a run whose end nothing observed from the
    # run's own stream, and the prediction consults the same helper rather than
    # restating the reading: a preview that skipped it would offer a lift the
    # real resume refuses, which is the whole claim `would_resume` makes.
    return _stream_reading_refusal(record, reading=_observed_process_reading(record))


def _parked_on_a_wait(record: Mapping[str, Any]) -> bool:
    """Whether the run's worker has parked on a declared wait.

    A parked worker has ended its turn: the manifest declares a wait the
    fleet's reader honours and no worker this host can observe is running it.
    Every door that refuses a second worker over a live process reads this, so
    a parked run is admitted while an idle supervisor sits beside it, and a
    worker that is still running is refused exactly as before.
    """
    from reckon.crew.recovery_liveness import parked_on_declared_wait

    return parked_on_declared_wait(record) is not None


def _newest_stream_ended(record: Mapping[str, Any]) -> bool:
    """Whether a run's newest stream shows a worker that wrote and then stopped.

    The complement the stream reading needs: a newest stream carrying an
    assistant turn is work that happened, and one quiet past the stall window
    is work that stopped. Both together are the reachable substitute for a
    process-table answer the reading host cannot take, so a worker on another
    machine that has gone quiet is read as ended rather than as unknown
    forever. A stream that carries no assistant turn — or none at all — is
    neither, so the missing verdict stands.

    The stream is read on its own clock rather than the attempt-bounded one:
    this door asks whether the *previous* worker stopped, and dating that
    silence from the attempt now starting would shorten it to nothing and let
    the resume through on no evidence at all.
    """
    return _newest_stream_shows_work(record) and not _newest_stream_in_motion(
        record, from_stream_alone=True
    )


def _observed_process_reading(record: Mapping[str, Any]) -> str:
    """The three-way process reading this host can stand behind for one run.

    A run's liveness and the record its supervisor left of the end are folded
    together here so every door that reads the pair composes one verdict. The
    supervisor's record is an observation of the end, and it is the one a
    prompt reader can never have: it outlives a pointer nobody updated and it
    stays readable on a machine that never launched the worker. Folding it in
    before the reading is what the classifier does for the same evidence, so a
    recorded exit reads as an ended process on both surfaces rather than as
    the unknown a bare reading of it composes. Where the process table says
    the worker is still there the record is an earlier attempt's and does not
    apply, which is why the fold is conditional on the living answer.
    """
    alive, liveness_proven = local_liveness(record)
    exit_record = _run_exit_record(record)
    ended_exit = exit_record if exit_record is not None and alive is not True else None
    return _process_reading(
        False if ended_exit is not None else alive,
        liveness_proven=liveness_proven,
        exit_record=ended_exit,
    )


def _stream_reading_refusal(
    record: Mapping[str, Any], *, reading: str
) -> CrewError | None:
    """The refusal a run owes while its newest stream shows a worker writing.

    A worker on another machine answers no pid this host can look up, so its
    liveness is unknown here rather than proven, and the run's own stream
    decides instead — exactly as the stalled-row reader decides it. An
    assistant turn written inside the stall window is a worker producing
    output right now, and no second worker may be started over it: the lift
    would collide with the one the stream shows writing. The message comes
    from the shared refusal composer, which names the attempt the stream shows
    writing and the age of its newest record, and says that attempt's liveness
    could not be read on this host — the reading this door holds is an unknown,
    not a proven life, so it never calls the attempt live.

    Only a run whose liveness this host cannot settle is decided this way. A
    proven death has already ended the work and a proven life is refused on
    its own terms, so the caller supplies the reading it took and this answers
    the unknown alone. Both the launcher and the dry-run prediction of it
    consult this one helper, so a preview cannot offer a lift the launcher
    then refuses.

    The stream's own clock is the one read here, for the reason
    :func:`_newest_stream_ended` states: the launch asking this question has
    already reserved its attempt, and that reservation must not silence the
    stream it is asking about.
    """
    if reading != "liveness unknown" or not _newest_stream_in_motion(
        record, from_stream_alone=True
    ):
        return None
    return live_process_refusal(record, reading=reading)


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

    So the lift requires an observation, and there are three: a pid checked on
    this host and found dead, the supervisor's exit record, or — for a worker
    that answers no pid this host can look up — a newest stream that carries an
    assistant turn and has gone quiet past the stall window. The third is the
    reachable substitute for a process-table answer the reading host cannot
    take, and it resolves the unknown both ways: a stream still in motion
    refuses, as a live pid does, and one that has gone quiet licenses the lift.
    The verdict is composed through the same three-way vocabulary the row
    states (``local_liveness`` under ``_process_reading``), so the sweep cannot
    act on a reading the reader was never shown, and the reason names the
    reading it is holding.
    """
    if _parked_on_a_wait(record):
        # A parked worker has ended its turn and declared what ends its park,
        # so the end of the *work that matters* is observed on the declaration
        # rather than on the process table. The live pid is the idle supervisor
        # beside it, and refusing on that pid is what kept a parked run
        # unwoken for as long as its supervisor lived.
        return None
    reading = _observed_process_reading(record)
    if reading == "process gone":
        return None
    if reading == "liveness unknown":
        stream_refusal = _stream_reading_refusal(record, reading=reading)
        if stream_refusal is not None:
            return stream_refusal
        # A stream that carries an assistant turn and has gone quiet past the
        # window is the opposite reading — the worker wrote and stopped — and
        # licenses the resume the way an observed end does. A run with no such
        # record at all stays unknown, and the missing end still refuses.
        if _newest_stream_ended(record):
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


def _spawn(
    plan: Any,
    *,
    log_path: Path,
    stderr_path: Path,
    prompt_path: Path,
    attempt: int | None = None,
    reservation: Any = None,
) -> int:
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
        attempt=attempt,
        reservation=reservation,
    )


def _tree_head(worktree: str) -> str:
    """The revision a tree's working checkout is at, or empty when unreadable.

    Read through the repository's own commit resolver rather than a second
    ``git rev-parse`` composed here: the resolver refuses a value that names no
    commit, bounds the query so a tree on a stalled filesystem cannot hold the
    caller open, and is what every other reader of a revision in this codebase
    already goes through.
    """
    if not worktree:
        return ""
    tree = Path(worktree).expanduser()
    if not tree.is_dir():
        return ""
    # Deferred: the resolver's module pulls in the review subject readers, which
    # import this package's siblings, so the import is made where it is used.
    from reckon.crew.recovery_review_subject import _resolve_commit

    return _resolve_commit(tree, "HEAD")


def _revision_suite_block(record: Mapping[str, Any]) -> str:
    """The two revisions a resumed run's suite arms must record, and each command.

    A resumed worker is handed the template its dispatch stated, but the tree it
    stands in has moved since: the run's head now holds the work the first
    attempt delivered, so the command resolved at the head is not the one the
    dispatch resolved at the base. A worker that re-resolves once and records
    both arms with that one command leaves an arm at a revision promotion never
    asked for, and the landing is refused for it — measured on 10 November, when
    two implement runs recorded their arms at mid-run revisions and cost a
    resume round each.

    So the advice names both revisions and resolves the declaration separately
    over each revision's own tree. An unarmed run owes no pair and is told
    nothing, because a run that measures no suite must not be handed a command
    it was never armed with.
    """
    declared = " ".join(str(record.get("suite_command") or "").split())
    if not declared:
        return ""
    # Deferred: promotion_records reads the run record through this module's
    # siblings, so the import is made where it is used rather than at load.
    from reckon.crew.promotion_records import _scoped_suite_command

    base = str(record.get("base_sha") or record.get("base") or "").strip()
    head = _tree_head(str(record.get("worktree") or ""))
    base_command = _scoped_suite_command(record, revision=base) if base else ""
    head_command = _scoped_suite_command(record, revision=head) if head else ""
    if not base_command:
        base_command = str(record.get("suite_command_resolved") or declared)
    if not head_command:
        head_command = str(record.get("suite_command_resolved") or declared)
    base_line = (
        f"  Base revision: {base}. Record the baseline arm at this revision,\n"
        "  with the command resolved over that revision's own tree:\n"
        f"    {base_command}\n"
        if base
        else "  Base revision: not recorded on this run.\n"
    )
    head_line = (
        f"  Run head now: {head}. Record the after arm at this revision once\n"
        "  your work is complete, with the command resolved over this head:\n"
        f"    {head_command}\n"
        if head
        else "  Run head now: unreadable, so resolve the command from the tree\n"
        "  you are standing in before recording the after arm.\n"
    )
    return (
        "\nFENCE — SUITE REVISIONS (the two revisions your arms must record)\n"
        + base_line
        + head_line
        + "  The two commands differ when a test file in your write scope exists\n"
        "  at one revision and not the other. Record each arm with the command\n"
        "  resolved at its own revision: promotion compares each arm's command\n"
        "  with the template resolved over that arm's own tree.\n"
    )


def _with_revision_suite_block(advice: str, record: Mapping[str, Any]) -> str:
    """The advice a resumed worker reads, with the suite revisions appended.

    Appended rather than prepended so the advice's own opening — which carries
    the repair round's token — stays the first line a worker sees, and a
    manifest quoting it still names the round it answered.
    """
    block = _revision_suite_block(record)
    if not block:
        return advice
    return advice.rstrip("\n") + "\n" + block


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


def _record_resume_supersedes_head(run_id: str) -> str:
    """Record the head a resume leaves behind, with the instant it was resumed.

    A resumed run keeps the head it had delivered until its worker delivers the
    head that settles the resume, and the review, the promotion and the
    coordinator's duties that read that revision all speak for work the run has
    moved past. The head and the instant travel on the pointer under one slot,
    so a reader that owes a duty against a head can see which revision a resume
    replaced and a queued review can be told from one the resume superseded.

    A run whose tree yields no head records nothing: a slot keyed on an empty
    head would withdraw every duty that reads against a head rather than only
    the superseded one. Returns the head recorded, or empty.
    """
    from reckon.crew.recovery import _run_head_for_review
    from reckon.crew.recovery_review_dispatch import RESUME_SUPERSEDED_HEAD

    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return ""
        try:
            head = _run_head_for_review(current)
        except Exception:  # noqa: BLE001 - an unreadable tree records nothing
            return ""
        if not head:
            return ""
        current[RESUME_SUPERSEDED_HEAD] = {"head": head, "at": _utc_now()}
        _write_json(pointer_path(run_id), current)
    return head


def _withdraw_review_superseded_by_resume(run_id: str) -> None:
    """Withdraw the queued review this resume supersedes, as the sweep would.

    The reflex's sweep withdraws a review its source run's resume has left
    behind, but a sweep runs on a cadence: between the resume and the next pass
    a queued review is launchable by any admission that reaches it, and it would
    read the revision the resume just replaced. Running the reflex's own reader
    here, at the resume that supersedes it, closes that window without a second
    rule — only the moment the withdrawal is noticed differs.
    """
    from reckon.crew.recovery_review_dispatch import withdraw_superseded_review

    try:
        record = read_pointer(run_id)
    except CrewError:
        return
    with suppress(Exception):
        withdraw_superseded_review(record)


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
    try:
        manifest = _manifest_for_repair(run_id)
    except CrewError:
        # The repair resolves the manifest the same way and refuses when there
        # is none at either path; a run with nothing delivered has nothing to
        # clear, so the refusal is not this caller's error.
        return None
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    rewritten, previous = _status_line_replaced(text, "in-progress")
    if rewritten is None:
        return None
    if previous.strip().lower() not in _WORKER_DONE_MANIFEST_STATUSES:
        return None
    attempt = int((record or {}).get("attempt") or 1)
    kept = manifest.with_name(f"{manifest.name}.attempt-{attempt}{AS_DELIVERED_SUFFIX}")
    if not kept.exists():
        kept.write_text(text, encoding="utf-8")
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
    # The attempt number is reserved before anything is built, and reserved
    # with an exclusive create rather than the pointer lock, so a door on
    # another host reading the same current attempt meets this claim where a
    # process-table guard cannot reach. The claim goes only when the launch
    # that took it failed before a worker existed: a worker that started is
    # held by its own claim, exactly as it is held by the in-flight mark.
    from reckon.crew.dispatch_launch import (
        release_launch_reservation,
        reserve_launch_attempt,
    )

    # The reservation is keyed on the newest attempt receipt in the run's own
    # directory plus one, the same key the dispatch door computes, so a resume
    # and a dispatch racing in the window between one door's attempt record and
    # its supervisor's worker record take one reservation between them.
    try:
        reservation = reserve_launch_attempt(run_dir(run_id), run_id, kind="resume")
    except CrewError:
        # The reservation was met by another door's launch, so this advice
        # belongs to the attempt that holds the run rather than to the one
        # refused. It is kept for the run's next resume, exactly as the
        # in-flight refusal in the plan builder keeps it.
        store_refused_advice(run_id, advice)
        raise
    # The registry entry is taken inside the guarded block and held across the
    # whole launch, including the attempt's own recording. Taken before the
    # guard it would leak on a mark that raised, leaving the run refused by a
    # launch that never started; released before record_resumption it would
    # leave the window between the launcher returning and the attempt being
    # written unguarded, so a second pass of this process could launch a worker
    # beside the one starting.
    try:
        try:
            _LAUNCHES_IN_THIS_PROCESS.add(run_id)
            mark_resume_launching(run_id)
            # The tree the resumed worker stands in has moved since its
            # dispatch, so the two revisions its suite arms record are stated
            # here, with the command resolved separately over each. Appended
            # before the plan is built so the prompt the worker reads carries
            # them whether the backend feeds its prompt on stdin or on argv.
            advice = _with_revision_suite_block(advice, record)
            plan = resume_plan(run_id, advice, config=config)
            directory = run_dir(run_id)
            turn = len(list(directory.glob("resume-*.jsonl"))) + 1
            advice_path = directory / f"resume-{turn}-advice.txt"
            advice_path.parent.mkdir(parents=True, exist_ok=True)
            _write_resume_prompt(advice_path, plan=plan, advice=advice)
            log_path = directory / f"resume-{turn}.jsonl"
            stderr_path = directory / f"resume-{turn}.stderr.log"
            attempt_started_at = _utc_now()
            manifest_baseline_mtime_ns = _manifest_mtime_ns(
                record.get("manifest_path") or ""
            )
            _require_fleet_gate_open()
            # The previous attempt may have left a done manifest on disk; clear
            # its verdict so the supervisor does not read the new worker's first
            # edit as that delivery.
            _reset_delivered_manifest_status(run_id, record)
            if launcher is None:
                pid = _spawn(
                    plan,
                    log_path=log_path,
                    stderr_path=stderr_path,
                    prompt_path=advice_path,
                    attempt=reservation.attempt,
                    reservation=reservation,
                )
            else:
                pid = launcher(
                    plan,
                    log_path=log_path,
                    stderr_path=stderr_path,
                    prompt_path=advice_path,
                )
                # A seam that starts nothing this process can outlive returns
                # from its launch, so the reservation must not be left standing
                # against this door. A launcher that reached the run's own
                # supervisor has recorded it, and that supervisor keeps the
                # hold.
                release_launch_reservation(reservation, only_when_unspawned=True)
        except PromotionInProgressError:
            # The refusal is the promotion's answer, and its pointer lock is
            # held by that promotion: undoing the mark here must not queue
            # behind it, which is the wait this refusal exists to avoid. The
            # claim goes, because no worker was started.
            clear_resume_launching_without_waiting(run_id)
            release_launch_reservation(reservation, only_when_unspawned=True)
            raise
        except BaseException:
            # A launch that ended before it held the session recorded no attempt
            # of its own, so nothing will drop the in-flight marker this door
            # set. Left there it would refuse the next resume against a launcher
            # already gone, so the marker goes with the launch that raised. This
            # arm covers the launch alone: once a worker has been started its
            # process holds the run, and a later failure recording the attempt
            # must not drop the guard that keeps another door from launching a
            # second worker beside it.
            clear_resume_launching(run_id)
            release_launch_reservation(reservation, only_when_unspawned=True)
            raise
        record_resumption(
            run_id,
            pid=pid,
            attempt=reservation.attempt,
            turn=turn,
            log_path=log_path,
            stderr_path=stderr_path,
            attempt_started_at=attempt_started_at,
            manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
        )
        _record_resume_supersedes_head(run_id)
        _withdraw_review_superseded_by_resume(run_id)
        # A resume launches a worker without sending a dispatch request, and a
        # host settles a child that reached the end of its session's work. So
        # without this the pane of a session whose last action was a resume
        # stops showing anything once that child settles: the worker runs, and
        # no follower is there to deliver its rows. The request is the one a
        # dispatch writes, and it is sent after the launch so the work it
        # publishes exists when the follower attaches. It is reported rather
        # than raised, because the worker is already running, and a host that
        # did not re-arm changes nothing a resume has to fail over.
        from reckon.crew.dispatch_watch import _rearm_session_follower

        follower_rearmed, follower_note = _rearm_session_follower(
            resume_project, str(record.get("session") or "")
        )
    finally:
        _LAUNCHES_IN_THIS_PROCESS.discard(run_id)
    return {
        "pid": pid,
        "turn": turn,
        "log_path": str(log_path),
        "plan": plan,
        "follower_rearmed": follower_rearmed,
        "follower_note": follower_note,
    }


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
    candidates.update(line.split(maxsplit=1)[0].casefold() for line in lines)
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    matched = any(
        candidate in terminal
        or (
            len(candidate) > 1
            and candidate.endswith("+")
            and any(value.startswith(candidate[:-1]) for value in terminal)
        )
        for candidate in candidates
    )
    return {
        "terminal": matched,
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
            "answered": True,
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
        # Whether the probe produced any answer at all, carried so the history
        # this observation is written into can tell a value that is standing
        # still from a probe that is reporting nothing.
        "answered": value.get("answered") is not False,
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


def _record_wait_probe_history(
    run_id: str,
    *,
    observed: str,
    answered: bool,
    now_seconds: float,
) -> None:
    """Append one probe observation to the run's wait history and prune it.

    The classifier reads this history to decide whether a wait's value has
    stopped moving, so the history has to reach back past the stillness bound
    however often the sweep runs and however long a station tick takes. Entries
    are therefore kept by age rather than by count: an entry is dropped only
    once it is older than the bound plus one sweep interval *and* a newer entry
    is already past the bound, so the recording never removes the last
    observation that predates the window it is measuring.
    """
    at = datetime.fromtimestamp(now_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        history = current.get("wait_probe_history")
        entries = [dict(item) for item in history or () if isinstance(item, Mapping)]
        entries.append({"at": at, "observed": observed, "answered": bool(answered)})
        kept: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            instant = parse_utc(entry.get("at"))
            older = instant is not None and (
                now_seconds - instant.timestamp()
                > WAIT_PRODUCER_GONE_SECONDS + DEFAULT_SWEEP_SECONDS
            )
            newer_past_bound = any(
                (later := parse_utc(other.get("at"))) is not None
                and now_seconds - later.timestamp() > WAIT_PRODUCER_GONE_SECONDS
                for other in entries[index + 1 :]
            )
            if older and newer_past_bound:
                continue
            kept.append(entry)
        current["wait_probe_history"] = kept
        _write_json(pointer_path(run_id), current)


# How many promotions one session's sweep runs beside itself. A promotion takes
# minutes on a shared checkout, and a sweep that ran several of them one after
# another held every newly delivered run's review for the sum of them.
DEFAULT_PROMOTION_WIDTH = 2

# How long a sweep waits for the promotions it handed to its workers. The wait
# bounds the sweep rather than the promotion: a promotion still running when it
# elapses keeps running in its own worker, and the claim below is what keeps a
# later sweep from promoting the same run beside it.
DEFAULT_PROMOTION_WAIT_SECONDS = 180.0

# The runs this process is promoting right now. The cross-host lease excludes a
# second host, but a lease taken by this host, pid and job reattaches rather
# than refusing, so two sweeps in one process need a claim of their own.
_PROMOTIONS_RUNNING: set[str] = set()
_PROMOTION_CLAIM_LOCK = threading.Lock()


def _promotion_settings(config: Mapping[str, Any] | None) -> tuple[int, float]:
    """The width and the sweep's wait for it, read from the resolved config.

    A caller that supplies no promotion block gets the defaults, which is what
    a station's own sweep does: the width is a property of how many promotions
    a session can usefully run beside itself, not something every layer must
    repeat.
    """
    block: Any = None
    if isinstance(config, Mapping):
        block = config.get("promotion")
        if not isinstance(block, Mapping):
            review_block = config.get("review")
            block = (
                review_block.get("promotion")
                if isinstance(review_block, Mapping)
                else None
            )

    def _read(key: str, default: Any) -> Any:
        return block.get(key, default) if isinstance(block, Mapping) else default

    try:
        width = int(_read("width", DEFAULT_PROMOTION_WIDTH))
    except (TypeError, ValueError):
        width = DEFAULT_PROMOTION_WIDTH
    try:
        wait_seconds = float(_read("wait_seconds", DEFAULT_PROMOTION_WAIT_SECONDS))
    except (TypeError, ValueError):
        wait_seconds = DEFAULT_PROMOTION_WAIT_SECONDS
    return max(1, width), max(0.0, wait_seconds)


class PromotionWorkers:
    """Run a sweep's promotions and acceptances in bounded workers of its own.

    A sweep hands the work off after the reviews it owed are dispatched, so one
    promotion running for minutes delays no review and no other promotion. The
    jobs run at most ``width`` at a time, each holding this process's claim on
    the run. Promotion takes its own cross-host lease when it writes, leaving
    coordinators free to complete a run while acceptance judges it.

    ``wait`` waits up to ``wait_seconds`` for the workers this sweep started and
    returns their outcomes in submission order, whether or not each finished.
    """

    def __init__(self, *, width: int, wait_seconds: float) -> None:
        self.width = max(1, int(width))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._slots = threading.BoundedSemaphore(self.width)
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._outcomes: dict[int, dict[str, Any]] = {}
        self._submitted = 0

    def submit(self, run_id: str, job: Callable[[], dict[str, Any] | None]) -> bool:
        """Start the job on a worker of its own, or report the run in flight.

        The claim is taken here rather than in the worker, so a second sweep
        reaching the same run reports it as being promoted instead of queueing
        behind it and promoting it again once the first finishes.
        """
        key = str(run_id)
        if not key:
            return False
        with _PROMOTION_CLAIM_LOCK:
            if key in _PROMOTIONS_RUNNING:
                return False
            _PROMOTIONS_RUNNING.add(key)
        with self._lock:
            index = self._submitted
            self._submitted += 1
            thread = threading.Thread(
                target=self._run,
                args=(index, key, job),
                name=f"promotion-{key} / {self._submitted}",
                daemon=True,
            )
            self._threads.append(thread)
        thread.start()
        return True

    def _run(
        self,
        index: int,
        run_id: str,
        job: Callable[[], dict[str, Any] | None],
    ) -> None:
        outcome: dict[str, Any]
        try:
            with self._slots:
                ran = job()
                outcome = (
                    dict(ran)
                    if isinstance(ran, Mapping)
                    else {"run_id": run_id, "result": ran}
                )
        except Exception as exc:  # noqa: BLE001 - one job's failure is its own
            outcome = {
                "run_id": run_id,
                "failed": {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"},
            }
        finally:
            with _PROMOTION_CLAIM_LOCK:
                _PROMOTIONS_RUNNING.discard(run_id)
        with self._lock:
            self._outcomes[index] = outcome

    def wait(self) -> list[dict[str, Any]]:
        """Wait up to this sweep's budget for the workers it started."""
        deadline = time.monotonic() + self.wait_seconds
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            outcomes, self._outcomes = self._outcomes, {}
        return [outcomes[index] for index in sorted(outcomes)]


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
        # Followers on different nodes merge into this record, so the
        # read-modify-write excludes across nodes rather than per kernel.
        with exclusive_lock(path.with_suffix(".lock")):
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
    session: str | None = None,
    pointers: Sequence[Mapping[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    launcher: Callable[..., int] | None = None,
    now: datetime | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
    writer: Any = None,
    promotions: Any = None,
) -> dict[str, Any]:
    """Resume runs whose provider hold or declared external wait has ended.

    A sweep also dispatches the review a run has already composed for itself
    once it reaches scoring, so the reflex fires from the same mechanism that
    tells a coordinator the run finished rather than from the coordinator
    remembering. That pass is idempotent for the same reason the resume pass
    is: a run whose review is stored or already in flight is left alone.

    ``session`` names the session this sweep is for, and both passes act only
    on runs whose pointer records that ownership. It is the one mechanism that
    scopes a sweep for any caller: a follower passes the session it serves so a
    sweep it runs never resumes another session's run or composes another
    session's review, and a caller holding no follower registration — the
    watcher that sweeps a run whose session armed no follower — passes it
    explicitly. Left unset, both passes behave as a whole-fleet sweep: the
    resume pass considers every lapsed run and the review pass resolves its
    session from the follower registration this process holds, empty for a
    process holding none, which the reflex reads as no filter.

    Idempotent by construction: a resumed run carries the trigger it was
    resumed for, so a second pass over the same fleet reports nothing to do.
    That is what makes it safe for something already running to call on a
    cadence.

    ``promotions`` supplies an explicit hand-off sink for internal callers.
    Without one, requests are persisted for the fleet service. A caller
    supplying ``PromotionWorkers`` receives the bounded synchronous worker
    report instead.

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

    live = list_live(project=project) if pointers is None else pointers
    for pointer in runs._tick_pointers(live):
        # A session-scoped sweep resumes only the runs that session owns: a
        # follower resuming another session's run restarts a worker under a
        # coordinator that did not choose its lane or its member.
        if session is not None and str(pointer.get("session") or "") != session:
            continue
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
                if not dry_run:
                    _record_wait_probe_history(
                        run_id,
                        observed="unavailable",
                        answered=False,
                        now_seconds=moment.timestamp(),
                    )
                skipped.append(
                    {
                        **entry,
                        "reason": "condition-check-failed",
                        "detail": str(exc),
                    }
                )
                continue
            # The observation is recorded whatever it says, before any branch
            # acts on it: the stillness a producer-gone reading measures is the
            # age of this history, so a sweep that skipped the recording on the
            # paths it does not act on would measure a history it never wrote.
            if not dry_run:
                _record_wait_probe_history(
                    run_id,
                    observed=str(observation["observed"]),
                    answered=bool(observation.get("answered", True)),
                    now_seconds=moment.timestamp(),
                )
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
            # The refusal is read from the attempt's own stream, so a run the
            # classifier still calls something other than "blocked" can be one
            # whose attempt has demonstrably ended: the classifier defers a
            # freshly resumed attempt to its own clock before it will read it
            # as halted, and gating on the word alone would skip the very
            # refusal a resumed turn recorded. A run that has not ended — a
            # live process, a worker whose end nothing observed — stays out of
            # the sweep's candidates as before.
            settled = _observed_end_refusal(pointer) is None
            if (
                str(classify_pointer(pointer).get("classification") or "") != "blocked"
                and not settled
            ):
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
        recovered = resolve_session(run_id, record=pointer)
        if not recovered["resolved"]:
            skipped.append(
                {
                    **entry,
                    "reason": "no-recoverable-session",
                    "detail": recovered["detail"],
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
                    "session_id": recovered["session_id"],
                    "session_source": recovered["source"],
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
                "session_id": recovered["session_id"],
                "session_source": recovered["source"],
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
            from reckon.crew.promotion_requests import PromotionRequests

            reviews = dispatch_awaiting_reviews(
                project=project,
                config=config,
                launcher=launch,
                session=session,
                pointers=live,
                promotions=promotions
                if promotions is not None
                else PromotionRequests(),
            )
        except Exception as exc:  # noqa: BLE001 - preserve the other sweep passes
            # A review pass that cannot run must not take the resume pass's
            # record down with it: the recoveries it did perform are already
            # durable on each resumed run.
            reviews = {"error": str(exc)}

    # The plan-review composition, on the same cadence and from the same site.
    # A plan whose author has just stopped editing owes a content review before
    # its sections may dispatch, and a coordinator paused at a rate limit
    # composes none; the pass that already dispatches each settled run's review
    # is what makes the plan's compose itself, under the session of the run
    # that last named the plan rather than this sweep's. Like the run pass it
    # is idempotent — a plan whose review is stored or in flight is left alone
    # — and it notes the findings a review it composed marked as not changing
    # the plan, so the coordinator is woken only for the ones that would.
    plan_reviews: dict[str, Any] = {}
    try:
        plan_reviews = plan_review.compose_owed_plan_reviews(
            project,
            config=config,
            launcher=launch,
            now_seconds=_now(now).timestamp(),
            dry_run=dry_run,
            pointers=live,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the other sweep passes
        plan_reviews = {"error": str(exc)}

    report = {
        "project": project,
        "dry_run": bool(dry_run),
        "checked": considered,
        "resumed": resumed,
        "skipped": skipped,
        "reviews": reviews,
        "swept_at": _utc_now(),
    }
    if any(
        plan_reviews.get(key)
        for key in ("composed", "skipped", "notes", "failed", "error")
    ):
        # A pass with nothing to say is reported as it was before the pass
        # existed, so a caller reading the sweep's shape is not handed an empty
        # block on every quiet tick.
        report["plan_reviews"] = plan_reviews
    # Two records, because they answer different questions. The append-only log
    # holds what happened, and only sweeps that judged something write to it —
    # an operator reading it wants recoveries and the refusals to recover, not a
    # heartbeat from every tick of every follower.
    if (
        considered
        or _review_outcome_counts(reviews)["dispatched"]
        or any(row.get("dispatched") for row in plan_reviews.get("composed") or ())
    ):
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
