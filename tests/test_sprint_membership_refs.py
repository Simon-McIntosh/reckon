from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon._plan_html import write_state
from reckon._schema import Sprint, parse_plan_ref
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
            },
        )
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
