"""The derived-value parity gate: rendered equals returned, field for field.

For the one fixture project in ``spa_parity_harness`` these tests compare the
value the SPA renders against the value the Python reader returns — never
against a literal on either side:

- each sprint's derived state and the drift flag rendered beside it;
- the endpoint set, with every closure's membership and completion counts,
  through both graph adapters (the payload reformatter and the raw-inventory
  closure recomputation);
- each project's rollup counts and activity-series length;
- the schedule's lane count and the chain far end.

The comparison is N value pairs (8 sprint, 10 per endpoint across both
adapters plus the two endpoint-set memberships, 2 schedule, 6 rollup). Every
mismatch message names the two sources it compares.

The companion case is what makes this a check rather than a mirror: patch
exactly one value on one side of any pair and the comparison must report a
mismatch.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from tests import spa_parity_harness as harness

_REFERENCE = harness.REFERENCE


def _case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, with_rollup: bool = True):
    inventory, sprints, runs = harness.fixture_inventory()
    rollup_dir = harness._rollup_repo(tmp_path) if with_rollup else None
    returned = harness.returned_read(
        inventory=inventory,
        sprints=sprints,
        runs=runs,
        rollup_dir=rollup_dir,
        monkeypatch=monkeypatch,
    )
    rendered = harness.rendered_read(returned=returned, inventory=inventory)
    pairs = harness.build_pairs(rendered, returned)
    return rendered, returned, pairs


def _messages(pairs, dimension):
    return [
        pair.message
        for pair in harness.mismatches(pairs)
        if pair.dimension == dimension
    ]


def test_sprint_derived_state_and_drift_flag_equal_the_roadmap_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=False)
    messages = _messages(pairs, "sprint")
    assert messages == [], "\n".join(messages)


def test_endpoint_closures_and_membership_counts_equal_the_roadmap_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=False)
    messages = _messages(pairs, "endpoint")
    assert messages == [], "\n".join(messages)


def test_rollup_counts_and_activity_series_length_equal_the_fleet_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)
    messages = _messages(pairs, "rollup")
    assert messages == [], "\n".join(messages)


def test_schedule_lane_count_and_chain_far_end_equal_the_schedule_return(
    tmp_path, monkeypatch
) -> None:
    _rendered, _returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)
    messages = _messages(pairs, "schedule")
    assert messages == [], "\n".join(messages)


def test_every_surface_agrees_and_the_pair_count_is_stated(
    tmp_path, monkeypatch
) -> None:
    _rendered, returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)

    endpoint_count = len(returned["endpoints"])
    expected = 8 + (10 * endpoint_count + 2) + 2 + 6
    assert len(pairs) == expected, (
        f"pair count drifted from {expected} ({endpoint_count} endpoints) to "
        f"{len(pairs)}; update the stated digit"
    )
    assert all(
        "rendered" in pair.message and "≠ returned" in pair.message
        for pair in pairs
    )

    messages = [pair.message for pair in harness.mismatches(pairs)]
    assert messages == [], "\n".join(messages)


# ── The companion: a mutation on one side must bring a mismatch ─────────────


def _patch_and_rebuild(base_rendered, base_returned, mutate) -> list[harness.Pair]:
    rendered = copy.deepcopy(base_rendered)
    returned = copy.deepcopy(base_returned)
    mutate(rendered, returned)
    return harness.build_pairs(rendered, returned)


def _sprint_state_rendered(rendered, _returned):
    rendered["sprints"]["scaffold"]["state"] = "shipped"


def _sprint_drift_returned(_rendered, returned):
    scaffold = next(row for row in returned["sprints"] if row["id"] == "scaffold")
    scaffold["state_drift"]["stored"] = "active"


def _endpoint_closure_returned(_rendered, returned):
    named_deep = next(row for row in returned["endpoints"] if row["slug"] == "named-deep")
    named_deep["completion"]["total"] += 1


def _rollup_count_returned(_rendered, returned):
    returned["project_rows"][0]["plans_count"] += 1


def _rollup_summary_rendered(rendered, _returned):
    rendered["rollup"]["summary"]["active"] += 1


def _schedule_lane_returned(_rendered, returned):
    returned["schedule"]["lane_count"] = 999


@pytest.mark.parametrize(
    "mutate",
    [
        _sprint_state_rendered,
        _sprint_drift_returned,
        _endpoint_closure_returned,
        _rollup_count_returned,
        _rollup_summary_rendered,
        _schedule_lane_returned,
    ],
    ids=[
        "sprint-state-rendered",
        "sprint-drift-returned",
        "endpoint-closure-returned",
        "rollup-count-returned",
        "rollup-summary-rendered",
        "schedule-lane-returned",
    ],
)
def test_a_single_patched_value_on_one_side_makes_the_comparison_fail(
    tmp_path, monkeypatch, mutate
) -> None:
    rendered, returned, pairs = _case(tmp_path, monkeypatch, with_rollup=True)

    assert not harness.mismatches(pairs), "the undisturbed fixture must agree"
    patched_pairs = _patch_and_rebuild(rendered, returned, mutate)

    bad = harness.mismatches(patched_pairs)
    assert bad, (
        f"mutation {mutate.__name__} changed a value yet the parity check "
        f"reported agreement — the check cannot distinguish a real drift from "
        f"a comparison of a value with itself"
    )
    for pair in bad:
        assert "rendered" in pair.message and "≠ returned" in pair.message, pair.message
        assert pair.dimension in {"sprint", "endpoint", "rollup", "schedule"}
