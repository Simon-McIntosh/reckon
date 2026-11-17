"""Landing reads a delivered review of the asserted head, and only that head.

A review of the current head that has been delivered but whose own run is still
promotion-requested is evidence about the diff being landed, and landing reads
it. The store is keyed by the revision a record read, not by the order records
were written, so a review of an earlier head that landed on disk later is not
read as this head's review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import promotion_release
from reckon.crew import review as review_module

PROJECT = "sample-project"
REVIEWED_RUN = "r-reviewed-run"
EARLIER_REVIEW_RUN = "r-earlier-review"
DELIVERED_REVIEW_RUN = "r-delivered-review"
BASE = "a" * 40
EARLIER_HEAD = "c" * 40
HEAD = "b" * 40
SCORES = {
    "goal_fidelity": 18,
    "evidence": 15,
    "scope_discipline": 17,
    "durability": 19,
    "fit": 16,
    "reuse": 20,
}


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the durable review store from the operator's crew state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _record(review_run_id: str, head: str) -> dict:
    """A review record written the way the store writes one."""
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": review_run_id,
        "status": "parsed",
        "scores": SCORES,
        "absent": [],
        "total": sum(SCORES.values()),
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": head,
    }


def _pointer(run_id: str, phase: str) -> None:
    """A live pointer, so the review run's own state is on disk to be read."""
    from reckon.crew.runs import _write_json, pointer_path

    pointer_path(run_id).parent.mkdir(parents=True, exist_ok=True)
    _write_json(pointer_path(run_id), {"run_id": run_id, "phase": phase})


def test_a_delivered_review_of_the_asserted_head_is_what_landing_reads(
    crew_home: Path,
) -> None:
    """The review's own run is still promoting; its record is evidence anyway."""
    _pointer(DELIVERED_REVIEW_RUN, "promotion-requested")
    review_module.store_review(_record(DELIVERED_REVIEW_RUN, HEAD))
    review_module.store_review(_record(EARLIER_REVIEW_RUN, EARLIER_HEAD))

    block, stale = promotion_release._review_for_promotion(
        PROJECT, REVIEWED_RUN, promoted_revision=HEAD
    )

    assert block is not None
    assert block["reviewed_head_sha"] == HEAD
    assert block["reviewed_base_sha"] == BASE
    assert stale == ""


def test_a_review_of_an_earlier_head_is_not_this_heads_evidence(
    crew_home: Path,
) -> None:
    """The earlier record is newest on disk and still not the asserted review."""
    review_module.store_review(_record(EARLIER_REVIEW_RUN, EARLIER_HEAD))

    block, stale = promotion_release._review_for_promotion(
        PROJECT, REVIEWED_RUN, promoted_revision=HEAD
    )

    assert block is None
    assert stale == EARLIER_HEAD
