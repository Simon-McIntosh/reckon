"""The review store refuses a status no reader acts on.

A hand-composed run-review record reached the store carrying ``"status":
"complete"`` — a word reckoned from an older record's shape, not from the
parser's vocabulary. Every reader acts on ``"parsed"`` alone, so the record sat
on disk while the promotion gate saw no review at all and refused the run with
"no complete independent review is stored". The operator then searched for a
record that was already there.

The store validates the subject, the material and the revisions, so it is where
the vocabulary is spelt once: a record whose status is absent or outside the
parser's own set is refused before anything is written, and the refusal names
the status it found and the set it accepts, so the reviewer learns the
vocabulary from the error.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module

PROJECT = "proj"
REVIEWED_RUN = "r-reviewed-run"
REVIEWER_RUN = "r-reviewer-run"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

ACCEPTED = {"parsed", "unparsed", "incomplete"}


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary home for this test."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _record(*, status: str | None = "parsed") -> dict:
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEWER_RUN,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "findings": [],
        "total": 108,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
    }
    if status is not None:
        record["status"] = status
    return record


def _stored_files(base_dir: Path) -> list[Path]:
    return sorted(path for path in base_dir.rglob("*") if path.is_file())


def test_the_accepted_statuses_are_the_parsers_own(crew_home: Path) -> None:
    # The set is public so a reader imports it rather than restating it, and it
    # holds exactly the values parse_review writes.
    assert review_module.RECOGNISED_REVIEW_STATUSES == ACCEPTED
    emitted = "\n".join(
        f"SCORE {dimension}: 18" for dimension in review_module.REVIEW_DIMENSIONS
    )
    assert review_module.parse_review(emitted)["status"] in ACCEPTED


def test_a_stored_review_refuses_a_complete_status(
    crew_home: Path, tmp_path: Path
) -> None:
    base_dir = tmp_path / "store"
    record = _record(status="complete")

    with pytest.raises(ValueError) as caught:
        review_module.store_review(record, base_dir=base_dir)

    message = str(caught.value)
    assert "complete" in message
    for accepted in ACCEPTED:
        assert accepted in message
    assert _stored_files(base_dir) == []


def test_a_stored_review_refuses_a_missing_status(
    crew_home: Path, tmp_path: Path
) -> None:
    base_dir = tmp_path / "store"
    record = _record(status=None)

    with pytest.raises(ValueError) as caught:
        review_module.store_review(record, base_dir=base_dir)

    message = str(caught.value)
    assert "no status" in message
    for accepted in ACCEPTED:
        assert accepted in message
    assert _stored_files(base_dir) == []


def test_a_parsed_review_is_stored_as_before(
    crew_home: Path, tmp_path: Path
) -> None:
    base_dir = tmp_path / "store"
    record = _record(status="parsed")

    path = review_module.store_review(record, base_dir=base_dir)

    assert path.is_file()
    stored = review_module.read_review(
        PROJECT, REVIEWED_RUN, base_dir=base_dir, reviewed_head_sha=HEAD_SHA
    )
    assert stored is not None
    assert stored["status"] == "parsed"