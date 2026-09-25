"""The pushed sprint: the open status, the push op, the derived state, the audit."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import mcp as mcp_module
from reckon._schema import SPRINT_STATUS_ENUM
from reckon.project_state import (
    ProjectStateConflict,
    create_project_state,
    push_sprint,
    read_resource,
    write_resource,
)
from reckon.roadmap import build_roadmap


def _docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    create_project_state(docs, "sample")
    return docs


def _write_sprint(docs: Path, sprint_id: str, status: str, items=None) -> int:
    return write_resource(
        docs,
        "sample",
        "sprint",
        sprint_id,
        {"status": status, "items": items or []},
        0,
        create=True,
    )


def _statuses(docs: Path, *sprint_ids: str) -> dict[str, str]:
    return {
        sprint_id: read_resource(docs, "sample", "sprint", sprint_id)[0]["status"]
        for sprint_id in sprint_ids
    }


def _plan(slug: str, status: str = "pending", impl: float = 0.0) -> dict:
    return {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": status,
        "impl": impl,
        "depends_on": [],
        "effort": "S",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": [],
    }


def test_sprint_status_enum_accepts_open() -> None:
    assert "open" in SPRINT_STATUS_ENUM


def test_sprint_resource_accepts_open_status(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    assert _write_sprint(docs, "S1", "open") == 1
    data, _ = read_resource(docs, "sample", "sprint", "S1")
    assert data["status"] == "open"


def test_push_leaves_other_sprints_active(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    _write_sprint(docs, "S1", "active")
    assert _write_sprint(docs, "S2", "open") == 1
    _target, target_version = read_resource(docs, "sample", "sprint", "S2")
    result = push_sprint(docs, "sample", "S2", target_version)
    assert "demoted" not in result
    assert _statuses(docs, "S1", "S2") == {"S1": "active", "S2": "active"}


def test_push_on_a_stale_version_changes_neither_sprint(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    _write_sprint(docs, "S1", "active")
    _write_sprint(docs, "S2", "open")
    with pytest.raises(ProjectStateConflict):
        push_sprint(docs, "sample", "S2", 99)
    assert _statuses(docs, "S1", "S2") == {"S1": "active", "S2": "open"}


def test_push_rejects_an_unknown_sprint(tmp_path: Path) -> None:
    docs = _docs(tmp_path)
    _write_sprint(docs, "S1", "active")
    with pytest.raises(ValueError):
        push_sprint(docs, "sample", "S9", 1)


def test_roadmap_derives_the_unique_active_sprint_and_buckets_open() -> None:
    report = build_roadmap(
        "sample",
        [_plan("moved", "active", 0.3)],
        [
            {"id": "S1", "status": "open", "items": []},
            {"id": "S2", "status": "active", "items": ["moved"]},
            {"id": "S3", "status": "open", "items": []},
        ],
    )
    assert report["active_sprint_id"] == "S2"
    assert report["open_sprint_ids"] == ["S1", "S3"]


def test_member_progress_derives_in_progress_without_open_sprint_drift() -> None:
    report = build_roadmap(
        "sample",
        [_plan("moved", "active", 0.3)],
        [{"id": "S1", "status": "open", "items": ["moved"]}],
    )
    row = report["sprints"][0]
    assert row["derived_state"] == "in-progress"
    assert "state_drift" not in row


def test_planned_sprint_with_started_members_reports_drift() -> None:
    """A stored `planned` sprint whose members have started is a real
    contradiction: the status says the work has not begun and the members say it
    has. Only `open` and `active` describe a scheduling state that a started
    member does not contradict."""
    report = build_roadmap(
        "sample",
        [_plan("moved", "active", 0.3)],
        [{"id": "S1", "status": "planned", "items": ["moved"]}],
    )
    row = report["sprints"][0]
    assert row["derived_state"] == "in-progress"
    assert row["state_drift"] == {"stored": "planned", "derived": "in-progress"}


def test_audit_raises_two_active_sprints_at_error_severity() -> None:
    findings = mcp_module._audit_sprint_findings(
        {
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": []},
                {"id": "S2", "status": "active", "items": []},
            ],
        },
        [],
    )
    row = next(f for f in findings if f["code"] == "multiple-active-sprints")
    assert row["severity"] == "error"


def test_audit_warns_when_the_pushed_sprint_has_no_ready_work() -> None:
    findings = mcp_module._audit_sprint_findings(
        {
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": ["done-plan"]},
            ],
        },
        [_plan("done-plan", "shipped", 1.0)],
    )
    row = next(
        f for f in findings if f["code"] == "pushed-sprint-has-no-ready-work"
    )
    assert row["severity"] == "warn"


def test_audit_is_quiet_when_the_pushed_sprint_has_ready_work() -> None:
    findings = mcp_module._audit_sprint_findings(
        {
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": ["live-plan"]},
            ],
        },
        [_plan("live-plan", "active", 0.2)],
    )
    assert not [
        f for f in findings if f["code"] == "pushed-sprint-has-no-ready-work"
    ]