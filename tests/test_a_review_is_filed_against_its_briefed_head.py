"""Filing compares a review's revision with its dispatch-time brief."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from reckon.crew import review, runs
from reckon.crew.recovery_review_delivery import review_delivery_refusal
from tests.helpers.git import git

PROJECT = "filing-fixture"
SUBJECT = "r-reviewed"
REVIEWER = "r-reviewer"


@pytest.fixture
def world(tmp_path, isolated_reckon_home):
    tree = tmp_path / "repo"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "config", "user.name", "Fixture")
    git(tree, "config", "user.email", "fixture@example.invalid")
    heads = []
    for content in ("seed", "earlier", "current"):
        (tree / "work.txt").write_text(content)
        git(tree, "add", "work.txt")
        git(tree, "commit", "-qm", f"test: {content}", "-m", "Create fixture content.")
        heads.append(git(tree, "rev-parse", "HEAD"))
    base, earlier, head = heads
    runs._write_json(
        runs.pointer_path(SUBJECT),
        {"run_id": SUBJECT, "project": PROJECT, "worktree": str(tree)},
    )
    brief = runs.run_dir(REVIEWER) / "brief.md"
    brief.parent.mkdir(parents=True)
    brief.write_text(f"Reviewed head: {head}\n")
    paths = review.run_review_record_paths(
        runs.run_dir(SUBJECT), SUBJECT, reviewed_head_sha=head
    )
    paths = [paths[-1]]
    reviewer = {
        "run_id": REVIEWER,
        "project": PROJECT,
        "created_at": "2020-01-01T00:00:00Z",
        "brief": {"path": str(brief)},
        "node": {"id": f"review-of-{SUBJECT}", "role": "review", "write_paths": paths},
    }
    runs._write_json(runs.pointer_path(REVIEWER), reviewer)

    def record(revision, reviewer_id=REVIEWER):
        return {
            "project": PROJECT,
            "reviewed_run_id": SUBJECT,
            "review_run_id": reviewer_id,
            "status": "parsed",
            "reviewed_base_sha": base,
            "reviewed_head_sha": revision,
            "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 20),
            "findings": [],
        }

    old = review.store_review(record(earlier, "r-earlier-reviewer"))
    return {
        "head": head,
        "earlier": earlier,
        "record": record,
        "reviewer": reviewer,
        "old": old,
        "old_bytes": old.read_bytes(),
        "source": Path(paths[-1]),
        "committed": tmp_path / "committed",
        "brief": brief,
    }


def _file(world, head):
    source = world["source"]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(world["record"](head)))
    return review.file_delivered_run_review_report(
        world["reviewer"], run_directory=runs.run_dir(SUBJECT)
    )


def test_the_briefed_head_is_filed_and_read_back(world):
    report = _file(world, world["head"])
    assert report["refused"] == []
    assert report["filed"]
    stored = json.loads(report["filed"][0].read_text())
    assert stored["reviewed_head_sha"] == world["head"]
    assert stored["review_run_id"] == REVIEWER


def test_an_earlier_head_is_refused_without_overwriting_its_review(world):
    report = _file(world, world["earlier"])
    promotion = review_delivery_refusal(world["reviewer"], PROJECT)
    print(f"filing receipt: {report}; promotion refusal: {promotion}")
    assert report["filed"] == [], "filing accepted a review of another head"
    assert len(report["refused"]) == 1
    reason = report["refused"][0]["reason"]
    assert world["earlier"] in reason
    assert world["head"] in reason
    assert world["old"].read_bytes() == world["old_bytes"]
    assert not review.review_path(
        PROJECT, SUBJECT, reviewed_head_sha=world["head"]
    ).exists()


@pytest.mark.parametrize("committed", [False, True])
def test_both_store_writers_refuse_a_mismatched_head(world, committed):
    record = world["record"](world["earlier"])
    store = (
        partial(review.store_committed_review, committed_root=world["committed"])
        if committed
        else review.store_review
    )
    with pytest.raises(ValueError, match=world["head"]) as refused:
        store(record)
    assert world["earlier"] in str(refused.value)
    assert world["old"].read_bytes() == world["old_bytes"]
    assert not world["committed"].exists()


def test_the_reviewers_snapshot_wins_over_a_moving_source_brief(world, tmp_path):
    source = tmp_path / "review-brief.md"
    source.write_text(f"Reviewed head: {world['earlier']}\n")
    pointer = {
        **world["reviewer"],
        "brief": {"path": str(world["brief"]), "source_path": str(source)},
    }
    runs._write_json(runs.pointer_path(REVIEWER), pointer)
    stored = review.store_review(world["record"](world["head"]))
    assert json.loads(stored.read_text())["reviewed_head_sha"] == world["head"]


def test_a_matching_abbreviated_revision_is_accepted(world):
    world["brief"].write_text(f"Reviewed head: {world['head'][:12]}\n")
    stored = review.store_review(world["record"](world["head"]))
    assert json.loads(stored.read_text())["reviewed_head_sha"] == world["head"]


def test_the_matching_head_reaches_the_committed_store(world):
    stored = review.store_committed_review(
        world["record"](world["head"]), committed_root=world["committed"]
    )
    assert json.loads(stored.read_text())["reviewed_head_sha"] == world["head"]


def test_the_snapshot_still_guards_filing_after_the_reviewer_pointer_retires(world):
    runs.pointer_path(REVIEWER).unlink()
    with pytest.raises(ValueError, match=world["head"]):
        review.store_review(world["record"](world["earlier"]))


def test_delivery_uses_its_reviewer_even_when_the_record_omits_that_identity(world):
    body = world["record"](world["earlier"])
    del body["review_run_id"]
    source = world["source"]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(body))
    report = review.file_delivered_run_review_report(
        world["reviewer"], run_directory=runs.run_dir(SUBJECT)
    )
    assert report["filed"] == []
    assert world["head"] in report["refused"][0]["reason"]
    assert world["old"].read_bytes() == world["old_bytes"]
