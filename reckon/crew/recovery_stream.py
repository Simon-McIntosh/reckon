# ruff: noqa: I001, UP035
from __future__ import annotations

import math
import re
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from reckon._timestamps import parse_utc
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    WAITING_REQUEST_PHASES,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    TERMINAL_MANIFEST_STATUSES,
    manifest_status_is_template,
)
from reckon.crew.routing import _signal_process_group, resolved_grace_multiple
from reckon.crew.runs import (
    _utc_now,
    dispatch_field,
)


SELF_LIFTING_RECOVERY_CLASSIFICATIONS = frozenset({"waiting", "paused"})
DEFAULT_LIFTING_CONDITIONS = {
    "waiting": "the declared condition reaches one of its terminal states",
    "paused": "the condition named by the row ends",
}


def manifest_status_is_terminal(value: Any) -> bool:
    """Whether a worker supplied one exact terminal status value."""
    status = str(value or "").strip().lower()
    return not manifest_status_is_template(status) and (
        status in TERMINAL_MANIFEST_STATUSES
    )


# A finished run's completion stamp is asked for on every producer pass, for
# every live run whose worker has gone, and resolving it parses every stream
# the run wrote. Those streams have stopped growing, so the answer is kept per
# process keyed by each stream file's path, inode, size and mtime: an unchanged
# set of streams is answered without a parse, and any write to any of them
# moves the key. The store is cleared whole when it reaches its bound.
_COMPLETION_STAMPS: dict[tuple[Any, ...], str | None] = {}
_COMPLETION_STAMPS_LIMIT = 4096


