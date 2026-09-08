"""The schedule axis honours the declared window width, and says when it is undeclared."""

from __future__ import annotations

from reckon.roadmap import build_roadmap


def _plan(
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
    sprint: str | None = None,
) -> dict:
    return {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": [],
        "sprint": sprint,
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }


def _result(declared: dict[str, int]) -> dict:
    inventory = [
        _plan("one", sprint="first"),
        _plan("two", sprint="second"),
        _plan("three", sprint="third"),
        _plan("fourth", sprint="fourth"),
        _plan("finished", status="shipped", impl=1.0, sprint="closed"),
    ]
    sprints = [
        {"id": "closed", "status": "done", "items": ["finished"]},
        {"id": "first", "status": "active", "items": ["one"]},
        {"id": "second", "status": "planned", "items": ["two"]},
        {"id": "third", "status": "planned", "items": ["three"]},
        {"id": "fourth", "status": "planned", "items": ["fourth"]},
    ]
    return build_roadmap("sample", inventory, sprints, project_manifest=declared)


def test_a_width_of_three_defers_a_fourth_open_sprint_behind_the_boundary() -> None:
    result = _result({"schedule_horizon_sprints": 3})

    assert result["schedule"] == {
        "configured": True,
        "configuration_key": "schedule_horizon_sprints",
        "window_sprints": 3,
        "horizon_depth": 4,
        "open_sprints": ["first", "second", "third", "fourth"],
        "earliest_open_sprint": "first",
        "ready_sprints": ["first", "second", "third"],
        "ready": 3,
        "deferred": 1,
    }
    rows = {row["slug"]: row for row in result["pending_work"]}
    assert rows["three"]["schedule_ready"] is True
    assert rows["fourth"]["schedule_readiness"] == "deferred"
    assert rows["fourth"]["schedule_behind_sprint"] == "third"
    assert "third" in rows["fourth"]["schedule_deferred_reason"]
    assert [row["slug"] for row in result["schedule_deferred"]] == ["fourth"]


def test_an_absent_key_keeps_the_axis_unconfigured() -> None:
    result = _result({})

    assert result["schedule"]["configured"] is False
    assert result["schedule"]["configuration_key"] == "schedule_horizon_sprints"
    assert result["schedule"]["window_sprints"] is None
    assert result["schedule_deferred"] == []
    assert all(row["schedule_ready"] for row in result["pending_work"])
