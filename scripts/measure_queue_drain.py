"""Read a queue drain's week from the ledger, the verdict log and the drain view.

This is the operator's side of :mod:`reckon.crew.drain_measure`: it gathers the
records the measures need, prints one JSON report, and can draw the figure the
report is read beside. Nothing here decides anything: a figure comes from the
measure that reads the records, and a guarantee without the records to answer
it stays unmeasured.

    python scripts/measure_queue_drain.py --project reckon --project imas-ambix

The report writes to stdout and the figure to ``--figure`` when one is named.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon import ledger
from reckon._schema import section_depends_on
from reckon._store import _docs_dir_for_project
from reckon._timestamps import parse_utc
from reckon.crew import drain_measure
from reckon.crew import runs as runs_module
from reckon.crew.drain_measure import launch_moments
from reckon.flight import declared_project_weights, resolve


def read_plan_waits(
    projects: Sequence[str], plan_slugs: Mapping[str, set[str]]
) -> dict:
    """Each plan's section-wait declaration, keyed by (project, plan)."""
    declared: dict[tuple[str, str], Mapping[str, Sequence[str]]] = {}
    for project in projects:
        docs = _docs_dir_for_project(project)
        if docs is None:
            continue
        for slug in sorted(plan_slugs.get(project, ())):
            path = docs / "plans" / f"{slug}.html"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            mapping = section_depends_on(text)
            if mapping:
                declared[(project, slug)] = mapping
    return declared


def read_project_runs(project: str) -> list[dict[str, Any]]:
    """The project's completed runs, each tagged with the project it belongs to."""
    data, _version = ledger.load(project)
    records = []
    for record in data.get("runs") or ():
        if not isinstance(record, Mapping):
            continue
        rows = dict(record)
        rows["project"] = project
        records.append(rows)
    return records


def read_live(project: str) -> dict[str, Any]:
    """The drain view's readiness and loading figures for one project."""
    view = runs_module.drain(project)
    week = view.get("week") or {}
    project_view = week.get("project") or {}
    return {
        "executable_remainder": view.get("executable_remainder") or 0,
        "unloaded": view.get("unloaded") or {},
        "phases": project_view.get("phases") or {},
        "live_pointers": view.get("live_pointers") or 0,
    }


def daily_counts(
    runs: Sequence[Mapping[str, Any]], days: int, now: datetime
) -> dict[str, Any]:
    """Launches and landings by day, per project."""
    start = (now - timedelta(days=days)).date()
    launches: dict[str, Counter] = defaultdict(Counter)
    landings: dict[str, Counter] = defaultdict(Counter)
    for run in runs:
        project = str(run.get("project") or "")
        launched = launch_moments(run)
        if launched and launched.date() >= start:
            launches[project][launched.date().isoformat()] += 1
        completed = parse_utc(run.get("completed_at"))
        if completed and completed.date() >= start:
            landings[project][completed.date().isoformat()] += 1
    return {"launches": launches, "landings": landings}


