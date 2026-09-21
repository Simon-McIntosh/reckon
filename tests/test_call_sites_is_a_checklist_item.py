"""Call-site review answers share the checklist's accountable record shape."""

from reckon.crew import recovery
from reckon.crew import review as review_module


def _dimension_scores() -> str:
    return "\n".join(
        f"SCORE {dimension}: 12" for dimension in review_module.REVIEW_DIMENSIONS
    )


def _five_item_verdicts() -> str:
    return "\n".join(
        f"VERDICT {item}: examined {item}"
        for item in review_module.REVIEW_ITEMS
        if item != "call_sites"
    )


def test_call_sites_uses_the_checklist_item_record_shape() -> None:
    record = review_module.parse_review(
        f"{_dimension_scores()}\n{_five_item_verdicts()}\n"
        "CALL_SITES: reckon/crew/recovery.py:_review_is_complete"
    )

    assert record["item_verdicts"]["call_sites"] == (
        "verified 1 production call site(s)"
    )
    assert record["absent_items"] == []
    assert record["item_aggregate"] == len(review_module.REVIEW_ITEMS)


def test_call_site_count_is_present_only_for_a_measurement() -> None:
    scores = _dimension_scores()
    emitted = review_module.parse_review(
        scores + "\nCALL_SITES: reckon/crew/recovery.py:_review_is_complete"
    )
    none = review_module.parse_review(scores + "\nCALL_SITES: none")
    omitted = review_module.parse_review(scores)

    assert emitted["call_site_count"] == 1
    assert none["call_site_count"] == 0
    assert "call_site_count" not in omitted
    assert "call_sites" not in omitted
    assert "call_sites" not in omitted["item_verdicts"]


def test_missing_call_sites_is_named_by_the_existing_item_absence_predicate() -> None:
    record = review_module.parse_review(_dimension_scores())

    assert "call_sites" in record["absent_items"]
    assert record["item_aggregate"] is None


def test_legacy_review_without_item_or_call_site_fields_stays_complete() -> None:
    legacy_review = {
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 12),
        "absent": [],
        "total": 60,
    }

    assert "item_verdicts" not in legacy_review
    assert "call_site_count" not in legacy_review
    assert recovery._review_is_complete(legacy_review) is True
