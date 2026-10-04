"""A cleared held blocker releases a plan persisted as blocked."""

from pathlib import Path

from reckon import _plan_html, project_state
from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle, discover_plans

PLAN_PAGE = (
    '<!doctype html><html><head><meta name="docs-project" content="sample">'
    "<title>work</title></head><body><main></main></body></html>"
)


def _roadmap_for(docs: Path) -> dict:
    composed = project_state.compose_project_state(docs, "sample")
    inventory, sprints = _derive_lifecycle(
        "sample",
        discover_plans(docs, "sample", None)["inventory"],
        composed["sprints"],
        composed["blockers"],
    )
    return build_roadmap("sample", inventory, sprints, docs_dir=docs)


def _write_plan(docs: Path, slug: str, status: str) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _plan_html.write_state(
            PLAN_PAGE,
            {
                "type": "plan",
                "slug": slug,
                "title": slug,
                "status": status,
                "impl": 0.5,
            },
        )
    )


def _row(roadmap: dict, slug: str) -> dict:
    for bucket in ("ready_now", "blocked", "deferred"):
        for row in roadmap.get(bucket, []):
            if row.get("slug") == slug:
                return {**row, "bucket": bucket}
    raise AssertionError(f"{slug} is absent from the roadmap: {roadmap.keys()}")


def _project_with_a_held_plan_and_an_orphan(docs: Path) -> None:
    """One plan held by a probe, one stored blocked with no blocker resource."""

    project_state.create_project_state(docs, "sample")
    _write_plan(docs, "probed", "blocked")
    _write_plan(docs, "orphan", "blocked")
    project_state.write_resource(
        docs,
        "sample",
        "blocker",
        "waiting",
        {
            "kind": "held",
            "summary": "Waiting for an outcome",
            "probe": "path-exists",
            "subject": "research/outcome.html",
        },
        0,
        create=True,
    )
    project_state.write_resource(
        docs,
        "sample",
        "sprint",
        "first",
        {
            "theme": "First",
            "status": "active",
            "items": [
                {"slug": "probed", "blocked_by": ["waiting"]},
                {"slug": "orphan", "blocked_by": []},
            ],
        },
        0,
        create=True,
    )


def test_cleared_held_blocker_releases_a_plan_stored_blocked(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    _project_with_a_held_plan_and_an_orphan(docs)

    held = _row(_roadmap_for(docs), "probed")
    assert held["bucket"] == "blocked", held
    assert [row["id"] for row in held["held_blockers"]] == ["waiting"]

    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir()
    subject.write_text("arrived")
    report = project_state.evaluate_held_blocker(docs, "sample", "waiting")
    assert report["status"] == "cleared", report

    released = _row(_roadmap_for(docs), "probed")
    assert released["bucket"] == "ready_now", released
    assert released["readiness"] == "ready"
    assert released["ready"] is True
    assert released["held_blockers"] == []
    assert released["explicit_blockers"] == []
    assert released["effective_status"] == "active"


def test_plan_stored_blocked_without_a_blocker_resource_stays_blocked(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    _project_with_a_held_plan_and_an_orphan(docs)

    before = _row(_roadmap_for(docs), "orphan")
    assert before["bucket"] == "blocked"
    assert before["explicit_blockers"] == [{"kind": "persisted", "id": "unrecorded"}]

    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir()
    subject.write_text("arrived")
    assert (
        project_state.evaluate_held_blocker(docs, "sample", "waiting")["status"]
        == "cleared"
    )

    after = _row(_roadmap_for(docs), "orphan")
    assert after["bucket"] == "blocked", after
    assert after["readiness"] == "blocked"
    assert after["ready"] is False
    assert after["explicit_blockers"] == [{"kind": "persisted", "id": "unrecorded"}]
    assert after["effective_status"] == "blocked"