def build_report(
    projects: Sequence[str], *, days: int, now: datetime | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    moment = now or datetime.now(UTC)
    all_runs: list[dict[str, Any]] = []
    live: dict[str, Any] = {}
    pointers: list[dict[str, Any]] = []
    plan_slugs: dict[str, set[str]] = defaultdict(set)
    for project in projects:
        project_runs = read_project_runs(project)
        all_runs.extend(project_runs)
        live[project] = read_live(project)
        pointers.extend(runs_module.list_live(project=project))
    window_start = moment - timedelta(days=days)
    windowed = [
        run for run in all_runs if drain_measure.in_window(run, window_start, moment)
    ]
    for run in windowed:
        definition = run.get("node_definition")
        slug = ""
        if isinstance(definition, Mapping):
            slug = str(definition.get("plan") or "")
        slug = slug or str(run.get("plan") or "")
        if slug:
            plan_slugs[str(run.get("project") or "")].add(slug)
    depends_on = read_plan_waits(projects, plan_slugs)
    weights = declared_project_weights(resolve().config)
    report = drain_measure.measure_drain(
        runs=all_runs,
        depends_on=depends_on,
        weights=weights,
        ready_by_project={
            project: live[project]["executable_remainder"] for project in projects
        },
        unloaded_by_project={
            project: sorted(
                {
                    f"{plan}#{section}"
                    for plan, sections in (live[project]["unloaded"] or {}).items()
                    for section in sections
                }
            )
            for project in projects
        },
        live_pointers=pointers,
        passes=runs_module.admission_pass_history(),
        now=moment,
        window_days=days,
        projects=projects,
    )
    report["declared_weights"] = weights
    report["live"] = live
    series = daily_counts(windowed, days, moment)
    return report, series


def draw_figure(
    report: Mapping[str, Any], series: Mapping[str, Any], path: Path
) -> None:
    """Draw launched and landed work per project per day, and depth by phase."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    with suppress(OSError):
        plt.style.use("data-ink")
    colours = {"reckon": "#2e6f9e", "imas-ambix": "#2e8b57", "nova": "#7d4fa8"}
    fallback = ["#b8327e", "#c07020", "#3f7f7f"]

    days = sorted(
        {day for kind in series.values() for project in kind for day in kind[project]}
    )
    figure, (left, right) = plt.subplots(1, 2, figsize=(14.0, 7.2), dpi=100)
    positions = {day: index for index, day in enumerate(days)}
    for index, project in enumerate(sorted(series["launches"])):
        colour = colours.get(project, fallback[index % len(fallback)])
        lau = [
            (positions[day], count)
            for day, count in sorted(series["launches"][project].items())
        ]
        lan = [
            (positions[day], count)
            for day, count in sorted(series["landings"][project].items())
        ]
        if lau:
            left.plot(
                [xy[0] for xy in lau],
                [xy[1] for xy in lau],
                color=colour,
                linewidth=2.6,
                marker="o",
                markersize=4,
                label=f"{project} launches",
            )
        if lan:
            left.plot(
                [xy[0] for xy in lan],
                [xy[1] for xy in lan],
                color=colour,
                linewidth=2.4,
                linestyle="--",
                marker="s",
                markersize=3.5,
                label=f"{project} landings",
            )
    left.margins(y=0.08)
    left.set_ylim(bottom=0)
    left.set_xticks(range(len(days)))
    left.set_xticklabels([day[5:] for day in days])
    left.set_xlabel("day [UTC]")
    left.set_ylabel("runs per day")

    phases = ["awaiting-upstream", "queued", "held", "launched", "failed"]
    width = 0.8 / max(len(report.get("live") or {}), 1)
    for index, (project, view) in enumerate(sorted((report.get("live") or {}).items())):
        values = [(view.get("phases") or {}).get(phase, 0) for phase in phases]
        xs = [position + index * width for position in range(len(phases))]
        right.bar(
            xs,
            values,
            width=width * 0.9,
            color=colours.get(project, fallback[index % len(fallback)]),
            label=project,
        )
    right.set_xticks([position + width / 2 for position in range(len(phases))])
    right.set_xticklabels(phases, rotation=20, ha="right")
    right.set_ylabel("requests at measurement")
    right.yaxis.set_major_locator(MaxNLocator(integer=True))
    handles, labels = left.get_legend_handles_labels()
    if handles:
        figure.legend(handles, labels, loc="upper center", ncol=2, frameon=False)
    for axis in (left, right):
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
    figure.tight_layout(rect=(0.0, 0.0, 1.0, 0.93), pad=2.4)
    _refuse_clipped_text(figure)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, format="svg", metadata={"Date": None})
    plt.close(figure)


def _visible_tick_labels(axis) -> list:
    """Tick labels whose tick lies inside the view; the rest are never drawn."""
    labels: list = []
    for ticks, names, (low, high) in (
        (axis.get_xticks(), axis.get_xticklabels(), axis.get_xlim()),
        (axis.get_yticks(), axis.get_yticklabels(), axis.get_ylim()),
    ):
        for position, label in zip(ticks, names, strict=False):
            if low <= position <= high:
                labels.append(label)
    return labels


def _refuse_clipped_text(figure) -> None:
    """Refuse to write a figure whose text runs off the canvas or overlaps."""
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    canvas = figure.bbox
    seen: list[tuple[object, str]] = []
    offenders: list[str] = []
    artists: list = []
    for axis in figure.axes:
        artists += [axis.title, axis.xaxis.label, axis.yaxis.label]
        artists += _visible_tick_labels(axis)
        legend = axis.get_legend()
        if legend is not None:
            artists += legend.get_texts()
    for legend in figure.legends:
        artists += legend.get_texts()
    for artist in artists:
        text = getattr(artist, "get_text", lambda: "")()
        if not text:
            continue
        box = artist.get_window_extent(renderer=renderer)
        if (
            box.x0 < canvas.x0
            or box.y0 < canvas.y0
            or box.x1 > canvas.x1
            or box.y1 > canvas.y1
        ):
            offenders.append(f"clipped: {text!r}")
            continue
        for other_box, other_text in seen:
            if box.overlaps(other_box):
                offenders.append(f"overlaps: {text!r} / {other_text!r}")
        seen.append((box, text))
    if offenders:
        raise SystemExit("refusing to write the figure: " + "; ".join(offenders[:6]))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", default=None)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--figure", type=Path, default=None)
    args = parser.parse_args(argv)
    projects = args.project or ["reckon", "imas-ambix"]
    report, series = build_report(projects, days=args.days)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    if args.figure:
        draw_figure(report, series, args.figure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
