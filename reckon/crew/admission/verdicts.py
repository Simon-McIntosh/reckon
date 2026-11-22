"""Record admission decisions, stage timings and queue stalls."""

from __future__ import annotations

from . import holds as _holds
from . import readiness as _readiness
from . import service as _service
from . import wait_refs as _wait_refs
from .shared import (
    ADMISSION_VERDICT_DAY_LOG_PREFIX,
    ADMISSION_VERDICT_LOG,
    ADMISSION_VERDICT_LOG_MAX_BYTES,
    ADMISSION_VERDICT_LOG_RETENTION_DAYS,
    ADMIT_MEMORY_BOUND_BYTES,
    HELD_PHASE,
    HOLD_VERDICTS,
    NOT_ADMISSIBLE_PHASES,
    READINESS_REF_PREFIX,
    STALL_AFTER_SECONDS,
    STALL_VERDICT,
    TERMINAL_VERDICTS,
    UTC,
    WAITING_UPSTREAM_PHASES,
    Any,
    Callable,
    Collection,
    Mapping,
    Path,
    Sequence,
    contextlib,
    datetime,
    json,
    os,
    parse_utc,
    runs,
    timedelta,
)

#: The holds the lane itself writes by granting no worker slot. The reason one of
#: these carries is the router's reading of a moment — a congestion verdict, a
#: headroom figure — and the lead's ruling is that a full lane is not a signal:
#: the queue absorbs it. So no surface repeats that reason, and the duty a request
#: under one of these holds owes is the wait itself.
CAPACITY_HOLD_VERDICTS = frozenset({"held-for-allowance"})

#: The words the lane's router uses when it publishes its own reading of a full
#: gate. A hold reason carrying one of them is the lane's figure rather than the
#: request's wait, and no surface repeats it.
CAPACITY_SENTENCE_WORDS = ("router", "congested", "headroom", "no extra worker")

#: The hold a request's own failed attempt puts it under, one word per cause the
#: surfaces read. A refusal that names the plan review it awaits is the plan
#: review's own word; every other refusal that names its remedy is a retry.
PLAN_REVIEW_HOLD_VERDICT = "held-on-plan-review"

REFUSED_ATTEMPT_HOLD_VERDICT = "held-on-retry-backoff"


