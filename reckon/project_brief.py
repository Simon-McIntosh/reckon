"""One brief an agent reads before choosing work.

The roadmap answers a question about a project by returning a payload: the
ready and blocked plans, the ranked section rows, the critical path through
:mod:`reckon.roadmap_ranking`'s graph, and the wiring findings. That payload is
correct and large. A coordinator opening a project needs the few lines of it
that decide what to do next, with the rows behind those lines intact so a
reader can act on one without a second call.

This module composes that page. It reads the payload the roadmap produces and
adds no analysis of its own: every figure in the brief is a field of the
payload, and the typed rows behind each line are those fields, so a brief and
the roadmap cannot disagree. The brief is bounded, so it is a composition
rather than a second analyser — a fact that is not in the payload is not in the
brief.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from reckon._timestamps import parse_utc

#: The plain-text budget one brief composes within. A brief that would exceed it
#: is a defect in the composition rather than a rendering choice, so the guard
#: below refuses rather than truncating a line a reader needs.
BRIEF_CHAR_BUDGET = 6_000

#: The signals the payload classifies runs with, read through the shared
#: classifier rather than re-derived here.
BLOCKED_RUN = "blocked"
INTERRUPTED_RUN = "interrupted"

#: How many ranked ready sections and wiring findings the brief carries. The
#: counts of each are reported in full beside the leading entries.
TOP_READY_SECTIONS = 10
TOP_WIRING_FINDINGS = 3

#: How many of the ready sections releasing the most work are named beside the
#: ranked list, so a reader sees which one opens the most before reading down.
TOP_REACH_SECTIONS = 3

#: The share of open plans carrying one ROI value above which the concentration
#: is worth a reader's attention, because the field no longer orders work.
ROI_SHARE_THRESHOLD = 0.8

#: A run blocked for longer than this has stopped being a passing hold.
BLOCKED_RUN_SECONDS = 86_400.0


@dataclass(frozen=True)
class BriefLine:
    """One line of a brief and the payload rows behind it.

    ``key`` names the line for a reader (and for the parity check), ``text`` is
    what prints, and ``values`` holds the payload figures the line reports.
    """

    key: str
    text: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ProjectBrief:
    """The composed page, its lines, and the rows behind them."""

    project: str
    text: str
    lines: tuple[BriefLine, ...]

    def line(self, key: str) -> BriefLine | None:
        for line in self.lines:
            if line.key == key:
                return line
        return None

    def row(self, key: str) -> Mapping[str, Any]:
        line = self.line(key)
        return line.values if line is not None else {}


def project_brief(
    project: str,
    since: str | None = None,
    *,
    payload: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> ProjectBrief:
    """Compose the brief for one project from its roadmap payload.

    ``payload`` is the roadmap report the caller already holds; when it is
    omitted the brief reads one through the same route the CLI's roadmap verb
    uses, so a caller on the command line pays for one scan. ``since`` is an ISO
    timestamp: given, the brief adds the work that started or closed after it.
    ``now`` is the instant run ages are measured against and defaults to the
    wall clock, so a caller may pin it in a test.
    """

    report = payload if payload is not None else _load_payload(project)
    moment = now or datetime.now(UTC)
    lines = _compose(report, since=since, now=moment)
    text = "\n".join(line.text for line in lines)
    if len(text) > BRIEF_CHAR_BUDGET:
        raise ValueError(
            f"brief for {report.get('project') or project} is {len(text)} "
            f"characters, over the {BRIEF_CHAR_BUDGET} budget"
        )
    return ProjectBrief(
        project=str(report.get("project") or project),
        text=text,
        lines=tuple(lines),
    )


def _load_payload(project: str) -> Mapping[str, Any]:
    """Read one project's roadmap payload the way the CLI's verb does."""

    from reckon.mcp import _roadmap

    result = _roadmap(project)
    if not result.get("ok", True):
        raise ValueError(str(result.get("detail") or result.get("error")))
    return result


def _compose(
    report: Mapping[str, Any], *, since: str | None, now: datetime
) -> list[BriefLine]:
    section_rows = _section_rows(report)
    runs = _runs(report)
    lines = [
        _overview_line(report),
        _sprint_line(report),
        _critical_path_line(report, section_rows),
        _ready_sections_line(section_rows),
        _overview_blockers_line(report, section_rows),
        _at_risk_line(runs, section_rows, now),
        _wiring_line(report),
    ]
    changed = _since_line(report, runs, since, now) if since else None
    if changed is not None:
        lines.append(changed)
    return lines


def _section_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The ranked section rows, in the payload's own order.

    A section row carries ``ref``; a plan row does not, so the two populations
    the ranking interleaves are separated by the field the section row owns.
    """

    return [
        row
        for row in report.get("immediate_roadmap") or []
        if isinstance(row, Mapping) and row.get("ref")
    ]


