"""A plan whose only open sections are deferred reads not closable, not 100 %.

A plan that has built sixteen sections and deferred the last two carries an
authored ``impl`` of 1.0, so every progress figure drawn from it reads complete
while ``close`` still refuses it. The fact that it is unfinished is derived once
— ``deferred_remainder`` in ``reckon/roadmap.py``, beside ``closure_blockers`` —
and served on the roadmap row and on the inventory row the crew flow and the
plan card render.

``deferred-only`` is that plan: sixteen done, two deferred, nothing
implementable, so it reads ``2 deferred, not closable`` while its progress
figure stays at 100. ``building`` keeps one implementable section beside a
deferred one, which is ordinary ready work and carries no such fact; that is the
control. Once both deferred sections are descoped the plan drops the fact and
its closure list ends with the ``close`` op.

The surfaces are rendered, not read as sources: each module is compiled with
``reckon.serve.compile_jsx`` and run under node against the served payload, and
the drawn element is inspected. The crew bar's label and its filled share must
both come from the served fact, and the plan card's signal must print the served
label — so deleting the render fails even where the identifier survives in a
comment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from reckon.roadmap import build_roadmap, schedule_report
from reckon.serve import _derive_lifecycle
from tests.roadmap_fixtures import plan_row
from tests.spa_module_eval import evaluate_jsx_render

REFERENCE = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
PROJECT = "deferred-remainder-sample"
SPRINT = [{"id": "current", "status": "active", "items": ["deferred-only", "building"]}]
ROOT = Path(__file__).resolve().parents[1]
CREW_JSX = ROOT / "docs" / "ui" / "crew.jsx"
PLANS_JSX = ROOT / "docs" / "ui" / "shell-plans.jsx"
LABEL = "2 deferred, not closable"


def _declarations(*, deferred: tuple[str, ...] = ("s17", "s18")) -> dict[str, str]:
    """Sixteen built sections, then the two the plan defers."""

    declarations = {f"s{number}": "done" for number in range(1, 17)}
    for section in deferred:
        declarations[section] = "deferred"
    return declarations


def _plans(declarations: dict[str, str] | None = None) -> list[dict]:
    return [
        plan_row(
            "deferred-only",
            impl=1.0,
            effort="L",
            effort_hours=16.0,
            section_declarations=declarations
            if declarations is not None
            else _declarations(),
            followups=[],
        ),
        # A deferred section beside implementable work is a plan still being
        # built, and its progress figure is the honest reading of it. It keeps
        # the fixture's open followup, which is what makes it dispatchable.
        plan_row(
            "building",
            impl=0.5,
            effort="S",
            effort_hours=2.0,
            section_declarations={"s1": "implementable", "s2": "deferred"},
        ),
    ]


def _rows(declarations: dict[str, str] | None = None) -> dict[str, dict]:
    report = build_roadmap(PROJECT, _plans(declarations), SPRINT)
    return {row["slug"]: row for row in report["pending_work"]}


def _served(declarations: dict[str, str] | None = None) -> dict[str, dict]:
    discovered, _hydrated = _derive_lifecycle(PROJECT, _plans(declarations), SPRINT)
    return {plan["slug"]: plan for plan in discovered}


def test_the_roadmap_row_reads_the_deferred_sections_not_the_percent() -> None:
    row = _rows()["deferred-only"]

    # The authored figure is what the surface drew before, and it is still what
    # the row carries: the fact is served beside it, not by overriding it.
    assert row["progress_pct"] == 100.0
    assert row["deferred_remainder"] == {
        "sections": ["s17", "s18"],
        "count": 2,
        # Sixteen of eighteen declared sections are resolved, so a bar drawn
        # from the served share leaves the deferred tail unfilled.
        "done_pct": 88.9,
        "label": LABEL,
    }
    # Nothing implementable is left, so the readiness ladder reads it closable;
    # the deferred sections are what the close op will refuse on.
    assert row["readiness"] == "closable"
    assert [
        (entry["kind"], entry.get("id", ""), entry["ops"])
        for entry in row["closure_blockers"]
    ] == [
        ("section", "s17", ["descope"]),
        ("section", "s18", ["descope"]),
        ("op", "", ["close"]),
    ]


def test_a_plan_still_building_carries_no_deferred_reading() -> None:
    row = _rows()["building"]

    assert row["deferred_remainder"] is None
    assert row["readiness"] == "ready"


def test_the_served_rows_read_two_deferred() -> None:
    served = _served()

    # The crew flow and the plan card both resolve their row from this payload.
    assert served["deferred-only"]["deferred_remainder"] == {
        "sections": ["s17", "s18"],
        "count": 2,
        "done_pct": 88.9,
        "label": LABEL,
    }
    assert served["building"]["deferred_remainder"] is None


def test_descoping_both_sections_makes_the_plan_read_closable() -> None:
    declarations = {**_declarations(), "s17": "descoped", "s18": "descoped"}

    row = _rows(declarations)["deferred-only"]

    assert row["deferred_remainder"] is None
    assert row["readiness"] == "closable"
    assert row["closure_blockers"] == [
        {"kind": "op", "plan": "deferred-only", "ops": ["close"]}
    ]
    assert _served(declarations)["deferred-only"]["deferred_remainder"] is None


def _run_probe(module: Path, exports: str, payload: dict, probe: str) -> object:
    """Render one authored module under the shared tree harness."""

    return evaluate_jsx_render(
        module,
        probe,
        exports=exports,
        bindings={
            "inventory": payload["inventory"],
            "schedule": payload["schedule"],
            "project": PROJECT,
        },
    )


def _payload() -> dict:
    plans = _plans()
    return {
        "inventory": list(_served().values()),
        "schedule": schedule_report(PROJECT, plans, [], reference=REFERENCE),
    }


def test_the_crew_bar_draws_the_served_fact_in_both_its_label_and_its_fill() -> None:
    """The bar the crew flow draws states the fact, and does not paint a finish.

    The served schedule splits the bar for a plan at ``impl`` 1.0 at the now
    line, so a fill drawn from the split covers the bar. The drawn fill must
    instead carry the share the served fact reports, and the drawn label must
    carry its wording.
    """

    payload = _payload()
    bars = _run_probe(
        CREW_JSX,
        "\nwindow.__probe = { DerivedFlow };\n",
        payload,
        """
