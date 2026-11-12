"""A write and queued admission share the review decision for the same inputs."""

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, mcp
from reckon.crew import (
    plan_review,
    queue_admission,
    recovery,
    review,
    review_need,
    runs,
)
from reckon.crew.node import TaskNode
from tests.test_review_plan_command import project as project  # noqa: PLC0414


@pytest.fixture
def queued_edit(project, monkeypatch):
    home, repo, path = project
    original = " ".join(f"original{index}" for index in range(40))
    replacement = " ".join(f"replacement{index}" for index in range(40))
    text = path.read_text().replace(
        '<h2 id="delivery">Delivery</h2><p>Extend the existing mechanism.</p>',
        f'<h2 id="delivery">Delivery</h2><p>{original}</p>'
        "<p><strong>Done when</strong> the shared owner answers.</p>",
    )
    state = _plan_html.read_state(text)
    state["section_declarations"] = {"delivery": "implementable"}
    state["sections"] = [
        {
            "id": "delivery",
            "effort_hours": 1.0,
            "status": "implementable",
            "capability": {
                "version": "1.0",
                "class": "general",
                "requirements": {
                    "reasoning": "standard",
                    "verification": "strict",
                    "risk": "low",
                },
            },
            "attempts": 0,
            "links": [],
        }
    ]
    path.write_text(_plan_html.write_state(text, state))
    fields = recovery._review_dispatch_fields(
        {
            "subject": "plan",
            "project": "sample",
            "plan_slug": "fixture",
            "plan_path": str(path),
            "repo": str(repo),
            "session": "coordinator",
            "rubric": "design",
            "local": True,
            "run_id": "review-queued-edit",
        }
    )
    sidecar = json.loads(Path(fields["sidecar"]).read_text())
    Path(sidecar["report_path"]).write_text(
        "".join(f"RUBRIC {item}: pass\n" for item in review.PLAN_DESIGN_REVIEW_ITEMS)
    )
    plan_review.store_plan_review(
        {
            **sidecar,
            "findings": [],
            "responses": {},
            "review_run_id": "review-queued-edit",
        }
    )
    node = TaskNode(
        id="build-delivery",
        goal="Build delivery",
        plan="fixture",
        section="delivery",
        role="implement",
    )
    pointer = {
        "run_id": "r-queued-delivery",
        "project": "sample",
        "plan": "fixture",
        "section": "delivery",
        "repo": str(repo),
        "session": "coordinator",
        "node": node.as_dict(),
        "phase": "queued",
        "dispatch_options": {},
        "review_contract": queue_admission.review_contract(node, repo_root=repo),
    }
    _store.write_json_atomically(runs.pointer_path(pointer["run_id"]), pointer)
    pointer = runs.read_pointer(pointer["run_id"])
    assert pointer["review_contract"]["section"] == "delivery"
    assert runs.crew_home() == home / "crew"

    # All writes must stay in the temporary home or the fixture checkout. This
    # also catches a writer that resolves the operator's home independently.
    writes = []
    write_atomically = _store.write_atomically

    def isolated_write(target, *args, **kwargs):
        destination = Path(target).resolve()
        assert destination.is_relative_to(home) or destination.is_relative_to(repo)
        writes.append(destination)
        return write_atomically(target, *args, **kwargs)

    monkeypatch.setattr(_store, "write_atomically", isolated_write)
    yield repo, path, pointer, original, replacement, sidecar
    assert writes, "the write guard must observe the real plan write"
    assert all(p.is_relative_to(home) or p.is_relative_to(repo) for p in writes)
    assert runs.crew_home() == home / "crew", "the real crew home was untouched"


def _edit(fixture):
    repo, path, _pointer, original, replacement, _sidecar = fixture
    result = mcp._edit_plan_tool(
        "sample",
        "fixture",
        _plan_html.read_state_file(path)["version"],
        mode="text",
        old_html=f"<p>{original}</p>",
        new_html=f"<p>{replacement}</p>",
        checkout_path=str(repo),
    )
    assert result.get("ok"), result
    return result


def _admission(pointer):
    return queue_admission._plan_review_owed(
        pointer, config_for=lambda _pointer: {"plan_review_gate": "enforce"}
    )


def test_an_edit_owing_nothing_leaves_its_request_admissible(queued_edit, monkeypatch):
    calls = []

    def changing_service(_state, questions, **_kwargs):
        calls.append(dict(questions))
        if len(calls) > 1:
            raise OSError("review judge unavailable on the later read")
        return {"answers": {key: {"noul": 0.0} for key in questions}}

    # A served answer whose optional cache cannot be written must still agree
    # with admission after the service becomes unavailable.
    monkeypatch.setattr(review_need._client(), "ask", changing_service)
    monkeypatch.setattr(review_need, "_write_cached", lambda *_args: None)
    response = _edit(queued_edit)
    detail = _admission(queued_edit[2])
    print(
        "write:",
        response["review_owed"],
        "admission:",
        detail,
        "judge calls:",
        len(calls),
    )
    assert response["review_owed"] == []
    assert response["uncovered_by_write"] == []
    assert detail is None
    assert len(calls) == 1


def test_an_owed_edit_holds_the_same_units(queued_edit, monkeypatch):
    monkeypatch.setattr(review_need, "judge", lambda *_args, **_kwargs: {})
    response = _edit(queued_edit)
    detail = _admission(queued_edit[2])
    units = {entry["unit"] for entry in response["review_owed"]}
    newly = {entry["unit"] for entry in response["uncovered_by_write"]}
    assert units == newly == {"delivery"}
    assert detail is not None
    assert f"uncovered units: {', '.join(sorted(units))};" in detail


@pytest.mark.parametrize("reading", ["write", "admission"])
def test_an_unmeasured_change_is_owed_by_both_readings(
    queued_edit,
    monkeypatch,
    reading,
):
    monkeypatch.setattr(review_need, "judge", lambda *_args, **_kwargs: {})
    snapshot = (
        plan_review.review_report_directory("sample", "fixture", "review-queued-edit")
        / "plan.html"
    )
    snapshot.unlink()
    response = _edit(queued_edit)
    detail = _admission(queued_edit[2])
    if reading == "write":
        assert response["review_owed"] == [{"unit": "delivery", "change": None}]
        assert response["uncovered_by_write"] == response["review_owed"]
    else:
        assert detail is not None
        assert "uncovered units: delivery;" in detail


def test_a_missing_snapshot_invalidates_the_shared_decision(queued_edit, monkeypatch):
    monkeypatch.setattr(
        review_need,
        "judge",
        lambda changes, **_kwargs: {
            change.identity: review_need.Verdict(False, 0.0, "fixture")
            for change in changes
        },
    )
    response = _edit(queued_edit)
    assert response["review_owed"] == []
    assert _admission(queued_edit[2]) is None
    snapshot = (
        plan_review.review_report_directory("sample", "fixture", "review-queued-edit")
        / "plan.html"
    )
    snapshot.unlink()
    from reckon.mcp_deadlines import _review_owed_fields

    response = _review_owed_fields(
        "sample", "fixture", str(queued_edit[1]), uncovered_before=set()
    )
    assert response["review_owed"] == [{"unit": "delivery", "change": None}]
    assert "uncovered units: delivery;" in _admission(queued_edit[2])


def test_an_unpersisted_decision_is_reported_as_unknown(queued_edit, monkeypatch):
    from reckon import metadata_index

    monkeypatch.setattr(metadata_index, "_write_persisted_memo", lambda *_args: None)
    response = _edit(queued_edit)
    assert response["review_owed"] is None
    assert "could not be persisted" in response["review_owed_error"]
