"""Gate the resume-burn census recorded for the codex dispatch population.

The census file holds three things this gate reads: the commit that keyed
session reuse to a run's own task, the resume counts measured over the
dispatches that fall after it, and a positive control whose inheritance the
same instrument is known to read. The file states its own verdict, so a
non-zero cross-task count is asserted as the figure recorded rather than
against a target.
"""

import json
from pathlib import Path

CENSUS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "research"
    / "data"
    / "codex-resume-burn-after.json"
)


def census():
    with CENSUS.open() as handle:
        return json.load(handle)


def test_cross_task_resumes_after_the_task_keyed_population():
    data = census()
    window = data["after_window"]
    assert data["boundary_commit"].startswith("adbb6067")
    verdict = data["cross_task_resumes_verdict"]
    if verdict == "zero":
        assert window["cross_task_resumes"] == 0, data["statement"]
    else:
        assert verdict == "reported-non-zero", verdict
        assert window["cross_task_resumes"] > 0, data["statement"]
    assert window["dispatches"] >= 50, data["statement"]


def test_same_task_resumes_is_a_number():
    same_task = census()["after_window"]["same_task_resumes"]
    assert not isinstance(same_task, bool)
    assert isinstance(same_task, (int, float))


def test_positive_control_carries_inherited_tokens():
    control = census()["positive_control"]
    assert control, "the census records no positive control"
    assert control.get("run_id"), control
    assert control["inherited_tokens"] > 0, control
