"""A parsed, complete review wins over an incomplete one at the same head.

Two reviews of one run can sit at the reviewed head: one that stopped before
it measured anything, and one that scored every dimension. The committed
reader kept the first head-matching file in sorted order, so a re-review whose
complete record sorted second was invisible to the promotion gate and the run
was refused as having no complete independent review while that record sat on
disk and passed the review check on its own.

These tests write both records into the committed run directory the gate reads
and drive the gate through the same selection a promotion uses, so what is
asserted is the refusal an operator sees rather than the selection helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import promotion_checks, promotion_release
from reckon.crew import review as review_module
from reckon.crew.node import CrewError

PROJECT = "demo"
REVIEWED_RUN = "r-20261108T171139362143-review-subject"
HEAD = "a" + "0" * 39

# The incomplete record's name sorts first, which is what the selection sees
# before the complete record is considered.
INCOMPLETE_NAME = "r-20261108T173448791464.json"
PARSED_NAME = "r-20261108T180752870871.json"

DIMENSIONS = review_module.REVIEW_DIMENSIONS


def _complete_record() -> dict:
    scores = dict.fromkeys(DIMENSIONS, 19)
    return {
        "status": "parsed",
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": PARSED_NAME.removesuffix(".json"),
        "reviewed_head_sha": HEAD,
        "scores": scores,
        "absent": [],
        "total": sum(scores.values()),
    }


def _incomplete_record() -> dict:
    return {
        "status": "incomplete",
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": INCOMPLETE_NAME.removesuffix(".json"),
        "reviewed_head_sha": HEAD,
        "scores": {},
        "absent": [],
    }


@pytest.fixture()
def committed_run(tmp_path: Path, monkeypatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    docs = tmp_path / "repo" / "docs"
    run_directory = docs / "state" / PROJECT / "reviews" / "run" / REVIEWED_RUN
    run_directory.mkdir(parents=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return run_directory


def _write(run_directory: Path, name: str, record: dict) -> None:
    (run_directory / name).write_text(json.dumps(record), encoding="utf-8")


def _gate_review() -> tuple[dict | None, str]:
    return promotion_release._review_for_promotion(
        PROJECT, REVIEWED_RUN, promoted_revision=HEAD
    )


def _waiver(review: dict | None, stale: str = "") -> dict[str, str] | None:
    return promotion_checks._require_review_waiver(
        REVIEWED_RUN,
        {"role": "implement"},
        verdict="passed",
        classification="scoring",
        review=review,
        review_action="dispatch a review",
        waiver_reason="",
        review_tier="full",
        promoted_head=HEAD,
        stale_head=stale,
    )


def test_a_parsed_complete_record_wins_over_an_incomplete_one(
    committed_run: Path,
) -> None:
    _write(committed_run, INCOMPLETE_NAME, _incomplete_record())
    _write(committed_run, PARSED_NAME, _complete_record())

    review, stale = _gate_review()

    assert review is not None
    assert review["status"] == "parsed"
    assert review["total"] == 19 * len(DIMENSIONS)
    assert stale == ""
    assert _waiver(review, stale) is None


def test_an_incomplete_record_alone_is_still_refused(committed_run: Path) -> None:
    _write(committed_run, INCOMPLETE_NAME, _incomplete_record())

    review, stale = _gate_review()

    assert review is not None
    assert review["status"] == "incomplete"
    with pytest.raises(CrewError, match="no complete independent review is stored"):
        _waiver(review, stale)
