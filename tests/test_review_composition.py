from __future__ import annotations

from pathlib import Path

from reckon import _plan_html
from reckon.project_state import create_project_state, read_resource, write_resource
from reckon.serve import discover_plans


def _write_plan(
    docs: Path,
    slug: str,
    *,
    status: str,
    impl: float,
    sprint: str,
    modified: str,
) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": status,
                "impl": impl,
                "sprint": sprint,
                "modified": modified,
            },
        ),
        encoding="utf-8",
    )


def _review() -> dict:
    return {
        "reviewed_at": "2026-08-26",
        "reviewed_by": "reviewer",
        "basis": "project state at the review boundary",
        "findings": [
            {
                "id": "alpha-scope",
                "code": "scope-needs-review",
                "category": "sprint",
                "severity": "warn",
                "subject": {"kind": "plan", "id": "alpha"},
                "evidence": ["Alpha still carried the broad scope."],
                "recommended_action": {
                    "verb": "rescope",
                    "owner_skill": "/reckon-sprint",
                    "detail": "Narrow the plan to its executable boundary.",
                },
                "validated": "confirmed",
                "checked_at": "2026-08-26",
                "resolved_at": "",
                "resolved_by": "",
                "outcome": "",
            }
        ],
        "priority": [
            {
                "rank": 1,
                "ref": "alpha",
                "reasons": ["critical-path"],
                "detail": "Alpha unlocks the next integration.",
            },
            {
                "rank": 2,
                "ref": "beta",
                "reasons": ["roi"],
                "detail": "Beta carries the next highest return.",
            },
        ],
    }


def test_discovery_composes_live_sprint_metrics_review_rows_and_order(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    create_project_state(docs, "sample")
    _write_plan(
        docs,
        "alpha",
        status="active",
        impl=0.5,
        sprint="S2",
        modified="2026-08-26",
    )
    _write_plan(
        docs,
        "beta",
        status="shipped",
        impl=1.0,
        sprint="S1",
        modified="2026-08-25",
    )
    for sprint_id, items in (
        ("S1", [{"slug": "beta"}]),
        ("S2", [{"slug": "alpha"}]),
        ("S3", []),
    ):
        write_resource(
            docs,
            "sample",
            "sprint",
            sprint_id,
            {"theme": sprint_id, "status": "active", "items": items},
            0,
            create=True,
        )
    write_resource(
        docs, "sample", "review", "review", _review(), 0, create=True
    )

    first = discover_plans(docs, "sample", docs / "state")
    by_id = {sprint["id"]: sprint for sprint in first["sprints"]}
    assert by_id["S2"]["metrics"] == {
        "item_count": 1,
        "by_effective_status": {"active": 1},
        "mean_impl": 0.5,
        "current_work": [
            {
                "slug": "alpha",
                "title": "Alpha",
                "effective_status": "active",
                "impl": 0.5,
            }
        ],
    }
    assert first["review"]["findings"][0]["subject_found"] is True
    assert first["review"]["findings"][0]["current"] is True
    assert first["review"]["priority"][0] | {} == {
        **first["review"]["priority"][0],
        "status": "active",
        "effective_status": "active",
        "impl": 0.5,
        "sprint": "S2",
        "landed": False,
    }
    assert first["review"]["priority"][1]["landed"] is True
    assert first["review"]["sprint_order"] == ["S2", "S1", "S3"]
    _, review_version = read_resource(docs, "sample", "review", "review")

    _write_plan(
        docs,
        "alpha",
        status="shipped",
        impl=1.0,
        sprint="S2",
        modified="2026-08-27",
    )
    second = discover_plans(docs, "sample", docs / "state")

    assert second["review"]["findings"][0]["subject_status"] == "shipped"
    assert second["review"]["findings"][0]["current"] is False
    assert second["review"]["priority"][0]["landed"] is True
    assert next(s for s in second["sprints"] if s["id"] == "S2")["metrics"] == {
        "item_count": 1,
        "by_effective_status": {"shipped": 1},
        "mean_impl": 1.0,
        "current_work": [],
    }
    assert read_resource(docs, "sample", "review", "review")[1] == review_version
