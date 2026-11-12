"""Record admission decisions, stage timings and queue stalls."""

from __future__ import annotations

from . import holds as _holds
from . import readiness as _readiness
from . import service as _service
from . import wait_refs as _wait_refs
from .shared import (
    ADMISSION_VERDICT_LOG,
    ADMISSION_VERDICT_LOG_MAX_BYTES,
    HOLD_VERDICTS,
    NOT_ADMISSIBLE_PHASES,
    READINESS_REF_PREFIX,
    STALL_AFTER_SECONDS,
    STALL_VERDICT,
    WAITING_UPSTREAM_PHASES,
    Any,
    Callable,
    Collection,
    Mapping,
    Path,
    Sequence,
    datetime,
    json,
    parse_utc,
    runs,
)


def _verdict_row(
    pointer: Mapping[str, Any],
    *,
    allowance: float | None,
    allowance_source: str,
    verdict: str,
    detail: str = "",
    retry_at: str = "",
    failure: str = "",
) -> dict[str, Any]:
    """One queued pointer's verdict for a pass, as the log records it."""
    row: dict[str, Any] = {
        "run_id": str(pointer.get("run_id") or ""),
        "session": str(pointer.get("session") or ""),
        "allowance": allowance,
        "allowance_source": allowance_source,
        "verdict": verdict,
    }
    if detail:
        row["detail"] = detail
    if retry_at:
        row["retry_at"] = retry_at
    if failure:
        row["failure"] = failure
    return row


def _last_verdict_of(run_id: str) -> str:
    """The verdict the most recent recorded pass gave this request.

    Read from the verdict log rather than from the pointer, because a verdict is
    what a pass decided rather than what the request says about itself. A later
    pass for the same request replaces the answer, so a request whose stall has
    since launched no longer reads as stalled.
    """
    if not run_id:
        return ""
    try:
        text = (runs.crew_home() / ADMISSION_VERDICT_LOG).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for row in record.get("pointers") or ():
            if str(row.get("run_id") or "") == run_id:
                return str(row.get("verdict") or "")
    return ""


def _last_stage_seconds(stage: str) -> float | None:
    """The duration the named stage took on the most recent recorded pass.

    A stall names the stage that held the request and that stage's duration on
    the last pass, so a reader learns which of release, launch and land to look
    at rather than only that the request waited. ``None`` when no pass has
    recorded that stage yet, which is the first pass of a service.
    """
    key = f"{stage}_seconds"
    try:
        text = (runs.crew_home() / ADMISSION_VERDICT_LOG).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        value = record.get(key)
        if isinstance(value, (int, float)):
            return round(float(value), 3)
    return None


def _stall_stage(pointer: Mapping[str, Any]) -> str:
    """The stage whose completion a still-waiting request is owed.

    Release hands a request to the queue, launch hands it to a worker, and land
    settles what the worker delivered. A request is owed the stage that reads
    its phase, so the stall names the stage a reader should look at rather than
    every stage the pass ran.
    """
    phase = str(pointer.get("phase") or "")
    if phase in {"awaiting-upstream", "blocked-upstream"}:
        return "release"
    if phase in {"landed", "landing"}:
        return "land"
    return "launch"


