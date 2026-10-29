"""The runs of one session that need the coordinator, as rows and counts.

This is the derivation behind the obligations view: every run the session still
owes action on, with the node it runs on, the state it holds now and how long it
has held it. A coordinator reads the names and next commands from that view, and
the pane's own rows carry the counts, so the pane prints no line of its own.

A run needs the coordinator when its state is in the fleet partition's blocked
bucket or its unpromoted bucket, both defined beside the fleet counts in
:mod:`reckon.crew.recovery_watch` rather than restated here, so this list can
never disagree with the counters beside it. A blocked run whose refusal has
already been accepted (the ``held`` classification) is waiting, not blocked, and
is left out exactly as the counters leave it out.

One request joins this list on the record's authority rather than the
partition's: a queued request an admission pass recorded as stalled. It is
counted as waiting by the partition, because a queued run is waiting, and the
stall is the record that its wait is not accounted for by its lane — nothing
about it is held, and the lane had room. It is reported under the blocked kind,
which is what the coordinator owes action on, until a later verdict clears it.

A run the coordinator has deferred is left out too, through the same reader the
obligations view defers it by. A blocked or unpromoted run the coordinator has
put off until an instant is not owed to them before that instant. The deferral
is read and not re-implemented, and it expires on its own: the instant passes
and the run is owed again.

Every row names the node, the state it holds now and how long it has held it. An
unpromoted run whose review is already stored is marked ``reviewed``: promotion
unlinks the pointer and the session it captured, so a review read after the fact
can no longer be answered by resuming its author, and the mark is the live
window's only warning that the chance to resume has gone.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _elapsed(seconds: float) -> str:
    """Render an age the way the pane renders every other elapsed span."""
    from reckon.crew import ticker as ticker_module

    return ticker_module._elapsed(max(0.0, float(seconds)))


def _state_entered_seconds(
    stream_path: Path | None, state: str, *, moment: float
) -> float | None:
    """The instant the run's own stream first recorded the state it holds now.

    A row the pane re-announces carries the clock the run's own record gave it,
    so a line that says how long a run has held its state reads the same clock
    rather than the moment the line happened to be composed. The recorded time
    is the earliest stamp in the unbroken trailing run of events whose
    ``to_state`` equals the state: a re-emitted baseline repeats a state the run
    already sits in and extends the run without moving it, while a genuine
    change opens a new run and stamps it.

    Returns ``None`` when the stream is absent or records no such transition, so
    the caller measures the age from the only other clock the run carries.
    """
    if stream_path is None or not state:
        return None
    from reckon.crew import recovery as recovery_module
    from reckon.crew import runs as runs_module

    try:
        text = Path(stream_path).read_text(encoding="utf-8")
    except OSError:
        return None
    earliest: float | None = None
    trailing_state: str | None = None
    for line in text.splitlines():
        event = runs_module.parse_stream_line(line)
        if not event or event.get("legacy"):
            continue
        word = str(event.get("to_state") or "")
        if not word:
            continue
        if word != trailing_state:
            trailing_state = word
            earliest = None
        if trailing_state != state:
            continue
        stamp = recovery_module.parse_utc(str(event.get("observed_at") or ""))
        if stamp is None:
            continue
        if earliest is None or stamp < earliest:
            earliest = stamp
    if earliest is None:
        return None
    return earliest.timestamp()


def _created_seconds(pointer: Mapping[str, Any], *, moment: float) -> float:
    """The run's creation instant, the fallback clock when its stream is silent."""
    from reckon.crew import recovery as recovery_module

    stamp = recovery_module.parse_utc(str(pointer.get("created_at") or ""))
    return moment if stamp is None else stamp.timestamp()


