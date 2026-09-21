"""Tests for the review record's production call-site measurement."""

from reckon.crew import recovery
from reckon.crew import review as review_module


def _dimension_scores() -> str:
    return "\n".join(
        f"SCORE {dimension}: 12" for dimension in review_module.REVIEW_DIMENSIONS
    )


def test_call_sites_are_recorded_with_a_machine_count() -> None:
    record = review_module.parse_review(
        _dimension_scores()
        + "\nCALL_SITES: reckon/crew/promotion.py:_require_review_waiver, "
        + "reckon/crew/recovery.py:_review_is_complete"
    )
    assert record["call_sites"] == [
        "reckon/crew/promotion.py:_require_review_waiver",
        "reckon/crew/recovery.py:_review_is_complete",
    ]
    assert record["call_site_count"] == 2


def test_none_call_sites_is_an_explicit_parseable_zero() -> None:
    record = review_module.parse_review(_dimension_scores() + "\nCALL_SITES: none")
    assert record["call_sites"] == []
    assert record["call_site_count"] == 0


def test_omitted_call_sites_is_absent_rather_than_zero() -> None:
    record = review_module.parse_review(_dimension_scores())
    assert "call_sites" not in record
    assert "call_site_count" not in record


def test_zero_count_can_be_filtered_without_matching_prose() -> None:
    records = [
        {"status": "parsed", "call_sites": [], "call_site_count": 0},
        {
            "status": "parsed",
            "call_sites": ["reckon/crew/recovery.py:_review_is_complete"],
            "call_site_count": 1,
        },
        {"status": "parsed"},
    ]
    zero_count = [record for record in records if record.get("call_site_count") == 0]
    assert zero_count == [records[0]]


def test_legacy_record_without_call_sites_remains_complete() -> None:
    record = review_module.parse_review(_dimension_scores())
    assert "call_sites" not in record
    assert recovery._review_is_complete(record) is True
