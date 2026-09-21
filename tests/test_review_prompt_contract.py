"""The contract the review prompt makes with its reviewer, and its limits.

An independent review is worth having only if it is quick enough to be
reflexive, so the prompt bounds what the reviewer reads as well as requiring a
verdict for every checklist item. Both halves are prompt text, and text drifts
silently: nothing fails when a bound stops being stated, or when the module
that carries it starts claiming it is not on any live path. The falsifiers here
read the prompt and the module as files and assert the claims that hold them.

The other half of the contract is a limit: per-item verdicts are recorded and
their absence named, but they must NOT enter the completeness predicate a
promotion reads. Reviews stored before the verdict line existed carry none, and
folding the items into that predicate would mark every one of them incomplete.
That is asserted here by calling the predicate, because asserting it only via
the parser would pass whether or not the predicate had moved.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew import recovery
from reckon.crew import review as review_module

# The three things the prompt tells the reviewer not to do. Each is one
# exclusion of the reading bound; the bound is only stated if all three are.
READING_BOUND_EXCLUSIONS = (
    "do not re-derive the implementation",
    "do not re-run the full suite",
    "do not review code the node did not touch",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DIMENSION_ONLY_REVIEW = "\n".join(
    f"SCORE {dimension}: 12" for dimension in review_module.REVIEW_DIMENSIONS
)


def test_prompt_states_the_reading_bound_with_all_three_exclusions() -> None:
    loaded = review_module.load_review_prompt().lower()
    for exclusion in READING_BOUND_EXCLUSIONS:
        assert exclusion in loaded, (
            f"the prompt no longer states the reading bound: {exclusion!r}"
        )


def test_review_module_no_longer_claims_it_is_unwired() -> None:
    source = (REPO_ROOT / "reckon" / "crew" / "review.py").read_text(encoding="utf-8")
    assert "Nothing here is wired into dispatch" not in source, (
        "the module docstring still says the parser is not on any live path"
    )
    # And the claim it should make instead: the call sites that reach it.
    for caller in ("promotion.py", "recovery.py"):
        assert caller in source, (
            f"the docstring no longer names {caller} among the call sites"
        )


def test_a_dimension_only_review_is_still_complete_for_promotion() -> None:
    record = review_module.parse_review(DIMENSION_ONLY_REVIEW)
    # The review genuinely carries no item verdicts, so the predicate's True
    # below is about the items not being required, not about them being there.
    assert record["absent_items"] == list(review_module.REVIEW_ITEMS)
    assert record["item_aggregate"] is None
    assert record["absent"] == []
    assert recovery._review_is_complete(record) is True


def test_a_review_missing_a_dimension_stays_incomplete_alongside_the_items() -> None:
    partial = "\n".join(
        f"SCORE {dimension}: 12"
        for dimension in review_module.REVIEW_DIMENSIONS
        if dimension != "fit"
    )
    record = review_module.parse_review(partial)
    assert record["absent"] == ["fit"]
    assert recovery._review_is_complete(record) is False
