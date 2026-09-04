from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reckon import serve

ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"
SPRINT = ROOT / "docs" / "ui" / "sprint.jsx"

NODE_PRELUDE = r"""
globalThis.window = globalThis;
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [globalThis.__selectedSprint ?? (typeof value === "function" ? value() : value), noop]; },
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

function hasClass(node, name) {
  return String(node?.props?.className || "").split(/\s+/).includes(name);
}

function findAll(node, predicate) {
  const matches = [];
  walk(node, candidate => { if (predicate(candidate)) matches.push(candidate); });
  return matches;
}
"""

TEST_EXPORTS = """
window.__derivedFlowTest = {
  CrewView,
  DerivedFlow,
  derivedFlowSchedule,
  flowPercent,
};
"""


def _run_probe(*, plans: list[dict], runs: list[dict], probe: str) -> object:
    source = CREW.read_text(encoding="utf-8") + TEST_EXPORTS
    compiled = serve.compile_jsx(source, filename="derived-flow-probe.jsx").decode()
    script = "\n".join(
        (
            NODE_PRELUDE,
            compiled,
            f"const plans = {json.dumps(plans)};",
            f"const runs = {json.dumps(runs)};",
            "const result = (() => {" + probe + "})();",
            "process.stdout.write(JSON.stringify(result));",
        )
    )
    result = subprocess.run(
        ["node"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _fixture() -> tuple[list[dict], list[dict]]:
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


def test_schedule_chains_pending_plans_and_best_fit_packs_two_lanes() -> None:
    plans, runs = _fixture()
    result = _run_probe(
        plans=plans,
        runs=runs,
        probe="""
const schedule = window.__derivedFlowTest.derivedFlowSchedule(plans, runs, "reckon", new Date("2026-09-04T04:00:00Z"));
const bySlug = Object.fromEntries(schedule.items.map(item => [item.plan.slug, { start: item.start, end: item.end }]));
return {
  bySlug,
  laneCount: schedule.lanes.length,
  low: schedule.low,
  high: schedule.high,
  earliestStart: schedule.earliestStart,
  latestEnd: schedule.latestEnd,
  ticks: schedule.ticks.map(tick => tick.label),
};
""",
    )

    assert result["bySlug"]["active-work"] == {"start": -4, "end": 4}
    assert (
        result["bySlug"]["pending-first"]["start"]
        == result["bySlug"]["active-work"]["end"]
    )
    assert (
        result["bySlug"]["pending-second"]["start"]
        == result["bySlug"]["pending-first"]["end"]
    )
    assert result["laneCount"] == 2
    assert result["low"] == max(-48, min(-24, result["earliestStart"]))
    assert result["high"] == max(24, result["latestEnd"])
    assert result["latestEnd"] == 30
    assert "now" in result["ticks"]


def test_active_plan_uses_elapsed_time_when_live_row_omits_dispatch_stamp() -> None:
    plans = [{"slug": "active-work", "status": "active", "wall_clock_hours": 8}]
    runs = [{"project": "reckon", "plan": "active-work", "elapsed_seconds": 14_400}]
    result = _run_probe(
        plans=plans,
        runs=runs,
        probe="""
const schedule = window.__derivedFlowTest.derivedFlowSchedule(plans, runs, "reckon", new Date("2026-09-04T04:00:00Z"));
return { start: schedule.items[0].start, end: schedule.items[0].end };
""",
    )
    assert result == {"start": -4, "end": 4}


def test_schedule_drops_bars_starting_more_than_sixty_hours_ago() -> None:
    plans = [
        {
            "slug": "too-old",
            "status": "shipped",
            "edited": "2026-09-01T00:00:00Z",
            "wall_clock_hours": 2,
        },
        {"slug": "current", "status": "pending", "wall_clock_hours": 3},
    ]
    result = _run_probe(
        plans=plans,
        runs=[],
        probe="""
const schedule = window.__derivedFlowTest.derivedFlowSchedule(plans, runs, "reckon", new Date("2026-09-04T04:00:00Z"));
return schedule.items.map(item => item.plan.slug);
""",
    )
    assert result == ["current"]


def test_rendered_flow_precedes_cards_and_dims_other_sprints_to_point_two_eight() -> (
    None
):
    plans, runs = _fixture()
    result = _run_probe(
        plans=plans,
        runs=runs,
        probe="""
globalThis.__selectedSprint = "beta";
const flow = window.__derivedFlowTest.DerivedFlow({ plans, runs, project: "reckon" });
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
const lanes = findAll(flow, node => hasClass(node, "r-derived-flow-lane"));
const nowLines = findAll(flow, node => hasClass(node, "r-derived-flow-now"));
delete globalThis.__selectedSprint;
window.STATE = { project: "reckon", inventory: plans };
const surface = window.__derivedFlowTest.CrewView({ visibleProjects: ["reckon"], mountedProjectCount: 1, selectedProject: "reckon" });
const directChildren = surface.children.flat().filter(Boolean);
return {
  laneCount: lanes.length,
  nowLineCount: nowLines.length,
  opacities: Object.fromEntries(bars.map(bar => [bar.props.href, bar.props.style.opacity])),
  flowIndex: directChildren.findIndex(node => node?.type === window.__derivedFlowTest.DerivedFlow),
  cardListIndex: directChildren.findIndex(node => hasClass(node, "r-crew-list")),
};
""",
    )

    assert result["laneCount"] == 2
    assert result["nowLineCount"] == 3
    assert result["opacities"]["#plan/active-work"] == 1
    assert result["opacities"]["#plan/pending-first"] == 1
    assert result["opacities"]["#plan/recorded-early"] == 0.28
    assert result["opacities"]["#plan/pending-second"] == 0.28
    assert result["flowIndex"] >= 0
    assert (
        result["cardListIndex"] == -1 or result["flowIndex"] < result["cardListIndex"]
    )


def test_sprint_chain_figure_uses_shared_schedule_far_end() -> None:
    source = SPRINT.read_text(encoding="utf-8")
    assert "derivedFlowChainHours(projectPlans" in source
    assert "window.ReckonCrewSchedule.farEnd" in source
    assert "chain <strong>{Math.round(chainHours)}h</strong>" in source
