"""The prescription judgement: what it refuses, and what it still admits.

The suite states the whole rule case by case: one refusal per property as a
sole failure, an exact one-artifact cardinality, and the numeric gate tested
against a recorded table of near-mixed done-whens beside genuine ones. The
near-miss table is the load-bearing part — the property it checks is the one
that decides whether the lane admits anything, so it is asserted against
strings that carry a digit and a baseline word without naming a measure.

The fixtures here are synthetic by rule: no plan document and no node identity
appears in this file, and two tests assert exactly that by reading this file's
own source rather than trusting the reader. A fixture that borrowed a real
slug would go stale the moment the plan moved.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from reckon.crew import prescription
from reckon.crew.node import TaskNode
from reckon.crew.prescription import (
    BASELINE_VALUE_RE,
    FILE_LINE_RE,
    GATE_QUANTITY_RE,
    MAX_FENCE_SECONDS,
    PRESCRIBED_PROPERTIES,
    judge_prescribed,
)

PLAN_SLUG = "example-slug"
ARTIFACT = "reckon/crew/prescription.py"
TEST_PATH = "tests/test_prescription.py"

GENUINE_GATE = (
    "pytest tests/test_things.py exits 0, against a before value of 3 failing tests"
)

# The reviewer's own false positive, quoted because the repair has to keep
# refusing it: a digit and a baseline word co-occur and no gate is named.
REVIEWER_FALSE_POSITIVE = "Before editing, read the 2026 note"

# Done-whens carrying the tokens the old check looked for — a number somewhere
# and a baseline-ish word somewhere — and naming no gate. Each must be refused.
NEAR_MISS_DONE_WHENS = (
    REVIEWER_FALSE_POSITIVE,
    "See docs/plans/example.html:2026 before starting",
    "The 30-minute fence was previously 2026 minutes long",
    "Update the 4 callers; the module previously had 3",
    "Set the timeout from 30s to 5s, as previously agreed",
    "The README's 2026 section was edited before",
    "Coverage was 90 before, the diff is 12 lines",
    "restore 3 helpers, previously 5",
)

# Done-whens that state a measure and the value it was measured against.
GENUINE_DONE_WHENS = (
    GENUINE_GATE,
    "tests/test_prescription.py passes with 0 failures, down from 18 failures",
    "the census reports 12 passing tests, previously 9",
    "pytest tests/test_things.py exits 0 with 3 skipped, baseline of 7 skipped",
)

# One override per property, each leaving the other four satisfied.
SOLE_FAILURE_OVERRIDES = {
    "one-artifact": {
        "write_paths": ["reckon/a.py", "reckon/b.py", TEST_PATH],
        "done_when": GENUINE_GATE,
    },
    "file-and-line": {"goal": "Add the check"},
    "numeric-gate": {"done_when": "the change is complete and reads well"},
    "literal-negative-control": {"negative_control": ""},
    "time-fence": {"time_budget": "31m"},
}


def prescribed_node(**overrides) -> TaskNode:
    """Return a node satisfying all five properties, unless overridden."""
    fields = {
        "id": "example-node",
        "goal": f"Add the check at {ARTIFACT}:1",
        "plan": PLAN_SLUG,
        "done_when": GENUINE_GATE,
        "write_paths": [ARTIFACT, TEST_PATH],
        "time_budget": "30m",
        "negative_control": "invert the comparison in the check; the test must fail",
    }
    fields.update(overrides)
    return TaskNode(**fields)


def test_a_fully_prescribed_node_is_prescribed_with_no_failures():
    verdict = judge_prescribed(prescribed_node())
    assert verdict["prescribed"] is True
    assert verdict["failures"] == []
    assert verdict["detail"] == {}


# --- the public surface ---------------------------------------------------


def test_the_module_public_surface_is_the_surface_its_record_claims():
    """The record claims one public function; the module must expose one."""
    claimed = {"judge_prescribed"}
    exposed = {
        name
        for name, obj in vars(prescription).items()
        if not name.startswith("_")
        and inspect.isfunction(obj)
        and getattr(obj, "__module__", "") == prescription.__name__
    }
    assert exposed == claimed


def test_the_module_declares_the_five_properties_the_ruling_names():
    assert PRESCRIBED_PROPERTIES == (
        "one-artifact",
        "file-and-line",
        "numeric-gate",
        "literal-negative-control",
        "time-fence",
    )


def test_every_failed_property_is_reported_in_the_ruling_order():
    node = prescribed_node(
        goal="Add the check",
        done_when="it works nicely",
        write_paths=["reckon/a.py", "reckon/b.py"],
        time_budget="2h",
        negative_control="",
    )
    verdict = judge_prescribed(node)
    assert verdict["failures"] == list(PRESCRIBED_PROPERTIES)
    assert set(verdict["detail"]) == set(PRESCRIBED_PROPERTIES)


# --- one-artifact, an exact cardinality -----------------------------------


def test_two_non_test_artifacts_are_refused_naming_one_artifact_only():
    node = prescribed_node(write_paths=["reckon/a.py", "reckon/b.py", TEST_PATH])
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["one-artifact"]
    assert "reckon/a.py" in verdict["detail"]["one-artifact"]


def test_zero_non_test_artifacts_are_refused_naming_one_artifact_only():
    node = prescribed_node(write_paths=[TEST_PATH])
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["one-artifact"]
    assert "no non-test artifact" in verdict["detail"]["one-artifact"]


def test_a_node_declaring_no_write_paths_at_all_is_refused():
    node = prescribed_node(write_paths=[])
    assert judge_prescribed(node)["failures"] == ["one-artifact"]


def test_the_landing_records_are_record_rather_than_artifact():
    node = prescribed_node(write_paths=[ARTIFACT, TEST_PATH])
    assert judge_prescribed(node)["prescribed"] is True


def test_landing_records_alone_leave_the_node_with_no_artifact():
    node = prescribed_node(
        write_paths=[
            TEST_PATH,
            f"docs/plans/{PLAN_SLUG}.html",
            f"docs/evidence/archive/{PLAN_SLUG}-landed.html",
        ]
    )
    assert judge_prescribed(node)["failures"] == ["one-artifact"]


# --- file-and-line --------------------------------------------------------


def test_a_node_without_a_path_and_line_is_refused_naming_file_and_line_only():
    node = prescribed_node(goal="Add the check")
    assert judge_prescribed(node)["failures"] == ["file-and-line"]


def test_the_declared_path_and_line_regex_matches_a_real_reference():
    assert FILE_LINE_RE.search("see reckon/crew/node.py:1 for the record")
    assert FILE_LINE_RE.search("docs/plans/some-doc.html:42")
    assert not FILE_LINE_RE.search("the module named without a line")


# --- numeric-gate, a measure and its before value -------------------------


def test_the_numeric_gate_refuses_the_reviewers_false_positive():
    node = prescribed_node(done_when=REVIEWER_FALSE_POSITIVE)
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["numeric-gate"]


@pytest.mark.parametrize("done_when", NEAR_MISS_DONE_WHENS)
def test_the_numeric_gate_refuses_each_near_miss_done_when(done_when):
    node = prescribed_node(done_when=done_when)
    assert judge_prescribed(node)["failures"] == ["numeric-gate"]


@pytest.mark.parametrize("done_when", GENUINE_DONE_WHENS)
def test_the_numeric_gate_admits_each_genuine_done_when(done_when):
    assert judge_prescribed(prescribed_node(done_when=done_when))["prescribed"] is True


def test_a_gate_quantity_without_a_baseline_value_is_refused():
    node = prescribed_node(done_when="pytest passes with 0 failures")
    assert judge_prescribed(node)["failures"] == ["numeric-gate"]


def test_a_baseline_value_with_no_gate_quantity_is_refused():
    node = prescribed_node(done_when="reduce the 12 helper functions, previously 20")
    assert judge_prescribed(node)["failures"] == ["numeric-gate"]


def test_the_gate_quantity_regex_binds_a_number_to_the_outcome_it_counts():
    assert GATE_QUANTITY_RE.search("3 failing tests")
    assert GATE_QUANTITY_RE.search("exits 0")
    assert not GATE_QUANTITY_RE.search("read the 2026 note")


def test_the_baseline_value_regex_requires_a_number_beside_the_marker():
    assert BASELINE_VALUE_RE.search("before value of 3 failing tests")
    assert BASELINE_VALUE_RE.search("its measured before value of 7")
    assert not BASELINE_VALUE_RE.search("before editing, the note was read")


# --- literal-negative-control --------------------------------------------


def test_a_missing_negative_control_is_refused_naming_literal_negative_control_only():
    node = prescribed_node(negative_control="")
    assert judge_prescribed(node)["failures"] == ["literal-negative-control"]


def test_a_none_declared_negative_control_is_refused_naming_literal_negative_control_only():
    node = prescribed_node(negative_control="none: no mutation applies here")
    assert judge_prescribed(node)["failures"] == ["literal-negative-control"]


# --- time-fence -----------------------------------------------------------


def test_an_over_long_fence_is_refused_naming_time_fence_only():
    assert judge_prescribed(prescribed_node(time_budget="31m"))["failures"] == [
        "time-fence"
    ]


def test_an_unparseable_fence_is_refused_naming_time_fence_only():
    assert judge_prescribed(prescribed_node(time_budget="later"))["failures"]


def test_a_fence_of_exactly_thirty_minutes_is_admitted():
    assert judge_prescribed(prescribed_node(time_budget="30m"))["prescribed"] is True


def test_a_fence_longer_than_the_ruling_limit_is_the_only_thing_that_bounds_it():
    assert MAX_FENCE_SECONDS == 30 * 60
    assert judge_prescribed(prescribed_node(time_budget="31m"))["prescribed"] is False


@pytest.mark.parametrize("budget", ["0s", "1m", "29m", "1800s"])
def test_budgets_at_or_under_the_limit_are_admitted(budget):
    assert judge_prescribed(prescribed_node(time_budget=budget))["prescribed"] is True


# --- the properties do not collapse into one another ----------------------


@pytest.mark.parametrize("prop", PRESCRIBED_PROPERTIES)
def test_each_property_is_reachable_as_a_sole_failure(prop):
    verdict = judge_prescribed(prescribed_node(**SOLE_FAILURE_OVERRIDES[prop]))
    assert verdict["failures"] == [prop]
    assert set(verdict["detail"]) == {prop}


# --- purity and the naming rule ------------------------------------------


def test_the_verdict_is_pure_and_reads_no_state_outside_the_node(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.iterdir())
    first = judge_prescribed(prescribed_node())
    second = judge_prescribed(prescribed_node())
    assert first == second
    assert first["prescribed"] is True
    assert sorted(tmp_path.iterdir()) == before == []


def test_this_file_names_no_plan_document_that_exists():
    source = Path(__file__).read_text(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    referenced = re.findall(r"docs/plans/([A-Za-z0-9._-]+\.html)", source)
    assert referenced, (
        "the fixture should exercise a plans/ path, or this checks nothing"
    )
    assert [
        name for name in referenced if (root / "docs" / "plans" / name).exists()
    ] == []


def test_this_file_names_no_node_identity():
    source = Path(__file__).read_text(encoding="utf-8")
    candidates = set(re.findall(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+){3,}\b", source))
    assert candidates <= set(PRESCRIBED_PROPERTIES)
