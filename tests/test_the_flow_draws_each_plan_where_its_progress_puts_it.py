"""Each flow bar carries its own state, position, split, typed key and lane.

The derived flow used to place a bar from its plan's status and its runs, and
the surface then re-derived the state, re-packed the lanes and resolved each
bar against a slug-keyed inventory. These cases derive the schedule for a
temporary project at a fixed reference instant and read the bar the derivation
serves: where it sits against the reference, what its progress split does to
it, which state its plan's status and pointers give it, which typed key it
carries, and which lane it was packed into. One case runs the shipped surface
over a served payload and requires the drawn bar's state class, position and
lane to come from that payload rather than from the plan row.

The negative control restores full-length placement from the reference
instant — a bar drawn at its anchor for its whole wall-clock length, the old
behaviour — and the half-done case above fails.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest

from reckon import serve
from reckon.resources import nav_key
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

function walk(node, visit) {
  if (node == null || node === false || node === true) return;
  if (Array.isArray(node)) { node.forEach(child => walk(child, visit)); return; }
  if (typeof node !== "object") return;
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function findAll(node, predicate) {
  const matches = [];
  walk(node, candidate => { if (predicate(candidate)) matches.push(candidate); });
  return matches;
}

function hasClass(node, name) {
  return String(node?.props?.className || "").split(/\s+/).includes(name);
}
"""

TEST_EXPORTS = """
window.__flowTest = { DerivedFlow };
"""


def _bars(schedule: dict) -> dict[str, dict]:
    return {item["slug"]: item for item in schedule["items"]}


def _derive(plans: list[dict], runs: list[dict] | None = None) -> dict[str, dict]:
    schedule = derive_schedule(plans, runs or [], "sample", REFERENCE)
    return _bars(schedule)