def _plan_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        row
        for row in report.get("immediate_roadmap") or []
        if isinstance(row, Mapping) and not row.get("ref")
    ]


def _runs(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every run the payload places, once each, as the row it carries.

    A run sits on at most one section, so the section rows hold the classified
    rows; a run the payload places nowhere this way still travels on its
    sprint's in-flight list, which is read as a fallback rather than a section.
    """

    seen: dict[str, Mapping[str, Any]] = {}
    for row in _section_rows(report):
        for run in row.get("runs") or []:
            if isinstance(run, Mapping) and run.get("run_id"):
                seen.setdefault(str(run["run_id"]), run)
    for sprint in report.get("sprints") or []:
        for key in ("in_flight", "interrupted"):
            for run in sprint.get(key) or []:
                if isinstance(run, Mapping) and run.get("run_id"):
                    seen.setdefault(str(run["run_id"]), run)
    return [seen[key] for key in sorted(seen)]


def _overview_line(report: Mapping[str, Any]) -> BriefLine:
    completion = report.get("completion") or {}
    lifecycle = float(completion.get("lifecycle_completion_pct") or 0.0)
    implementation = float(completion.get("implementation_pct") or 0.0)
    plans = int(completion.get("plans") or 0)
    ready = len(report.get("ready_now") or [])
    blocked = len(report.get("blocked") or [])
    deferred = len(report.get("deferred") or [])
    dominant_roi, dominant_share = _dominant_roi(_plan_rows(report))
    text = (
        f"{report.get('project') or ''}: {lifecycle:.1f}% lifecycle, "
        f"{implementation:.1f}% implementation over {plans} plans; "
        f"{ready} ready, {blocked} blocked, {deferred} deferred"
    )
    if dominant_roi and dominant_share >= ROI_SHARE_THRESHOLD:
        text += (
            f"; {dominant_share * 100:.0f}% of open plans are ROI {dominant_roi}, "
            "so ROI orders nothing"
        )
    return BriefLine(
        key="overview",
        text=text,
        values={
            "lifecycle_completion_pct": lifecycle,
            "implementation_pct": implementation,
            "plans": plans,
            "ready": ready,
            "blocked": blocked,
            "deferred": deferred,
            "dominant_roi": dominant_roi,
            "dominant_roi_share": round(dominant_share, 4),
        },
    )


def _dominant_roi(plan_rows: Iterable[Mapping[str, Any]]) -> tuple[str, float]:
    """The ROI value most open plans carry, and the share that carry it."""

    values = [str(row.get("roi") or "") for row in plan_rows]
    values = [value for value in values if value]
    if not values:
        return "", 0.0
    value, count = Counter(values).most_common(1)[0]
    return value, count / len(values)


def _sprint_line(report: Mapping[str, Any]) -> BriefLine:
    """The active sprint's goal, and the sprint window left when declared.

    A sprint's goal is the theme the summary row carries. A declared end date
    travels on the sprint record, and the payload reports it where it exists;
    where it does not, the brief says so rather than inventing a deadline.
    """

    active = str(report.get("active_sprint_id") or "")
    summary = next(
        (
            row
            for row in report.get("summary_sprints") or []
            if isinstance(row, Mapping) and str(row.get("id") or "") == active
        ),
        None,
    )
    detail = next(
        (
            row
            for row in report.get("sprints") or []
            if isinstance(row, Mapping) and str(row.get("id") or "") == active
        ),
        None,
    )
    goal = str((summary or {}).get("theme") or (detail or {}).get("theme") or "")
    ends = str(
        (summary or {}).get("ends")
        or (summary or {}).get("end")
        or (detail or {}).get("ends")
        or ""
    )
    status = str((summary or {}).get("status") or (detail or {}).get("status") or "")
    if not active:
        text = "No active sprint is declared."
    else:
        head = f"Active sprint {active}"
        if goal:
            head += f": {goal}"
        if ends:
            head += f" (ends {ends})"
        elif status:
            head += f" ({status}; no end declared)"
        text = head
    return BriefLine(
        key="sprint",
        text=text,
        values={"sprint": active, "goal": goal, "ends": ends, "status": status},
    )


def _critical_path_line(
    report: Mapping[str, Any], section_rows: list[Mapping[str, Any]]
) -> BriefLine:
    """The critical path as sections, its hours, and what holds its head.

    The head is the first node of the path, which is the deepest prerequisite
    the chain forces. What holds it is read from its own section row: the runs
    sitting on it and the holds they carry, so the brief names the run that has
    stopped rather than the fact that a path exists.
    """

    critical = report.get("critical_path") or {}
    sections = [str(node) for node in critical.get("sections") or []]
    plans = [str(node) for node in critical.get("plans") or []]
    head = sections[0] if sections else (plans[0] if plans else "")
    hours = float(critical.get("length_hours") or 0.0)
    worker_hours = float(critical.get("worker_hours") or 0.0)
    head_row = next(
        (row for row in section_rows if str(row.get("ref") or "") == head), None
    )
    head_runs = [
        str(run.get("run_id") or "") for run in (head_row or {}).get("runs") or []
    ]
    head_holds = [
        str(hold.get("run_id") or hold.get("path") or "")
        for hold in (head_row or {}).get("blockers") or []
    ]
    head_holds = [hold for hold in head_holds if hold]

    values = {
        "sections": sections,
        "plans": plans,
        "head": head,
        "length_hours": hours,
        "worker_hours": worker_hours,
        "head_runs": head_runs,
        "head_holds": head_holds,
    }
    if not head:
        return BriefLine(
            key="critical_path",
            text="Critical path: none — no open path carries work.",
            values=values,
        )
    chain = " -> ".join(sections) if sections else " -> ".join(plans)
    text = f"Critical path: {chain} ({hours:g} h elapsed, {worker_hours:g} h effort)."
    if head_runs:
        text += f" Head {head} carries {', '.join(head_runs)}."
    if head_holds:
        text += f" Held by {', '.join(head_holds)}."
    return BriefLine(key="critical_path", text=text, values=values)


def _ready_sections_line(section_rows: list[Mapping[str, Any]]) -> BriefLine:
    """The ten highest-ranked ready sections, with the signal that placed each.

    Reach is counted from each row's own ``unlocks``, so the three sections
    releasing the most work are named beside the ranked list rather than left
    for a reader to rank again.
    """

    ready = [row for row in section_rows if row.get("ready")]
    top = ready[:TOP_READY_SECTIONS]
    entries = [
        {
            "ref": str(row.get("ref") or ""),
            "plan": str(row.get("plan") or ""),
            "reason": str(row.get("reason") or ""),
            "unlocks": len(row.get("unlocks") or []),
        }
        for row in top
    ]
    largest_reach = sorted(
        entries, key=lambda entry: (-entry["unlocks"], entry["ref"])
    )[:TOP_REACH_SECTIONS]
    if not entries:
        text = "Ready sections: none ranked."
    else:
        parts = [
            f"{index}. {entry['ref']} — {entry['reason']}"
            for index, entry in enumerate(entries, start=1)
        ]
        text = "Ready sections: " + "; ".join(parts)
        if len(ready) > len(entries):
            text += f" (+{len(ready) - len(entries)} more)"
        reach_text = ", ".join(
            f"{entry['ref']} ({entry['unlocks']})" for entry in largest_reach
        )
        text += f". Largest reach: {reach_text}."
    return BriefLine(
        key="ready_sections",
        text=text,
        values={
            "ready_total": len(ready),
            "sections": entries,
            "largest_reach": largest_reach,
        },
    )


def _overview_blockers_line(
    report: Mapping[str, Any], section_rows: list[Mapping[str, Any]]
) -> BriefLine:
    """Every named hold, grouped by the kind that names it.

    A hold travels on the plan that declares it and on the section it stops, so
    both populations are read and counted under one vocabulary: a reader asking
    "what is blocking this project" gets the kinds, not a single total.
    """

    kinds: Counter[str] = Counter()
    for row in report.get("blocked") or []:
        if not isinstance(row, Mapping):
            continue
        for field, kind in (
            ("explicit_blockers", "explicit"),
            ("held_blockers", "held"),
            ("gate_blockers", "gate"),
            ("section_gate_blockers", "gate-section"),
            ("decision_blockers", "decision"),
            ("closure_blockers", "closure"),
        ):
            count = len(row.get(field) or [])
            if count:
                kinds[kind] += count
    for row in section_rows:
        for hold in row.get("blockers") or []:
            if isinstance(hold, Mapping):
                kinds[str(hold.get("kind") or "unknown")] += 1
    blocked_plans = len(report.get("blocked") or [])
    if not kinds:
        text = f"Blockers: none named across {blocked_plans} blocked plans."
    else:
        parts = ", ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
        text = f"Blockers across {blocked_plans} blocked plans: {parts}."
    return BriefLine(
        key="blockers",
        text=text,
        values={
            "blocked_plans": blocked_plans,
            "kinds": OrderedDict(sorted(kinds.items())),
            "total": sum(kinds.values()),
        },
    )


def _at_risk_line(
    runs: list[Mapping[str, Any]],
    section_rows: list[Mapping[str, Any]],
    now: datetime,
) -> BriefLine:
    """Work that keeps coming back: stuck sections and runs that have stopped."""

    stuck = [str(row.get("ref") or "") for row in section_rows if row.get("stuck")]
    blocked = [run for run in runs if _classification(run) == BLOCKED_RUN]
    interrupted = [run for run in runs if _classification(run) == INTERRUPTED_RUN]
    oldest_blocked = min(blocked, key=_started_at) if blocked else None
    long_blocked = [
        str(run.get("run_id") or "")
        for run in blocked
        if (_age_seconds(run, now) or 0.0) > BLOCKED_RUN_SECONDS
    ]
    interrupted_ids = [str(run.get("run_id") or "") for run in interrupted]

    parts: list[str] = []
    if stuck:
        parts.append(f"stuck sections {', '.join(stuck)}")
    if oldest_blocked is not None:
        parts.append(
            f"oldest blocked run {oldest_blocked.get('run_id')} "
            f"(since {oldest_blocked.get('started_at')})"
        )
    if interrupted_ids:
        parts.append(f"interrupted runs {', '.join(interrupted_ids)}")
    text = "At risk: " + ("; ".join(parts) if parts else "nothing flagged.")
    return BriefLine(
        key="at_risk",
        text=text,
        values={
            "stuck_sections": stuck,
            "blocked_runs": [str(run.get("run_id") or "") for run in blocked],
            "oldest_blocked_run": (
                str(oldest_blocked.get("run_id") or "") if oldest_blocked else ""
            ),
            "blocked_over_a_day": long_blocked,
            "interrupted_runs": interrupted_ids,
        },
    )


def _classification(run: Mapping[str, Any]) -> str:
    return str(run.get("classification") or "")


def _started_at(run: Mapping[str, Any]) -> datetime:
    parsed = _iso_stamp(run.get("started_at"))
    return parsed or datetime.max.replace(tzinfo=UTC)


def _age_seconds(run: Mapping[str, Any], now: datetime) -> float | None:
    started = _iso_stamp(run.get("started_at"))
    if started is None:
        return None
    return max(0.0, (now - started).total_seconds())


def _wiring_line(report: Mapping[str, Any]) -> BriefLine:
    """The wiring findings, counted by code, with the leading few shown."""

    findings = [
        finding
        for finding in report.get("wiring_findings") or []
        if isinstance(finding, Mapping)
    ]
    counts = Counter(str(finding.get("code") or "unknown") for finding in findings)
    top = [
        {
            "code": str(finding.get("code") or ""),
            "severity": str(finding.get("severity") or ""),
            "message": str(finding.get("message") or ""),
        }
        for finding in findings[:TOP_WIRING_FINDINGS]
    ]
    if not findings:
        text = "Wiring findings: none."
    else:
        parts = ", ".join(f"{code} {count}" for code, count in sorted(counts.items()))
        text = f"Wiring findings: {len(findings)} total ({parts})."
        for finding in top:
            text += f" {finding['code']}: {finding['message']}"
    return BriefLine(
        key="wiring",
        text=text,
        values={
            "total": len(findings),
            "counts": OrderedDict(sorted(counts.items())),
            "top": top,
        },
    )


def _since_line(
    report: Mapping[str, Any],
    runs: list[Mapping[str, Any]],
    since: str,
    now: datetime,
) -> BriefLine:
    """What the payload records as having moved since a given instant."""

    moment = _iso_stamp(since)
    floor = moment or datetime.min.replace(tzinfo=UTC)
    started = [
        str(run.get("run_id") or "")
        for run in runs
        if (_iso_stamp(run.get("started_at")) or datetime.min.replace(tzinfo=UTC))
        >= floor
    ]
    closed = [
        str(row.get("id") or "")
        for row in report.get("summary_sprints") or []
        if isinstance(row, Mapping)
        and row.get("closed_at")
        and (_iso_stamp(row.get("closed_at")) or datetime.min.replace(tzinfo=UTC))
        >= floor
    ]
    text = (
        f"Since {since}: {len(started)} run(s) started"
        + (f" ({', '.join(started)})" if started else "")
        + f"; {len(closed)} sprint(s) closed"
        + (f" ({', '.join(closed)})" if closed else "")
        + "."
    )
    return BriefLine(
        key="since",
        text=text,
        values={"since": since, "runs_started": started, "sprints_closed": closed},
    )


def _iso_stamp(value: Any) -> datetime | None:
    """Read one of the brief's timestamps: an ISO string, or an absent one.

    The brief's callers pass ISO strings or nothing at all, so a value that is
    not a string is treated as absent rather than handed to the shared parser,
    which reads a number as an epoch and would accept a domain the brief never
    meets. The parsing rule itself lives once, in
    :func:`reckon._timestamps.parse_utc`.
    """

    if not isinstance(value, str):
        return None
    return parse_utc(value)
