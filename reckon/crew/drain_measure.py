"""Measure what a queue drain did, from the records the drain kept.

A drain's guarantees are claims about history, not about the code that enforces
them: no request launched before the section it waits on landed, launches split
between projects in proportion to the declared fair-use weights, no waiting
request went an hour without a recorded admission verdict, and the runs that
landed through their own review were not coordinator turns. Each is answered
here from the durable records — the ledger's completed runs, the admission pass
log and the drain view — so a week can be read afterwards without the process
that ran it.

Every measure carries a ``status``. ``measured`` means the records answered the
question. ``unmeasured`` names the record that is missing, and it is never
reported as a zero: a counter that never saw an input and a counter that saw
none both print zero, and only one of those is a result. A drain whose
instrument was never connected would otherwise report a perfect week.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from reckon._plan_html import section_record_id
from reckon._timestamps import parse_utc
from reckon.ledger import is_unmetered_backend

#: How long a waiting request may go without a recorded admission verdict
#: before the silence is itself the finding.
VERDICT_SILENCE_SECONDS = 3600

#: The tolerance the share of launches is held to, as a fraction of the
#: weighted share each project should have received.
FAIR_SHARE_TOLERANCE = 0.15

#: Verdicts that mean a pass saw the request but did not launch it.
WAITING_VERDICTS = frozenset(
    {
        "awaiting-upstream",
        "blocked-upstream",
        "queued",
        "held",
        "held-for-allowance",
        "held-by-gate",
        "held-by-budget",
        "held-by-review",
        "retired",
    }
)

#: A review run carries its subject's section and is not held by that section's
#: waits, so its launch neither waits for an upstream nor violates the order.
#: Counting it was the measure's first defect: twelve of fourteen reported
#: launches were reviews of sections whose own waits did not apply to them.
UNHELD_ROLES = frozenset({"review"})

#: A run record may carry the reason its section wait was lifted before launch.
#: A string covers every wait the run declares; a mapping names a reason per
#: waited-on ref. Reading it is what lets an early launch be reported as allowed
#: rather than as a defect.
WAIVER_FIELD = "section_wait_waiver"

#: A run record may carry the waits it was dispatched under. A wait the plan
#: carries now and the run's own dispatch record does not is an edited wait:
#: the declaration moved after the request was queued, so the launch was never
#: held by it. A run that records no dispatch waits is judged as held by every
#: declared wait, which reports an unexplained early launch as a defect.
DISPATCHED_WAITS_FIELD = "dispatched_waits"

#: How a run records that its own review landed it, with no coordinator turn.
SELF_LANDED = frozenset({"review-self", "review-acceptance"})


def measure_drain(
    *,
    runs: Sequence[Mapping[str, Any]],
    depends_on: Mapping[tuple[str, str], Mapping[str, Sequence[str]]] | None = None,
    weights: Mapping[str, ...] | Sequence[float] | None = None,
    ready_by_project: Mapping[str, int] | None = None,
    unloaded_by_project: Mapping[str, Sequence[str]] | None = None,
    live_pointers: Sequence[Mapping[str, Any]] = (),
    passes: Sequence[Mapping[str, Any]] = (),
    now: datetime | None = None,
    window_days: int = 7,
    projects: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Answer every drain guarantee from one reading of the durable records.

    ``runs`` are completed run records (the ledger's rows). ``depends_on`` maps
    ``(project, plan)`` to that plan's section-wait declaration, as the plan
    head carries it. ``weights`` are the fair-use weights by project.
    ``ready_by_project`` and ``unloaded_by_project`` are the drain view's own
    figures: a project's executable remainder, and the sections it could not
    load. ``passes`` are admission pass records, oldest first.
    """
    moment = now or datetime.now(UTC)
    runs = list(runs)
    window_start = moment - timedelta(days=window_days)
    windowed = [run for run in runs if in_window(run, window_start, moment)]
    report: dict[str, Any] = {
        "window": {
            "start": window_start.isoformat(),
            "end": moment.isoformat(),
            "days": window_days,
        },
        "runs_in_window": len(windowed),
    }
    report["dag_order"] = measure_dag_order(windowed, depends_on or {}, history=runs)
    report["fair_share"] = measure_fair_share(
        windowed, weights=weights, ready_by_project=ready_by_project
    )
    report["verdict_gap"] = measure_verdict_gap(
        passes, window_start=window_start, now=moment
    )
    report["landed_without_a_turn"] = measure_landed_without_a_turn(windowed)
    report["unloaded_sections"] = measure_unloaded(unloaded_by_project or {})
    report["live_queue_interventions"] = measure_live_interventions(live_pointers)
    report["metered_routing"] = measure_metered_routing(windowed)
    report["coordinator_turns"] = _unmeasured(
        "an authoring turn is recorded as token counts with no turn identity, so "
        "two nodes authored in one turn and two nodes authored in separate turns "
        "are indistinguishable"
    )
    report["held_launch"] = _unmeasured(
        "a queue hold lives on the live request pointer, which is removed when the "
        "run launches, so no durable record shows a request was held at its launch"
    )
    report["bumped_priority"] = _unmeasured(
        "priority_override and priority_set_at live only on the live request "
        "pointer; a promoted run record carries neither"
    )
    report["edited_digest"] = _unmeasured(
        "the steering digest lives only on the live request pointer, so the text a "
        "launched request ran cannot be compared with the text it was dispatched with"
    )
    return report


