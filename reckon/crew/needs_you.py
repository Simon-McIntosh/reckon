"""The runs of one session that need the coordinator, and the line that names them.

One derivation serves two readers: the follower's standing pane line and, behind
it, the row it composes at the end of an arming. Both must name the same runs,
so the list is composed here and never re-derived at a call site.

A run needs the coordinator when its state is in the fleet partition's blocked
bucket or its unpromoted bucket, both defined beside the fleet counts in
:mod:`reckon.crew.recovery_watch` rather than restated here, so the line can
never disagree with the counters beside it. A blocked run whose refusal has
already been accepted (the ``held`` classification) is waiting, not blocked, and
is left out exactly as the counters leave it out.

Every row names the node, the state it holds now and how long it has held it. An
unpromoted run whose review is already stored is marked ``reviewed``: promotion
unlinks the pointer and the session it captured, so a review read after the fact
can no longer be answered by resuming its author, and the mark is the live
window's only warning that the chance to resume has gone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# The order the line prints its groups in, and the word each group carries.
# Unpromoted work leads because a delivered run is one promotion away from
# landing, where a blocked run may need a repair first.
NEEDS_YOU_KINDS = ("unpromoted", "blocked")

# The follower re-emits the line on this cadence even when the set is empty, so
# a quiet pane is a heartbeat rather than a silence a dead follower also shows.
DEFAULT_REMIND_SECONDS = 600.0


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
    :func:`reckon.crew.recovery_watch._fleet_counts`, so the line and the
    counters beside it cannot disagree about which runs they are naming. The
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
    # Oldest first within a kind, so the run that has needed attention longest
    # leads its group.
    rows.sort(key=lambda row: (-float(row["age_seconds"]), str(row["node"])))
    return rows, counts


def _is_reviewed(project: str, run_id: str) -> bool:
    """Whether a review of this run is already stored against its staging store."""
    if not run_id:
        return False
    from reckon.crew import review as review_module

    try:
        return bool(review_module.stored_records_for_run(project, run_id))
    except Exception:  # noqa: BLE001 - an unreadable store must not silence the line
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


def needs_you_runs(project: str, *, session: str | None) -> list[dict[str, Any]]:
    """The runs alone, for a caller that composes its own line."""
    return needs_you(project, session=session)["runs"]


def needs_you_signature(summary: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """The identity of the set, so an emitter fires on change and not on repaint.

    Two summaries with the same signature name the same runs in the same states,
    so a line composed for one is the line the other would compose.
    """
    return tuple(
        sorted(
            (
                str(row.get("run_id") or ""),
                str(row.get("kind") or ""),
                str(row.get("state") or ""),
            )
            for row in summary.get("runs") or ()
        )
    )


def _entry(row: Mapping[str, Any]) -> str:
    text = f"{row.get('node')} {_elapsed(float(row.get('age_seconds') or 0.0))}"
    if row.get("reviewed"):
        text = f"{text} reviewed"
    return text


def needs_you_line(summary: Mapping[str, Any]) -> str:
    """Compose the one plain-text line, reading fully without colour.

    With nothing outstanding it is the cadence heartbeat, naming the session's
    own working count so a quiet pane is distinguishable from a dead one. With
    runs outstanding each group leads with its count and its word.
    """
    runs: Iterable[Mapping[str, Any]] = summary.get("runs") or ()
    groups: list[str] = []
    for kind in NEEDS_YOU_KINDS:
        members = [row for row in runs if str(row.get("kind") or "") == kind]
        if not members:
            continue
        entries = ", ".join(_entry(row) for row in members)
        groups.append(f"{len(members)} {kind} {entries}")
    if not groups:
        return f"yours: {int(summary.get('working') or 0)} working · nothing needs you"
    return "needs you: " + " · ".join(groups)
