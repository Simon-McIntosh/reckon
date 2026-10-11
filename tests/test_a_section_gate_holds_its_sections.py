"""A gate naming sections holds those sections, not the whole plan.

Four plans separate the cases. Plan ``p`` declares an ungated implementable
``s1`` beside an implementable ``s2`` its unpassed gate names, so the gate
leaves ``s1`` reachable and holds only ``s2``: ``p`` reads unblocked, is
dispatchable, and carries the gate in the section-hold field. Plans ``q``,
``r`` and ``t`` read ``blocked``, because no section is left for the gate to
spare: ``q``'s only implementable section is the one its gate names, ``r``'s
gate names no section at all, and ``t`` declares no section.

Every reader that projects unpassed gates into ``effective_status`` is
checked, so the split is shared rather than applied in one place: the roadmap
row, the served plan, and the MCP plan view.
"""

from __future__ import annotations

from reckon.mcp_views import ResourceSelector, resource_view
from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle

PROJECT = "sample"
SPRINT = [{"id": "current", "status": "active", "items": ["p", "q", "r", "t"]}]


def _gate(gate_id: str, *, gated_sections: list[str] | None = None) -> dict:
    gate = {
        "id": gate_id,
        "section": "s1",
        "status": "closed",
        "measure": "a check that has not passed",
        "verdict": "failed",
        "evidence": f"/sample/evidence/{gate_id}",
    }
    if gated_sections is not None:
        gate["gated_sections"] = gated_sections
    return gate


def _plan(
    slug: str,
    *,
    declarations: dict[str, str] | None = None,
    gates: list[dict] | None = None,
) -> dict:
    plan = {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": "active",
        "impl": 0.0,
        "depends_on": [],
        "sprint": None,
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": gates if gates is not None else [],
        "followups": [
            {"id": "next", "status": "open", "prompt": f"/reckon-build {slug}"}
        ],
    }
    if declarations is not None:
        plan["section_declarations"] = declarations
    return plan


def _plans() -> list[dict]:
    return [
        # p: s1 is free, the unpassed gate names only s2.
        _plan(
            "p",
            declarations={"s1": "implementable", "s2": "implementable"},
            gates=[_gate("g-p", gated_sections=["s2"])],
        ),
        # q: its only implementable section is the one the gate names.
        _plan(
            "q",
            declarations={"s2": "implementable"},
            gates=[_gate("g-q", gated_sections=["s2"])],
        ),
        # r: the gate names no section, so it holds the whole plan.
        _plan(
            "r",
            declarations={"s1": "implementable"},
            gates=[_gate("g-r")],
        ),
        # t: no declarations at all; the section-naming gate still holds it.
        _plan("t", gates=[_gate("g-t", gated_sections=["s2"])]),
    ]


def _roadmap_rows() -> dict[str, dict]:
    result = build_roadmap(PROJECT, _plans(), SPRINT)
    rows = {
        row["slug"]: row
        for section in ("ready_now", "blocked", "deferred")
        for row in result.get(section, [])
    }
    assert set(rows) == {"p", "q", "r", "t"}, result
    return rows


def _served_plans() -> dict[str, dict]:
    discovered, _hydrated = _derive_lifecycle(PROJECT, _plans(), SPRINT)
    return {plan["slug"]: plan for plan in discovered}


def _served_plan(slug: str) -> dict:
    return _served_plans()[slug]


def _mcp_view(slug: str) -> dict:
    plan = next(plan for plan in _plans() if plan["slug"] == slug)
    return resource_view(
        ResourceSelector(PROJECT, "plan", slug),
        1,
        plan,
        view="summary",
        provenance={},
    )


def test_an_ungated_section_keeps_the_plan_unblocked_across_every_reader() -> None:
    rows = _roadmap_rows()
    row = rows["p"]

    # The roadmap row is unblocked, dispatchable, and holds the gate in the
    # section field rather than the plan's blocker list.
    assert row["effective_status"] != "blocked"
    assert row["readiness"] == "ready"
    assert row["gate_blockers"] == []
    assert [
        (gate["id"], gate["gated_sections"]) for gate in row["section_gate_blockers"]
    ] == [("g-p", ["s2"])]
    assert "p" in [ready["slug"] for ready in build_roadmap(PROJECT, _plans(), SPRINT)["ready_now"]]

    served = _served_plan("p")
    assert served["effective_status"] != "blocked"
    assert served["blocking"] == []
    assert [gate["id"] for gate in served["section_gate_blockers"]] == ["g-p"]

    view = _mcp_view("p")
    assert view["state"]["effective_status"] != "blocked"
    assert view["blocking"] == []
    assert [gate["id"] for gate in view["section_gate_blockers"]] == ["g-p"]


def test_a_gate_with_no_free_section_holds_the_whole_plan() -> None:
    rows = _roadmap_rows()

    for slug, gate_id in (("q", "g-q"), ("r", "g-r"), ("t", "g-t")):
        row = rows[slug]
        assert row["effective_status"] == "blocked", slug
        assert [gate["id"] for gate in row["gate_blockers"]] == [gate_id], slug
        assert row["section_gate_blockers"] == [], slug

        served = _served_plan(slug)
        assert served["effective_status"] == "blocked", slug
        assert [gate["id"] for gate in served["blocking"]] == [gate_id], slug

        view = _mcp_view(slug)
        assert view["state"]["effective_status"] == "blocked", slug
        assert [gate["id"] for gate in view["blocking"]] == [gate_id], slug