def _completion_stamp_key(record: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """The stat identity of every stream a run wrote, or None if one is unreadable."""
    from reckon.crew import metering

    identities = []
    for candidate in metering.run_streams(str(record.get("log_path") or "")):
        try:
            info = candidate.stat()
        except OSError:
            return None
        identities.append((str(candidate), info.st_ino, info.st_size, info.st_mtime_ns))
    if not identities:
        return None
    return (
        str(record.get("run_id") or ""),
        str(record.get("backend") or ""),
        tuple(identities),
    )


def _stream_completion_stamp(record: Mapping[str, Any]) -> str | None:
    """The run's own finish stamp as its recorded stream dates it, else None.

    Promotion writes this same stream completion stamp to the ledger row, so
    the elapsed measure and the promoted record agree on when a run ended
    rather than each keeping its own idea of the finish. None for an in-harness
    run or a stream that was never written; the caller only resolves it once
    liveness says the process is gone, so a still-writing stream is never
    mistaken for a completion. A run whose streams are unchanged since the last
    answer is given that answer without parsing them again.
    """
    if record.get("launch") != "cli":
        return None
    from reckon.crew.promotion import _terminal_stream_data

    key = _completion_stamp_key(record)
    if key is not None and key in _COMPLETION_STAMPS:
        return _COMPLETION_STAMPS[key]
    stamp = _terminal_stream_data(record).completed_at
    if key is not None:
        if len(_COMPLETION_STAMPS) >= _COMPLETION_STAMPS_LIMIT:
            _COMPLETION_STAMPS.clear()
        _COMPLETION_STAMPS[key] = stamp
    return stamp


def _declared_token_budget(record: Mapping[str, Any]) -> int | None:
    """The run's token-denominated allowance, or None when none is set.

    The budget lives on the node block as dispatch resolves and records it;
    a top-level mirror is accepted as a fallback so a hand-built or imported
    record that carries the value at the pointer root still reads it. A value
    that does not coerce to a positive integer is treated as unset rather
    than as a charge surface, so a malformed declaration degrades to the
    wall-clock behaviour instead of refusing to measure.
    """
    node = record.get("node")
    value = node.get("token_budget") if isinstance(node, Mapping) else None
    if value is None:
        value = record.get("token_budget")
    if value is None or value == "":
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    return budget if budget > 0 else None


def _generated_tokens(record: Mapping[str, Any]) -> int | None:
    """The run's recorded generated output tokens, or None when unmeasured.

    observe() folds the stream's measured throughput block into the pointer,
    so a run that has been observed carries its token total here. Absence is
    not a verdict: an unmeasured run is charged nothing, matching how a run
    with no stream is never called an overrun on elapsed either.
    """
    throughput = record.get("throughput")
    if not isinstance(throughput, Mapping):
        return None
    value = throughput.get("generated_tokens")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_rate(value: Any) -> float | None:
    """Read a measured rate without treating zero or a boolean as throughput."""
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


#: One rate entry per run: its completion instant and its measured rate.
_LaneRateEntry = tuple[float, float]
_LaneRateContributions = dict[tuple[str, str], dict[Any, _LaneRateEntry]]

_BUCKET_SECONDS = 300
_REFERENCE_WINDOW_SECONDS = 7 * 86400

#: Each project's last answered bucket and the contributions that answered it.
#: A bucket roll then reads only the rows whose newest stamp reaches past the
#: previous bucket's end, and drops the rows that aged out of the window,
#: instead of decoding the window again. A project this process has not seen
#: yet reads its window once. The population a project's window can hold is the
#: bound on memory, and the projects kept are capped like the read cache was.
_LANE_RATE_BUCKETS: dict[str, tuple[int, _LaneRateContributions]] = {}
_LANE_RATE_BUCKETS_LIMIT = 16


def _lane_rate_contributions(
    project: str, *, activity_since: float
) -> _LaneRateContributions | None:
    """The usable rows a bounded ledger read returns, keyed by lane and run.

    ``activity_since`` filters on the index's own activity scalar, which is the
    newest of a row's dispatch, launch and completion stamps, so a bound at a
    bucket's end admits every row that completed after it and any row that was
    touched after it. The completion instant is kept beside the rate because
    the caller drops a row when the window's start passes it. None means the
    read failed, which the caller treats as a reason to widen the read once. If
    the failure was a genuine defect, the fallback returns None too.
    """
    from reckon import ledger as ledger_module

    try:
        rows = ledger_module.load(project, activity_since=activity_since)[0]["runs"]
    except (ledger_module.LedgerError, OSError, ValueError, KeyError, TypeError):
        return None
    contributions: _LaneRateContributions = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        agent = row.get("agent")
        if not isinstance(agent, Mapping):
            continue
        completed = parse_utc(str(row.get("completed_at") or ""))
        if completed is None:
            continue
        throughput = row.get("throughput")
        if not isinstance(throughput, Mapping):
            continue
        rate = _positive_rate(throughput.get("tokens_per_second"))
        if rate is None:
            continue
        lane = (str(row.get("backend") or ""), str(agent.get("model") or ""))
        stamp, rate_value = completed.timestamp(), rate
        # A row is identified by its run id so a row the next bucket's read
        # returns again replaces its entry rather than counting twice. A row
        # without one — an imported or hand-built fixture — is identified by
        # the facts a lane's median sees, so distinct rows still count apart.
        identity: Any = str(row.get("run_id") or "") or (
            "",
            stamp,
            rate_value,
        )
        contributions.setdefault(lane, {})[identity] = (stamp, rate_value)
    return contributions


def _lane_reference_rates(
    project: str, window_bucket: int
) -> dict[tuple[str, str], tuple[float, ...]]:
    """Every backend and model's recent committed rates, from one ledger read.

    The rate a run is judged against is a per-lane median, but reading the
    ledger is the cost. One read per project answers every lane, and the read
    is bounded by the window it judges against rather than by the ledger's
    length, because a station that pays a whole window every five minutes on a
    long history runs out of memory.

    Between two consecutive buckets the window changes only by the rows that
    completed since the previous bucket and the rows that aged out of it, so a
    bucket roll reads the rows whose newest stamp reaches past the previous
    bucket's end and folds them into the previous bucket's contributions. A
    project with no previous bucket, or one that skipped a bucket, still reads
    its window once. Whatever the path, every lane's median is the median over
    exactly the rows completed inside the window.
    """
    end = window_bucket * _BUCKET_SECONDS
    start = end - _REFERENCE_WINDOW_SECONDS
    previous = _LANE_RATE_BUCKETS.get(project)
    if previous is not None and previous[0] == window_bucket:
        return _lane_rates_from(previous[1], start, end)
    contributions: _LaneRateContributions | None = None
    if previous is not None and previous[0] == window_bucket - 1:
        contributions = {lane: dict(entries) for lane, entries in previous[1].items()}
        added = _lane_rate_contributions(
            project, activity_since=previous[0] * _BUCKET_SECONDS
        )
        if added is None:
            contributions = None
        else:
            for lane, entries in added.items():
                contributions.setdefault(lane, {}).update(entries)
    if contributions is None:
        contributions = _lane_rate_contributions(project, activity_since=start)
    if contributions is None:
        _LANE_RATE_BUCKETS.pop(project, None)
        return {}
    _LANE_RATE_BUCKETS.pop(project, None)
    _LANE_RATE_BUCKETS[project] = (window_bucket, contributions)
    while len(_LANE_RATE_BUCKETS) > _LANE_RATE_BUCKETS_LIMIT:
        _LANE_RATE_BUCKETS.pop(next(iter(_LANE_RATE_BUCKETS)))
    return _lane_rates_from(contributions, start, end)


def _lane_rates_from(
    contributions: _LaneRateContributions, start: float, end: float
) -> dict[tuple[str, str], tuple[float, ...]]:
    """The window's rates by lane, in the order the ledger read would give."""
    rates: dict[tuple[str, str], tuple[float, ...]] = {}
    for lane, entries in contributions.items():
        kept = [
            (completed, run_id, rate)
            for run_id, (completed, rate) in entries.items()
            if start <= completed <= end
        ]
        if kept:
            kept.sort()
            rates[lane] = tuple(rate for _completed, _run_id, rate in kept)
    return rates


def _clear_lane_reference_rates() -> None:
    """Forget every project's bucket, so the next read widens to its window."""
    _LANE_RATE_BUCKETS.clear()


_lane_reference_rates.cache_clear = _clear_lane_reference_rates  # type: ignore[attr-defined]


def _historical_reference_rate(
    project: str, backend: str, model: str, window_bucket: int
) -> tuple[float | None, int]:
    """Median recent committed rate for the same backend and model.

    The five-minute bucket bounds repeated ledger reads by a live watcher. A
    small or absent cohort is not a reference; a run then keeps an unknown
    cause instead of inheriting another backend's or model's rate.
    """
    rates = _lane_reference_rates(project, window_bucket).get((backend, model), ())
    return (median(rates), len(rates)) if len(rates) >= 10 else (None, len(rates))


# Callers clear the reference cache through this name; the read it caches is
# the per-project one above.
_historical_reference_rate.cache_clear = _lane_reference_rates.cache_clear  # type: ignore[attr-defined]


def _budget_overrun_cause(
    record: Mapping[str, Any],
    timing: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
) -> dict[str, Any]:
    """Attribute an overrun using its rate beside a same-model reference.

    A slow generation rate is lane saturation in this operational vocabulary;
    it can also reflect a serving defect, so the rate and reference travel with
    the label. Normal-rate work is over-large only when its token allowance was
    exceeded or its generated volume would exceed the seconds allowance even
    at the reference rate. Anything unmeasured stays unknown.
    """
    if not timing.get("budget_overrun") and not timing.get("budget_overrun_seconds"):
        return {}
    throughput = record.get("throughput")
    rate = (
        _positive_rate(throughput.get("tokens_per_second"))
        if isinstance(throughput, Mapping)
        else None
    )
    project = str(record.get("project") or "")
    backend = str(record.get("backend") or "")
    agent = record.get("agent")
    model = str(agent.get("model") or "") if isinstance(agent, Mapping) else ""
    reference: float | None = None
    count = 0
    if rate is not None and project and backend and model:
        moment = _utc_seconds() if now_seconds is None else float(now_seconds)
        reference, count = _historical_reference_rate(
            project, backend, model, int(moment // 300)
        )
    cause = "unknown"
    if rate is not None and reference is not None:
        if rate < reference / 2:
            cause = "lane-saturated"
        else:
            tokens = _generated_tokens(record)
            budget_seconds = timing.get("budget_seconds")
            if timing.get("budget_overrun_tokens", 0) or (
                tokens is not None
                and isinstance(budget_seconds, (int, float))
                and budget_seconds > 0
                and tokens / reference > budget_seconds
            ):
                cause = "over-large"
    return {
        "budget_overrun_cause": cause,
        "budget_overrun_rate": rate,
        "budget_overrun_reference_rate": reference,
        "budget_overrun_reference_runs": count,
        "budget_overrun_reference_source": "recent committed runs, same backend and model",
    }


def _token_budget_timing(
    token_budget: int,
    generated_tokens: int | None,
    *,
    budget_seconds: int | None,
    elapsed_seconds: int | None,
) -> dict[str, Any]:
    """Measure a run against a token budget, keeping the seconds ceiling.

    The worker's own budget is denominated in generated tokens — the quantity
    the same task needs regardless of what else the lane is doing — so a slow
    lane inside its token budget is not an overrun however long it took, and
    a lane that delivered more tokens than the allowance is charged for the
    work. Wall clock cannot bound a process that stopped producing, so the
    seconds allowance survives here under its own name as the ceiling that
    still refuses such a run; the two verdicts never share a name.
    """
    wall_overrun = (
        max(0, int(elapsed_seconds) - int(budget_seconds))
        if elapsed_seconds is not None and budget_seconds is not None
        else 0
    )
    if generated_tokens is None:
        token_overrun = 0
    else:
        token_overrun = max(0, generated_tokens - token_budget)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed_seconds,
        "budget_overrun": generated_tokens is not None and token_overrun > 0,
        "budget_overrun_seconds": wall_overrun,
        "budget_tokens": token_budget,
        "generated_tokens": generated_tokens,
        "budget_overrun_tokens": token_overrun,
        "hang_ceiling_seconds": budget_seconds,
        "ceiling_overrun": wall_overrun > 0,
    }


def _parked_budget_timing(
    record: Mapping[str, Any],
    *,
    token_budget: int | None,
    budget_seconds: int | None,
    now_seconds: float | None = None,
) -> dict[str, Any]:
    """Measure a request that has not launched: no elapsed time, and its wait.

    A request is charged for the time it spent running, so a pointer that has
    never launched — queued, waiting on upstream work, blocked on it, or held —
    has spent none of its allowance however long it has sat there. The wait is
    still reported under its own key, measured from the request's creation, so
    a reader can see how long it has been parked without that figure being
    charged as budget.
    """
    created = parse_utc(str(record.get("created_at") or ""))
    waiting_seconds = None
    if created is not None:
        moment = _utc_seconds() if now_seconds is None else float(now_seconds)
        waiting_seconds = max(0, int(moment - created.timestamp()))
    if token_budget is not None:
        timing = _token_budget_timing(
            token_budget,
            _generated_tokens(record),
            budget_seconds=budget_seconds,
            elapsed_seconds=None,
        )
        # Nothing a request that never launched generated is chargeable.
        timing["budget_overrun"] = False
        timing["budget_overrun_tokens"] = 0
    else:
        timing = {
            "budget_seconds": budget_seconds,
            "elapsed_seconds": None,
            "budget_overrun": False,
            "budget_overrun_seconds": 0,
        }
    timing["waiting_seconds"] = waiting_seconds
    return timing


def _budget_timing(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> dict[str, Any]:
    """Measure one run against its declared allowance without mutating it.

    Elapsed time is charged from the run's first launch, never from the moment
    its request was created: a request that waited a week for its upstream work
    spent that week parked, not working. A pointer still in a waiting phase has
    no launch stamp, so it reports no elapsed time and no overrun, and carries
    its wait under ``waiting_seconds`` instead.

    A run whose worker process is gone has finished, so its elapsed is measured
    to its own stream completion — the same stamp promotion records — rather
    than to the moment of reading. A still-running run measures to now, and the
    wall-clock ceiling that protects the fleet from a hang is untouched because
    a live process still anchors here. A reader resolving a run late therefore
    reports the worker's own time, not the coordinator's wait to promote it.

    When the run carries a token budget, the budget verdict is denominated in
    generated tokens (the worker is charged for the work, not the queue) and
    the wall-clock allowance becomes the separately named hang ceiling. Without
    one, the wall-clock overrun is the only verdict, unchanged.
    """
    node = record.get("node") or {}
    token_budget = _declared_token_budget(record)
    budget_seconds: int | None = None
    waiting_phase = False
    started = None
    try:
        if "attempt_budget_seconds" in record:
            budget_seconds = int(record["attempt_budget_seconds"])
        else:
            budget_seconds = parse_duration(str(node.get("time_budget") or ""))
        launched = parse_utc(
            str(record.get("attempt_started_at") or record.get("launched_at") or "")
        )
        waiting_phase = str(record.get("phase") or "").strip() in WAITING_REQUEST_PHASES
        if launched is not None:
            started = launched
        elif waiting_phase:
            # A request in a waiting phase has never launched, so its creation
            # stamp measures the wait rather than any budget.
            started = None
        else:
            started = parse_utc(str(record.get("created_at") or ""))
    except (CrewError, TypeError, ValueError):
        started = None
    if waiting_phase and started is None:
        return _parked_budget_timing(
            record,
            token_budget=token_budget,
            budget_seconds=budget_seconds,
            now_seconds=now_seconds,
        )
    if started is None:
        if token_budget is not None:
            return _token_budget_timing(
                token_budget,
                _generated_tokens(record),
                budget_seconds=None,
                elapsed_seconds=None,
            )
        return {
            "budget_seconds": None,
            "elapsed_seconds": None,
            "budget_overrun": False,
            "budget_overrun_seconds": 0,
        }
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    elapsed_to = None
    if record.get("process_alive") is False:
        try:
            completion = _stream_completion_stamp(record)
        except (CrewError, OSError):
            completion = None
        if isinstance(completion, str) and completion:
            finished = parse_utc(completion)
            if finished is not None:
                elapsed_to = finished.timestamp()
    if elapsed_to is None:
        elapsed = max(0, int(moment - started.timestamp()))
    else:
        elapsed = max(0, int(elapsed_to - started.timestamp()))
    if token_budget is not None:
        return _token_budget_timing(
            token_budget,
            _generated_tokens(record),
            budget_seconds=budget_seconds,
            elapsed_seconds=elapsed,
        )
    overrun = max(0, elapsed - budget_seconds)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed,
        "budget_overrun": overrun > 0,
        "budget_overrun_seconds": overrun,
    }


def _apply_budget_watchdog(
    record: dict[str, Any], config: Mapping[str, Any] | None
) -> None:
    """Record deadline posture and optionally stop an over-grace CLI worker."""
    timing = _budget_timing(record)
    timing.update(_budget_overrun_cause(record, timing))
    record.update(timing)
    fences = (config or {}).get("fences") or {}
    if not fences.get("enforce_budget_watchdog"):
        return
    budget_seconds = timing["budget_seconds"]
    elapsed_seconds = timing["elapsed_seconds"]
    grace = resolved_grace_multiple(config)
    if grace is None:
        return
    if (
        budget_seconds is None
        or elapsed_seconds is None
        or elapsed_seconds <= budget_seconds * grace
        or record.get("launch") != "cli"
        or record.get("phase") in _TERMINAL_RUN_PHASES
        or record.get("process_alive") is not True
    ):
        return
    pid = record.get("pid")
    try:
        _signal_process_group(
            int(pid),
            record.get("pid_start_time"),
            run_dir=_run_directory(record),
            reason="budget-watchdog",
        )
    except (
        CrewError,
        ProcessLookupError,
        PermissionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        record["watchdog_detail"] = f"budget watchdog could not stop pid {pid}: {exc}"
        return
    record["phase"] = "stopped"
    record["stopped_at"] = _utc_now()
    record["watchdog_enforced"] = True
    record["detail"] = (
        f"budget watchdog stopped pid {pid} after {elapsed_seconds}s "
        f"against {budget_seconds}s with {grace:g}x grace"
    )


def _refusal_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalise a refusal budget block into the fields a blocked reason needs."""
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "quota"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _harness_command(record: Mapping[str, Any], argv: Any) -> str | None:
    """The command that names a cli run's harness, for a stream translation.

    A placed launch prefixes its resolved argv with the scheduler invocation, so
    ``argv[0]`` on such a record names the scheduler rather than the harness and
    a translation built from it fails. The record carries the harness the launch
    resolved under ``command``, captured before the placement wrapped the plan,
    so that field is taken first and ``argv[0]`` is the fallback for a record
    written before the field existed.
    """
    command = record.get("command")
    if command:
        return str(command)
    if isinstance(argv, list) and argv:
        return str(argv[0])
    dialect = record.get("dialect")
    return str(dialect) if dialect else None


def _observed_stream(
    record: Mapping[str, Any],
    *,
    memo: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One cli run's stream observation, served from a memo while it still holds.

    Two readers of a classification consult the same stream — the budget gates
    and the background-wait signal — and each would otherwise pay a full parse
    of it. They share this read, so one classification parses the stream once
    and the memo beside the pointer carries what it found, which is what makes a
    second classification of an unchanged run cost no parse at all.

    The memo's stream entry is served only while the file it was read from is
    still that file by identity. When it is not, the cursor carries the byte
    offset the last read reached and a fingerprint of the stream's opening, so the read
    resumes only while the stream still opens with that same fingerprint: an
    offset past the end of the file, a replaced stream, or one rewritten in place
    to a new opening all read from the first record, because an offset into a
    predecessor's bytes means nothing in a file that no longer holds them.
    """
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    command = _harness_command(record, dispatch_field(record, "argv"))
    if not command:
        return None
    from reckon import _backends

    stored = memo.get("stream") if memo is not None else None
    resume: dict[str, Any] | None = None
    if isinstance(stored, Mapping) and str(stored.get("path") or "") == str(log):
        state = stored.get("state")
        # An offset only means the same thing in the file it was reached in: a
        # stream replaced at this path since means nothing here, so a changed
        # inode re-reads from the first record while a grown one resumes.
        if isinstance(state, Mapping) and str(stored.get("inode") or "") == _file_inode(
            log
        ):
            resume = {
                "offset": int(stored.get("offset") or 0),
                "state": state,
                "head": stored.get("head"),
            }
        # What an observation is a function of is the file it was read from and
        # the lane it was translated for, so those are what an entry is served
        # against: an unchanged stream read for the same command and backend
        # cannot have a different observation, and one held by a memo written
        # before the key moved is still this run's own reading of this file.
        if (
            stored.get("ident") == _file_identity(log)
            and stored.get("command") == command
            and stored.get("backend") == str(record.get("backend") or "")
            and isinstance(stored.get("observation"), Mapping)
        ):
            return dict(stored.get("observation") or {})

    try:
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend={"command": command},
            log_path=log,
            resume=resume,
        )
    except (_backends.BackendError, CrewError, OSError, ValueError):
        # An unreadable or untranslatable stream carries no readable budget and
        # no final message; the manifest and liveness paths still classify it.
        return None
    seen = observation.as_dict()
    if memo is not None:
        offset = int(observation.stream_state.get("offset") or 0)
        memo["stream"] = {
            "path": str(log),
            "ident": _file_identity(log),
            "inode": _file_inode(log),
            "head": _backends.stream_head_fingerprint(log, offset=offset),
            "command": command,
            "backend": str(record.get("backend") or ""),
            "offset": offset,
            "state": observation.stream_state,
            "observation": seen,
        }
    return seen


def _stream_budget(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The budget block a cli run's stream records, folded in or read fresh.

    Shared by the refusal and retry-shape gates so the stream is parsed once
    even when both are consulted for the same run. observe() folds the stream's
    budget into the pointer, while the ticker reads raw pointers that have not
    been through observe; both paths resolve through the same backend
    translation, so they reach the same block and a ticker reading a raw
    pointer cannot disagree with observe's phase. The read itself, memo
    included, belongs to :func:`_observed_stream`.
    """
    budget = record.get("budget")
    if isinstance(budget, Mapping) and budget.get("refusal"):
        return budget
    seen = _observed_stream(record, memo=_memo_for(record))
    if seen is None:
        return None
    return seen.get("budget") or None


def _stream_refusal_block(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider refusal a cli run's stream records, folded in or read fresh.

    A spend or usage refusal is a block, not an abandonment: the account is not
    broken, only spent until a moment the refusal names. The block comes from
    the same budget the retry-shape gate reads, so the two dead-lane readings
    agree on one stream rather than each owning a separate translation.

    Declining is the load-bearing half. A stream that reports an ordinary
    failed turn — a bad model id, a lost stream, a context overflow — carries
    none of the recognised limit phrases and returns None, so a crash is never
    mistaken for a block.
    """
    budget = _stream_budget(record)
    if budget is not None and budget.get("refusal"):
        return _refusal_block(record, budget)
    return None


# A spent local lane's mid-flight shape, folded from the budget block the
# stream observer wrote: rate-limit retries counted with no terminal result, so
# the number is a magnitude and liveness is the verdict. The exhaustion shape
# (retries ended in a terminal error result) reads as a refusal instead, so the
# two dead-lane readings never overlap.
_RATE_LIMIT_RETRY_RE = re.compile(r"after (\d+) rate-limit retries")

# An exhausted unmetered lane folds no refusal at all: the observer writes
# refusal false with lane_backpressure true and a detail naming the retry count
# ("run died after N consumer-queue retries ...; the lane refused"), because a
# lane without a budget has nothing to refuse from. The marker is what a run
# that retried and recovered never carries, so it discriminates terminal
# exhaustion from routine retries.
_BACKPRESSURE_RETRY_RE = re.compile(r"run died after (\d+) consumer-queue retries")


def _stream_exhaustion_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Terminal retry exhaustion on an unmetered lane, as a refusal block.

    A spent unmetered consumer ends its retries in an error result, and the
    budget observer surfaces that terminal shape as ``lane_backpressure`` true
    with the retry count in the detail — not as a budget refusal, because the
    lane's budget is not what was spent. The marker is absent on a run that
    retried and recovered, so no block is reached for a live or successful run;
    only classify_pointer's dead-process hand joins the marker into a blocked
    reading, so the row names the lane and offers resume. ``budget`` is the
    block :func:`_stream_budget` already resolved, so the stream is parsed once
    regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    if not budget.get("lane_backpressure"):
        return None
    detail = str(budget.get("detail") or "")
    match = _BACKPRESSURE_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": "rate-limit",
        "resets_at": None,
        "retries": int(match.group(1)),
    }


def _stream_retry_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The mid-flight rate-limit retry shape budget carries, else None.

    The local lane reports a spent consumer as rate-limit ``api_retry`` records,
    and the budget observer surfaces the count in the block's detail with no
    terminal result ("no terminal result yet"). From the stream alone that
    shape is indistinguishable from a live worker mid-retry-burst, so no verdict
    is reached here: the block names the lane and the count, and only
    classify_pointer's dead-process hand joins it into a blocked reading. An
    alive worker mid-retry-burst (measured completing with seven retries) reads
    running, not blocked. ``budget`` is the block :func:`_stream_budget` already
    resolved, so the stream is parsed once regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    detail = str(budget.get("detail") or "")
    if "no terminal result yet" not in detail:
        return None
    match = _RATE_LIMIT_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "retries": int(match.group(1)),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


# The client substitutes this exact string into a message's model field when no
# model served the turn, so it is a marker rather than a model name.
_SYNTHETIC_MODEL = "<synthetic>"


def _assistant_refusal_text(message: Mapping[str, Any]) -> str:
    """The prose of a synthetic assistant message, or the empty string."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return " ".join(parts).strip()


def _result_turned_no_tokens(event: Mapping[str, Any]) -> bool:
    """Whether a result record reports an error turn that generated nothing.

    Every token counter zero and a zero API duration are what separate a turn
    the client refused before dispatching from one that ran and then failed —
    a failed turn still reports the tokens it spent and the API duration it
    waited on.
    """
    if event.get("is_error") is not True:
        return False
    try:
        if int(event.get("duration_api_ms") or 0) != 0:
            return False
        if int(event.get("num_turns") or 0) > 1:
            return False
    except (TypeError, ValueError):
        return False
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return False
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        try:
            if int(usage.get(key) or 0) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _admission_marks(
    state: tuple[str, str, bool], events: Sequence[Mapping[str, Any]]
) -> tuple[str, str, bool]:
    """Fold a stream's records into the marks an admission refusal leaves.

    The refusal text and the terminal reason are the last ones the stream
    states, and a zero-token result anywhere is remembered: a stream that wrote
    an attempt and then a retry does not lose the refusal of the attempt that
    was refused. Folding forward lets a later read extend the state it already
    holds instead of re-reading the stream from its first record.
    """
    refusal_reason, terminal_reason, zero_token_error = state
    for event in events:
        kind = str(event.get("type") or "")
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, Mapping):
                continue
            if str(message.get("model") or "") != _SYNTHETIC_MODEL:
                continue
            if str(event.get("error") or "") != "invalid_request":
                continue
            text = _assistant_refusal_text(message)
            if text:
                refusal_reason = text
        elif kind == "result":
            terminal_reason = str(event.get("terminal_reason") or "")
            if _result_turned_no_tokens(event):
                zero_token_error = True
    return refusal_reason, terminal_reason, zero_token_error


def _admission_scan(log: Path) -> tuple[str, str, bool]:
    """The admission marks for a stream, folding only the bytes it gained."""
    from reckon import _backends

    return _backends.cached_stream_fold(
        log, "admission", _admission_marks, ("", "", False)
    )


def _admission_refusal(
    record: Mapping[str, Any], *, memo: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The marks of a run the backend refused before serving its first turn.

    A refusal at admission ends the run in three lines: an assistant record
    whose model is the client's substitution for "no model served this turn"
    (the literal ``<synthetic>``), carrying ``error: invalid_request`` with the
    reason it refused; and a result record whose terminal reason is
    ``blocking_limit`` with a zero API duration and every token counter zero.
    Together they say no model was reached at all, which is a different stop
    from a worker whose process died mid-turn. The generic dead-process
    classification cannot say which happened, so this one names it and carries
    the paths a reader acts on.

    Requiring the zero-token result beside the synthetic message is deliberate:
    a stream that merely mentions the same words while doing real work returns
    None, and an ordinary failed turn — which reports the tokens it spent —
    cannot reach this reading. None means the ordinary dead-process arms
    classify the run, so this gate never widens them.

    The scan's result is memoised against the stream's stat identity. The marks
    come from the per-stream fold, which holds the stream's own record sequence
    and folds only the bytes a stream gained, so a grown stream costs its append
    and an unchanged stream costs nothing here.
    """
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    ident = _file_identity(log)
    cached = memo.get("admission") if memo is not None else None
    if isinstance(cached, Mapping) and str(cached.get("ident") or "") == ident:
        refusal = cached.get("refusal")
        return dict(refusal) if isinstance(refusal, Mapping) else None
    try:
        refusal_reason, terminal_reason, zero_token_error = _admission_scan(log)
        size = log.stat().st_size
    except OSError:
        return None
    _count_admission_bytes(size)
    refusal: dict[str, Any] | None = None
    if refusal_reason and terminal_reason == "blocking_limit" and zero_token_error:
        refusal = {
            "reason": refusal_reason,
            "terminal_reason": terminal_reason,
        }
    if memo is not None:
        memo["admission"] = {"ident": ident, "refusal": refusal}
    return refusal


def _budget_hold_block(
    record: Mapping[str, Any], budget: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A rate-limit event that rejected the turn, as a hold that ages out.

    A metered harness reports a spent window as ``rate_limit_event`` with
    ``status: rejected`` long before any prose refusal appears: the request was
    refused, the window names itself, and its reset is the moment time lifts the
    hold. This is distinct from :func:`_stream_refusal_block`, which reads a
    terminal prose or retry-exhaustion refusal, so the two never compete for the
    same run — a rejected window carries ``refusal`` false and reaches only
    this gate, while a refusal block is read through the other. ``budget`` is
    the block :func:`_stream_budget` already resolved.
    """
    if budget is None or budget.get("refusal"):
        return None
    if str(budget.get("threshold_status") or "").casefold() != "rejected":
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _blocked_session_resolution(
    record: Mapping[str, Any], run_id: str
) -> dict[str, Any]:
    """Resolve a blocked run's session without changing its evidence.

    Session resolution already has one ordered authority spanning the live
    pointer, the run's stream, and its promoted ledger row. Importing it only
    when a block needs a session answer avoids making routine classification consult
    durable history, while keeping this read pure: neither the pointer nor any
    of its evidence is rewritten here.
    """
    from reckon.crew.resumption import resolve_session

    return resolve_session(
        run_id,
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )


def _resume_remedy(resolution: Mapping[str, Any], run_id: str) -> dict[str, str] | None:
    """Return an executable recovery command when session evidence exists."""
    if not resolution.get("resolved"):
        return None
    return {
        "command": (f"reckon crew resume --run {run_id} --advice continue"),
        "session_id": str(resolution["session_id"]),
        "source": str(resolution["source"]),
    }


# A print-mode invocation makes exactly one turn and exits when it ends, so a
# worker still waiting on a background task at that moment leaves one of two
# traces rather than a clean result. The ceiling message is the harness's own
# stderr line when it gave up waiting and terminated the task itself. The
# duration is read from the environment and varies, so only the sentence
# around it is fixed.
_BACKGROUND_WAIT_CEILING_RE = re.compile(
    r"Background tasks still running after \d+s; terminating\.\s*"
    r"Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely\.",
)
# The agent's own last words when its turn ended before the background work
# it was waiting on did. Matched loosely around the fixed clause so a run
# naming a different suite or task still recognises the same shape.
_BACKGROUND_WAIT_FINAL_MESSAGE_RE = re.compile(
    r"waiting for (the )?background .+? before finalizing the manifest",
    re.IGNORECASE | re.DOTALL,
)


from .recovery_memo import (  # noqa: E402
    _count_admission_bytes,
    _file_identity,
    _file_inode,
    _memo_for,
)
from .recovery_wait import (  # noqa: E402
    _run_directory,
)
from .recovery_watch import (  # noqa: E402
    _utc_seconds,
)
