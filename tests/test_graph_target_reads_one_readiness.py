"""A graph target's ready set reads the roadmap's own readiness verdict."""

from pathlib import Path

from reckon import _plan_html, project_state
from reckon.roadmap import build_roadmap, resolve_graph_target
from reckon.serve import _derive_lifecycle, discover_plans

PLAN_PAGE = (
    '<!doctype html><html><head><meta name="docs-project" content="sample">'
    "<title>work</title></head><body><main></main></body></html>"
)


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
                "graph_handle": "release",
            },
        )
    )


def _live_inventory(docs: Path) -> tuple[list[dict], list[dict]]:
    composed = project_state.compose_project_state(docs, "sample")
    return _derive_lifecycle(
        "sample",
        discover_plans(docs, "sample", None)["inventory"],
        composed["sprints"],
        composed["blockers"],
    )


def _views(docs: Path) -> tuple[dict, dict]:
    """The roadmap and the graph target built from the same project state."""

    inventory, sprints = _live_inventory(docs)
    roadmap = build_roadmap("sample", inventory, sprints, docs_dir=docs)
    graph = resolve_graph_target(
        "release",
        {
            "sample": {
                "inventory": inventory,
                "sprints": sprints,
                "docs_dir": docs,
            }
        },
    )
    return roadmap, graph


def _row(roadmap: dict, slug: str) -> dict:
    for bucket in ("ready_now", "blocked", "deferred"):
        for row in roadmap.get(bucket, []):
            if row.get("slug") == slug:
                return {**row, "bucket": bucket}
    raise AssertionError(f"{slug} is absent from the roadmap: {roadmap.keys()}")


def _write_blocker(
    docs: Path,
    blocker_id: str,
    *,
    probe: str | None = None,
    subject: str | None = None,
) -> None:
    payload: dict = {"kind": "held", "summary": f"awaiting {blocker_id}"}
    if probe is not None:
        payload.update({"probe": probe, "subject": subject})
    project_state.write_resource(
        docs, "sample", "blocker", blocker_id, payload, 0, create=True
    )


def _project_with_one_hold(docs: Path) -> None:
    project_state.create_project_state(docs, "sample")
    _write_plan(docs, "probed", "blocked")
    _write_blocker(
        docs, "waiting", probe="path-exists", subject="research/outcome.html"
    )
    project_state.write_resource(
        docs,
        "sample",
        "sprint",
        "first",
        {
            "theme": "First",
            "status": "active",
            "items": [{"slug": "probed", "blocked_by": ["waiting"]}],
        },
        0,
        create=True,
    )


def _project_with_two_holds(docs: Path) -> None:
    """One hold whose probe arrives, one that never does."""

    project_state.create_project_state(docs, "sample")
    _write_plan(docs, "probed", "blocked")
    _write_blocker(
        docs, "waiting", probe="path-exists", subject="research/outcome.html"
    )
    _write_blocker(docs, "later", probe="path-exists", subject="research/never.html")
    project_state.write_resource(
        docs,
        "sample",
        "sprint",
        "first",
        {
            "theme": "First",
            "status": "active",
            "items": [{"slug": "probed", "blocked_by": ["waiting", "later"]}],
        },
        0,
        create=True,
    )


def _clear_one_probe(docs: Path) -> None:
    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir(exist_ok=True)
    subject.write_text("arrived")
    report = project_state.evaluate_held_blocker(docs, "sample", "waiting")
    assert report["status"] == "cleared", report


def test_graph_member_released_by_a_cleared_hold_is_ready_in_both_views(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    _project_with_one_hold(docs)

    roadmap, graph = _views(docs)
    assert _row(roadmap, "probed")["bucket"] == "blocked"
    assert graph["ready"] == []

    _clear_one_probe(docs)

    roadmap, graph = _views(docs)
    assert _row(roadmap, "probed")["bucket"] == "ready_now"
    assert graph["ready"] == ["sample:probed"]
    member = next(row for row in graph["members"] if row["slug"] == "probed")
    assert member["status"] == "blocked"


def test_partial_clear_stays_blocked_in_both_views(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    _project_with_two_holds(docs)

    _clear_one_probe(docs)

    roadmap, graph = _views(docs)
    row = _row(roadmap, "probed")
    assert row["bucket"] == "blocked", row
    assert [blocker["id"] for blocker in row["held_blockers"]] == ["later"]
    assert graph["ready"] == []
