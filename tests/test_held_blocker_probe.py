"""A held blocker is released only by a registered probe finding."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, held_probes, project_state
from reckon.cli import main
from reckon.roadmap import build_roadmap
from reckon.serve import _derive_lifecycle, discover_plans


def _roadmap_for(docs: Path) -> dict:
    composed = project_state.compose_project_state(docs, "sample")
    inventory, sprints = _derive_lifecycle(
        "sample",
        discover_plans(docs, "sample", None)["inventory"],
        composed["sprints"],
        composed["blockers"],
    )
    return build_roadmap("sample", inventory, sprints)


def test_named_probe_clears_only_when_its_subject_arrives(tmp_path: Path) -> None:
    assert callable(getattr(project_state, "evaluate_held_blocker", None))
    docs = tmp_path / "docs"
    docs.mkdir()
    project_state.create_project_state(docs, "sample")
    plan = docs / "plans" / "work.html"
    plan.parent.mkdir()
    plan.write_text(
        _plan_html.write_state(
            '<!doctype html><html><head><meta name="docs-project" content="sample">'
            "<title>work</title></head><body><main></main></body></html>",
            {
                "type": "plan",
                "slug": "work",
                "title": "Work",
                "status": "active",
                "impl": 0.5,
            },
        )
    )
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
            "items": [{"slug": "work", "blocked_by": ["waiting"]}],
        },
        0,
        create=True,
    )

    absent = project_state.evaluate_held_blocker(docs, "sample", "waiting")
    held, version = project_state.read_resource(docs, "sample", "blocker", "waiting")
    assert absent == {
        "id": "waiting",
        "probe": "path-exists",
        "subject": "research/outcome.html",
        "finding": "research/outcome.html is absent",
        "status": "held",
    }
    assert "cleared_reason" not in held
    assert version == 1
    assert project_state.compose_project_state(docs, "sample")["sprints"][0]["items"][
        0
    ]["blocked_by"] == ["waiting"]
    held_row = next(
        row for row in _roadmap_for(docs)["blocked"] if row["slug"] == "work"
    )
    assert [row["id"] for row in held_row["held_blockers"]] == ["waiting"]

    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir()
    subject.write_text("arrived")
    command = CliRunner().invoke(
        main,
        [
            "probe-held-blocker",
            "--project",
            "sample",
            "--checkout-path",
            str(tmp_path),
            "waiting",
        ],
    )
    assert command.exit_code == 0, command.output
    arrived = json.loads(command.output)
    cleared, version = project_state.read_resource(docs, "sample", "blocker", "waiting")
    assert arrived["status"] == "cleared"
    assert arrived["reason"] == "probe path-exists: research/outcome.html exists"
    assert cleared["cleared_reason"] == arrived["reason"]
    assert cleared["status"] == "cleared"
    assert version == 2
    assert (
        project_state.compose_project_state(docs, "sample")["sprints"][0]["items"][0][
            "blocked_by"
        ]
        == []
    )
    blocked = _roadmap_for(docs)["blocked"]
    assert all(row["slug"] != "work" for row in blocked), blocked

    project_state.write_resource(
        docs,
        "sample",
        "blocker",
        "unknown",
        {
            "kind": "held",
            "summary": "Unknown probe",
            "probe": "missing-probe",
            "subject": "research/outcome.html",
        },
        0,
        create=True,
    )
    with pytest.raises(
        project_state.ProjectStateError,
        match="unknown held blocker probe id: 'missing-probe'",
    ):
        project_state.evaluate_held_blocker(docs, "sample", "unknown")
    unknown, version = project_state.read_resource(docs, "sample", "blocker", "unknown")
    assert unknown.get("status") != "cleared"
    assert version == 1


def test_cleared_blocker_reports_its_clear_after_its_probe_leaves_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    project_state.create_project_state(docs, "sample")
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
    subject = docs / "research" / "outcome.html"
    subject.parent.mkdir()
    subject.write_text("arrived")
    cleared = project_state.evaluate_held_blocker(docs, "sample", "waiting")
    assert cleared == {
        "id": "waiting",
        "probe": "path-exists",
        "subject": "research/outcome.html",
        "finding": "research/outcome.html exists",
        "status": "cleared",
        "reason": "probe path-exists: research/outcome.html exists",
    }

    monkeypatch.delitem(held_probes.PROBES, "path-exists")
    assert project_state.evaluate_held_blocker(docs, "sample", "waiting") == {
        "id": "waiting",
        "status": "cleared",
        "reason": "probe path-exists: research/outcome.html exists",
    }

    project_state.write_resource(
        docs,
        "sample",
        "blocker",
        "unknown",
        {
            "kind": "held",
            "summary": "Unknown probe",
            "probe": "missing-probe",
            "subject": "research/outcome.html",
        },
        0,
        create=True,
    )
    with pytest.raises(
        project_state.ProjectStateError,
        match="unknown held blocker probe id: 'missing-probe'",
    ):
        project_state.evaluate_held_blocker(docs, "sample", "unknown")
    unknown, version = project_state.read_resource(docs, "sample", "blocker", "unknown")
    assert unknown.get("status") != "cleared"
    assert version == 1


def test_command_reports_unknown_probe_without_clearing(tmp_path: Path) -> None:
    assert callable(getattr(project_state, "evaluate_held_blocker", None))
    docs = tmp_path / "docs"
    docs.mkdir()
    project_state.create_project_state(docs, "sample")
    project_state.write_resource(
        docs,
        "sample",
        "blocker",
        "waiting",
        {"kind": "held", "probe": "missing-probe", "subject": "research/outcome.html"},
        0,
        create=True,
    )
    result = CliRunner().invoke(
        main,
        [
            "probe-held-blocker",
            "--project",
            "sample",
            "--checkout-path",
            str(tmp_path),
            "waiting",
        ],
    )
    assert result.exit_code != 0
    assert "unknown held blocker probe id: 'missing-probe'" in result.output
    assert project_state.read_resource(docs, "sample", "blocker", "waiting")[1] == 1
