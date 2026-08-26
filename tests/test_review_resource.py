from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from reckon import mcp as mcp_module
from reckon.project_state import (
    RESOURCE_SCRIPT_ID,
    create_project_state,
    read_resource,
    resource_path,
    write_resource,
)


def _finding() -> dict:
    return {
        "id": "active-pointer",
        "code": "active-sprint-mismatch",
        "category": "sprint",
        "severity": "error",
        "subject": {"kind": "sprint", "id": "current"},
        "evidence": [
            "2026-08-26: project pointer named planned sprint current",
            "commit c62a9fa: sprint next carried status active",
        ],
        "recommended_action": {
            "verb": "repair-pointer",
            "owner_skill": "/reckon-sprint",
            "detail": "Point active_sprint_id at the uniquely active sprint.",
        },
        "validated": "confirmed",
        "checked_at": "2026-08-26",
        "resolved_at": "",
        "resolved_by": "",
        "outcome": "",
    }


def _priority() -> list[dict]:
    return [
        {
            "rank": 1,
            "ref": "alpha",
            "reasons": ["critical-path", "unlock"],
            "detail": "Unblocks the remaining project-state consumers.",
        },
        {
            "rank": 2,
            "ref": "other:beta",
            "reasons": ["deadline"],
            "detail": "Required before the dated integration window.",
        },
    ]


def _review_ops(
    *, finding: dict | None = None, priority: list[dict] | None = None
) -> list[dict]:
    return [
        {"op": "set", "path": "reviewed_at", "value": "2026-08-26"},
        {"op": "set", "path": "reviewed_by", "value": "review-session"},
        {
            "op": "set",
            "path": "basis",
            "value": "roadmap at commit c62a9fa",
        },
        {"op": "set", "path": "priority", "value": priority or _priority()},
        {"op": "append", "target": "findings", "item": finding or _finding()},
    ]


def _distributed_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    docs = checkout / "docs"
    docs.mkdir(parents=True)
    create_project_state(docs, "sample")
    return checkout, docs


def test_edit_plan_round_trips_a_versioned_review_and_derives_resolution(
    tmp_path: Path,
) -> None:
    checkout, docs = _distributed_checkout(tmp_path)
    write_resource(
        docs,
        "sample",
        "sprint",
        "current",
        {"theme": "Current", "status": "active", "items": []},
        0,
        create=True,
    )

    created = mcp_module._edit_plan_tool(
        "sample",
        "review",
        expected_version=0,
        ops=_review_ops(),
        create=True,
        checkout_path=str(checkout),
        doc_type="review",
    )

    assert created["ok"] is True
    assert created["new_version"] == 1
    assert created["path"] == str(docs / "state/sample/review.html")
    read_back = mcp_module._read_plan(
        "sample", "review", checkout_path=str(checkout), doc_type="review"
    )
    assert read_back["version"] == 1
    assert read_back["data"]["findings"] == [{**_finding(), "status": "open"}]
    assert read_back["data"]["priority"] == _priority()

    resolved = mcp_module._edit_plan_tool(
        "sample",
        "review",
        expected_version=1,
        ops=[
            {
                "op": "resolve",
                "target": "findings",
                "id": "active-pointer",
                "by": "repair-session",
                "outcome": "The pointer now names the active sprint.",
            }
        ],
        checkout_path=str(checkout),
        doc_type="review",
    )
    assert resolved["ok"] is True
    assert resolved["new_version"] == 2
    review, review_version = read_resource(docs, "sample", "review", "review")
    _, sprint_version = read_resource(docs, "sample", "sprint", "current")
    assert review_version == 2
    assert sprint_version == 1
    assert review["findings"][0]["status"] == "resolved"
    assert review["findings"][0]["resolved_at"]

    html = resource_path(docs, "sample", "review", "review").read_text()
    state_text = re.search(
        rf'<script[^>]+id="{RESOURCE_SCRIPT_ID}">(.*?)</script>', html, re.DOTALL
    ).group(1)
    stored = json.loads(state_text)
    assert "status" not in stored["findings"][0]


@pytest.mark.parametrize(
    ("mutate", "offending_field"),
    [
        (lambda finding, priority: finding.update(category="unknown"), "findings[0].category"),
        (lambda finding, priority: finding.update(evidence=[]), "findings[0].evidence"),
        (lambda finding, priority: priority[0].update(rank=2), "priority[0].rank"),
        (
            lambda finding, priority: priority[1].update(ref=priority[0]["ref"]),
            "priority[1].ref",
        ),
        (
            lambda finding, priority: finding.update(
                subject={"kind": "plan", "id": "bad::ref"}
            ),
            "findings[0].subject.id",
        ),
    ],
)
def test_review_strict_write_names_invalid_fields(
    tmp_path: Path, mutate, offending_field: str
) -> None:
    checkout, _ = _distributed_checkout(tmp_path)
    finding = deepcopy(_finding())
    priority = deepcopy(_priority())
    mutate(finding, priority)

    rejected = mcp_module._edit_plan_tool(
        "sample",
        "review",
        expected_version=0,
        ops=_review_ops(finding=finding, priority=priority),
        create=True,
        checkout_path=str(checkout),
        doc_type="review",
    )

    assert rejected["ok"] is False
    assert rejected["error"] == "resource_edit_error"
    assert offending_field in rejected["detail"]


def test_review_priority_can_only_be_set_as_one_whole_list(tmp_path: Path) -> None:
    checkout, _ = _distributed_checkout(tmp_path)
    created = mcp_module._edit_plan_tool(
        "sample",
        "review",
        expected_version=0,
        ops=_review_ops(),
        create=True,
        checkout_path=str(checkout),
        doc_type="review",
    )
    assert created["ok"] is True

    rejected = mcp_module._edit_plan_tool(
        "sample",
        "review",
        expected_version=1,
        ops=[{"op": "set", "path": "priority.0.rank", "value": 2}],
        checkout_path=str(checkout),
        doc_type="review",
    )

    assert rejected["ok"] is False
    assert "priority.0.rank" in rejected["detail"]
