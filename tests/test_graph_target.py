"""Graph ship targets are endpoint identities with wholly derived membership."""

from __future__ import annotations

import pytest

from reckon import mcp as mcp_module
from reckon._plan_html import parse_meta, write_state
from reckon._schema import PlanState, gen_json_schema
from reckon._store import apply_ops
from reckon.roadmap import GraphTargetError, resolve_graph_target


def _plan(
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
    depends_on: list[str] | None = None,
    graph_handle: str | None = None,
    sprint: str | None = None,
    decisions: list[dict] | None = None,
) -> dict:
    return {
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": status,
        "impl": impl,
        "depends_on": depends_on or [],
        "graph_handle": graph_handle,
        "sprint": sprint,
        "decisions": decisions or [],
    }


@pytest.fixture(autouse=True)
def isolated_reckon_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))


def test_handle_resolves_current_transitive_closure_without_stored_membership() -> None:
    inventory = [
        _plan("source", status="shipped", impl=1.0),
        _plan("middle", depends_on=["source"]),
        _plan("endpoint", depends_on=["middle"], graph_handle="release"),
    ]

    result = resolve_graph_target("release", {"alpha": inventory})

    assert [row["ref"] for row in result["members"]] == [
        "alpha:endpoint",
        "alpha:middle",
        "alpha:source",
    ]
    assert result["endpoint"]["ref"] == "alpha:endpoint"
    assert all("members" not in plan for plan in inventory)

    inventory[1]["depends_on"] = []
    refreshed = resolve_graph_target("release", {"alpha": inventory})
    assert [row["ref"] for row in refreshed["members"]] == [
        "alpha:endpoint",
        "alpha:middle",
    ]


def test_closure_crosses_a_mounted_project_dependency() -> None:
    projects = {
        "alpha": [_plan("endpoint", depends_on=["beta:base"], graph_handle="release")],
        "beta": [
            _plan("root", status="shipped", impl=1.0),
            _plan("base", depends_on=["root"]),
        ],
    }

    result = resolve_graph_target("release", projects)

    assert result["repositories"] == ["alpha", "beta"]
    assert {row["ref"] for row in result["members"]} == {
        "alpha:endpoint",
        "beta:base",
        "beta:root",
    }


def test_closure_reports_derived_progress_critical_path_and_average_width() -> None:
    inventory = [
        _plan("root", status="shipped", impl=1.0),
        _plan("left", status="shipped", impl=1.0, depends_on=["root"]),
        _plan("right", depends_on=["root"]),
        _plan(
            "endpoint",
            depends_on=["left", "right"],
            graph_handle="release",
        ),
    ]

    result = resolve_graph_target("release", {"alpha": inventory})

    assert result["completion"] == {"shipped": 2, "total": 4}
    assert result["shipped_of_total"] == "2/4"
    assert result["critical_path"]["depth"] == 3
    assert result["critical_path"]["plans"] in (
        ["alpha:root", "alpha:left", "alpha:endpoint"],
        ["alpha:root", "alpha:right", "alpha:endpoint"],
    )
    assert result["average_width"] == 1.333


def test_missing_or_deleted_endpoint_names_the_handle() -> None:
    endpoint = _plan("endpoint", graph_handle="release")
    assert resolve_graph_target("release", {"alpha": [endpoint]})["handle"] == "release"

    with pytest.raises(GraphTargetError, match="release.*names no live plan"):
        resolve_graph_target("release", {"alpha": []})


def test_open_endpoint_decision_refuses_shipping_but_deferred_decision_does_not() -> None:
    open_endpoint = _plan(
        "endpoint",
        graph_handle="release",
        decisions=[{"key": "mode", "title": "Choose mode"}],
    )
    result = resolve_graph_target("release", {"alpha": [open_endpoint]})
    assert result["ship_ready"] is False
    assert result["decision_blockers"][0]["id"] == "mode"

    open_endpoint["decisions"][0]["rationale"] = "Explicitly deferred"
    assert resolve_graph_target("release", {"alpha": [open_endpoint]})[
        "ship_ready"
    ] is True


def test_schedule_deferred_members_are_reported_as_a_visible_override() -> None:
    inventory = [
        _plan("source", sprint="S1"),
        _plan(
            "endpoint",
            depends_on=["source"],
            graph_handle="release",
            sprint="S2",
        ),
    ]
    sprints = [
        {"id": "S1", "status": "active", "items": [{"slug": "source"}]},
        {"id": "S2", "status": "planned", "items": [{"slug": "endpoint"}]},
    ]

    result = resolve_graph_target(
        "release",
        {
            "alpha": {
                "inventory": inventory,
                "sprints": sprints,
                "project_manifest": {"schedule_horizon_sprints": 1},
            }
        },
    )

    assert result["schedule_override"] == {
        "required": True,
        "deferred": 1,
        "members": ["alpha:endpoint"],
    }


def test_graph_handle_round_trips_through_schema_html_store_and_mcp_inventory(
    tmp_path,
) -> None:
    state = PlanState(project="alpha", slug="endpoint", graph_handle="release")
    assert state.graph_handle == "release"
    assert "graph_handle" in gen_json_schema()["properties"]
    assert "graph_members" not in gen_json_schema()["properties"]

    html = write_state(
        "<html><head></head><body></body></html>",
        {"graph_handle": "release"},
    )
    path = tmp_path / "endpoint.html"
    path.write_text(html)
    assert parse_meta(path)["graph_handle"] == "release"

    working = {"type": "plan", "graph_handle": None}
    apply_ops(
        working,
        [{"op": "set", "path": "graph_handle", "value": "release"}],
        is_index=False,
    )
    assert working["graph_handle"] == "release"
    assert mcp_module._inventory_row(
        {"type": "plan", "slug": "endpoint", "graph_handle": "release"}
    )["graph_handle"] == "release"


def test_mcp_graph_target_reads_every_mounted_project(monkeypatch) -> None:
    inventories = {
        "alpha": [
            _plan("endpoint", depends_on=["beta:base"], graph_handle="release")
        ],
        "beta": [_plan("base", status="shipped", impl=1.0)],
    }
    monkeypatch.setattr(
        mcp_module,
        "_list_projects",
        lambda: {"projects": [{"name": "alpha"}, {"name": "beta"}]},
    )
    monkeypatch.setattr(
        mcp_module,
        "_discover_project",
        lambda project, root=None: {
            "inventory": inventories[project],
            "sprints": [],
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "read_plan",
        lambda project, slug, checkout_path=None: (
            {"projects": [{"name": project}]},
            0,
        ),
    )
    monkeypatch.setattr(mcp_module, "list_followups_across", lambda *args, **kwargs: [])

    result = mcp_module._roadmap("graph:release", view="raw")

    assert result["target"] == "graph:release"
    assert result["shipped_of_total"] == "1/2"
    assert result["repositories"] == ["alpha", "beta"]
