"""Tests for sizing a review to a finished run's risk (reckon/review_tiers.py).

The resolver assigns each finished run one of three tiers from what it actually
changed. These tests hold it to the declared rule with a fixture per tier, one
``none`` fixture for each path class a node can land without touching runtime
source, and one mixed fixture, and they hold the thresholds to the flight key
that carries them rather than to a literal in the resolver.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import flight
from reckon.path_classes import file_class, path_class
from reckon.review_tiers import FULL, LIGHT, NONE, review_tier

# ── Fixtures: what a run changed, and the tier review_tier() gives it ───────

# One fixture per tier, one ``none`` fixture per path class, and one mixed
# fixture. ``spec`` and ``lines`` are the run's declared specification level and
# its added-plus-deleted line count; ``risk`` is the capability risk the run's
# plan or section declares.
FIXTURES = [
    pytest.param(
        ["reckon/crew/promotion.py"],
        120,
        "guided",
        None,
        FULL,
        id="full-source-over-the-changed-line-ceiling",
    ),
    pytest.param(
        ["reckon/review_tiers.py"],
        4,
        "open",
        None,
        FULL,
        id="full-source-at-an-open-specification-level",
    ),
    pytest.param(
        ["docs/plans/a-review-is-sized-to-its-risk.html"],
        3,
        "guided",
        "critical",
        FULL,
        id="full-non-source-under-elevated-capability-risk",
    ),
    pytest.param(
        ["reckon/review_tiers.py"],
        10,
        "guided",
        None,
        LIGHT,
        id="light-small-source-at-a-guided-specification-level",
    ),
    pytest.param(
        ["reckon/crew/query.py", "tests/test_review_tiers.py"],
        49,
        "exact",
        None,
        LIGHT,
        id="light-small-source-at-an-exact-specification-level",
    ),
    pytest.param(
        ["tests/test_review_tiers.py"],
        40,
        "guided",
        None,
        NONE,
        id="none-tests",
    ),
    pytest.param(
        ["docs/plans/a-review-is-sized-to-its-risk.html"],
        80,
        "guided",
        None,
        NONE,
        id="none-plans",
    ),
    pytest.param(
        ["docs/evidence/archive/a-review-is-sized-to-its-risk-landed.html"],
        25,
        "guided",
        None,
        NONE,
        id="none-evidence",
    ),
    pytest.param(
        ["docs/research/data/crew-pattern-review/velocity/summary.json"],
        500,
        "guided",
        None,
        NONE,
        id="none-research-data",
    ),
    pytest.param(
        ["docs/figures/a-review-is-sized-to-its-risk/tiers.png"],
        1,
        "exact",
        None,
        NONE,
        id="none-figures",
    ),
    pytest.param(
        ["tests/test_review_tiers.py", "docs/plans/a-review-is-sized-to-its-risk.html"],
        60,
        "guided",
        None,
        NONE,
        id="none-mixed-tests-and-plan",
    ),
]


@pytest.mark.parametrize("changed_paths,lines,spec,risk,expected", FIXTURES)
def test_each_fixture_resolves_to_its_declared_tier(
    changed_paths, lines, spec, risk, expected
):
    """Every declared fixture resolves to the tier the rule assigns it."""
    assert review_tier(changed_paths, lines, spec, risk) == expected


def test_the_spa_the_server_delivers_is_runtime_source():
    """The delivered SPA is runtime source; ``file_class`` alone would miss it.

    ``path_class`` carries the one override the classes cannot express — the
    planning SPA is product code even under ``docs/`` — and the resolver is
    built on that entry point. A ``.jsx`` file is source at whatever size, so
    a run shipping one is reviewed rather than passed to the merged-head gate.
    """
    spa = "docs/ui/shell.jsx"
    assert path_class(spa) == "source"
    assert file_class(spa) == "other"
    # Resolved at an open spec level so the tier turns on the classification
    # rather than on the diff size.
    assert review_tier([spa], 3, "open") == FULL


def test_a_run_that_changes_nothing_is_not_reviewed():
    """An empty diff has no runtime source, but the rule still answers."""
    assert review_tier([], 0, "exact") == NONE
    assert review_tier([], 0, "guided", "critical") == FULL


def test_an_unmeasurable_diff_takes_the_fuller_review():
    """A caller that cannot measure the diff gets ``full``, never ``light``."""
    assert review_tier(["reckon/flight.py"], None, "guided") == FULL
    assert review_tier(["reckon/flight.py"], "not-a-number", "guarded") == FULL


def test_thresholds_come_from_the_resolved_flight_config():
    """The light ceiling is read from the config, not fixed in the resolver.

    A project declaring a tighter ceiling turns a diff that would otherwise be
    reviewed lightly into a full review, which is the point of putting the
    threshold in the flight layer.
    """
    default_lines, default_budget = flight.review_tier_thresholds({})
    assert (default_lines, default_budget) == (50, "10m")

    retuned = {
        "review": {"tiers": {"light_changed_lines": 5, "light_time_budget": "3m"}}
    }
    lines, budget = flight.review_tier_thresholds(retuned)
    assert (lines, budget) == (5, "3m")

    changed = ["reckon/flight.py"]
    assert (
        review_tier(changed, 10, "guided", light_changed_lines=default_lines) == LIGHT
    )
    assert review_tier(changed, 10, "guided", light_changed_lines=lines) == FULL


def test_shipped_defaults_carry_the_light_tier_defaults():
    """The bottom layer declares the thresholds and validates against the schema."""
    path = flight.shipped_defaults_path()
    flight.validate_layer(flight.read_layer_file(path), path)
    lines, budget = flight.review_tier_thresholds(flight.read_layer_file(path))
    assert (lines, budget) == (50, "10m")

    resolved = flight.resolve(host_path=Path("/nonexistent/flight.yaml"))
    assert resolved.origin("review.tiers.light_changed_lines") == "shipped"
