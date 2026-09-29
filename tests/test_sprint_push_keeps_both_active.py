"""Pushing a second sprint leaves the first active, and the audit is quiet.

Pushing a sprint once demoted the previously active sprint and the audit raised
a ``multiple-active-sprints`` error whenever two sprints carried ``active``.
Both are retired: push sets ``active`` on its target and leaves every other
sprint's stored status alone, so several active sprints are an ordinary
scheduling state. The fixture builds a distributed project under a temporary
config home, so the push and the audit exercise the real code paths without
reading the workstation's live crew state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import mcp as mcp_module
from reckon.project_state import (
    create_project_state,
    push_sprint,
    read_resource,
    write_resource,
)

PROJECT = "two-active"


def _docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    create_project_state(docs, PROJECT)
    return docs


def _write_plan(docs: Path, slug: str, sprint: str) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="pending">'
        '<meta name="plan-impl" content="0.0">'
        f'<meta name="plan-sprint" content="{sprint}">'
        "</head><body></body></html>",
        encoding="utf-8",
    )


def _write_sprint(docs: Path, sprint_id: str, status: str, items: list[str]) -> int:
    return write_resource(
        docs,
        PROJECT,
        "sprint",
        sprint_id,
        {"status": status, "items": items},
        0,
        create=True,
    )


def _statuses(docs: Path, *sprint_ids: str) -> dict[str, str]:
    return {
        sprint_id: read_resource(docs, PROJECT, "sprint", sprint_id)[0]["status"]
        for sprint_id in sprint_ids
    }


def test_push_keeps_both_sprints_active_and_the_audit_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config-home"))
    docs = _docs(tmp_path)
    _write_plan(docs, "plan-one", "S1")
    _write_plan(docs, "plan-two", "S2")
    _write_sprint(docs, "S1", "open", ["plan-one"])
    _write_sprint(docs, "S2", "open", ["plan-two"])

    for sprint_id in ("S1", "S2"):
        _, version = read_resource(docs, PROJECT, "sprint", sprint_id)
        result = push_sprint(docs, PROJECT, sprint_id, version)
        assert "demoted" not in result

    assert _statuses(docs, "S1", "S2") == {"S1": "active", "S2": "active"}

    audit = mcp_module._audit(PROJECT, checkout_path=str(tmp_path))
    assert audit.get("ok") is not False, audit
    codes = [finding["code"] for finding in audit["findings"]]
    assert "multiple-active-sprints" not in codes
