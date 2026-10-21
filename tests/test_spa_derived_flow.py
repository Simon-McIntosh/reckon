from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from reckon import serve
from reckon.roadmap import schedule_report

ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"
SPRINT = ROOT / "docs" / "ui" / "sprint.jsx"

REFERENCE = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)

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
  flowPercent,
};
"""


def _run_probe(
    *, plans: list[dict], runs: list[dict], probe: str, schedule: dict | None = None
) -> object:
    source = CREW.read_text(encoding="utf-8") + TEST_EXPORTS
    compiled = serve.compile_jsx(source, filename="derived-flow-probe.jsx").decode()
    served = (
        f"const servedSchedule = {json.dumps(schedule)};"
        if schedule
        else "const servedSchedule = null;"
    )
    script = "\n".join(
        (
            NODE_PRELUDE,
            compiled,
            f"const plans = {json.dumps(plans)};",
            f"const runs = {json.dumps(runs)};",
            served,
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


def test_rendered_flow_precedes_cards_and_dims_other_sprints_to_point_two_eight() -> (
    None
):
    plans, runs = _fixture()
    served = schedule_report("reckon", plans, runs, reference=REFERENCE)
    assert all(isinstance(bar["plan"], str) for bar in served["bars"])
    result = _run_probe(
        plans=plans,
        runs=runs,
        schedule=served,
        probe="""
const injectedNow = new Date("2026-09-04T04:00:00Z");
const RealDate = Date;
class FixedNowDate extends RealDate {
  constructor(...args) {
    super(...(args.length ? args : [injectedNow.getTime()]));
  }
  static now() {
    return injectedNow.getTime();
  }
}
globalThis.Date = FixedNowDate;
globalThis.__selectedSprint = "beta";
window.STATE = { project: "reckon", inventory: plans, schedule: servedSchedule };
const flow = window.__derivedFlowTest.DerivedFlow({ runs, project: "reckon" });
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
const lanes = findAll(flow, node => hasClass(node, "r-derived-flow-lane"));
const nowLines = findAll(flow, node => hasClass(node, "r-derived-flow-now"));
delete globalThis.__selectedSprint;
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


def test_a_stale_schedule_draws_its_bars_and_the_line_that_ages_them() -> None:
    """A kept schedule is drawn, with one line naming why it is stale.

    The loader keeps the last schedule that was served when a discovery cannot
    build one, and marks it stale with that failure. The flow must still draw
    the bars — an emptied flow is what the keeping exists to prevent — and one
    line above them must say what failed and when the schedule was last served,
    so the age of the picture is readable rather than implied.
    """

    plans, runs = _fixture()
    served = schedule_report("reckon", plans, runs, reference=REFERENCE)
    stale = {
        **served,
        "stale": True,
        "stale_error": "RuntimeError: crew rows unavailable",
    }
    probe = """
window.STATE = { project: "reckon", inventory: plans, schedule: STALE_SCHEDULE };
const flow = window.__derivedFlowTest.DerivedFlow({ runs, project: "reckon" });
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
const staleNodes = findAll(flow, node => hasClass(node, "r-derived-flow-stale"));
const staleText = JSON.stringify(staleNodes.map(node => node.children));
delete window.STATE;
return { barCount: bars.length, staleCount: staleNodes.length, staleText };
"""
    result = _run_probe(
        plans=plans,
        runs=runs,
        probe=probe.replace("STALE_SCHEDULE", json.dumps(stale)),
    )
    assert result["barCount"] == len(served["bars"])
    assert result["staleCount"] == 1, (
        "the stale schedule was drawn without the line that ages it"
    )
    assert "crew rows unavailable" in result["staleText"]
    # The stamp is the instant the schedule was served, not the current clock.
    assert "2026-09-04T04:00:00Z" in result["staleText"]

    # A schedule that is not stale draws no such line: the control for the
    # assertion above, so it cannot be satisfied by a line always drawn.
    fresh = _run_probe(
        plans=plans,
        runs=runs,
        probe=probe.replace("STALE_SCHEDULE", json.dumps(served)),
    )
    assert fresh["barCount"] == len(served["bars"])
    assert fresh["staleCount"] == 0


def test_the_replaced_schedule_draws_the_newly_working_runs_bar_in_flight() -> None:
    """A schedule the flow refreshes to draws the run that just went live.

    The crew view replaces the schedule from its own route, so the flow it
    renders is the route's answer rather than the one discovery served. A run
    that went live since then must draw its plan's bar in flight, and the bar
    must carry the done share its split describes — a bar merely labelled in
    flight would not show the reader where the work has reached.
    """

    plans, runs = _fixture()
    before = schedule_report("reckon", plans, runs, reference=REFERENCE)
    working = [
        *runs,
        {
            "run_id": "live-pending",
            "project": "reckon",
            "plan": "pending-first",
            "phase": "working",
        },
    ]
    after = schedule_report("reckon", plans, working, reference=REFERENCE)
    assert (
        next(bar for bar in after["bars"] if bar["plan"] == "pending-first")["state"]
        == "in-flight"
    ), after

    probe = """
window.STATE = { project: "reckon", inventory: plans, schedule: servedSchedule };
const flow = window.__derivedFlowTest.DerivedFlow({ runs, project: "reckon" });
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
const target = bars.find(bar => bar.props.href === "#plan/pending-first");
const share = target
  ? findAll(target, node => hasClass(node, "r-derived-flow-bar-done"))[0]
  : null;
return {
  cls: target ? String(target.props.className) : null,
  doneWidth: share ? share.props.style.width : null,
  barCount: bars.length,
};
"""
    replaced = _run_probe(plans=plans, runs=runs, schedule=after, probe=probe)
    assert replaced["cls"] is not None, replaced
    assert "in-flight" in replaced["cls"], (
        "the replaced schedule drew the newly working run's bar as "
        f"{replaced['cls']} rather than in flight"
    )
    assert replaced["doneWidth"] is not None, replaced

    # Control: the schedule the view held before that run went live draws the
    # same bar predicted, so the assertion above is about the replacement and
    # not about a bar always classed in flight.
    control = _run_probe(plans=plans, runs=runs, schedule=before, probe=probe)
    assert control["cls"] is not None, control
    assert "in-flight" not in control["cls"], control
    # Both answers carry the same bars, so the state is the only thing that
    # moved between them.
    assert control["barCount"] == replaced["barCount"], (control, replaced)