def _snapshots(
    project: str, *, moment: float
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Every live pointer of the project: its watch snapshot, and the pointer.

    The snapshot is what the fleet partition and its counters read, so it is
    what this module hands them. The pointer is returned beside it because a
    run joins the set with no dated transition often enough to matter — a stall
    writes no ``blocked`` row, and a run that never streamed has no stream to
    date it from — and the snapshot carries no creation instant, so the run's
    own pointer is the only other clock such a row can age from.
    """
    from reckon.crew import recovery as recovery_module
    from reckon.crew import runs as runs_module

    stall_seconds = recovery_module.parse_duration(
        recovery_module.DEFAULT_WATCH_STALL_WINDOW
    )
    snapshots: dict[str, dict[str, Any]] = {}
    pointers: dict[str, dict[str, Any]] = {}
    for pointer in runs_module._list_live_records(project=project):
        run_id = str(pointer.get("run_id") or "")
        if not run_id:
            continue
        snapshots[run_id] = recovery_module._watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds
        )
        pointers[run_id] = dict(pointer)
    return snapshots, pointers


def _deferred_run_ids(project: str, *, moment: float) -> frozenset[str]:
    """The runs whose recorded deferral is still in force at ``moment``.

    Read from the obligations view's own deferral reader rather than from a
    second copy of its rules, so a run the coordinator has deferred is held out
    of this line exactly as it is held out of that view. The reader takes the
    instant rather than reading one itself, so a caller that composes the line
    for a moment composes the deferral for the same moment.

    An unreadable deadline is never a reason to withhold a run: the reader
    treats it as undeferred, so the run stays in the set.
    """
    from datetime import UTC, datetime

    from reckon.crew import obligations as obligations_module

    in_force = obligations_module._acknowledgements_in_force(
        project, now=datetime.fromtimestamp(float(moment), tz=UTC)
    )
    return frozenset(in_force)


def _stall_verdicts() -> dict[str, dict[str, Any]]:
    """The requests an admission pass recorded as stalled, by run id.

    Read from the verdict log the admission service writes, where each request's
    last verdict replaces the one before it. A stall therefore stops reading as
    one the moment a later pass records a different verdict for the request —
    the pass that launches it, or one that names the hold that replaced it — so
    a stall is reported once and clears on the record rather than on a second
    judgement made here.
    """
    from reckon.crew import queue_admission as admission_module
    from reckon.crew import runs as runs_module

    path = runs_module.crew_home() / admission_module.ADMISSION_VERDICT_LOG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        for row in record.get("pointers") or ():
            run_id = str(row.get("run_id") or "")
            if not run_id:
                continue
            if str(row.get("verdict") or "") == admission_module.STALL_VERDICT:
                latest[run_id] = {
                    "at": str(record.get("at") or ""),
                    "stage": str(row.get("stage") or ""),
                    "stage_seconds": row.get("stage_seconds"),
                    "age_seconds": row.get("age_seconds"),
                }
            else:
                latest.pop(run_id, None)
    return latest


def _stall_rows(
    pointers: Mapping[str, Mapping[str, Any]],
    *,
    session: str | None,
    already: set[str],
) -> list[dict[str, Any]]:
    """One row per request a recorded stall leaves waiting for its session.

    A stalled request is queued, so the fleet partition counts it as waiting and
    it reaches no row of its own; the stall is the record that says the wait is
    not accounted for by its lane. The row keeps the blocked kind because that
    is what the coordinator owes action on, and it carries the stage the stall
    named so the next command can be chosen from the record rather than
    inferred.
    """
    rows: list[dict[str, Any]] = []
    for run_id, stall in _stall_verdicts().items():
        if run_id in already:
            continue
        pointer = pointers.get(run_id)
        if pointer is None:
            continue
        owner = str(pointer.get("session") or "")
        if session is not None and owner != session:
            continue
        age = stall.get("age_seconds")
        if not isinstance(age, (int, float)):
            age = 0.0
        node = pointer.get("node")
        if not isinstance(node, Mapping):
            node = {}
        rows.append(
            {
                "run_id": run_id,
                "node": str(
                    node.get("id") or pointer.get("node_id") or run_id or "unknown"
                ),
                "state": str(pointer.get("phase") or ""),
                "kind": "blocked",
                "age_seconds": max(0.0, float(age)),
                "reviewed": False,
                "stalled": True,
                "stage": str(stall.get("stage") or ""),
                "stage_seconds": stall.get("stage_seconds"),
                "stalled_at": str(stall.get("at") or ""),
            }
        )
    return rows


def needs_you_rows(
    project: str,
    *,
    session: str | None,
    moment: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The rows that need the coordinator, beside the counters that count them.

    Both halves come from the fleet partition the pane's own counter reads:
    ownership through :func:`reckon.crew.recovery_watch._partition_session_rows`
    and the working, blocked and unpromoted tallies through
    :func:`reckon.crew.recovery_watch._fleet_counts`, so the rows and the
    counters beside them cannot disagree about which runs they are naming. The
    states themselves are read from that module's ``FLEET_*`` buckets rather
    than restated here, and a run whose refusal has been accepted is held out of
    the blocked bucket exactly as the counter holds it out.

    Returns the rows, and the counters they must agree with.
    """
    from reckon.crew import recovery as recovery_module

    at = recovery_module._utc_seconds() if moment is None else float(moment)
    snapshots, pointers = _snapshots(project, moment=at)
    counts = recovery_module._fleet_counts(snapshots, session=session)
    own, _peers = recovery_module._partition_session_rows(snapshots.values(), session)
    blocked_states = frozenset(recovery_module.FLEET_BLOCKED_STATES)
    unpromoted_states = frozenset(recovery_module.FLEET_UNPROMOTED_STATES)
    rows: list[dict[str, Any]] = []
    for snapshot in own:
        state = str(snapshot.get("state") or "")
        classification = str(snapshot.get("recovery_classification") or "")
        if state in blocked_states and classification != "held":
            kind = "blocked"
        elif state in unpromoted_states:
            kind = "unpromoted"
        else:
            continue
        run_id = str(snapshot.get("run_id") or "")
        stream = snapshot.get("stall_stream_path")
        entered = _state_entered_seconds(
            Path(stream) if stream else None, state, moment=at
        )
        if entered is None:
            # The snapshot carries no creation instant, so the run's own pointer
            # is the only clock left for a run that joined the set with no dated
            # transition — a stall writes no ``blocked`` row — and no stream to
            # date it from. Handing the snapshot here reads an absent key and
            # ages every such row at zero.
            entered = _created_seconds(pointers.get(run_id) or {}, moment=at)
        rows.append(
            {
                "run_id": run_id,
                "node": str(snapshot.get("node") or run_id or "unknown"),
                "state": state,
                "kind": kind,
                "age_seconds": max(0.0, at - entered),
                "reviewed": kind == "unpromoted" and _is_reviewed(project, run_id),
            }
        )
    # A request an admission pass recorded as stalled is queued, so the fleet
    # partition holds it out of both buckets and it reaches no row of its own.
    # The stall is the record that its wait is not accounted for by its lane, so
    # it joins the blocked rows here until a later verdict clears it.
    rows.extend(
        _stall_rows(
            pointers,
            session=session,
            already={str(row["run_id"]) for row in rows},
        )
    )
    # Oldest first within a kind, so the run that has needed attention longest
    # leads its group.
    rows.sort(key=lambda row: (-float(row["age_seconds"]), str(row["node"])))
    # A deferred run is not owed to the coordinator until its deferral expires,
    # so it joins the rows once the instant has passed. The filter sits here
    # rather than in each reader so the obligations view and the counters
    # cannot disagree about which runs the coordinator still owes.
    deferred = _deferred_run_ids(project, moment=at)
    rows = [row for row in rows if row["run_id"] not in deferred]
    return rows, counts


def _is_reviewed(project: str, run_id: str) -> bool:
    """Whether a review of this run is already stored against its staging store."""
    if not run_id:
        return False
    from reckon.crew import review as review_module

    try:
        return bool(review_module.stored_records_for_run(project, run_id))
    except Exception:  # noqa: BLE001 - an unreadable store must not hide the run
        return False


def needs_you(
    project: str,
    *,
    session: str | None,
    moment: float | None = None,
) -> dict[str, Any]:
    """The owning session's runs that need action, and its working count.

    Read at the moment of composition rather than accumulated from a stream: a
    run falls into a needs-action state whether or not a producer ever wrote a
    transition for it, so a run that finished while nothing was attached would
    returns by nobody if the list were built from rows.

    The population is the owning session's own runs; a pointer with no recorded
    owner stays in the counted set, matching the dispatch fence, because absence
    cannot prove it belongs to a peer.
    """
    rows, counts = needs_you_rows(project, session=session, moment=moment)
    return {"working": int(counts.get("working") or 0), "runs": rows}


def needs_you_runs(
    project: str,
    *,
    session: str | None,
    moment: float | None = None,
) -> list[dict[str, Any]]:
    """The runs alone, for a caller that wants no counts beside them.

    ``moment`` is the instant the set is composed for, defaulting to now; a
    deferral is in force at that instant, so a caller that composes the same
    moment twice composes the same set.
    """
    return needs_you(project, session=session, moment=moment)["runs"]
