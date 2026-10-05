"""Acceptance requires nine tenths of the maximum across every dimension."""

import pytest

from reckon.crew import recovery, review


def test_review_acceptance_floor_tracks_the_dimension_count():
    expected = len(review.REVIEW_DIMENSIONS) * review.REVIEW_MAX_SCORE * 9 // 10
    assert expected == recovery.REVIEW_ACCEPTANCE_FLOOR


@pytest.mark.parametrize("offset, accepted", [(-1, False), (0, True)])
def test_acceptance_enforces_the_derived_floor(offset, accepted):
    total = len(review.REVIEW_DIMENSIONS) * review.REVIEW_MAX_SCORE * 9 // 10 + offset
    score, remainder = divmod(total, len(review.REVIEW_DIMENSIONS))
    record = review.parse_review(
        "\n".join(
            f"SCORE {dimension}: {score + (index < remainder)}"
            for index, dimension in enumerate(review.REVIEW_DIMENSIONS)
        )
    )
    assert recovery._review_accepts_promotion(record) is accepted


@pytest.mark.parametrize("extra_dimensions", [(), ("additional",)])
def test_review_dispatch_brief_tracks_the_dimension_count(
    monkeypatch, tmp_path, extra_dimensions
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    dimensions = review.REVIEW_DIMENSIONS + extra_dimensions
    monkeypatch.setattr(review, "REVIEW_DIMENSIONS", dimensions)
    fields = recovery._review_dispatch_fields(
        {"run_id": "fixture-run", "project": "fixture"}
    )
    assert (
        f"scoring all {len(dimensions)} dimensions in the range 0..{review.REVIEW_MAX_SCORE}"
        in fields["done_when"]
    )
