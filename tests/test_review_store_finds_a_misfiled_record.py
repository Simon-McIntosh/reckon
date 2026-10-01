"""A review filed under the reviewer's own run id is found for the run it reviews.

A hand-dispatched review wrote its record keyed on its own run id while the
review's content named the run it reviewed; the promotion gate found no review
until the file was copied by hand. Production reviews are JSON a review worker
writes by hand, so keying a writer's filename cannot catch this — the read path
must fall back to the record's own content when nothing sits at the path a
correctly filed record would occupy, and it must say that it did, so a misfiled
record is found and flagged rather than silently used.

Every crew directory is environment-resolved under ``tmp_path``; nothing
touches the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery as recovery_module
from reckon.crew import review as review_module

PROJECT = "proj"
REVIEWED_RUN = "r-reviewed-run"
REVIEWER_RUN = "r-reviewer-run"
OTHER_RUN = "r-some-other-run"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
OTHER_SHA = "c" * 40
TOTAL = 90


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary home for this test."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_misfiled_record(
    *,
    filename_run: str = REVIEWER_RUN,
    reviewed_run_id: str = REVIEWED_RUN,
    head: str | None = HEAD_SHA,
) -> Path:
    """Write a review record by hand under the reviewer's own run id."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": reviewed_run_id,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": TOTAL,
        "timestamp": "2026-09-26T18:30:00+00:00",
    }
    if head is not None:
        record["reviewed_head_sha"] = head
    path = review_module.review_path(PROJECT, filename_run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def test_stored_record_finds_a_record_filed_under_the_reviewers_run_id(
    crew_home: Path,
) -> None:
    written = _write_misfiled_record()
    assert written.name == f"{REVIEWER_RUN}.json"
    assert not review_module.review_path(PROJECT, REVIEWED_RUN).exists()

    found_path, found = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_SHA
    )

    assert found_path == written, "the path returned is the file it was read from"
    assert found is not None, "the record's own reviewed_run_id names this run"
    assert found["misfiled"] is True, (
        "a record found away from its expected path is flagged, so a reader "
        "can tell it was not filed where the store keys reviews"
    )
    assert found["reviewed_run_id"] == REVIEWED_RUN


def test_read_review_returns_the_misfiled_record_flagged(crew_home: Path) -> None:
    _write_misfiled_record()

    review = review_module.read_review(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_SHA
    )

    assert review is not None
    assert review["misfiled"] is True
    assert review["total"] == TOTAL


def test_select_review_for_head_returns_the_misfiled_record(crew_home: Path) -> None:
    """The production selector reads a misfiled record as review evidence."""
    _write_misfiled_record()

    review, stale = recovery_module.select_review_for_head(
        PROJECT, REVIEWED_RUN, HEAD_SHA
    )

    assert review is not None, "a promotion must not be refused for a review on disk"
    assert review["misfiled"] is True
    assert stale == ""


def test_a_correctly_filed_record_is_not_flagged(crew_home: Path) -> None:
    """The flag marks where a record was found, not how the read path works."""
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "status": "parsed",
        "total": TOTAL,
        "reviewed_head_sha": HEAD_SHA,
    }
    path = review_module.review_path(PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    found_path, found = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_SHA
    )

    assert found_path == path
    assert found is not None
    assert "misfiled" not in found


def test_a_record_naming_another_run_is_not_returned(crew_home: Path) -> None:
    """The fallback matches the record's content, not merely any record present."""
    _write_misfiled_record(reviewed_run_id=OTHER_RUN)

    found_path, found = review_module.stored_record(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=HEAD_SHA
    )

    assert (found_path, found) == (None, None)


def test_a_misfiled_record_of_another_head_is_not_this_runs_review(
    crew_home: Path,
) -> None:
    """Content-based discovery does not relax head selection."""
    _write_misfiled_record(head=OTHER_SHA)

    review, stale = recovery_module.select_review_for_head(
        PROJECT, REVIEWED_RUN, HEAD_SHA
    )

    assert review is None
    assert stale == OTHER_SHA, "the refusal names the head the record did read"
