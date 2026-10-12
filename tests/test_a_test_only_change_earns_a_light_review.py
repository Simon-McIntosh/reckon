"""A change to test source alone earns a light review rather than none.

A weakened, skipped or deleted assertion changes what the suite protects, and
that edit is invisible to a machine: the suite still passes, and only a reader
sees that the bar moved. A run whose changed paths include test source and no
runtime source therefore earns the light tier, while a run that changes only
plans, evidence, research, figures or state still resolves to none, and a run
that changes runtime source keeps the rule it had.
"""

from __future__ import annotations

import pytest

from reckon.review_tiers import (
    FULL,
    LIGHT,
    NONE,
    changes_test_source,
    review_tier,
)

# A test-only run is light whatever its size and specification level, so the
# cases vary both: the tier turns on the changed path class, not on the diff.
TEST_ONLY_CASES = [
    pytest.param(
        ["tests/test_review_tiers.py"], 40, "guided", None, LIGHT, id="test-file-alone"
    ),
    pytest.param(
        ["tests/test_something.py"],
        200,
        "open",
        None,
        LIGHT,
        id="test-file-over-the-changed-line-ceiling",
    ),
    pytest.param(
        ["tests/test_a.py", "tests/test_b.py"],
        0,
        "open",
        None,
        LIGHT,
        id="two-test-files-at-an-open-specification-level",
    ),
    pytest.param(
        ["tests/test_x.py", "docs/plans/a-plan.html"],
        60,
        "guided",
        None,
        LIGHT,
        id="test-file-together-with-a-plan",
    ),
]

# A run that changes no runtime source and no test source keeps ``none``, one
# fixture per path class it can land.
NON_REVIEWED_CASES = [
    pytest.param(["docs/plans/a-plan.html"], 80, "guided", None, NONE, id="plan-only"),
    pytest.param(
        ["docs/evidence/archive/a-landed.html"],
        25,
        "guided",
        None,
        NONE,
        id="evidence-only",
    ),
    pytest.param(
        ["docs/research/data/a-summary.json"],
        500,
        "guided",
        None,
        NONE,
        id="research-data-only",
    ),
    pytest.param(
        ["docs/figures/a-topic/tiers.png"], 1, "exact", None, NONE, id="figure-only"
    ),
    pytest.param(
        ["docs/state/proj/crew.json"],
        12,
        "guided",
        None,
        NONE,
        id="state-only",
    ),
]

# A runtime-source run keeps the rule that already existed, whether or not it
# also edits a test.
RUNTIME_CASES = [
    pytest.param(
        ["reckon/review_tiers.py", "tests/test_review_tiers.py"],
        10,
        "guided",
        None,
        LIGHT,
        id="small-source-with-its-test-is-light",
    ),
    pytest.param(
        ["reckon/crew/promotion.py", "tests/test_promotion.py"],
        120,
        "guided",
        None,
        FULL,
        id="source-over-the-ceiling-earns-full",
    ),
    pytest.param(
        ["tests/test_promotion.py"],
        3,
        "guided",
        "critical",
        FULL,
        id="elevated-risk-forces-full-even-for-a-test-only-run",
    ),
]


@pytest.mark.parametrize("changed_paths,lines,spec,risk,expected", TEST_ONLY_CASES)
def test_a_test_only_run_earns_a_light_review(
    changed_paths, lines, spec, risk, expected
):
    """Test source with no runtime source is light, not none."""
    assert review_tier(changed_paths, lines, spec, risk) == expected


@pytest.mark.parametrize("changed_paths,lines,spec,risk,expected", NON_REVIEWED_CASES)
def test_a_run_that_changes_neither_source_keeps_none(
    changed_paths, lines, spec, risk, expected
):
    """Plans, evidence, research data, figures and state alone stay unreviewed."""
    assert review_tier(changed_paths, lines, spec, risk) == expected


@pytest.mark.parametrize("changed_paths,lines,spec,risk,expected", RUNTIME_CASES)
def test_a_runtime_source_run_keeps_its_existing_rule(
    changed_paths, lines, spec, risk, expected
):
    """One runtime-source path puts the run back on the light/full rule."""
    assert review_tier(changed_paths, lines, spec, risk) == expected


def test_an_empty_diff_is_not_reviewed():
    """A run that changed nothing resolves to none, as it always did."""
    assert review_tier([], 0, "guided") == NONE


def test_changes_test_source_classifies_by_path_class():
    """The helper reads the same path classes the resolver does."""
    assert changes_test_source(["tests/test_a.py"]) is True
    assert changes_test_source(["tests/nested/test_b.py"]) is True
    assert changes_test_source(["reckon/review_tiers.py"]) is False
    assert changes_test_source(["docs/plans/a-plan.html"]) is False
    assert changes_test_source(["docs/ui/shell.jsx"]) is False
    assert changes_test_source([]) is False
