from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon._plan_html import from_html, to_html, write_state
from reckon._schema import PlanState, Sprint, parse_plan_ref
from reckon.project_state import (
    create_project_state,
    read_resource,
    write_resource,
)
from reckon.roadmap import build_roadmap


def _plan(
    project: str,
    slug: str,
    *,
    status: str = "active",
    impl: float = 0.0,
) -> dict:
    return {
        "project": project,
        "type": "plan",
        "slug": slug,
        "title": slug.title(),
        "status": status,
        "impl": impl,
        "depends_on": [],
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }


def _write_plan(
    docs: Path,
    project: str,
    slug: str,
    *,
    status: str,
    impl: float,
    sprint: str | None = None,
) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{project}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        write_state(
            html,
            {
                "project": project,
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": status,
                "impl": impl,
                "sprint": sprint,
            },
        )
    )


def _create_sprint(docs: Path, project: str, items: list[dict[str, str]]) -> None:
    create_project_state(docs, project)
    write_resource(
        docs,
        project,
        "sprint",
        "shared",
        {"theme": "Shared", "status": "active", "items": items},
        0,
        create=True,
    )


def test_qualified_sprint_items_resolve_across_mounted_projects(
    tmp_path: Path, monkeypatch
) -> None:
    docs_by_project = {
        project: tmp_path / project / "docs" for project in ("alpha", "beta")
    }
    for docs in docs_by_project.values():
        docs.mkdir(parents=True)
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(
        json.dumps({project: str(docs) for project, docs in docs_by_project.items()})
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_path))

    _write_plan(
        docs_by_project["beta"],
        "beta",
        "remote",
        status="shipped",
        impl=1.0,
    )
    local_plans = [
        _plan("alpha", "local", impl=0.25),
        _plan("alpha", "qualified-local", impl=0.5),
    ]
    sprint = {
        "id": "current",
        "status": "active",
        "items": [
            {"slug": "local"},
            {"slug": "beta:remote"},
            {"slug": "alpha:qualified-local"},
            {"slug": "beta:missing"},
        ],
    }

    report = build_roadmap("alpha", local_plans, [sprint])
    row = report["sprints"][0]

    assert [member["ref"] for member in row["members"]] == [
        "local",
        "beta:remote",
        "alpha:qualified-local",
        "beta:missing",
    ]
    assert row["members"][0] == {
        "ref": "local",
        "scope": "local",
        "project": "alpha",
        "slug": "local",
        "found": True,
        "status": "active",
        "impl": 0.25,
        "title": "Local",
    }
    assert row["members"][1]["scope"] == "external"
    assert row["members"][1]["found"] is True
    assert row["members"][1]["status"] == "shipped"
    assert row["members"][1]["impl"] == 1.0
    assert row["members"][2]["scope"] == "local"
    assert row["members"][2]["found"] is True
    assert row["members"][2]["status"] == "active"
    assert row["members"][2]["impl"] == 0.5
    assert row["members"][3] == {
        "ref": "beta:missing",
        "scope": "external",
        "project": "beta",
        "slug": "missing",
        "found": False,
    }
    assert row["items"] == 4
    assert row["resolved_items"] == 3
    assert row["completed"] == 1
    assert row["lifecycle_completion_pct"] == 25.0
    assert row["implementation_pct"] == 43.8
    assert report["schedule"]["open_sprints"] == ["current"]

    unresolved = [
        finding
        for finding in report["wiring_findings"]
        if finding["code"] == "unresolved-sprint-item"
    ]
    assert len(unresolved) == 1
    assert unresolved[0]["extra"] == {
        "sprint": "current",
        "ref": "beta:missing",
    }


def test_sprint_item_schema_uses_the_plan_ref_grammar() -> None:
    sprint = Sprint.model_validate(
        {
            "id": "current",
            "items": ["bare", "other:qualified", "owner:qualified-local"],
        }
    )

    assert [item.slug for item in sprint.items] == [
        "bare",
        "other:qualified",
        "owner:qualified-local",
    ]
    assert all(parse_plan_ref(item.slug) is not None for item in sprint.items)


def test_existing_bare_sprint_items_keep_local_resolution() -> None:
    report = build_roadmap(
        "alpha",
        [_plan("alpha", "bare", status="shipped", impl=1.0)],
        [{"id": "current", "status": "active", "items": ["bare"]}],
    )

    assert report["sprints"][0]["members"] == [
        {
            "ref": "bare",
            "scope": "local",
            "project": "alpha",
            "slug": "bare",
            "found": True,
            "status": "shipped",
            "impl": 1.0,
            "title": "Bare",
        }
    ]
    assert not any(
        finding["code"] == "unresolved-sprint-item"
        for finding in report["wiring_findings"]
    )


