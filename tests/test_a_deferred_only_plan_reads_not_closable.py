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

The surfaces are checked as sources: each names the served field and neither
carries the wording, so the label can only come from the server.
"""

from __future__ import annotations

from pathlib import Path

from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle
from tests.roadmap_fixtures import plan_row

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


def test_the_surfaces_render_the_served_fact_without_deriving_it() -> None:
    crew = CREW_JSX.read_text(encoding="utf-8")
    plans = PLANS_JSX.read_text(encoding="utf-8")

    for source in (crew, plans):
        assert "deferred_remainder" in source
        # The wording is the server's; a surface that carried it would be a
        # second derivation of the fact.
        assert "not closable" not in source
