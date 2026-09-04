"""Resume the runs a lapsed provider refusal is still holding.

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

Four bounds keep the sweep from becoming its own hazard, and each is checked
before anything is launched:

* **At most one resume per hold.** A resumed run is stamped with the hold it
  was resumed for. A lane that refuses again writes a new hold, and the sweep
  waits for that hold's own expiry rather than retrying into a spent account.
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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon import budget as budget_module
from reckon.crew.dispatch import (
    BudgetHold,
    _backend_settings,
    _spawn,
    record_resumption,
    resume_plan,
)
from reckon.crew.node import CrewError
from reckon.crew.recovery import _stream_refusal_block, classify_pointer
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

# How often the follower may sweep. Long enough that a tight poll loop cannot
# turn the sweep into a hot path, short enough that a lane returning minutes
# later is picked up while the wave still matters.
DEFAULT_SWEEP_SECONDS = 120.0


def _parse_stamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 stamp, returning None for anything unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(tz=UTC)


def recovery_log_path(project: str) -> Path:
    """Where one project's recovery record accumulates."""
    readable = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(project)
    ).strip("-")
    return crew_home() / "recovery" / f"{readable or 'project'}.jsonl"


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


def _recoverable_session(record: Mapping[str, Any]) -> dict[str, str] | None:
    """The session a resume could continue, and which source held it.

    A pointer carrying no session id is not evidence of an unresumable run: a
    resume that finds none on the record re-reads the run's stream for one, so
    the stream is a second source rather than a fallback nobody consults.
    """
    pointer_session = str(record.get("session_id") or "").strip()
    if pointer_session:
        return {"session_id": pointer_session, "source": "pointer"}
    from reckon import _backends

    log = Path(str(record.get("log_path") or ""))
    if record.get("launch") != "cli" or not log.is_file():
        return None
    try:
        backend = _backend_settings(record, None)
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend=backend,
            log_path=log,
        )
    except (CrewError, OSError, ValueError):
        return None
    stream_session = str(observation.session_id or "").strip()
    return (
        {"session_id": stream_session, "source": "stream"} if stream_session else None
    )


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
        if process_alive(other.get("pid")) is not True:
            continue
        other_node = other.get("node") or {}
        claimed |= mine & {str(path) for path in (other_node.get("write_paths") or ())}
    return sorted(claimed)


def _resume(
    run_id: str,
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    launcher: Callable[..., int],
) -> dict[str, Any]:
    """Launch one resumption exactly the way a hand-typed resume does."""
    plan = resume_plan(run_id, CONTINUE_ADVICE, config=config)
    directory = run_dir(run_id)
    turn = len(list(directory.glob("resume-*.jsonl"))) + 1
    advice_path = directory / f"resume-{turn}-advice.txt"
    advice_path.parent.mkdir(parents=True, exist_ok=True)
    advice_path.write_text(CONTINUE_ADVICE + "\n", encoding="utf-8")
    log_path = directory / f"resume-{turn}.jsonl"
    stderr_path = directory / f"resume-{turn}.stderr.log"
    attempt_started_at = _utc_now()
    manifest_baseline_mtime_ns = _manifest_mtime_ns(record.get("manifest_path") or "")
    pid = launcher(
        plan, log_path=log_path, stderr_path=stderr_path, prompt_path=advice_path
    )
    record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    return {"pid": pid, "turn": turn, "log_path": str(log_path)}


def _stamp_hold(run_id: str, signature: str) -> None:
    """Record which hold this run was resumed for, so it is resumed once."""
    with _pointer_lock(run_id):
        try:
            current = read_pointer(run_id)
        except CrewError:
            return
        current["auto_resume"] = {"hold": signature, "at": _utc_now()}
        _write_json(pointer_path(run_id), current)


def sweep(
    project: str,
    *,
    config: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    launcher: Callable[..., int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resume every run this project holds on a refusal that has lapsed.

    Idempotent by construction: a resumed run carries the hold it was resumed
    for, so a second pass over the same fleet reports nothing to do. That is
    what makes it safe for something already running to call on a cadence.
    """
    launch = _spawn if launcher is None else launcher
    policy_block = budget_module.policy(config)
    resumed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    considered = 0

    for pointer in list_live(project=project):
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        refusal = _stream_refusal_block(pointer)
        if refusal is None:
            continue
        if str(classify_pointer(pointer).get("classification") or "") != "blocked":
            continue
        considered += 1
        entry = {"run_id": run_id, "backend": refusal.get("backend")}

        hold = hold_state(pointer, refusal, policy_block=policy_block, now=now)
        if hold["in_force"]:
            skipped.append(
                {**entry, "reason": "hold-in-force", "detail": hold["detail"]}
            )
            continue
        previous = pointer.get("auto_resume")
        if (
            isinstance(previous, Mapping)
            and hold["signature"]
            and str(previous.get("hold") or "") == hold["signature"]
        ):
            skipped.append(
                {
                    **entry,
                    "reason": "already-resumed-for-this-hold",
                    "detail": (
                        "this run was already resumed for the hold that lapsed; "
                        "a further refusal writes its own hold to wait on"
                    ),
                }
            )
            continue
        session = _recoverable_session(pointer)
        if session is None:
            skipped.append(
                {
                    **entry,
                    "reason": "no-recoverable-session",
                    "detail": "neither the pointer nor the run's stream carries a session id",
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
        if dry_run:
            resumed.append(
                {
                    **entry,
                    "would_resume": True,
                    "session_id": session["session_id"],
                    "session_source": session["source"],
                    "hold": hold["signature"],
                    "advice": CONTINUE_ADVICE,
                }
            )
            continue
        try:
            launched = _resume(run_id, pointer, config=config, launcher=launch)
        except BudgetHold as exc:
            skipped.append(
                {
                    **entry,
                    "reason": "hold-in-force",
                    "detail": (
                        "the lane's own budget verdict still holds it: "
                        + str(exc.verdict.get("reason") or exc)
                    ),
                }
            )
            continue
        except (CrewError, OSError) as exc:
            skipped.append({**entry, "reason": "resume-refused", "detail": str(exc)})
            continue
        _stamp_hold(run_id, hold["signature"])
        resumed.append(
            {
                **entry,
                "session_id": session["session_id"],
                "session_source": session["source"],
                "hold": hold["signature"],
                "advice": CONTINUE_ADVICE,
                **launched,
            }
        )

    report = {
        "project": project,
        "dry_run": bool(dry_run),
        "checked": considered,
        "resumed": resumed,
        "skipped": skipped,
        "swept_at": _utc_now(),
    }
    # A sweep that found nothing to judge says nothing: an operator reading the
    # record wants the recoveries and the refusals to recover, not a heartbeat
    # from every cadence tick of every follower.
    if considered:
        path = recovery_log_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, sort_keys=True) + "\n")
    return report
