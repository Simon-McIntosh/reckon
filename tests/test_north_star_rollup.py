from __future__ import annotations

import reckon.mcp as mcp_module


def test_roadmap_inventory_preserves_plan_orientation(monkeypatch) -> None:
    directions = [
        {
            "id": "truthful-state",
            "name": "Truthful state",
            "statement": "Every reported state is backed by evidence.",
        },
        {
            "id": "portable-execution",
            "name": "Portable execution",
            "statement": "Execution contracts hold across harnesses.",
        },
    ]
    plans = [
        {
            "slug": "state-reader",
            "title": "State reader",
            "type": "plan",
            "status": "active",
            "north_star": "truthful-state",
        },
        {
            "slug": "backend-contract",
            "title": "Backend contract",
            "type": "plan",
            "status": "active",
            "north_star": "portable-execution",
        },
    ]

    monkeypatch.setattr(
        mcp_module,
        "_discover_project",
        lambda project, root=None: {"inventory": plans, "sprints": []},
    )
    monkeypatch.setattr(
        mcp_module,
        "read_plan",
        lambda project, slug, root=None: (
            {"projects": [{"north_stars": directions}]},
            0,
        ),
    )
    monkeypatch.setattr(mcp_module, "list_followups_across", lambda *args, **kwargs: [])

    report = mcp_module._roadmap("sample")

    assert [row["plans"] for row in report["north_stars"]] == [1, 1]
    assert not any(
        finding["code"] == "unoriented-plan"
        for finding in report["wiring_findings"]
        if finding.get("slug") in {"state-reader", "backend-contract"}
    )
