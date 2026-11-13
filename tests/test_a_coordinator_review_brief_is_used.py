"""A coordinator's brief for a review the reflex already has in hand is not lost.

The reflex composes its own review for a delivered run under the review's node
id. A coordinator dispatching that same node with a focused brief used to be
answered as though the request had been met, while the reflex's generic brief
was what the review actually read. The cases below fix what each state owes:

* a review still queued has not begun, so the coordinator's brief replaces the
  reflex's in that one request and the queued run launches with it;
* a review already past the queue keeps the brief it launched with, so the
  answer names the coordinator's brief as unused and the commands that apply it.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_dispatch_commands
from reckon.crew import runs
from tests import test_dispatch_names_its_backend as backend_tests

LOCAL = "clive"
PROJECT = "proj"
SESSION = "session"
PLAN = "plan-a"
SECTION = "dispatch"
REVIEW_NODE = "review-of-tf-verdict-to-cs-tape"
REFLEX_RUN = "r-reflex-review"
COORDINATOR_BRIEF = "coordinator-focus.md"


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _configure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, session_slots: float
) -> dict:
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": session_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": session_slots}},
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _brief(repo: Path, name: str) -> str:
    path = repo.parent / name
    path.write_text("Focus on the tape writer's verdict mapping.\n", encoding="utf-8")
    return str(path)


def _node(node_id: str = REVIEW_NODE) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="review the delivered run",
        plan=PLAN,
        section=SECTION,
        role="implement",
        spec_level="exact",
        done_when="the review report is delivered",
        write_paths=["result.json"],
        time_budget="25m",
    )


def _live_review(*, phase: str, brief: str, pid: int | None = None) -> dict:
    record = {
        "run_id": REFLEX_RUN,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "queued_at": "2026-10-22T05:38:00+00:00",
        "created_at": "2026-10-22T05:38:00+00:00",
        "node": {
            "id": REVIEW_NODE,
            "plan": PLAN,
            "section": SECTION,
            "brief": brief,
            "goal": "review the delivered run",
            "done_when": "the review report is delivered",
            "write_paths": ["result.json"],
        },
    }
    if pid is not None:
        record["pid"] = pid
    runs._write_json(runs.pointer_path(REFLEX_RUN), record)
    return record


def _dispatch(repo: Path, brief: str):
    arguments = backend_tests._arguments(repo, node=REVIEW_NODE, dry_run=False)
    return CliRunner().invoke(
        cli.main,
        [*arguments, "--local", "--brief", brief, "--no-watch"],
    )


def test_a_queued_review_launches_with_the_coordinator_brief(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reflex's queued review takes the coordinator's brief in place."""
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    reflex_brief = _brief(dispatch_repo, "reflex-brief.md")
    coordinator_brief = _brief(dispatch_repo, COORDINATOR_BRIEF)
    _live_review(phase="queued", brief=reflex_brief)

    result = _dispatch(dispatch_repo, coordinator_brief)
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    assert payload["run_id"] == REFLEX_RUN
    assert payload["position"] is not None
    # One pointer, and the brief it will launch with is the coordinator's.
    assert [p["run_id"] for p in runs.list_live(project=PROJECT)] == [REFLEX_RUN]
    stored = runs.read_pointer(REFLEX_RUN)
    assert stored["node"]["brief"] == coordinator_brief


def test_a_working_review_names_the_unused_brief_and_how_to_apply_it(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review past the queue refuses the brief by name, never as met."""
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    reflex_brief = _brief(dispatch_repo, "reflex-brief.md")
    coordinator_brief = _brief(dispatch_repo, COORDINATOR_BRIEF)
    _live_review(phase="working", brief=reflex_brief, pid=os.getpid())

    result = _dispatch(dispatch_repo, coordinator_brief)
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is False
    assert payload["phase"] == "working"
    assert payload["run_id"] == REFLEX_RUN
    assert payload["position"] is None
    assert payload["detail"] == f"run {REFLEX_RUN} is already working for this node"
    reason = payload["reason"]
    assert coordinator_brief in reason
    assert "was not used" in reason
    assert f"reckon crew stop --run {REFLEX_RUN}" in reason
    # The refusal writes no second pointer and does not touch the reflex's brief.
    assert [p["run_id"] for p in runs.list_live(project=PROJECT)] == [REFLEX_RUN]
    assert runs.read_pointer(REFLEX_RUN)["node"]["brief"] == reflex_brief