def failure_hold(pointer: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hold a queued request's last failed attempt puts it under, or ``None``.

    A request an admission attempt refused comes back to the queue carrying that
    refusal in ``failure``, with the command that clears it quoted in the
    refusal's own text. The pass's own hold, written before that attempt, then
    still says whatever held the request then — a router congestion sentence, in
    the case that motivated this — and a reader of the hold learns a lane figure
    about a request no lane reading releases.

    So the failure and its remedy outrank that older hold, and the surfaces that
    read a hold compose it through this one reader: the request's hold, the
    pass's verdict row for it, and the duty row a coordinator reads. The reason
    is the refusal's own reading and the instant is the one the refusal was
    recorded at, so a hold's age is measured from the reason it shows.

    A refusal that names no remedy keeps its hold: there is nothing to publish
    and no accumulation to report, and the waiting reading is the true one.
    Imported here rather than at module load because the recovery classifier
    reads the admission package back inside its own functions.
    """
    from reckon.crew import recovery_classification

    refusal = recovery_classification._queued_failure_remedy(pointer)
    if refusal is None:
        return None
    text = str(refusal.get("detail") or "")
    stamped = recovery_classification._stamped_refusal_class(
        str(pointer.get("failure") or "")
    )
    verdict = (
        PLAN_REVIEW_HOLD_VERDICT
        if stamped == "PlanReviewMissingError"
        else REFUSED_ATTEMPT_HOLD_VERDICT
    )
    return {
        "verdict": verdict,
        "reason": text,
        "remedy": str(refusal.get("next_command") or ""),
        "recorded_at": str(refusal.get("recorded_at") or ""),
    }


def queued_hold(pointer: Mapping[str, Any]) -> dict[str, Any]:
    """The hold a waiting request is under, composed once for every reader.

    A request whose last attempt failed on a refusal that names its remedy is
    held by that refusal, whatever hold the last pass wrote: the pass asks the
    lane before it reads the request's back-off, and a lane with no slot answers
    first. The composition lives here, beside the two records it joins, so the
    obligations view, this document's queue and a verdict row cannot disagree
    about what holds one request.
    """
    coordinator = pointer.get("queue_hold")
    if isinstance(coordinator, Mapping) and str(coordinator.get("reason") or ""):
        held: dict[str, Any] = {
            "verdict": HELD_PHASE,
            "reason": str(coordinator["reason"]),
            "recorded_at": str(coordinator.get("started_at") or ""),
        }
        if not coordinator.get("automatic"):
            held["remedy"] = (
                f"reckon crew queue release --run {pointer.get('run_id') or ''!s}"
            )
        return held
    named = failure_hold(pointer)
    if named is not None:
        return dict(named)
    return _holds.recorded_pass_hold(pointer)


def hold_reason(hold: Mapping[str, Any]) -> str:
    """The reason a hold may show a reader, never the lane's capacity report.

    The lane's router publishes its own reading when it grants no slot — how many
    extra workers, and the word it decided that on. Repeating it on a row makes a
    full lane read as a finding to act on, which it is not: the queue absorbs it.
    So a capacity hold whose stored reason is that router sentence shows the hold
    and nothing more, while a hold whose reason describes the request's own wait
    keeps it — a row that said nothing at all would hide a real wait.
    """
    verdict = str(hold.get("verdict") or "")
    reason = str(hold.get("reason") or "")
    if verdict not in CAPACITY_HOLD_VERDICTS:
        return reason
    lowered = reason.lower()
    if any(word in lowered for word in CAPACITY_SENTENCE_WORDS):
        return ""
    return reason


def _verdict_row(
    pointer: Mapping[str, Any],
    *,
    allowance: float | None,
    allowance_source: str,
    verdict: str,
    detail: str = "",
    retry_at: str = "",
    failure: str = "",
    launch_seconds: float | None = None,
    slowest_step: str = "",
    slowest_step_seconds: float | None = None,
) -> dict[str, Any]:
    """One queued pointer's verdict for a pass, as the log records it.

    A launched row carries what the launch cost: its duration and the step of
    it that took longest, so a pass that ran long says which launch and which
    of its steps to look at rather than only that it was slow.

    A hold row for a request whose own last attempt failed on a refusal naming
    its remedy carries that refusal and its command instead of the hold the pass
    would otherwise record. The pass asks the lane questions in a fixed order,
    and the first one that holds the request writes the row; a lane with no slot
    holds it before its retry back-off is ever read, so the lane's congestion
    sentence would answer for a request the lane cannot release. The refusal is
    a later fact about the same request, so it answers here, through the reader
    the hold itself is composed by.
    """
    row_remedy = ""
    if verdict in CAPACITY_HOLD_VERDICTS:
        named = failure_hold(pointer)
        if named is not None:
            verdict = str(named["verdict"])
            detail = str(named["reason"])
            row_remedy = str(named.get("remedy") or "")
    row: dict[str, Any] = {
        "run_id": str(pointer.get("run_id") or ""),
        "session": str(pointer.get("session") or ""),
        "allowance": allowance,
        "allowance_source": allowance_source,
        "verdict": verdict,
    }
    if detail:
        row["detail"] = detail
    if row_remedy:
        row["remedy"] = row_remedy
    if retry_at:
        row["retry_at"] = retry_at
    if failure:
        row["failure"] = failure
    if launch_seconds is not None:
        row["launch_seconds"] = launch_seconds
    if slowest_step:
        row["slowest_step"] = slowest_step
    if slowest_step_seconds is not None:
        row["slowest_step_seconds"] = slowest_step_seconds
    return row


def _day_log_name(day: str) -> str:
    """The file name a rotated day's verdict log carries."""
    return f"{ADMISSION_VERDICT_DAY_LOG_PREFIX}{day}.jsonl"


def _day_from_log_name(name: str) -> str:
    """The ISO day a rotated log's file name carries, or ``""`` for any other."""
    if not name.startswith(ADMISSION_VERDICT_DAY_LOG_PREFIX):
        return ""
    stem = name[len(ADMISSION_VERDICT_DAY_LOG_PREFIX) :]
    if not stem.endswith(".jsonl"):
        return ""
    day = stem[: -len(".jsonl")]
    try:
        datetime.fromisoformat(day)
    except ValueError:
        return ""
    return day


def _verdict_log_dir(home: Path | None = None) -> Path:
    return home if home is not None else runs.crew_home()


def _current_verdict_log_path(home: Path | None = None) -> Path:
    return _verdict_log_dir(home) / ADMISSION_VERDICT_LOG


def _day_verdict_log_paths(home: Path | None = None) -> list[tuple[str, Path]]:
    """Every rotated day log under the crew home, oldest day first."""
    try:
        entries = list(_verdict_log_dir(home).iterdir())
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for entry in entries:
        day = _day_from_log_name(entry.name)
        if day:
            found.append((day, entry))
    found.sort()
    return found


def _pass_record_paths(home: Path | None = None) -> list[tuple[str, Path]]:
    """The files a week read parses, oldest first, day-labelled.

    The rotated day files hold complete days; the current file holds the day in
    progress and is labelled by its own newest record so a report names which
    day's rows it carries.
    """
    files = _day_verdict_log_paths(home)
    current = _current_verdict_log_path(home)
    if current.exists():
        files.append((_last_record_day(current) or "current", current))
    return files


def _last_record_day(path: Path) -> str:
    """The UTC day of the newest complete record in a log file, or ``""``.

    Read from the file's tail, so an append need not deserialize a whole day of
    records to learn whether the day has rolled over. A partial final line, from
    a writer mid-append, is skipped.
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 65536)
            handle.seek(size - window)
            tail = handle.read(window)
    except OSError:
        return ""
    for raw in reversed(tail.splitlines()):
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        at = parse_utc(record.get("at"))
        if at is not None:
            return at.date().isoformat()
    return ""


def _rotate_current_verdict_log(day: str, home: Path | None = None) -> Path | None:
    """Move the current log to the day file its records belong to.

    Called before an append whose record is dated a later day than the file's
    newest record. A target that already exists is folded into rather than
    replaced, because a second lane on the same shared home can rotate the same
    day concurrently and the day file holds records no other file carries.
    """
    current = _current_verdict_log_path(home)
    held = _last_record_day(current)
    if not held or held == day:
        return None
    target = _verdict_log_dir(home) / _day_log_name(held)
    try:
        if target.exists():
            data = current.read_bytes()
            with open(target, "ab") as handle:
                handle.write(data)
            current.unlink()
        else:
            os.replace(current, target)
    except OSError:
        return None
    return target


def _prune_day_verdict_logs(today: str, home: Path | None = None) -> list[Path]:
    """Drop day logs that have fallen outside the retention window.

    The horizon is ``today`` minus the retention, so the current day plus one
    file per retained day is held and a read of the last seven days always finds
    its whole window. Only files whose name carries an ISO day are considered,
    so the current log is never a candidate.
    """
    try:
        horizon = datetime.fromisoformat(today) - timedelta(
            days=ADMISSION_VERDICT_LOG_RETENTION_DAYS
        )
    except ValueError:
        return []
    removed: list[Path] = []
    for day, path in _day_verdict_log_paths(home):
        try:
            day_at = datetime.fromisoformat(day)
        except ValueError:
            continue
        if day_at >= horizon:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


def verdict_log_append(
    record: Mapping[str, Any], *, home: Path | None = None
) -> dict[str, Any]:
    """Write one pass record to its day's log, rotating and pruning around it.

    Rotation and pruning happen on the append rather than on a timer, so a
    service that is idle across midnight rolls over on its next pass, and one
    that stops leaves the day it stopped in to hold its records rather than a
    current file nothing will ever rotate.
    """
    at = parse_utc(record.get("at"))
    day = at.date().isoformat() if at is not None else _service._utc_now()[:10]
    _rotate_current_verdict_log(day, home)
    path = _current_verdict_log_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (
            json.dumps(dict(record), separators=(",", ":"), default=str) + "\n"
        ).encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
    except OSError:
        return {"written": False, "day": day, "path": str(path)}
    from reckon.crew.fleet_supervisor import trim_service_log

    with contextlib.suppress(OSError):
        trim_service_log(
            path,
            limit=ADMISSION_VERDICT_LOG_MAX_BYTES,
            keep=ADMISSION_VERDICT_LOG_MAX_BYTES,
        )
    _prune_day_verdict_logs(day, home)
    return {"written": True, "day": day, "path": str(path)}


def _record_instant(record: Mapping[str, Any]) -> datetime:
    return parse_utc(record.get("at")) or datetime.min.replace(tzinfo=UTC)


def read_pass_verdicts(
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """Every admission verdict the retained log holds, and the span it covers.

    A read is answered from the current day's file and the rotated day files
    kept under the crew home, so a verdict written on any retained day is
    returned from
    one call rather than from whatever fitted in a single byte-bounded file.
    Records are filtered to ``[since, until]`` and returned oldest first.

    The covered span is the span of the whole retained log rather than of the
    filtered rows, so a caller asking for a stretch older than the log's start
    is told the span it covers instead of being handed a window the log cannot
    support. ``days`` carries, per retained file, the day it holds, its path,
    its bytes and its record count; ``bytes_per_day`` is the same byte figure
    keyed by day, which is the cost the log keeps per day.
    """
    paths = _pass_record_paths(home)
    days: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    covered: list[datetime] = []
    for label, path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        day_records = 0
        for raw in data.splitlines():
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, Mapping):
                continue
            at = parse_utc(record.get("at"))
            if at is None:
                continue
            day_records += 1
            covered.append(at)
            if since is not None and at < since:
                continue
            if until is not None and at > until:
                continue
            records.append(dict(record))
        days.append(
            {
                "day": label,
                "path": str(path),
                "bytes": len(data),
                "records": day_records,
            }
        )
    records.sort(key=_record_instant)
    covered_start = min(covered) if covered else None
    covered_end = max(covered) if covered else None
    total_bytes = sum(day["bytes"] for day in days)
    uncovered = None
    if covered_start is not None and since is not None:
        uncovered = round((covered_start - since).total_seconds(), 3)
    span = None
    if covered_start is not None and covered_end is not None:
        span = round((covered_end - covered_start).total_seconds(), 3)
    return {
        "status": "measured" if covered else "unmeasured",
        "retention_days": ADMISSION_VERDICT_LOG_RETENTION_DAYS,
        "requested_start": since.isoformat() if since is not None else None,
        "requested_end": until.isoformat() if until is not None else None,
        "covered_start": covered_start.isoformat() if covered_start else None,
        "covered_end": covered_end.isoformat() if covered_end else None,
        "covered_span_seconds": span,
        "uncovered_seconds": uncovered,
        "records": records,
        "records_by_day": {day["day"]: day["records"] for day in days},
        "days": days,
        "bytes_per_day": {day["day"]: day["bytes"] for day in days},
        "total_bytes": total_bytes,
        "mean_bytes_per_day": round(total_bytes / len(days), 1) if days else 0.0,
    }


#: The newest reading of the newest retained pass records, memoised against the
#: identity of the files it parsed. A pass asks a verdict and a stage duration
#: of every queued request, so one reading of a day of records is shared by all
#: and re-read only when a file it parsed has changed.
_RECENT_PASS_CACHE: dict[str, Any] = {}


def _recent_pass_reading(paths: Sequence[tuple[str, Path]]) -> dict[str, Any]:
    """The last verdict and last stage duration each request carries."""
    signature: list[tuple[str, int, int, int]] = []
    for _label, path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino))
    key = tuple(signature)
    if _RECENT_PASS_CACHE.get("key") == key:
        return _RECENT_PASS_CACHE
    verdicts: dict[str, str] = {}
    stages: dict[str, float] = {}
    for _label, path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, Mapping):
                continue
            for name, value in record.items():
                if name.endswith("_seconds") and isinstance(value, (int, float)):
                    stages[name] = round(float(value), 3)
            for row in record.get("pointers") or ():
                if isinstance(row, Mapping):
                    run_id = str(row.get("run_id") or "")
                    if run_id:
                        verdicts[run_id] = str(row.get("verdict") or "")
    _RECENT_PASS_CACHE["key"] = key
    _RECENT_PASS_CACHE["verdicts"] = verdicts
    _RECENT_PASS_CACHE["stages"] = stages
    return _RECENT_PASS_CACHE


def _recent_pass_files() -> list[tuple[str, Path]]:
    """The current log and the newest day log, oldest first.

    A rotation at midnight leaves the current file with today's records, so a
    reader that looked only there would lose the verdict a request carried
    across the boundary. Two files hold more than a day of passes at any
    measured rate.
    """
    return _pass_record_paths()[-2:]


def _last_verdict_of(run_id: str) -> str:
    """The verdict the most recent recorded pass gave this request.

    Read from the verdict log rather than from the pointer, because a verdict
    is what a pass decided rather than what the request says about itself. A
    later pass for the same request replaces the answer, so a request whose
    stall has since launched no longer
    reads as stalled.
    """
    if not run_id:
        return ""
    reading = _recent_pass_reading(_recent_pass_files())
    return str((reading.get("verdicts") or {}).get(run_id, ""))


def _last_stage_seconds(stage: str) -> float | None:
    """The duration the named stage took on the most recent recorded pass.

    A stall names the stage that held the request and that stage's duration on
    the last pass, so a reader learns which of release, launch and land to look
    at rather than only that the request waited. ``None`` when no pass has
    recorded that stage yet, which is the first pass of a service.
    """
    stages = _recent_pass_reading(_recent_pass_files()).get("stages") or {}
    value = stages.get(f"{stage}_seconds")
    return None if value is None else float(value)


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
        if (
            last_verdict == STALL_VERDICT
            or last_verdict in HOLD_VERDICTS
            or last_verdict in TERMINAL_VERDICTS
        ):
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
) -> dict[str, Any]:
    """The one record a pass leaves, whether or not it decided anything.

    A pass always leaves a reading: an idle pass is a fact a reader needs, and
    the reading carries the service's resident memory so the bound the service
    holds to is read from its own passes rather than inferred from the outside.
    The same object is what the verdict log holds and what the service loop
    prints, so a coordinator reading the log and a caller reading stdout see
    one account of the pass rather than two that can drift.
    """
    verdicts = result.get("verdicts") or []
    landing = result.get("landing") or {}
    return {
        "at": _service._utc_now(),
        "started_at": result.get("started_at"),
        "ended_at": result.get("ended_at"),
        "release_seconds": result.get("release_seconds"),
        "waits_seconds": result.get("waits_seconds"),
        "launch_seconds": result.get("launch_seconds"),
        "launch_budget_seconds": result.get("launch_budget_seconds"),
        "launch_budget_exhausted": bool(result.get("launch_budget_exhausted")),
        "land_seconds": result.get("land_seconds"),
        "reads": result.get("reads"),
        "reads_by_source": result.get("reads_by_source"),
        "resident_bytes": result.get("resident_bytes"),
        "memory_bound_bytes": ADMIT_MEMORY_BOUND_BYTES,
        "lane": lane,
        "code_stamp": code_stamp,
        "gate": result.get("gate"),
        "admissible_age_seconds": result.get("admissible_age_seconds"),
        "roles": result.get("role_cadence"),
        "stalls": list(result.get("stalls") or ()),
        "pointers": list(verdicts),
        "retired": list(result.get("retired") or ()),
        "retired_lost_session": list(result.get("retired_lost_session") or ()),
        "retirement_refused": list(result.get("retirement_refused") or ()),
        "obligations": list(result.get("obligations") or ()),
        "landing": landing,
    }


def _publish_pass_verdicts(record: Mapping[str, Any] | None) -> None:
    """Write one pass's verdicts to stdout as one JSON line.

    The bare ``crew admit`` verb is the fleet's admit service: a coordinator
    watching it must see each decision as the pass reaches it rather than only
    afterwards out of the verdict log. Every pass prints its reading, so an
    idle service is visible as a run of pass readings rather than as silence.
    """
    print(json.dumps(record), flush=True)


def _record_pass_verdicts(
    result: Mapping[str, Any], *, lane: str, code_stamp: str
) -> dict[str, Any]:
    """Write one log line for every pass, including a pass that decided nothing.

    A reading that carried only outcomes made an idle service indistinguishable
    from a stopped one, and left the service's resident memory unrecorded: the
    pass reading is where the bound the service holds to is read. The record is
    written by :func:`verdict_log_append`, which rolls the current file to its
    day file when the day changes and prunes what has fallen out of the
    retention window, so the log a later seven-day read sees holds a week.

    The trim keeps the file's inode, so the appender's own descriptor stays
    valid, but it reads the tail and then rewrites the file in place: a line
    another lane's service appends in that window is still lost. The window is
    narrow and accepted, because this log is a best-effort record rather than a
    decision the service rests on. For the same reason a trim that raises is
    swallowed: a storage error while bounding the record must not end a service
    whose admission decision has already been made.
    """
    record = _pass_verdict_record(result, lane=lane, code_stamp=code_stamp)
    verdict_log_append(record)
    return record
