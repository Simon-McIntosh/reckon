"""A gate over finished work stops holding the plan that carries it.

A gate holds a section only while the plan still declares that section
implementable, and a settled gate holds nothing at all. Six plans built from the
shared inventory builder separate the cases, and three readers of the one split
are checked against each other.

``open-gate-on-done`` and ``open-gate-on-deferred`` name a section the plan has
resolved, and ``closed-failed-on-deferred`` is a concluded gate over the same
resolved section: none of the three holds anything, so its gate appears in
neither hold list. ``closed-failed-on-implementable`` is the same concluded gate
over a section that is still work — the verdict records the outcome and the work
proceeds without it, so it holds nothing either, where an open gate over that
section would hold it. ``open-gate-on-implementable`` is the control for that:
the section it names is still work and the gate is open, so it holds the plan
and the plan reads ``blocked``. ``gate-over-finished-beside-live-work`` keeps an
implementable ``s2`` beside a gate over a done ``s1``: the gate holds neither
the plan nor ``s2``, and ``s2`` stays ready.

The declared mutation restores the split's reading of a plan whose implementable
sections are all inside the unpassed gates' scope, where every unpassed gate
becomes plan-holding again and no gate is read as settled. Under it the first
three plans report their gate in ``gate_blockers``, the fourth does too, and the
sixth reports it in ``section_gate_blockers``, so six of the seven tests fail
and only the control passes.
"""

from __future__ import annotations

from reckon.mcp_views import ResourceSelector, resource_view
from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle
from tests.roadmap_fixtures import plan_row

PROJECT = "finished-work-gates"
SPRINT = [
    {
        "id": "current",
        "status": "active",
        "items": [
            "open-gate-on-done",
            "closed-failed-on-deferred",
            "open-gate-on-deferred",
            "closed-failed-on-implementable",
            "open-gate-on-implementable",
            "gate-over-finished-beside-live-work",
        ],
    }
]


def _gate(
    gate_id: str,
    *,
    gated_sections: list[str] | None = None,
    status: str = "open",
    verdict: str = "",
) -> dict:
    """Build one gate row, naming the sections it guards when given any."""

    gate = {
        "id": gate_id,
        "measure": "a check over the work a plan still holds",
        "status": status,
        "verdict": verdict,
    }
    if gated_sections is not None:
        gate["gated_sections"] = list(gated_sections)
        gate["section"] = gated_sections[0]
    return gate


def _plans() -> list[dict]:
    """Build the five plans fresh, because the derivation writes into its rows."""

    return [
        plan_row(
            "open-gate-on-done",
            impl=1.0,
            section_declarations={"s1": "done", "s2": "done"},
            gates=[_gate("g-done", gated_sections=["s1"])],
            followups=[],
        ),
        plan_row(
            "closed-failed-on-deferred",
            impl=1.0,
            section_declarations={"s1": "done", "s2": "deferred"},
            gates=[
                _gate(
                    "g-deferred-closed",
                    gated_sections=["s2"],
                    status="closed",
                    verdict="failed",
                )
            ],
            followups=[],
        ),
        plan_row(
            "open-gate-on-deferred",
            impl=1.0,
            section_declarations={"s1": "done", "s2": "deferred"},
            gates=[_gate("g-deferred-open", gated_sections=["s2"])],
            followups=[],
        ),
        plan_row(
            "open-gate-on-implementable",
            section_declarations={"s2": "implementable"},
            gates=[_gate("g-live", gated_sections=["s2"])],
        ),
        plan_row(
            "closed-failed-on-implementable",
            section_declarations={"s2": "implementable"},
            gates=[
                _gate(
                    "g-settled-live",
                    gated_sections=["s2"],
                    status="closed",
                    verdict="failed",
                )
            ],
        ),
        plan_row(
            "gate-over-finished-beside-live-work",
            section_declarations={"s1": "done", "s2": "implementable"},
            gates=[_gate("g-finished", gated_sections=["s1"])],
        ),
    ]