def _lane_has_room(
    pointer: Mapping[str, Any],
    *,
    gated: bool,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> bool:
    """Whether the lane could launch this request on this pass.

    A request the lane has no room for is waiting on capacity, which the pass
    records as its own hold; it is not stalled, because nothing about it is
    stuck. This is the clause that keeps a full lane from reading as a stall.
    """
    if gated:
        return False
    if _holds._lane_gate_hold(pointer, config_for=config_for) is not None:
        return False
    allowance, _source = _holds._session_allowance_read(pointer, config_for=config_for)
    return allowance is None or allowance > 0


def _record_stalls(
    order: Sequence[Mapping[str, Any]],
    *,
    moment: datetime,
    gated: bool,
    live: Sequence[Mapping[str, Any]],
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    claim_dispositions: dict[str, Any],
    shared_files: dict[tuple[str, str], frozenset[str]],
    held: Collection[str],
    result: dict[str, Any],
) -> list[str]:
    """Record one stall verdict per request that waited while it could have run.

    A request is stalled when the lane could launch it now, no live run's claim
    holds it, and it has been admissible for longer than
    :data:`STALL_AFTER_SECONDS`. The verdict names the stage the request is owed
    and that stage's duration on the last pass, so the record a coordinator
    already reads says which stage held it. The verdict is written once: a
    request already recorded as stalled keeps that record until a later verdict
    for it replaces it, so a wait that spans many passes yields one stall rather
    than one per pass.

    A request this pass holds on purpose is never stalled, and neither is one an
    earlier pass's hold verdict accounts for: the hold is the pass doing its job
    and naming why, so nothing admissible is being left undone. ``held`` names
    the requests this pass wrote a hold for.
    """
    stalled: list[str] = []
    for pointer in order:
        run_id = str(pointer.get("run_id") or "")
        if not run_id or run_id in held:
            continue
        if str(pointer.get("phase") or "") in NOT_ADMISSIBLE_PHASES:
            continue
        last_verdict = _last_verdict_of(run_id)
        if last_verdict == STALL_VERDICT or last_verdict in HOLD_VERDICTS:
            continue
        queued_at = parse_utc(pointer.get("queued_at"))
        if queued_at is None:
            continue
        age = (moment - queued_at).total_seconds()
        if age < STALL_AFTER_SECONDS:
            continue
        if not _lane_has_room(pointer, gated=gated, config_for=config_for):
            continue
        shared_key = (
            str(pointer.get("project") or ""),
            str(pointer.get("repo") or ""),
        )
        if shared_key not in shared_files:
            shared_files[shared_key] = (
                runs._shared_write_paths(shared_key[0], Path(shared_key[1]))
                if all(shared_key)
                else frozenset()
            )
        if (
            _holds._live_claim_hold(
                pointer,
                live,
                dispositions=claim_dispositions,
                shared_files=shared_files[shared_key],
            )
            is not None
        ):
            continue
        stage = _stall_stage(pointer)
        allowance, source = _holds._session_allowance_read(
            pointer, config_for=config_for
        )
        row = _verdict_row(
            pointer,
            allowance=allowance,
            allowance_source=source,
            verdict=STALL_VERDICT,
            detail=f"held at the {stage} stage",
        )
        row["stage"] = stage
        row["stage_seconds"] = _last_stage_seconds(stage)
        row["age_seconds"] = round(age, 3)
        result["verdicts"].append(row)
        stalled.append(run_id)
    return stalled


def _admissible_age_seconds(
    order: Sequence[Mapping[str, Any]],
    *,
    moment: datetime,
    gated: bool,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> float | None:
    """How long the oldest request the lane could launch now has waited.

    A pass that reports only what it did leaves a reader unable to tell an idle
    lane from one whose queue is old: a request held for its session's allowance
    and a request nothing has examined look identical when neither appears in a
    verdict. This figure is the age of the head of the queue a launch would have
    taken, and it is `None` on a pass with nothing the lane could launch.
    """
    oldest: float | None = None
    for pointer in order:
        queued_at = parse_utc(pointer.get("queued_at"))
        if queued_at is None:
            continue
        if not _lane_has_room(pointer, gated=gated, config_for=config_for):
            continue
        age = (moment - queued_at).total_seconds()
        oldest = age if oldest is None else max(oldest, age)
    return None if oldest is None else round(max(0.0, oldest), 3)


def _record_gated_verdicts(
    result: dict[str, Any],
    *,
    gate: Mapping[str, Any],
    lane: str,
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
) -> None:
    """Name every queued request a waiting fleet gate held, and the gate itself.

    The gate is read before the queue is, so a pass that returns here would
    otherwise record nothing for requests its own gate is holding — the same
    empty trace as a request held for its session's allowance. The requests are
    read only to name them: nothing is released, launched or rewritten.
    """
    state = str(gate.get("state") or "")
    for pointer in runs.list_live():
        if pointer.get("phase") != "queued":
            continue
        if lane and str(pointer.get("backend")) != lane:
            continue
        allowance, source = _holds._session_allowance_read(
            pointer, config_for=config_for
        )
        result["verdicts"].append(
            _verdict_row(
                pointer,
                allowance=allowance,
                allowance_source=source,
                verdict="held-by-gate",
                detail=state,
            )
        )


def _record_readiness_verdicts(
    result: dict[str, Any],
    *,
    live: Sequence[Mapping[str, Any]],
    lane: str = "",
    config_for: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> None:
    """Name what holds each request waiting on its section, and record the
    blocker on the request itself.

    A request waiting on a refs it declares is accounted for by that wait, but
    a request held on its own section's readiness is held by a gate or an open
    decision, and a pass otherwise records nothing for it: the request never
    reaches the queue it launches from, so every other verdict skips it. The
    hold and the request's own record carry the same blocker names, so a
    coordinator reading the pass and one reading the pointer agree.
    """
    readonly: dict[str, Any] = {}
    for pointer in live:
        if str(pointer.get("phase") or "") not in WAITING_UPSTREAM_PHASES:
            continue
        if lane and str(pointer.get("backend")) != lane:
            continue
        refs = _wait_refs.section_waits_for(pointer)
        if not any(ref.startswith(READINESS_REF_PREFIX) for ref in refs):
            continue
        hold = _readiness.section_hold(
            refs,
            project=str(pointer.get("project") or ""),
            docs_dir=Path(str(pointer.get("repo") or "") or ".") / "docs",
            cache=readonly,
        )
        allowance, source = _holds._session_allowance_read(
            pointer, config_for=config_for
        )
        verdict = _readiness.section_hold_verdict(hold)
        row = _verdict_row(
            pointer,
            allowance=allowance,
            allowance_source=source,
            verdict=verdict,
            detail=hold["detail"],
        )
        row["reason"] = hold["reason"]
        row["hold"] = list(hold["names"])
        result["verdicts"].append(row)
        _holds._record_pass_hold(
            pointer,
            {
                "verdict": verdict,
                "reason": hold["reason"],
                "detail": hold["detail"],
                "hold": list(hold["names"]),
            },
            live=live,
            moment=_service._now(),
        )


def _pass_verdict_record(
    result: Mapping[str, Any], *, lane: str, code_stamp: str
) -> dict[str, Any] | None:
    """The one record a pass leaves, or ``None`` when it had no outcome.

    The same object is what the verdict log holds and what the service loop
    prints, so a coordinator reading the log and a caller reading stdout see
    one account of the pass rather than two that can drift.
    """
    verdicts = result.get("verdicts") or []
    landing = result.get("landing") or {}
    if (
        not verdicts
        and not landing.get("landed")
        and not landing.get("skipped")
        and not landing.get("obligations")
    ):
        return None
    return {
        "at": _service._utc_now(),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "release_seconds": result.get("release_seconds"),
        "waits_seconds": result.get("waits_seconds"),
        "launch_seconds": result.get("launch_seconds"),
        "land_seconds": result.get("land_seconds"),
        "lane": lane,
        "code_stamp": code_stamp,
        "gate": result.get("gate"),
        "admissible_age_seconds": result.get("admissible_age_seconds"),
        "stalls": list(result.get("stalls") or ()),
        "pointers": list(verdicts),
        "retired": list(result.get("retired") or ()),
        "obligations": list(result.get("obligations") or ()),
        "landing": landing,
    }


def _publish_pass_verdicts(record: Mapping[str, Any] | None) -> None:
    """Write one pass's verdicts to stdout as one JSON line.

    The bare ``crew admit`` verb is the fleet's admit service: a coordinator
    watching it must see each decision as the pass reaches it rather than only
    afterwards out of the verdict log. A pass that examined no queued pointer
    decided nothing, so it prints nothing — the same silence it records.
    """
    if record is None:
        return
    print(json.dumps(record), flush=True)


def _record_pass_verdicts(
    result: Mapping[str, Any], *, lane: str, code_stamp: str
) -> dict[str, Any] | None:
    """Write one log line for a pass with an admission or landing outcome.

    A pass with no pointer verdict or landing outcome writes nothing: an idle
    pass is the absence of a line, not a line saying so. The line uses the shared
    crew-home log writer, so this log and the picker's timings are written the
    same way, and the bound that follows is the shared service-log trim.

    The trim keeps the file's inode, so the appender's own descriptor stays
    valid, but it reads the tail and then rewrites the file in place: a line
    another lane's service appends in that window is still lost. The window is
    narrow and accepted, because this log is a best-effort record rather than a
    decision the service rests on. For the same reason a trim that raises is
    swallowed: a storage error while bounding the record must not end a service
    whose admission decision has already been made.
    """
    record = _pass_verdict_record(result, lane=lane, code_stamp=code_stamp)
    if record is None:
        return None
    from reckon.crew.fleet_supervisor import trim_service_log
    from reckon.crew.picker import _append_crew_log_line

    _append_crew_log_line(ADMISSION_VERDICT_LOG, record)
    try:
        trim_service_log(
            runs.crew_home() / ADMISSION_VERDICT_LOG,
            limit=ADMISSION_VERDICT_LOG_MAX_BYTES,
            keep=ADMISSION_VERDICT_LOG_MAX_BYTES,
        )
    except OSError:  # a pass never fails on its own record
        return record
    return record
