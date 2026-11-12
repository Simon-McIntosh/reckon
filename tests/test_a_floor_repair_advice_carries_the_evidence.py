"""A floor repair's advice carries the review's account of the score.

A dimension under its declared floor composes a finding of its own, and that
finding's text was the score and the floor alone: a repair resumed with it was
told reuse scored 6 against a floor of 10 and nothing about what scored 6. The
reviewer had recorded why, and had raised a finding naming the duplicated
helper responsible, and neither reached the worker — so the worker had to guess
what to fix.

The advice now carries, for each dimension under its floor, the review's own
justification for that dimension and the findings the record raised. A
dimension with no finding still carries its justification, because the
justification is the reviewer's account of the score whether or not a finding
was raised beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery_repair_dispatch as reflex
from reckon.crew import review as review_module
from reckon.crew import review_dispositions, runs
from tests import test_repair_answers_only_blocking_findings as prior

dispatch_project = prior.dispatch_project

DIMENSION = "reuse"
FLOOR = 10
SCORE = 6

JUSTIFICATION = (
    "request_key in reckon/crew/ticker.py duplicates runs.py:_node_identity, "
    "so the same node is named two ways"
)
HELPER_PATH = "reckon/crew/ticker.py"
HELPER_TEXT = (
    "request_key duplicates runs.py:_node_identity; delete it and call the helper"
)
DUPLICATED_HELPER = {
    "file": HELPER_PATH,
    "line": "88",
    "text": HELPER_TEXT,
    "severity": "follow-on",
}

CONFIG = {
    "gates": {"dimension_floors": {DIMENSION: FLOOR}},
    **prior.CONFIG,
}


def _store_review(head: str, findings: list[dict], *, justification: str = "") -> None:
    """Store a review scoring one dimension under its floor, with its account."""
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15)
    scores[DIMENSION] = SCORE
    record = {
        "project": prior.PROJECT,
        "reviewed_run_id": prior.RUN_ID,
        "review_run_id": "r-independent-review",
        "reviewed_base_sha": head,
        "reviewed_head_sha": head,
        "status": "parsed",
        "scores": scores,
        "total": sum(scores.values()),
        "findings": findings,
    }
    if justification:
        record["justifications"] = {DIMENSION: justification}
    review_module.store_review(record)


def _dispatch(record: dict, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Drive the reflex's repair dispatch with the resume entry point stubbed."""
    resumed = prior._stub_resume(monkeypatch)
    with runs.follower_claim(prior.PROJECT, "session-orchestrating", delivery="stream"):
        reflex.dispatch_repair_for_run(
            record, config=CONFIG, launcher=lambda *a, **k: 1
        )
    return resumed


@pytest.mark.arms_watch_producer
def test_advice_for_a_sub_floor_dimension_names_its_justification_and_finding(
    dispatch_project: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, head = dispatch_project
    pointer = prior._reviewed_pointer(home, repo)
    pointer["node"]["write_paths"] = ["seed.txt"]
    runs._write_json(runs.pointer_path(prior.RUN_ID), pointer)
    _store_review(head, [DUPLICATED_HELPER], justification=JUSTIFICATION)

    resumed = _dispatch(pointer, monkeypatch)

    assert len(resumed) == 1, resumed
    advice = resumed[0]["advice"]
    assert f"dimension-{DIMENSION}" in advice
    assert JUSTIFICATION in advice, advice
    assert HELPER_TEXT in advice, advice
    assert f"{HELPER_PATH}:88" in advice, advice


@pytest.mark.arms_watch_producer
def test_a_sub_floor_dimension_with_no_finding_carries_its_justification(
    dispatch_project: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, head = dispatch_project
    pointer = prior._reviewed_pointer(home, repo)
    pointer["node"]["write_paths"] = ["seed.txt"]
    runs._write_json(runs.pointer_path(prior.RUN_ID), pointer)
    _store_review(head, [], justification=JUSTIFICATION)

    resumed = _dispatch(pointer, monkeypatch)

    assert len(resumed) == 1, resumed
    advice = resumed[0]["advice"]
    assert JUSTIFICATION in advice, advice
    assert "finding:" not in advice, advice


def test_sub_floor_evidence_carries_the_records_account() -> None:
    """The evidence rows read the record's own justification and findings."""
    record = {
        "scores": {DIMENSION: SCORE},
        "justifications": {DIMENSION: JUSTIFICATION},
        "findings": [DUPLICATED_HELPER],
    }
    rows = review_dispositions.sub_floor_evidence(record, {DIMENSION: FLOOR})
    assert len(rows) == 1
    assert rows[0]["dimension"] == DIMENSION
    assert rows[0]["score"] == SCORE
    assert rows[0]["floor"] == FLOOR
    assert rows[0]["justification"] == JUSTIFICATION
    assert rows[0]["findings"] == [DUPLICATED_HELPER]
