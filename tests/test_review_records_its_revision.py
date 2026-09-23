"""A review names the base and head revisions it actually read."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module

BASE = "9d208d1e1a5181ceabbcab6eebd2d029ec08e7fa"
FIRST_HEAD = "aa2675650000000000000000000000000000aaaa"
SECOND_HEAD = "bb2675650000000000000000000000000000bbbb"
INTERMEDIATE = "cc2675650000000000000000000000000000cccc"


def _scores() -> str:
    return "\n".join(
        f"SCORE {dimension}: 10" for dimension in review_module.REVIEW_DIMENSIONS
    )


def _record(head: str) -> dict[str, object]:
    return {
        "project": "reckon",
        "reviewed_run_id": "r-reviewed-run",
        "review_run_id": f"r-review-{head[:8]}",
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 10),
        "absent": [],
        "total": 50,
        "raw_text": _scores(),
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": head,
    }


@pytest.mark.parametrize(
    ("spelling", "record"),
    [
        (
            "reviewed_commit",
            {"reviewed_base_sha": BASE, "reviewed_commit": FIRST_HEAD},
        ),
        (
            "reviewed_base",
            {"reviewed_base": BASE, "reviewed_head_sha": FIRST_HEAD},
        ),
        (
            "reviewed_head_sha",
            {"reviewed_base_sha": BASE, "reviewed_head_sha": FIRST_HEAD},
        ),
        (
            "reviewed_base_sha",
            {"reviewed_base_sha": BASE, "reviewed_commit": FIRST_HEAD},
        ),
        (
            "commits_read",
            {
                "reviewed_base_sha": BASE,
                "commits_read": [INTERMEDIATE, FIRST_HEAD],
            },
        ),
    ],
)
def test_existing_revision_spelling_normalises_to_the_pair(
    spelling: str, record: dict[str, object], tmp_path: Path
) -> None:
    parsed = review_module.parse_review(_scores(), record=record)
    reviewed_run_id = f"r-{spelling.replace('_', '-')}"
    review_module.store_review(
        {
            "project": "reckon",
            "reviewed_run_id": reviewed_run_id,
            "review_run_id": f"r-review-{spelling.replace('_', '-')}",
            **record,
            **parsed,
        },
        base_dir=tmp_path,
    )
    stored = review_module.read_review(
        "reckon",
        reviewed_run_id,
        base_dir=tmp_path,
        reviewed_head_sha=FIRST_HEAD,
    )

    assert parsed["status"] == "parsed"
    assert parsed["reviewed_base_sha"] == BASE
    assert parsed["reviewed_head_sha"] == FIRST_HEAD
    assert stored is not None
    assert stored[spelling] == record[spelling]
    assert stored["reviewed_base_sha"] == BASE
    assert stored["reviewed_head_sha"] == FIRST_HEAD


def test_a_record_without_the_revision_pair_is_incomplete() -> None:
    parsed = review_module.parse_review(_scores(), record={})

    assert parsed["status"] == "incomplete"
    assert "reviewed_base_sha" not in parsed
    assert "reviewed_head_sha" not in parsed


@pytest.mark.parametrize(
    "record",
    [
        {"reviewed_base_sha": BASE},
        {"reviewed_head_sha": FIRST_HEAD},
        {"reviewed_base": "", "reviewed_commit": FIRST_HEAD},
        {"reviewed_base_sha": BASE, "commits_read": []},
    ],
)
def test_a_record_with_only_one_usable_revision_is_incomplete(
    record: dict[str, object],
) -> None:
    parsed = review_module.parse_review(_scores(), record=record)

    assert parsed["status"] == "incomplete"


def test_rechecks_at_different_heads_accumulate_and_are_read_by_head(
    tmp_path: Path,
) -> None:
    first_record = _record(FIRST_HEAD)
    second_record = _record(SECOND_HEAD)

    first_path = review_module.store_review(first_record, base_dir=tmp_path)
    second_path = review_module.store_review(second_record, base_dir=tmp_path)

    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert first_path.name.endswith(f".at-{FIRST_HEAD}.json")
    assert second_path.name.endswith(f".at-{SECOND_HEAD}.json")
    first_read = review_module.read_review(
        "reckon",
        "r-reviewed-run",
        base_dir=tmp_path,
        reviewed_head_sha=FIRST_HEAD,
    )
    second_read = review_module.read_review(
        "reckon",
        "r-reviewed-run",
        base_dir=tmp_path,
        reviewed_head_sha=SECOND_HEAD,
    )
    assert first_read is not None
    assert second_read is not None
    assert first_read["reviewed_head_sha"] == FIRST_HEAD
    assert first_read["review_run_id"] == first_record["review_run_id"]
    assert second_read["reviewed_head_sha"] == SECOND_HEAD
    assert second_read["review_run_id"] == second_record["review_run_id"]


@pytest.mark.parametrize("legacy_complete", [False, True])
def test_an_incomplete_recheck_never_supersedes_a_complete_review(
    tmp_path: Path,
    legacy_complete: bool,
) -> None:
    complete_record = _record(FIRST_HEAD)
    complete_path = review_module.store_review(complete_record, base_dir=tmp_path)
    if legacy_complete:
        legacy_path = review_module.review_path(
            "reckon", "r-reviewed-run", base_dir=tmp_path
        )
        complete_path.replace(legacy_path)
        complete_path = legacy_path
    incomplete_record = {
        **complete_record,
        "review_run_id": "r-incomplete-review",
        "status": "incomplete",
    }
    incomplete_record.pop("reviewed_head_sha")

    incomplete_path = review_module.store_review(
        incomplete_record,
        base_dir=tmp_path,
    )
    current = review_module.read_review(
        "reckon",
        "r-reviewed-run",
        base_dir=tmp_path,
    )

    assert complete_path.is_file()
    assert incomplete_path.is_file()
    assert incomplete_path != complete_path
    assert ".incomplete-r-incomplete-review.json" in incomplete_path.name
    assert current is not None
    assert current["status"] == "parsed"
    assert current["reviewed_base_sha"] == BASE
    assert current["reviewed_head_sha"] == FIRST_HEAD
    assert current["review_run_id"] == complete_record["review_run_id"]


def test_read_review_returns_none_when_the_named_head_was_not_reviewed(
    tmp_path: Path,
) -> None:
    review_module.store_review(_record(FIRST_HEAD), base_dir=tmp_path)

    assert (
        review_module.read_review(
            "reckon",
            "r-reviewed-run",
            base_dir=tmp_path,
            reviewed_head_sha=SECOND_HEAD,
        )
        is None
    )