def measure_dag_order(
    runs: Iterable[Mapping[str, Any]],
    depends_on: Mapping[tuple[str, str], Mapping[str, Sequence[str]]],
    *,
    history: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Count work runs launched before the landing of a section they wait on.

    Only a run the queue holds on its section waits is counted. A review run
    carries its subject's section and is never held by that section's waits, so
    a review run is skipped rather than reported: the measure's first reading
    counted twelve review launches as violations of a wait that did not bind
    them.

    A section's landing moment is the earliest completion recorded for that
    section, read from the run record that landed it. That is the earliest
    moment the work could have landed, so a launch is reported only when it
    preceded even that: the count is a lower bound on the violations, never an
    artefact of a landing recorded late.

    ``history`` is every run the landing moments may be read from. It defaults
    to ``runs``, and a caller measuring a window must pass the whole ledger:
    an upstream that landed before the window opened is landed, and reading
    only the window would report it as never having landed at all. Because a
    landing moment comes from an immutable completion record and the rows are
    sorted, two reads of one window return identical rows; a run added outside
    the window cannot move them.

    An early launch is reported by why it was allowed. A ``section_wait_waiver``
    recorded on the run names the reason the wait was lifted. A run whose
    ``dispatched_waits`` omit a wait the plan declares now was never held by
    it: the declaration was edited after the request was queued. An early
    launch with neither is a defect.
    """
    runs = list(runs)
    landed = landed_moments(history if history is not None else runs)
    checked = 0
    reviews_skipped = 0
    blocked_edges = 0
    violations: list[dict[str, Any]] = []
    waived: list[dict[str, Any]] = []
    edited: list[dict[str, Any]] = []
    for run in runs:
        if _unheld_role(run):
            reviews_skipped += 1
            continue
        project = str(run.get("project") or "")
        plan = _run_plan(run)
        section = _run_section(run)
        launched = launch_moments(run)
        edges = (depends_on.get((project, plan)) or {}).get(section) or ()
        if not edges:
            continue
        if not launched:
            blocked_edges += 1
            continue
        checked += 1
        for ref in edges:
            upstream = dependency_key(ref, default_project=project)
            upstream_moment = landed.get(upstream)
            if upstream_moment is not None and launched >= upstream_moment:
                continue
            row = {
                "run_id": str(run.get("run_id") or ""),
                "role": _run_role(run),
                "project": project,
                "plan": plan,
                "section": section,
                "waits_on": "{}#{}".format(*upstream[1:]),
                "launched_at": launched.isoformat(),
            }
            waiver = _recorded_waiver(run, ref, upstream)
            if waiver:
                row["reason"] = (
                    "launched before the waited-on section landed; the wait was "
                    "lifted by a recorded waiver"
                )
                row["waiver"] = waiver
                waived.append(row)
                continue
            if _wait_was_edited(run, ref, upstream):
                row["reason"] = (
                    "launched before the waited-on section landed; the request "
                    "was queued under an edited wait the plan no longer holds"
                )
                edited.append(row)
                continue
            if upstream_moment is None:
                row["reason"] = "the waited-on section never landed"
            else:
                row["reason"] = "launched before the waited-on section landed"
                row["seconds_early"] = round(
                    (upstream_moment - launched).total_seconds(), 3
                )
            violations.append(row)
    waived.sort(key=_row_order)
    edited.sort(key=_row_order)
    violations.sort(key=_row_order)
    return {
        "status": "measured",
        "basis": "launch compared with the earliest completion recorded for each "
        "declared upstream section, counting only runs the queue holds on those "
        "waits",
        "launches_with_declared_waits": checked,
        "launches_without_a_launch_moment": blocked_edges,
        "review_runs_not_counted": reviews_skipped,
        "violations": len(violations),
        "runs_in_violation": len({row["run_id"] for row in violations}),
        "waived_launches": len(waived),
        "waived_rows": waived[:50],
        "edited_wait_launches": len(edited),
        "edited_wait_rows": edited[:50],
        "allowed_early_launches": len(edited) + len(waived),
        "rows": violations[:50],
    }


def _run_role(run: Mapping[str, Any]) -> str:
    """The role a run records, defaulting to the implement role when it says none."""
    return str(run.get("role") or "implement")


def _unheld_role(run: Mapping[str, Any]) -> bool:
    """Whether the run's role is one the queue does not hold on section waits."""
    return _run_role(run) in UNHELD_ROLES


def _ref_keys(ref: str, upstream: tuple[str, str, str]) -> tuple[str, ...]:
    """Every spelling a recorded reason may use for one waited-on section."""
    return (ref, "{}#{}".format(*upstream[1:]))


def _recorded_waiver(
    run: Mapping[str, Any], ref: str, upstream: tuple[str, str, str]
) -> str:
    """The recorded reason this run's wait on ``ref`` was lifted, or nothing.

    A string on the run covers every wait it declares; a mapping names a reason
    per waited-on ref, in either the raw declaration's spelling or the
    ``plan#section`` form a reported row displays.
    """
    value = run.get(WAIVER_FIELD)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in _ref_keys(ref, upstream):
            reason = value.get(key)
            if reason:
                return str(reason).strip()
    return ""


def _wait_was_edited(
    run: Mapping[str, Any], ref: str, upstream: tuple[str, str, str]
) -> bool:
    """Whether the run was queued under a wait set that no longer names this edge.

    A run that records the waits it was dispatched under and does not name this
    edge was never held by it: the plan's declaration moved after the request
    was queued. A run that records no dispatch waits is judged as held by every
    declared wait, so an unexplained early launch stays a defect.
    """
    dispatched = run.get(DISPATCHED_WAITS_FIELD)
    if isinstance(dispatched, Mapping):
        present = {str(key) for key in dispatched}
    elif isinstance(dispatched, Sequence) and not isinstance(dispatched, str):
        present = {str(item) for item in dispatched}
    else:
        return False
    return not any(key in present for key in _ref_keys(ref, upstream))


def _row_order(row: Mapping[str, Any]) -> tuple[str, str]:
    """A stable order for reported rows, so two reads return the same list."""
    return (str(row.get("run_id") or ""), str(row.get("waits_on") or ""))


def measure_fair_share(
    runs: Iterable[Mapping[str, Any]],
    *,
    weights: Mapping[str, Any] | Sequence[float] | None = None,
    ready_by_project: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Compare each project's share of launches with its declared weight.

    Only projects the caller says had ready work are comparable, and only when
    at least two of them launched: one project with an empty queue cannot show
    whether the split was fair, it shows that the other project had the only
    work to do.
    """
    launches: Counter[str] = Counter()
    for run in runs:
        project = str(run.get("project") or "")
        if project and launch_moments(run):
            launches[project] += 1
    total = sum(launches.values())
    if not total:
        return {**_unmeasured("no launch was recorded in the window"), "launches": {}}
    declared = _weight_map(weights, launches)
    ready = ready_by_project or {}
    comparable = [p for p in launches if ready.get(p, 0) > 0]
    if len(comparable) < 2:
        return {
            "status": "unmeasured",
            "reason": "a weighted split is only visible while at least two "
            "projects hold ready work; the drain view reported no second project "
            "with an executable remainder",
            "launches": dict(launches),
            "weights": declared,
        }
    expected_total = sum(declared[p] for p in comparable)
    rows: dict[str, Any] = {}
    within = True
    observed_total = sum(launches[p] for p in comparable)
    for project in comparable:
        expected_share = declared[project] / expected_total
        observed_share = launches[project] / observed_total
        deviation = observed_share / expected_share - 1.0 if expected_share else 0.0
        within = within and abs(deviation) <= FAIR_SHARE_TOLERANCE
        rows[project] = {
            "launches": launches[project],
            "observed_share": round(observed_share, 4),
            "expected_share": round(expected_share, 4),
            "deviation": round(deviation, 4),
        }
    return {
        "status": "measured",
        "basis": "each comparable project's share of launches against its share "
        "of the declared weights, while it held an executable remainder",
        "readiness_observed": "at measurement time; no snapshot records which "
        "projects were loaded earlier in the window",
        "tolerance": FAIR_SHARE_TOLERANCE,
        "within_tolerance": within,
        "projects_without_ready_work": {
            project: count
            for project, count in launches.items()
            if project not in comparable
        },
        "projects": rows,
    }


def measure_verdict_gap(
    passes: Sequence[Mapping[str, Any]],
    *,
    window_start: datetime,
    now: datetime,
    limit_seconds: int = VERDICT_SILENCE_SECONDS,
) -> dict[str, Any]:
    """Find waiting requests that went longer than the limit without a verdict.

    The pass log is the only record of what admission decided. A request that
    waited across a stretch the log does not cover has no verdict for that
    stretch, whether or not a pass ran, so the log's own span is reported beside
    the result: a window the log does not cover cannot support the claim.
    """
    rows: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    stamps: list[datetime] = []
    for record in passes:
        at = parse_utc(record.get("at"))
        if at is None or at < window_start or at > now:
            continue
        stamps.append(at)
        for pointer in record.get("pointers") or ():
            if not isinstance(pointer, Mapping):
                continue
            run_id = str(pointer.get("run_id") or "")
            verdict = str(pointer.get("verdict") or "")
            if run_id and verdict in WAITING_VERDICTS:
                rows[run_id].append((at, verdict))
    if not stamps:
        return {
            **_unmeasured("the admission pass log holds nothing inside the window"),
            "log_span_seconds": 0,
        }
    coverage_start = min(stamps)
    coverage_end = max(stamps)
    gaps: list[dict[str, Any]] = []
    widest = 0.0
    for run_id, verdicts in rows.items():
        verdicts.sort()
        for (earlier_at, _), (later_at, _) in pairwise(verdicts):
            gap = (later_at - earlier_at).total_seconds()
            widest = max(widest, gap)
            if gap > limit_seconds:
                gaps.append(
                    {
                        "run_id": run_id,
                        "silent_seconds": round(gap, 3),
                        "from": earlier_at.isoformat(),
                        "to": later_at.isoformat(),
                    }
                )
    uncovered = (coverage_start - window_start).total_seconds()
    result: dict[str, Any] = {
        "status": "measured",
        "limit_seconds": limit_seconds,
        "waiting_requests": len(rows),
        "violations": len(gaps),
        "widest_gap_seconds": round(widest, 3),
        "log_span_seconds": round((coverage_end - coverage_start).total_seconds(), 3),
        "log_starts_after_window_start_seconds": round(uncovered, 3),
        "rows": gaps[:50],
    }
    if uncovered > limit_seconds:
        result["status"] = "unmeasured"
        result["reason"] = (
            f"the pass log opens {round(uncovered, 3)}s after the window does, so a request that "
            "waited across the start of the window has no rows to read; the "
            "figures below describe only the covered span"
        )
    return result


def measure_landed_without_a_turn(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count the runs whose own review landed them, with no coordinator turn."""
    rows = list(runs)
    if not rows:
        return {**_unmeasured("no run was recorded in the window"), "runs": 0}
    by_promoter: Counter[str] = Counter(
        str(run.get("promoted_by") or "") for run in rows
    )
    self_landed = sum(by_promoter[value] for value in SELF_LANDED)
    return {
        "status": "measured",
        "basis": "the promoter a run records when it lands",
        "runs": len(rows),
        "landed_without_a_coordinator_turn": self_landed,
        "share": round(self_landed / len(rows), 4),
        "by_promoter": {key or "(none)": value for key, value in by_promoter.items()},
    }


def measure_live_interventions(
    pointers: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Report the interventions the live queue is carrying right now.

    A priority override, a queue hold and a steering digest are each recorded on
    the request pointer and nowhere else, so the queue at this moment is the only
    place they can be observed. Their presence here is evidence the queue carries
    them; their disappearance at launch is why the launch cannot be checked
    against them afterwards.
    """
    rows = list(pointers)
    if not rows:
        return _unmeasured("no live request pointer was read")
    return {
        "status": "measured",
        "basis": "the live request pointers, which are removed when a run launches",
        "requests": len(rows),
        "with_a_priority_override": sum(
            1 for row in rows if row.get("priority_override") is not None
        ),
        "held": sum(1 for row in rows if row.get("queue_hold")),
        "edited": sum(1 for row in rows if row.get("steering_sha256")),
    }


def measure_unloaded(
    unloaded_by_project: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Report sections the drain view could not load, per project."""
    sections = {
        str(project): sorted(str(section) for section in section_ids)
        for project, section_ids in unloaded_by_project.items()
        if section_ids
    }
    if not unloaded_by_project:
        return {
            "status": "unmeasured",
            "reason": "no drain view was read, so no project's loading state was seen",
        }
    return {
        "status": "measured",
        "projects_read": len(unloaded_by_project),
        "plans": len(sections),
        "sections": sum(len(ids) for ids in sections.values()),
        "unloaded": sections,
    }


def measure_metered_routing(runs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Count metered launches and the fallbacks that were recorded with a reason.

    Whether a metered request launched the moment it became ready needs the
    moment it became ready, which no durable record keeps; the counts and the
    recorded fallback reasons are what the ledger can answer.
    """
    rows = list(runs)
    by_backend: Counter[str] = Counter(
        str(run.get("backend") or run.get("lane") or "") for run in rows
    )
    local = {
        backend: count
        for backend, count in by_backend.items()
        if backend and is_unmetered_backend(backend)
    }
    metered = sum(
        count
        for backend, count in by_backend.items()
        if backend and not is_unmetered_backend(backend)
    )
    fallbacks: list[dict[str, str]] = []
    for run in rows:
        selection = run.get("picker_selection")
        if not isinstance(selection, Mapping):
            continue
        reason = selection.get("fallback_reason")
        if reason:
            fallbacks.append(
                {
                    "run_id": str(run.get("run_id") or ""),
                    "backend": str(selection.get("backend") or ""),
                    "reason": str(reason),
                }
            )
    return {
        "status": "measured" if rows else "unmeasured",
        "reason": "" if rows else "no run was recorded in the window",
        "launches_by_backend": dict(sorted(by_backend.items())),
        "metered_launches": metered,
        "local_launches": sum(local.values()),
        "recorded_fallbacks": len(fallbacks),
        "fallbacks": fallbacks[:50],
        "unmeasured": (
            [
                (
                    "the moment a request became ready is not recorded, so how "
                    "long a metered request waited for a slot cannot be measured"
                )
            ]
            if rows
            else []
        ),
    }


def launch_moments(record: Mapping[str, Any]) -> datetime | None:
    """When a run's worker started, or the dispatch moment when that is unrecorded."""
    worker_exit = record.get("worker_exit")
    if isinstance(worker_exit, Mapping):
        launched = parse_utc(worker_exit.get("launched_at"))
        if launched is not None:
            return launched
    return parse_utc(record.get("dispatched_at"))


def landed_moments(
    runs: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], datetime]:
    """The earliest completion recorded for each (project, plan, section)."""
    landed: dict[tuple[str, str, str], datetime] = {}
    for run in runs:
        completed = parse_utc(run.get("completed_at"))
        if completed is None:
            continue
        key = (
            str(run.get("project") or ""),
            _run_plan(run),
            _run_section(run),
        )
        if not key[1] or not key[2]:
            continue
        if key not in landed or completed < landed[key]:
            landed[key] = completed
    return landed


def dependency_key(ref: str, *, default_project: str) -> tuple[str, str, str]:
    """Resolve one declared dependency reference to (project, plan, section).

    A reference reads ``plan#section`` inside its own project, or
    ``project:plan#section`` when it crosses one.
    """
    text = str(ref or "").strip()
    project = default_project
    if ":" in text.split("#", 1)[0]:
        project, text = text.split(":", 1)
    plan, _, section = text.partition("#")
    return (project.strip(), plan.strip(), section_record_id(section))


def _run_plan(run: Mapping[str, Any]) -> str:
    definition = run.get("node_definition")
    if isinstance(definition, Mapping):
        plan = str(definition.get("plan") or "")
        if plan:
            return plan
    return str(run.get("plan") or "")


def _run_section(run: Mapping[str, Any]) -> str:
    definition = run.get("node_definition")
    raw: object = ""
    if isinstance(definition, Mapping):
        raw = definition.get("section")
    if not raw:
        raw = run.get("section")
    return section_record_id(raw)


def in_window(run: Mapping[str, Any], start: datetime, end: datetime) -> bool:
    moment = launch_moments(run) or parse_utc(run.get("completed_at"))
    return moment is not None and start <= moment <= end


def _weight_map(
    weights: Mapping[str, Any] | Sequence[float] | None,
    launches: Mapping[str, int],
) -> dict[str, float]:
    if isinstance(weights, Mapping):
        return {
            str(project): float(weights.get(project, 1.0) or 1.0)
            for project in launches
        }
    return {str(project): 1.0 for project in launches}


def _unmeasured(reason: str) -> dict[str, Any]:
    return {"status": "unmeasured", "reason": reason}
