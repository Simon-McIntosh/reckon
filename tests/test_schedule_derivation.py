"""The Python schedule derivation, computed from the same inputs the surface uses.

Every case passes a fixed reference instant; the derivation never reads the
wall clock, because this plan's sibling surface test became a fuse exactly that
way — it rendered with no injected clock, so its fixture bars fell outside the
retention window nine days later and stayed red. One case runs the rendered
surface's own schedule against the same input and requires the two to agree,
so the Python result is shown against the JSX result rather than assumed.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import serve
from reckon.roadmap import schedule_report
from reckon.schedule import derive_schedule

ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"

REFERENCE = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)

NODE_PRELUDE = r"""
globalThis.window = globalThis;
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [value, noop]; },
  useMemo(factory) { return factory(); },
  useEffect() {},
};
globalThis.navigator = { clipboard: { writeText: noop } };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ runs: [] }) });
window.setInterval = noop;
window.clearInterval = noop;
window.flashSaved = noop;
"""


def _fixture() -> tuple[list[dict], list[dict]]:
    """The five-plan surface fixture: two shipped at edited stamps 30 and 10
    hours before the reference, one active with a live run dispatched 4 hours
    before it, two pending chained behind the active plan."""

    plans = [
        {
            "slug": "recorded-early",
            "title": "Recorded early",
            "status": "shipped",
            "edited": "2026-09-02T22:00:00Z",
            "wall_clock_hours": 8,
            "sprint": "alpha",
        },
        {
            "slug": "recorded-wide",
            "title": "Recorded wide",
            "status": "shipped",
            "edited": "2026-09-03T18:00:00Z",
            "wall_clock_hours": 32,
            "sprint": "alpha",
        },
        {
            "slug": "active-work",
            "title": "Active work",
            "status": "active",
            "wall_clock_hours": 8,
            "sprint": "beta",
        },
        {
            "slug": "pending-first",
            "title": "Pending first",
            "status": "pending",
            "wall_clock_hours": 14,
            "depends_on": ["active-work"],
            "sprint": "beta",
        },
        {
            "slug": "pending-second",
            "title": "Pending second",
            "status": "draft",
            "wall_clock_hours": 12,
            "depends_on": ["pending-first"],
            "sprint": "gamma",
        },
    ]
    runs = [
        {
            "run_id": "live-active",
            "project": "reckon",
            "plan": "active-work",
            "role": "implement",
            "dispatched_at": "2026-09-04T00:00:00Z",
        }
    ]
    return plans, runs


def _bars_by_slug(schedule: dict) -> dict[str, dict]:
    return {item["slug"]: item for item in schedule["items"]}


def test_pending_plan_starts_at_the_later_of_its_prerequisite_and_reference() -> None:
    plans, runs = _fixture()
    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    bars = _bars_by_slug(schedule)

    # The active plan starts at its live run's dispatch, 4 hours before the
    # reference instant.
    assert bars["active-work"]["start"] == -4
    assert bars["active-work"]["end"] == 4
    # Each pending plan starts exactly at its prerequisite's end, which is
    # later than the reference instant.
    assert bars["pending-first"]["start"] == bars["active-work"]["end"]
    assert bars["pending-first"]["end"] == 18
    assert bars["pending-second"]["start"] == bars["pending-first"]["end"]
    assert bars["pending-second"]["end"] == 30


def test_active_plan_uses_elapsed_time_when_live_row_omits_dispatch_stamp() -> None:
    plans = [
        {"slug": "active-work", "status": "active", "wall_clock_hours": 8},
    ]
    runs = [
        {"project": "reckon", "plan": "active-work", "elapsed_seconds": 14_400},
    ]
    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    bar = _bars_by_slug(schedule)["active-work"]
    assert bar["start"] == -4
    assert bar["end"] == 4


def test_reference_is_a_parameter_never_the_wall_clock() -> None:
    plans, runs = _fixture()
    # A fixed instant far from the wall clock: if the clock were read, the
    # recorded/active stamps would sit tens of hours before a 2026-09-15-ish
    # anchor and every assertion below would fail.
    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    assert {
        slug: (bar["start"], bar["end"])
        for slug, bar in _bars_by_slug(schedule).items()
    } == {
        "recorded-early": (-38.0, -30.0),
        "recorded-wide": (-42.0, -10.0),
        "active-work": (-4.0, 4.0),
        "pending-first": (4.0, 18.0),
        "pending-second": (18.0, 30.0),
    }

    # Moving the reference re-anchors every bar. Stamp-anchored bars (recorded
    # and active) shift by exactly the delta; dependent bars take the later of
    # their prerequisite end and the reference, so a prerequisite that crossed
    # the reference clamps the chain at the reference instead. A clock-reading
    # implementation would return the same clock-anchored values on both calls
    # and fail every shifted assertion below.
    shifted = REFERENCE + timedelta(hours=6)
    shifted_schedule = derive_schedule(plans, runs, "reckon", shifted)
    moved = _bars_by_slug(shifted_schedule)
    assert moved["recorded-early"]["start"] == -38 - 6
    assert moved["recorded-early"]["end"] == -30 - 6
    assert moved["recorded-wide"]["start"] == -42 - 6
    assert moved["active-work"]["start"] == -4 - 6
    assert moved["active-work"]["end"] == 4 - 6
    assert moved["pending-first"]["start"] == 0
    assert moved["pending-first"]["end"] == 14
    assert moved["pending-second"]["start"] == 14
    assert moved["pending-second"]["end"] == 26
    assert shifted_schedule["latest_end"] == 26


def test_bars_pack_into_best_fit_lanes() -> None:
    plans, runs = _fixture()
    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)

    assert len(schedule["lanes"]) == 2
    occupations = [
        [item["slug"] for item in lane["items"]] for lane in schedule["lanes"]
    ]
    # The four-tall chained column shares one lane; the old recorded stash that
    # fits nothing else sits alone in the second.
    assert occupations == [
        ["recorded-wide", "active-work", "pending-first", "pending-second"],
        ["recorded-early"],
    ]


def test_best_fit_prefers_the_fitting_lane_that_ended_latest() -> None:
    # Two early bars leave two free lanes that ended at different hours; the
    # last bar fits both, and best-fit must place it on the lane that ended
    # latest rather than the first that fits.
    plans = [
        {
            "slug": "r-a",
            "status": "shipped",
            "edited": "2026-09-02T22:00:00Z",
            "wall_clock_hours": 4,
        },
        {
            "slug": "r-b",
            "status": "shipped",
            "edited": "2026-09-03T08:00:00Z",
            "wall_clock_hours": 12,
        },
        {
            "slug": "r-d",
            "status": "shipped",
            "edited": "2026-09-03T10:00:00Z",
            "wall_clock_hours": 2,
        },
    ]
    schedule = derive_schedule(plans, [], "reckon", REFERENCE)
    occupations = [
        [item["slug"] for item in lane["items"]] for lane in schedule["lanes"]
    ]
    assert occupations == [["r-a"], ["r-b", "r-d"]]


def test_schedule_reports_the_axis_bounds() -> None:
    plans, runs = _fixture()
    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    assert schedule["earliest_start"] == -42
    assert schedule["latest_end"] == 30
    assert schedule["low"] == max(-48, min(-24, schedule["earliest_start"]))
    assert schedule["high"] == max(24, schedule["latest_end"])

    # A schedule whose bars never leave the recent window clamps the axis:
    # low never falls below -24 and high never falls below +24.
    overhang = [
        {"slug": "recent", "status": "pending", "wall_clock_hours": 2},
    ]
    clamped = derive_schedule(overhang, [], "reckon", REFERENCE)
    assert clamped["low"] == -24
    assert clamped["high"] == 24


def test_unknown_prerequisite_is_placed_at_the_reference_not_dropped() -> None:
    plans = [
        {"slug": "real", "status": "pending", "wall_clock_hours": 6},
        {
            "slug": "hanging",
            "status": "pending",
            "wall_clock_hours": 10,
            "depends_on": ["never-named"],
        },
    ]
    schedule = derive_schedule(plans, [], "reckon", REFERENCE)
    bars = _bars_by_slug(schedule)
    assert "hanging" in bars
    assert bars["hanging"]["start"] == 0
    assert bars["real"]["start"] == 0


def test_bars_older_than_the_retention_window_are_excluded() -> None:
    # The boundary: a bar starting exactly 60 hours before the reference is
    # dropped (start > -60 excludes it), one starting just inside is kept.
    plans = [
        {
            "slug": "ancient",
            "status": "shipped",
            "edited": "2026-08-30T04:00:00Z",
            "wall_clock_hours": 2,
        },
        {
            "slug": "boundary",
            "status": "shipped",
            "edited": "2026-09-01T16:00:00Z",
            "wall_clock_hours": 0,
        },
        {
            "slug": "recent-stash",
            "status": "shipped",
            "edited": "2026-09-01T19:30:00Z",
            "wall_clock_hours": 2,
        },
    ]
    schedule = derive_schedule(plans, [], "reckon", REFERENCE)
    assert [item["slug"] for item in schedule["items"]] == ["recent-stash"]


def test_python_schedule_matches_the_surface_render_of_the_served_payload() -> None:
    plans, runs = _fixture()
    served = schedule_report("reckon", plans, runs, reference=REFERENCE)

    # The surface derives nothing: it packs the served bars into lanes and
    # draws ticks from the served low/high. Feed the payload a server would
    # serve to the surface's own layout functions and require the same lanes
    # and ticks the derivation produces.
    source = CREW.read_text(encoding="utf-8")
    test_exports = """
