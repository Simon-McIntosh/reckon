"""The prescription judgement: one refusal per property, and one node that passes."""

from __future__ import annotations

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.prescription import (
    FILE_LINE_RE,
    MAX_FENCE_SECONDS,
    PRESCRIBED_PROPERTIES,
    artifact_paths,
    judge_prescribed,
)

PLAN = "background-agent-fitness-and-dispatch-scope"
ARTIFACT = "reckon/crew/prescription.py"
TEST_PATH = "tests/test_prescription.py"


def prescribed_node(**overrides) -> TaskNode:
    """Return a node that satisfies all five properties, unless overridden."""
    fields = {
        "id": "a-prescribed-node",
        "goal": f"Add the check at {ARTIFACT}:1",
        "plan": PLAN,
        "done_when": (
            "pytest tests/test_things.py exits 0, against a before value of "
            "3 failing tests"
        ),
        "write_paths": [ARTIFACT, TEST_PATH],
        "time_budget": "30m",
        "negative_control": ("invert the comparison in the check; the test must fail"),
    }
    fields.update(overrides)
    return TaskNode(**fields)


def test_a_fully_prescribed_node_is_prescribed_with_no_failures():
    verdict = judge_prescribed(prescribed_node())
    assert verdict["prescribed"] is True
    assert verdict["failures"] == []
    assert verdict["detail"] == {}


def test_two_non_test_artifacts_are_refused_naming_one_artifact_only():
    node = prescribed_node(write_paths=["reckon/a.py", "reckon/b.py", TEST_PATH])
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["one-artifact"]
    assert "reckon/a.py" in verdict["detail"]["one-artifact"]


def test_a_node_without_a_path_and_line_is_refused_naming_file_and_line_only():
    node = prescribed_node(goal="Add the check")
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["file-and-line"]


def test_a_node_without_a_baseline_is_refused_naming_numeric_gate_only():
    node = prescribed_node(
        done_when="pytest tests/test_things.py passes with no counts at all"
    )
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["numeric-gate"]


def test_a_missing_negative_control_is_refused_naming_literal_negative_control_only():
    node = prescribed_node(negative_control="")
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["literal-negative-control"]


def test_a_none_declared_negative_control_is_refused_naming_literal_negative_control_only():
    node = prescribed_node(negative_control="none: no mutation applies here")
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["literal-negative-control"]


def test_an_over_long_fence_is_refused_naming_time_fence_only():
    node = prescribed_node(time_budget="31m")
    verdict = judge_prescribed(node)
    assert verdict["failures"] == ["time-fence"]


def test_a_fence_of_exactly_thirty_minutes_is_admitted():
    assert judge_prescribed(prescribed_node(time_budget="30m"))["prescribed"] is True


def test_the_landing_records_are_not_counted_as_artifacts():
    node = prescribed_node(
        write_paths=[
            ARTIFACT,
            TEST_PATH,
            f"docs/plans/{PLAN}.html",
            f"docs/evidence/archive/{PLAN}-landed.html",
        ]
    )
    assert artifact_paths(node) == [ARTIFACT]
    assert judge_prescribed(node)["prescribed"] is True


def test_this_nodes_own_dispatch_record_is_prescribed():
    """The record this worker ran under satisfies the rule it implements."""
    node = TaskNode(
        id="prescription-check-for-a-lane",
        goal=(
            "Add reckon/crew/prescription.py holding one pure function that "
            "judges whether a TaskNode is prescribed, returning the failing "
            "property names"
        ),
        plan=PLAN,
        section="§2",
        role="implement",
        spec_level="exact",
        done_when=(
            "reckon/crew/prescription.py defines one public function taking a "
            "TaskNode (reckon/crew/node.py:1) and returning a mapping with keys "
            "prescribed, failures and detail, judging exactly the five "
            "properties the lead ruling of 2026-09-21 06:35Z states on "
            f"docs/plans/{PLAN}.html: one artifact (the node's write paths name "
            "at most one non-test path), the file and line named, a numeric gate "
            "with its before value stated, a literal negative-control mutation, "
            "and a fence of at most thirty minutes. tests/test_prescription.py "
            "passes with at least 12 test functions and 0 failures, and asserts "
            "one refusal case per property. Nothing outside these two files "
            "changes, so the 15-file promotion cohort still fails exactly 18, "
            "its measured before value"
        ),
        write_paths=[
            ARTIFACT,
            TEST_PATH,
            f"docs/evidence/archive/{PLAN}-landed.html",
            f"docs/plans/{PLAN}.html",
        ],
        time_budget="30m",
        negative_control=(
            "change the fence comparison in prescription.py so an over-long "
            "time budget is admitted instead of refused; tests/test_prescription.py "
            "must fail"
        ),
    )
    verdict = judge_prescribed(node)
    assert verdict["failures"] == []
    assert verdict["prescribed"] is True


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


def test_the_module_declares_the_five_properties_the_ruling_names():
    assert PRESCRIBED_PROPERTIES == (
        "one-artifact",
        "file-and-line",
        "numeric-gate",
        "literal-negative-control",
        "time-fence",
    )


def test_the_declared_path_and_line_regex_matches_a_real_reference():
    assert FILE_LINE_RE.search("see reckon/crew/node.py:1 for the record")
    assert FILE_LINE_RE.search("docs/plans/some-plan.html:42")
    assert not FILE_LINE_RE.search("the module named without a line")


def test_the_verdict_is_pure_and_reads_no_state_outside_the_node(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.iterdir())
    first = judge_prescribed(prescribed_node())
    second = judge_prescribed(prescribed_node())
    assert first == second
    assert first["prescribed"] is True
    assert sorted(tmp_path.iterdir()) == before == []


def test_a_fence_longer_than_the_ruling_limit_is_the_only_thing_that_bounds_it():
    too_long = prescribed_node(time_budget="31m")
    assert MAX_FENCE_SECONDS == 30 * 60
    assert judge_prescribed(too_long)["prescribed"] is False


@pytest.mark.parametrize("budget", ["0s", "1m", "29m", "1800s"])
def test_budgets_at_or_under_the_limit_are_admitted(budget):
    assert judge_prescribed(prescribed_node(time_budget=budget))["prescribed"] is True
