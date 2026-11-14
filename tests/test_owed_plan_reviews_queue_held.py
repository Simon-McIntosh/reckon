"""Review debt holds a stored request until the gate accepts its own section."""

import importlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import plan_review, queue_admission, routing, runs
from reckon.crew.dispatch import LaneHeld
from reckon.crew.node import PlanReviewMissingError, TaskNode
from tests.test_a_queued_request_is_stamped_at_queue_time import (
    _reviewed,
    _store_content,
)
from tests.test_a_queued_request_is_stamped_at_queue_time import (
    two_section_plan as two_section_plan,  # noqa: PLC0414
)
from tests.test_review_plan_command import CONFIG
from tests.test_review_plan_command import project as project  # noqa: PLC0414


def _config():
    return {
        **CONFIG,
        "roles": {**CONFIG["roles"], "implement": {}},
        "plan_review_gate": "enforce",
        "routing": {"mode": "deterministic"},
    }


def _node(section="s2"):
    return TaskNode(
        id=f"build-{section}",
        goal="Build the requested section",
        plan="fixture",
        section=section,
        role="implement",
        write_paths=[f"{section}.py"],
        done_when="The section check passes.",
        time_budget="20m",
        spec_level="exact",
    )


def _gate(repo, section="s2"):
    try:
        routing.require_plan_reviewed(
            node=_node(section),
            project="sample",
            repo=repo,
            authority={"plan": {"docs": str(repo / "docs"), "source": "repository"}},
            enforce=True,
        )
    except PlanReviewMissingError as exc:
        return str(exc)
    return None


def _queue(repo):
    dispatch = importlib.import_module("reckon.crew.dispatch")
    return dispatch.queue_requests(
        [_node("s2"), _node("s3")],
        project="sample",
        session="coordinator",
        repo_root=repo,
        config=_config(),
    )


@pytest.fixture
def review_launcher(two_section_plan, monkeypatch):
    """Persist review pointers without starting a worker or touching live state."""
    home, repo, path = two_section_plan
    dispatch = importlib.import_module("reckon.crew.dispatch")
    original = dispatch.dispatch
    composed = []

    def launch(**kwargs):
        node = kwargs["node"]
        if node.role != "review":
            return original(**kwargs)
        run_id = f"review-{len(composed)}"
        sidecar = json.loads((Path(node.brief).parent / "plan-review.json").read_text())
        composed.append(sidecar["rubric"])
        runs.queue_dispatch(
            {
                "run_id": run_id,
                "project": "sample",
                "session": "coordinator",
                "repo": str(repo),
                "node": node.as_dict(),
                "phase": "queued",
                "queued_at": datetime.now(UTC).isoformat(),
                "created_at": datetime.now(UTC).isoformat(),
                "backend": "worker",
                "review_rubric": sidecar["rubric"],
            }
        )
        return {"run_id": run_id}

    monkeypatch.setattr(dispatch, "dispatch", launch)
    # The plan is committed, quiet, and inside the reflex's authoring burst.
    subprocess.run(["git", "add", "docs/plans/fixture.html"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-qm",
            "test: describe sections",
            "-m",
            "Supply section contracts.",
        ],
        cwd=repo,
        check=True,
    )
    stamp = time.time() - plan_review.PLAN_REVIEW_QUIET_SECONDS - 10
    os.utime(path, (stamp, stamp))
    return home, repo, path, composed


def test_single_dispatch_queues_instead_of_refusing_review_debt(review_launcher):
    _, repo, _, composed = review_launcher
    dispatch = importlib.import_module("reckon.crew.dispatch")
    refused = _gate(repo)
    assert refused and "design rubric" in refused
    print("base gate refusal:", refused)
    with pytest.raises(LaneHeld) as caught:
        dispatch.dispatch(
            node=_node(),
            project="sample",
            repo=repo,
            config=_config(),
            session="coordinator",
            check_budget=False,
            route="deterministic",
        )
    pointer = runs.read_pointer(caught.value.queued["run_id"])
    assert pointer["phase"] == "held"
    assert pointer["queue_hold"]["reason"] == "plan-review-owed"
    assert pointer["queue_hold"]["detail"] == refused
    assert sorted(composed) == ["content", "design"]
    assert caught.value.queued["plan_reviews"]["composed"]


def test_one_call_holds_all_sections_composes_once_and_admission_releases(
    review_launcher, monkeypatch
):
    _, repo, path, composed = review_launcher
    queued = _queue(repo)
    assert len(queued) == 2
    for run_id, row in queued.items():
        pointer = runs.read_pointer(run_id)
        assert row["phase"] == pointer["phase"] == "held"
        assert pointer["queue_hold"]["reason"] == "plan-review-owed"
        assert pointer["queue_hold"]["detail"] == _gate(repo, row["section"])
    assert sorted(composed) == ["content", "design"]
    assert any(row["plan_reviews"]["composed"] for row in queued.values())
    repeated = _queue(repo)
    assert set(repeated) == set(queued)
    assert sorted(composed) == ["content", "design"]
    _reviewed(path)
    # Keep released requests queued so the next-pass release is visible.
    monkeypatch.setattr(queue_admission, "_launch_queued", lambda *a, **k: None)
    report = queue_admission.admit_once(config_for=lambda _: _config())
    assert set(queued) <= set(report["released_plan_review"])
    for run_id in queued:
        pointer = runs.read_pointer(run_id)
        assert pointer["phase"] == "queued"
        assert "queue_hold" not in pointer


def test_unanswered_finding_holds_its_section_and_answer_releases(review_launcher):
    _, repo, path, _ = review_launcher
    _reviewed(path)
    content = _store_content(path, "content-with-finding")
    content["findings"] = [
        {
            "id": "section-concern",
            "type": "reasoning",
            "anchor": "fixture#s2",
            "text": "The section s2 needs a measurable acceptance check.",
        }
    ]
    plan_review.store_plan_review(content)
    queued = _queue(repo)
    by_section = {row["section"]: run_id for run_id, row in queued.items()}
    held = runs.read_pointer(by_section["s2"])
    assert held["phase"] == "held"
    assert "unanswered" in held["queue_hold"]["detail"]
    assert runs.read_pointer(by_section["s3"])["phase"] == "queued"
    # Answering changes no plan text: the evidence cache must still invalidate.
    with queue_admission.plan_review_pass():
        assert queue_admission._plan_review_owed(held, config_for=lambda _: _config())
        content["responses"] = {
            "section-concern": {
                "action": "declined",
                "reason": "The acceptance check is stated in the section.",
            }
        }
        plan_review.store_plan_review(content)
        assert (
            queue_admission._plan_review_owed(held, config_for=lambda _: _config())
            is None
        )
    released = queue_admission._release_plan_review_holds(
        runs.list_live(), config_for=lambda _: _config()
    )
    assert released == [by_section["s2"]]


@pytest.mark.parametrize("state", ["editing", "terminal", "covered"])
def test_dispatch_uses_the_composers_skip_rules(review_launcher, state):
    _, repo, path, composed = review_launcher
    if state == "editing":
        os.utime(path, None)
    elif state == "terminal":
        path.write_text(
            path.read_text().replace('content="active"', 'content="shipped"')
        )
        stamp = time.time() - plan_review.PLAN_REVIEW_QUIET_SECONDS - 10
        os.utime(path, (stamp, stamp))
    else:
        _reviewed(path)
    _queue(repo)
    assert composed == []