window.STATE = { project, inventory, schedule };
const flow = window.__probe.DerivedFlow({ runs: [], project: "deferred-remainder-sample" });
const bars = findAll(flow, node => hasClass(node, "r-derived-flow-bar"));
return Object.fromEntries(bars.map(bar => [bar.props.href, {
  label: textOf(findAll(bar, node => hasClass(node, "r-derived-flow-progress"))[0]),
  fill: findAll(bar, node => hasClass(node, "r-derived-flow-bar-done"))[0].props.style.width,
}]));
""",
    )

    served = {plan["slug"]: plan for plan in payload["inventory"]}
    drawn = bars[f"#plan/{served['deferred-only'].get('nav_key') or 'deferred-only'}"]
    assert drawn["label"] == LABEL
    # Sixteen of eighteen sections resolved: the bar shows the deferred tail,
    # never the whole span the served split would fill.
    assert served["deferred-only"]["deferred_remainder"]["done_pct"] == 88.9
    assert drawn["fill"] == "88.9%"
    # The control plan carries no fact, so its bar keeps the split's share and
    # its own percentage.
    control = served["building"].get("nav_key") or "building"
    assert bars[f"#plan/{control}"]["label"] == "50%"


def test_the_plan_card_prints_the_served_label() -> None:
    payload = _payload()
    signals = _run_probe(
        PLANS_JSX,
        "\nwindow.__probe = { ListCol: window.ReckonShell.plans.ListCol };\n",
        payload,
        """
window.STATE = { project, inventory, schedule };
const col = window.__probe.ListCol({
  route: { view: "list", slug: null },
  onNav: () => {}, onSelectPlan: () => {},
  items: inventory, sortBy: "slug", setSortBy: () => {},
  sortDir: "asc", toggleSortDir: () => {},
  filters: {}, setFilters: () => {}, onClearFilters: () => {},
  onClearContext: () => {}, onSetContext: () => {},
});
return findAll(col, node => hasClass(node, "r-compact-signal")).map(node => ({
  aria: node.props["aria-label"],
  text: textOf(node),
}));
""",
    )

    by_aria = {signal["aria"]: signal["text"] for signal in signals}
    assert by_aria["Deferred-Only: 2 deferred, not closable"] == LABEL
    assert by_aria["Building: 50%"] == "50%"