def _roadmap_row(slug: str) -> dict:
    result = build_roadmap(PROJECT, _plans(), SPRINT)
    for section in ("ready_now", "blocked", "deferred", "pending_work"):
        for row in result.get(section, []):
            if row["slug"] == slug:
                return row
    raise AssertionError(f"{slug} is absent from every roadmap section")


def _served_row(slug: str) -> dict:
    discovered, _hydrated = _derive_lifecycle(PROJECT, _plans(), SPRINT)
    return next(row for row in discovered if row["slug"] == slug)


def _plan_view(slug: str) -> dict:
    plan = next(row for row in _plans() if row["slug"] == slug)
    return resource_view(
        ResourceSelector(PROJECT, "plan", slug),
        1,
        plan,
        view="summary",
        provenance={},
    )


def _gate_ids(rows: list[dict]) -> list[str]:
    return [row["id"] for row in rows]


def test_an_open_gate_over_a_done_section_leaves_the_plan_unblocked() -> None:
    row = _roadmap_row("open-gate-on-done")

    assert row["readiness"] == "closable"
    assert row["gate_blockers"] == []
    assert row["section_gate_blockers"] == []
    # The gate is still an outcome to record before the plan closes, so it is
    # named where the closure list reports what stands in the way.
    assert _gate_ids(row["closure_blockers"][:-1]) == ["g-done"]


def test_a_concluded_gate_over_a_deferred_section_leaves_the_plan_unblocked() -> None:
    row = _roadmap_row("closed-failed-on-deferred")

    assert row["readiness"] == "closable"
    assert row["gate_blockers"] == []
    assert row["section_gate_blockers"] == []


def test_an_open_gate_over_a_deferred_section_leaves_the_plan_unblocked() -> None:
    row = _roadmap_row("open-gate-on-deferred")

    assert row["readiness"] == "closable"
    assert row["gate_blockers"] == []
    assert row["section_gate_blockers"] == []


def test_a_settled_gate_over_an_implementable_section_holds_nothing() -> None:
    """A recorded verdict is the plan's outcome, live section or not."""

    result = build_roadmap(PROJECT, _plans(), SPRINT)
    row = _roadmap_row("closed-failed-on-implementable")

    assert row["readiness"] == "ready"
    assert row["gate_blockers"] == []
    assert row["section_gate_blockers"] == []
    assert "closed-failed-on-implementable" in [
        ready["slug"] for ready in result["ready_now"]
    ]


def test_an_open_gate_over_an_implementable_section_still_holds_the_plan() -> None:
    row = _roadmap_row("open-gate-on-implementable")

    assert row["readiness"] == "blocked"
    assert _gate_ids(row["gate_blockers"]) == ["g-live"]
    assert row["section_gate_blockers"] == []


def test_a_gate_over_finished_work_beside_live_work_holds_neither() -> None:
    result = build_roadmap(PROJECT, _plans(), SPRINT)
    row = _roadmap_row("gate-over-finished-beside-live-work")

    assert row["readiness"] == "ready"
    assert row["gate_blockers"] == []
    assert row["section_gate_blockers"] == []
    assert "gate-over-finished-beside-live-work" in [
        ready["slug"] for ready in result["ready_now"]
    ]


def test_every_reader_of_the_split_agrees_on_which_plans_a_gate_holds() -> None:
    """The roadmap, the served inventory and the plan view read the one split."""

    for plan in _plans():
        slug = plan["slug"]
        row = _roadmap_row(slug)
        served = _served_row(slug)
        view = _plan_view(slug)

        blocked = row["readiness"] == "blocked"
        assert (served["effective_status"] == "blocked") is blocked, slug
        assert (view["state"]["effective_status"] == "blocked") is blocked, slug
        assert _gate_ids(served["blocking"]) == _gate_ids(row["gate_blockers"]), slug
        assert _gate_ids(view["blocking"]) == _gate_ids(row["gate_blockers"]), slug
        assert _gate_ids(served["section_gate_blockers"]) == _gate_ids(
            row["section_gate_blockers"]
        ), slug
        assert _gate_ids(view["section_gate_blockers"]) == _gate_ids(
            row["section_gate_blockers"]
        ), slug
