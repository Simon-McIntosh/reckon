from __future__ import annotations

from pathlib import Path

from reckon._plan_html import write_state
from reckon.doccheck import audit_links
from reckon.project_state import _render_resource
from reckon.roadmap import build_roadmap


def _plan(
    slug: str,
    *,
    sprint: str,
    depends_on: list[str] | None = None,
    modified: str = "2026-08-25",
) -> dict:
    return {
        "slug": slug,
        "title": slug.title(),
        "type": "plan",
        "status": "active",
        "workflow_status": "active",
        "effective_status": "active",
        "impl": 0.4,
        "sprint": sprint,
        "depends_on": depends_on or [],
        "modified": modified,
        "gates": [{"id": "evidence", "verdict": "passed"}],
        "followups": [{"id": "next", "status": "open"}],
    }


def _review() -> dict:
    return {
        "reviewed_at": "2026-08-25",
        "reviewed_by": "review-session",
        "findings": [
            {
                "id": "pointer",
                "code": "active-sprint-mismatch",
                "category": "sprint",
                "severity": "warn",
                "subject": {"kind": "plan", "id": "consumer"},
                "current": False,
                "checked_at": "2026-08-24",
                "resolved_at": "",
            }
        ],
        "priority": [
            {
                "rank": 1,
                "ref": "consumer",
                "reasons": ["deadline"],
                "detail": "The delivery window is fixed.",
                "status": "active",
                "effective_status": "active",
                "impl": 0.4,
                "sprint": "later",
                "landed": False,
            },
            {
                "rank": 2,
                "ref": "foundation",
                "reasons": ["unlock"],
                "detail": "The consumer requires this foundation.",
                "status": "active",
                "effective_status": "active",
                "impl": 0.4,
                "sprint": "earlier",
                "landed": False,
            },
        ],
        "sprint_order": ["later", "earlier"],
    }


def test_roadmap_review_block_is_optional_and_advisory() -> None:
    inventory = [
        _plan("foundation", sprint="earlier", modified="2026-08-26"),
        _plan("consumer", sprint="later", depends_on=["foundation"]),
    ]
    sprints = [
        {"id": "earlier", "status": "active", "items": ["foundation"]},
        {"id": "later", "status": "planned", "items": ["consumer"]},
    ]

    absent = build_roadmap("unmounted", inventory, sprints, review={})
    report = build_roadmap("unmounted", inventory, sprints, review=_review())

    assert absent["review"] is None
    assert report["review"]["reviewed_at"] == "2026-08-25"
    assert report["review"]["reviewed_by"] == "review-session"
    assert report["review"]["priority"] == _review()["priority"]
    assert report["review"]["sprint_order"] == ["later", "earlier"]
    assert report["review"]["findings"][0] == {
        **_review()["findings"][0],
        "subject_found": True,
        "subject_status": "active",
        "current": False,
    }
    by_code = {item["code"]: item for item in report["wiring_findings"]}
    assert by_code["priority-order-inversion"]["severity"] == "warn"
    assert by_code["priority-order-inversion"]["extra"] == {
        "plan": "consumer",
        "rank": 1,
        "dependency": "foundation",
        "dependency_rank": 2,
    }
    assert by_code["review-stale"]["severity"] == "warn"
    assert by_code["review-stale"]["extra"]["subjects"] == [
        "plan:consumer",
        "plan:foundation",
    ]
    assert [row["slug"] for row in report["ready_now"]] == ["foundation"]


def _write_plan(docs: Path, slug: str) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(
            '<!doctype html><html><head><meta name="docs-project" content="sample">'
            f"<title>{slug}</title></head><body><main></main></body></html>",
            {"slug": slug, "title": slug.title(), "status": "active", "version": 0},
        ),
        encoding="utf-8",
    )
    return path


def _audit_review(docs: Path) -> Path:
    finding = {
        "id": "missing-subject",
        "code": "missing-owner",
        "category": "broken-category",
        "severity": "warn",
        "subject": {"kind": "plan", "id": "ghost"},
        "evidence": ["The referenced plan is absent."],
        "recommended_action": {
            "verb": "resolve",
            "owner_skill": "/reckon-edit",
            "detail": "Repair or resolve the finding.",
        },
        "validated": "confirmed",
        "checked_at": "2026-08-26",
        "resolved_at": "",
        "resolved_by": "",
        "outcome": "",
    }
    review = {
        "id": "review",
        "type": "review",
        "version": 1,
        "reviewed_at": "2026-08-26",
        "reviewed_by": "review-session",
        "basis": "roadmap at a stable revision",
        "findings": [finding],
        "priority": [
            {"rank": 2, "ref": "alpha", "reasons": ["unlock"], "detail": "First."}
        ],
    }
    path = docs / "state" / "sample" / "review.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_resource("sample", "review", "review", review), encoding="utf-8")
    return path


def test_review_audit_reports_conformance_without_mutating(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    plan = _write_plan(docs, "alpha")
    review = _audit_review(docs)
    before = review.read_bytes()

    results = audit_links([plan], docs, project="sample")

    assert review.read_bytes() == before
    assert {finding.code for finding in results[review]} == {
        "dangling-review-subject",
        "review-enum-violation",
        "review-priority-noncontiguous",
    }
    assert all(finding.severity == "warn" for finding in results[review])
