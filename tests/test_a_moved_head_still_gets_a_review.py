"""A stored review's brief cannot prevent requesting a later head's review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_review_dispatch as dispatch
from reckon.crew import review, runs
from reckon.crew.recovery_review_acceptance import dispatch_awaiting_reviews
from tests.helpers.git import git

PROJECT = "moved-head-fixture"
SUBJECT = "r-moved"
REVIEWER = "r-reviewed-earlier"


def _commit(tree, path, text):
    target = tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(tree, "add", path)
    git(tree, "commit", "-qm", "test: write fixture", "-m", "Record fixture content.")
    return git(tree, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, isolated_reckon_home, monkeypatch):
    tree = tmp_path / "repo"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "config", "user.name", "Fixture")
    git(tree, "config", "user.email", "fixture@example.invalid")
    base = _commit(tree, "seed.txt", "seed\n")
    git(tree, "update-ref", "refs/remotes/origin/main", base)
    head = _commit(tree, "pkg/value.py", "VALUE = 1\n")
    brief = runs.run_dir(REVIEWER) / "brief.md"
    brief.parent.mkdir(parents=True)
    brief.write_text(f"Reviewed head: {head}\n")
    stored = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEWER,
        "status": "parsed",
        "reviewed_base_sha": base,
        "reviewed_head_sha": head,
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 20),
        "findings": [],
    }
    review.store_review(stored)
    manifest = tmp_path / "manifest.md"
    manifest.write_text(f'status: complete\ncommits: ["{head}"]\n')
    pointer = {
        "run_id": SUBJECT,
        "project": PROJECT,
        "session": "fixture-session",
        "repo": str(tree),
        "worktree": str(tree),
        "base_sha": base,
        "role": "implement",
        "launch": "in-harness",
        "process_alive": False,
        "manifest_path": str(manifest),
        "node": {"id": SUBJECT, "write_paths": ["pkg/value.py"]},
    }
    runs._write_json(runs.pointer_path(SUBJECT), pointer)
    composed = []

    def launch(record, fields, **kwargs):
        composed.append(fields)
        return {
            "run_id": record["run_id"],
            "review_run_id": fields["node_id"],
            "dispatched": True,
            "head": fields["head"],
        }

    monkeypatch.setattr(dispatch, "_dispatch_composed_review", launch)
    return {
        "tree": tree,
        "head": head,
        "brief": brief,
        "pointer": pointer,
        "stored": stored,
        "composed": composed,
    }


def _assert_new_review(world, report, head):
    assert report["dispatched"] is True, report
    assert report["head"] == head
    fields = world["composed"][-1]
    assert fields["head"] == head
    assert fields["review_tier"] == "light"
    assert head in Path(fields["brief"]).read_text()
    assert world["brief"].read_text() == f"Reviewed head: {world['head']}\n"
    assert review.read_review(PROJECT, SUBJECT, reviewed_head_sha=head) is None


def test_a_data_move_requests_a_review_without_refiling_the_earlier_brief(world):
    head = _commit(world["tree"], "data/reading.txt", "reading\n")
    report = dispatch.dispatch_review_for_run(world["pointer"])
    _assert_new_review(world, report, head)


def test_a_product_source_move_composes_a_new_review(world):
    head = _commit(world["tree"], "pkg/value.py", "VALUE = 2\n")
    report = dispatch.dispatch_review_for_run(world["pointer"])
    _assert_new_review(world, report, head)
    assert world["composed"][-1]["scope"] == ["pkg/value.py"]


def test_the_review_sweep_reports_the_moved_run_and_another_run(world):
    _commit(world["tree"], "data/reading.txt", "reading\n")
    other = {**world["pointer"], "run_id": "r-other"}
    runs._write_json(runs.pointer_path("r-other"), other)
    report = dispatch_awaiting_reviews(
        project=PROJECT,
        session="fixture-session",
        pointers=[world["pointer"], other],
    )
    assert report["failed"] == [], report
    assert {row["run_id"] for row in report["reports"]} == {SUBJECT, "r-other"}
    assert len(report["dispatched"]) == 2


def test_a_review_without_a_brief_still_carries_an_unrelated_data_move(world):
    world["brief"].unlink()
    head = _commit(world["tree"], "data/reading.txt", "reading\n")
    report = dispatch.dispatch_review_for_run(world["pointer"])
    assert report["carried"] is True
    carried = review.read_review(PROJECT, SUBJECT, reviewed_head_sha=head)
    assert carried["carried_forward"]["from"] == world["head"]
    assert world["composed"] == []


def test_the_store_still_refuses_a_record_that_disagrees_with_its_brief(world):
    head = _commit(world["tree"], "data/reading.txt", "reading\n")
    forged = {**world["stored"], "reviewed_head_sha": head}
    with pytest.raises(ValueError, match=head) as refused:
        review.store_review(forged)
    assert world["head"] in str(refused.value)
    stored = review.review_path(PROJECT, SUBJECT, reviewed_head_sha=world["head"])
    assert json.loads(stored.read_text())["reviewed_head_sha"] == world["head"]