def test_qualified_sprint_item_round_trips_through_canonical_edit(
    tmp_path: Path, monkeypatch
) -> None:
    docs_by_project = {
        project: tmp_path / project / "docs" for project in ("alpha", "beta")
    }
    for docs in docs_by_project.values():
        docs.mkdir(parents=True)
    _write_plan(
        docs_by_project["beta"],
        "beta",
        "remote",
        status="active",
        impl=0.4,
    )
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(
        json.dumps({project: str(docs) for project, docs in docs_by_project.items()})
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_path))

    create_project_state(docs_by_project["alpha"], "alpha")
    created_version = write_resource(
        docs_by_project["alpha"],
        "alpha",
        "sprint",
        "current",
        {"theme": "Current", "status": "active", "items": []},
        0,
        create=True,
    )
    sprint, version = read_resource(
        docs_by_project["alpha"], "alpha", "sprint", "current"
    )
    assert version == created_version

    stored_version = write_resource(
        docs_by_project["alpha"],
        "alpha",
        "sprint",
        "current",
        {**sprint, "items": [{"slug": "beta:remote"}]},
        version,
    )
    stored, round_trip_version = read_resource(
        docs_by_project["alpha"], "alpha", "sprint", "current"
    )

    assert round_trip_version == stored_version
    assert stored["items"] == [{"slug": "beta:remote"}]

    with pytest.raises(ValueError, match=r"\[project:\]slug plan ref"):
        write_resource(
            docs_by_project["alpha"],
            "alpha",
            "sprint",
            "current",
            {**stored, "items": [{"slug": "bad ref"}]},
            round_trip_version,
        )


def test_qualified_plan_sprint_resolves_the_same_membership_from_either_project(
    tmp_path: Path, monkeypatch
) -> None:
    docs_by_project = {
        project: tmp_path / project / "docs" for project in ("alpha", "beta")
    }
    for docs in docs_by_project.values():
        docs.mkdir(parents=True)
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(
        json.dumps({project: str(docs) for project, docs in docs_by_project.items()})
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_path))

    _write_plan(
        docs_by_project["alpha"],
        "alpha",
        "member-a",
        status="active",
        impl=0.25,
        sprint="beta:shared",
    )
    _write_plan(
        docs_by_project["beta"],
        "beta",
        "member-b",
        status="shipped",
        impl=1.0,
        sprint="shared",
    )
    _create_sprint(
        docs_by_project["beta"],
        "beta",
        [{"slug": "alpha:member-a"}, {"slug": "member-b"}],
    )

    from_alpha = build_roadmap(
        "alpha",
        [{**_plan("alpha", "member-a", impl=0.25), "sprint": "beta:shared"}],
        [],
    )
    sprint, _version = read_resource(
        docs_by_project["beta"], "beta", "sprint", "shared"
    )
    from_beta = build_roadmap(
        "beta",
        [
            {
                **_plan("beta", "member-b", status="shipped", impl=1.0),
                "sprint": "shared",
            }
        ],
        [sprint],
    )

    alpha_row = from_alpha["sprints"][0]
    beta_row = from_beta["sprints"][0]
    assert alpha_row["ref"] == "beta:shared"
    assert alpha_row["scope"] == "external"
    assert alpha_row["found"] is True
    assert beta_row["ref"] == "shared"
    assert beta_row["scope"] == "local"
    assert alpha_row["members"] == beta_row["members"]
    assert [member["ref"] for member in alpha_row["members"]] == [
        "alpha:member-a",
        "member-b",
    ]
    assert not any(
        finding["code"].startswith("plan-sprint-")
        for finding in from_alpha["wiring_findings"]
    )


def test_unmounted_qualified_plan_sprint_remains_explicit(
    tmp_path: Path, monkeypatch
) -> None:
    mounts_path = tmp_path / "mounts.json"
    mounts_path.write_text(json.dumps({"alpha": str(tmp_path / "alpha" / "docs")}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_path))
    report = build_roadmap(
        "alpha",
        [{**_plan("alpha", "member"), "sprint": "missing:shared"}],
        [],
    )

    assert len(report["sprints"]) == 1
    assert report["sprints"][0]["ref"] == "missing:shared"
    assert report["sprints"][0]["scope"] == "external"
    assert report["sprints"][0]["found"] is False
    unresolved = [
        finding
        for finding in report["wiring_findings"]
        if finding["code"] == "unresolved-plan-sprint"
    ]
    assert len(unresolved) == 1
    assert unresolved[0]["extra"] == {"ref": "missing:shared"}


def test_qualified_plan_sprint_round_trips_and_malformed_ref_is_refused() -> None:
    source = (
        '<!doctype html><html><head><meta name="docs-project" content="alpha">'
        "<title>Member</title></head><body><main></main></body></html>"
    )
    state = PlanState.model_validate(
        {
            "project": "alpha",
            "type": "plan",
            "slug": "member",
            "title": "Member",
            "status": "active",
            "sprint": "beta:shared",
        }
    ).validate_for_write()
    round_trip = from_html(to_html(source, state))

    assert round_trip.sprint == "beta:shared"
    assert round_trip.validate_for_write() is round_trip
    with pytest.raises(ValueError, match=r"expected \[project:\]id"):
        PlanState.model_validate(
            {
                "project": "alpha",
                "type": "plan",
                "slug": "member",
                "title": "Member",
                "status": "active",
                "sprint": "bad ref",
            }
        ).validate_for_write()