window.__scheduleParity = { flowPackLanes, flowTicks };
"""
    compiled = serve.compile_jsx(
        source + test_exports, filename="schedule-parity.jsx"
    ).decode()
    script = "\n".join(
        (
            NODE_PRELUDE,
            compiled,
            f"const bars = {json.dumps(served['bars'])};",
            ("const lanes = window.__scheduleParity.flowPackLanes(bars)"
             ".map(lane => lane.items.map(item => item.slug));"),
            f"const ticks = window.__scheduleParity.flowTicks({served['low']}, {served['high']});",
            "process.stdout.write(JSON.stringify({ lanes, ticks: ticks.map(tick => tick.label) }));",
        )
    )
    ran = subprocess.run(
        ["node"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert ran.returncode == 0, ran.stderr
    jsx = json.loads(ran.stdout)

    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    # The served payload carries exactly the derivation: same bars, bounds,
    # lane count and far end.
    assert {
        slug: {"start": bar["start"], "end": bar["end"]}
        for slug, bar in _bars_by_slug(schedule).items()
    } == {
        slug: {"start": bar["start"], "end": bar["end"]}
        for slug, bar in {item["slug"]: item for item in served["bars"]}.items()
    }
    assert served["low"] == schedule["low"]
    assert served["high"] == schedule["high"]
    assert served["lane_count"] == len(schedule["lanes"])
    assert served["latest_end_hours"] == schedule["latest_end"]
    # And the surface's own layout over that payload reproduces the lanes and
    # ticks the derivation reports.
    assert [
        [item["slug"] for item in lane["items"]] for lane in schedule["lanes"]
    ] == jsx["lanes"]
    assert [tick["label"] for tick in schedule["ticks"]] == jsx["ticks"]


def test_roadmap_schedule_read_reports_far_end_and_lane_count() -> None:
    plans, runs = _fixture()
    report = schedule_report("reckon", plans, runs, reference=REFERENCE)

    schedule = derive_schedule(plans, runs, "reckon", REFERENCE)
    assert report["lane_count"] == 2
    assert report["item_count"] == 5
    assert report["low"] == schedule["low"]
    assert report["high"] == schedule["high"]
    assert report["latest_end_hours"] == schedule["latest_end"]
    # The far end is the high axis bound the surface's chain figure reads:
    # window.ReckonCrewSchedule.farEnd returns window.STATE?.schedule?.high.
    assert report["far_end_hours"] == 30

    assert report["reference"] == "2026-09-04T04:00:00+00:00"
    assert [bar["slug"] for bar in report["bars"]] == [
        "recorded-wide",
        "recorded-early",
        "active-work",
        "pending-first",
        "pending-second",
    ]