def _run_probe(*, inventory: list[dict], schedule: dict, probe: str) -> object:
    source = CREW.read_text(encoding="utf-8") + TEST_EXPORTS
    compiled = serve.compile_jsx(source, filename="flow-probe.jsx").decode()
    script = "\n".join(
        (
            NODE_PRELUDE,
            compiled,
            f"const inventory = {json.dumps(inventory)};",
            f"const served = {json.dumps(schedule)};",
            "window.STATE = { project: 'sample', inventory, schedule: served };",
            "const result = (() => {" + probe + "})();",
            "process.stdout.write(JSON.stringify(result));",
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
    assert ran.stdout, ran.stderr
    return json.loads(ran.stdout)


def test_a_half_done_plan_spans_its_progress_around_the_reference() -> None:
    bars = _derive(
        [
            {
                "slug": "half",
                "type": "plan",
                "status": "in-progress",
                "wall_clock_hours": 18,
                "impl": 0.5,
            }
        ]
    )
    half = bars["half"]  # the negative control draws 0.0 to 18.0 here
    assert half["start"] == -9
    assert half["end"] == 9
    assert half["split"] == 0


def test_a_finished_plan_that_is_not_recorded_ends_at_now() -> None:
    bars = _derive(
        [
            {
                "slug": "complete",
                "type": "plan",
                "status": "in-progress",
                "wall_clock_hours": 12,
                "impl": 1.0,
            }
        ]
    )
    done = bars["complete"]
    # Every hour of it is done, so the whole bar sits behind the reference and
    # its end is the anchor: nothing remains to draw ahead of now.
    assert done["end"] == 0
    assert done["start"] == -12
    assert done["split"] == 0


def test_a_working_run_puts_the_bar_in_flight_and_a_held_pointer_moves_nothing() -> (
    None
):
    plan = {
        "slug": "busy",
        "type": "plan",
        "status": "in-progress",
        "wall_clock_hours": 16,
        "impl": 0.25,
    }
    working_only = _derive(
        [plan], [{"project": "sample", "plan": "busy", "phase": "working"}]
    )
    assert working_only["busy"]["state"] == "in-flight"
    assert working_only["busy"]["start"] == -4
    assert working_only["busy"]["end"] == 12

    # An older held pointer sits on the same plan. It neither takes the state
    # away from the working run nor moves the bar's position.
    with_held = _derive(
        [plan],
        [
            {"project": "sample", "plan": "busy", "phase": "blocked"},
            {"project": "sample", "plan": "busy", "phase": "working"},
        ],
    )
    assert with_held["busy"]["state"] == "in-flight"
    assert with_held["busy"]["start"] == working_only["busy"]["start"]
    assert with_held["busy"]["end"] == working_only["busy"]["end"]

    # The held pointer alone is enough to hold the bar.
    held_only = _derive(
        [plan], [{"project": "sample", "plan": "busy", "phase": "blocked"}]
    )
    assert held_only["busy"]["state"] == "held"


def test_an_in_progress_plan_whose_effective_status_is_blocked_reads_held() -> None:
    bars = _derive(
        [
            {
                "slug": "stuck",
                "type": "plan",
                "status": "in-progress",
                "effective_status": "blocked",
                "wall_clock_hours": 10,
                "impl": 0.5,
            }
        ]
    )
    stuck = bars["stuck"]
    assert stuck["state"] == "held"
    # Held decides the state, never the position: progress still splits it.
    assert stuck["start"] == -5
    assert stuck["end"] == 5


def test_a_draft_plan_with_a_working_run_reads_in_flight() -> None:
    bars = _derive(
        [{"slug": "drafty", "type": "plan", "status": "draft", "wall_clock_hours": 4}],
        [{"project": "sample", "plan": "drafty", "phase": "dispatched"}],
    )
    drafty = bars["drafty"]
    assert drafty["state"] == "in-flight"
    assert drafty["start"] == 0
    assert drafty["end"] == 4


def test_a_plan_and_a_research_document_sharing_a_slug_keep_distinct_keys() -> None:
    bars = _derive(
        [
            {
                "slug": "shared",
                "type": "plan",
                "status": "pending",
                "wall_clock_hours": 6,
            },
            {
                "slug": "shared",
                "type": "research",
                "status": "done",
                "wall_clock_hours": 6,
            },
            {
                "slug": "filed",
                "type": "plan",
                "status": "pending",
                "archived": True,
                "wall_clock_hours": 6,
            },
        ]
    )
    # The derivation places the plans; the research document of the same slug
    # is not a bar at all. Each bar carries the typed key its own document type
    # yields, asserted in full against the one function that defines it, so a
    # reader keyed on the typed key cannot swap one document for another.
    assert sorted(bars) == ["filed", "shared"]
    assert bars["shared"]["nav_key"] == nav_key("plan", "shared")
    assert bars["filed"]["nav_key"] == nav_key("plan", "filed", archived=True)
    # The archived key is the one a bar keyed on the bare slug cannot produce.
    assert bars["filed"]["nav_key"] == "plan:archive:filed"
    assert bars["shared"]["nav_key"] != nav_key("research", "shared")


def test_every_bar_carries_a_lane_and_no_two_bars_in_one_lane_overlap() -> None:
    schedule = derive_schedule(
        [
            {
                "slug": "a",
                "type": "plan",
                "status": "pending",
                "wall_clock_hours": 6,
                "impl": 0.5,
            },
            {
                "slug": "b",
                "type": "plan",
                "status": "pending",
                "wall_clock_hours": 8,
                "impl": 0.25,
            },
            {
                "slug": "c",
                "type": "plan",
                "status": "shipped",
                "edited": "2026-09-03T20:00:00Z",
                "wall_clock_hours": 6,
            },
            {
                "slug": "d",
                "type": "plan",
                "status": "draft",
                "wall_clock_hours": 4,
                "depends_on": ["a"],
            },
        ],
        [],
        "sample",
        REFERENCE,
    )
    by_slug = {item["slug"]: item for item in schedule["items"]}
    # The packing is asserted against values written here, not against the
    # lane index the packer stamped on the same bar: the recorded bar opens a
    # lane, the two pending bars overlap and so cannot share one, and the
    # dependent bar returns to the lane that fits it.
    assert {slug: bar["lane"] for slug, bar in by_slug.items()} == {
        "c": 0,
        "a": 0,
        "b": 1,
        "d": 0,
    }
    assert [item["slug"] for item in schedule["lanes"][0]["items"]] == ["c", "a", "d"]
    assert [item["slug"] for item in schedule["lanes"][1]["items"]] == ["b"]
    assert len(schedule["lanes"]) == 2

    # The overlapping pairs are in different lanes, and the overlap is real —
    # each pair genuinely covers hours the other also covers.
    for first, second in (("a", "b"), ("b", "d")):
        assert by_slug[first]["lane"] != by_slug[second]["lane"]
        assert by_slug[second]["start"] < by_slug[first]["end"] - 1e-6
        assert by_slug[first]["start"] < by_slug[second]["end"] - 1e-6

    # And no lane holds two bars that overlap, whichever lane it is.
    for index, lane in enumerate(schedule["lanes"]):
        assert all(item["lane"] == index for item in lane["items"])
        ordered = sorted(lane["items"], key=lambda item: item["start"])
        for earlier, later in pairwise(ordered):
            assert earlier["end"] <= later["start"] + 1e-6, (
                earlier["slug"],
                later["slug"],
            )


def test_the_surface_draws_the_served_state_position_and_lane() -> None:
    plans = [
        {
            "slug": "half",
            "type": "plan",
            "title": "Half",
            "status": "in-progress",
            "sprint": "S1",
            "wall_clock_hours": 18,
            "impl": 0.5,
        },
        {
            "slug": "busy",
            "type": "plan",
            "title": "Busy",
            "status": "in-progress",
            "sprint": "S1",
            "wall_clock_hours": 16,
            "impl": 0.25,
        },
    ]
    runs = [{"project": "sample", "plan": "busy", "phase": "working"}]
    served = schedule_report("sample", plans, runs, reference=REFERENCE)
    inventory = [
        {**plan, "nav_key": plan["slug"], "effective_status": plan["status"]}
        for plan in plans
    ]

    result = _run_probe(
        inventory=inventory,
        schedule=served,
        probe="""
const flow = window.__flowTest.DerivedFlow();
const collect = node => {
  const parts = [];
  walk(node, candidate => {
    if (candidate.type === "span" || candidate.type === "strong") {
      parts.push((candidate.children || []).flat().join(""));
    }
  });
  return parts;
};
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
const lanes = findAll(flow, node => hasClass(node, "r-derived-flow-lane"));
return {
  bars: bars.map(bar => {
    const done = findAll(bar, node => hasClass(node, "r-derived-flow-bar-done"))[0];
    return {
      href: bar.props.href,
      state: String(bar.props.className || "").split(/\\s+/).filter(name => name !== "r-derived-flow-bar"),
      left: bar.props.style.left,
      width: bar.props.style.width,
      done: done?.props?.style?.width,
      parts: collect(bar),
    };
  }),
  laneCount: lanes.length,
  laneLabels: lanes.map(lane => collect(lane).join(" ")),
};
""",
    )

    by_href = {bar["href"]: bar for bar in result["bars"]}
    # The state class is the served state, not a mapping from the plan status:
    # both plans are in-progress, and only the one with a working run draws in
    # flight.
    assert by_href["#plan/half"]["state"] == ["predicted"]
    assert by_href["#plan/busy"]["state"] == ["in-flight"]

    # The position and the shaded done share are the served payload's own
    # numbers, not a second computation of the surface's.
    low, high = served["low"], served["high"]

    def share(hour: float) -> float:
        return (hour - low) / (high - low) * 100

    half = by_href["#plan/half"]
    assert float(half["left"].rstrip("%")) == pytest.approx(share(-9))
    assert float(half["width"].rstrip("%")) == pytest.approx(share(9) - share(-9))
    assert float(half["done"].rstrip("%")) == pytest.approx(50.0)
    busy = by_href["#plan/busy"]
    assert float(busy["left"].rstrip("%")) == pytest.approx(share(-4))
    assert float(busy["done"].rstrip("%")) == pytest.approx(25.0)
    assert "25%" in busy["parts"]

    # The lane count is the served packing's, and the rows the surface draws
    # are labelled as lanes rather than as sessions.
    assert result["laneCount"] == served["lane_count"]
    assert any("lane 1" in label for label in result["laneLabels"])
    assert not any("session" in label for label in result["laneLabels"])


def test_the_surface_resolves_a_bar_by_its_typed_key_not_by_bare_slug() -> None:
    # The plan and the research document below share a slug. A surface keying
    # its inventory by bare slug would keep whichever row it saw last — the
    # research one — and draw the research sprint and progress.
    inventory = [
        {
            "slug": "shared",
            "nav_key": "shared",
            "type": "plan",
            "title": "The plan",
            "sprint": "S1",
            "status": "pending",
            "wall_clock_hours": 6,
            "impl": 0.4,
        },
        {
            "slug": "shared",
            "nav_key": "research:shared",
            "type": "research",
            "title": "The research",
            "sprint": "R9",
            "status": "done",
            "wall_clock_hours": 6,
            "impl": 0.9,
        },
    ]
    schedule = schedule_report("sample", inventory, [], reference=REFERENCE)

    result = _run_probe(
        inventory=inventory,
        schedule=schedule,
        probe="""
const flow = window.__flowTest.DerivedFlow();
const bar = findAll(flow, node => hasClass(node, "r-derived-flow-bar"))[0];
const parts = [];
walk(flow, candidate => {
  if (hasClass(candidate, "r-derived-flow-bar")) {
    walk(candidate, inner => {
      if (inner.type === "span" || inner.type === "strong") {
        parts.push((inner.children || []).flat().join(""));
      }
    });
  }
});
return { href: bar?.props?.href, parts };
""",
    )

    assert result["href"] == "#plan/shared"
    # The drawn bar carries the plan's sprint and its progress, not the
    # research row's.
    assert "S1" in result["parts"]
    assert "40%" in result["parts"]
    assert "R9" not in result["parts"]
    assert "90%" not in result["parts"]
