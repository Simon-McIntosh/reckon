"""A negative control is admitted on the runner's own record of its run.

The capture convention appends a terminal ``EXIT=<n>`` line, and reading only
that line refuses a control log that a capture without the convention wrote —
its pytest output says, in the runner's own words, which tests failed and that
the run failed, while no shell status was ever appended. The reader therefore
takes the failure from either record: the shell's status when it is present, and
otherwise the runner's ``FAILED`` list beside its ``N failed`` summary.

What is still refused is a log that states no failure at all (a bare
``EXIT=1`` with no runner output names nothing that broke), and a log whose only
failing id the head arm also fails (the control reddened nothing the change
newly broke). The identity requirement is unchanged: the failure must be a test
id the head arm passes.

The dispatch prompt carries the same contract, because a worker that is not told
the ``EXIT=`` line is optional writes a red log it believes is incomplete.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import CrewError, TaskNode
from reckon.crew.promotion import _require_declared_negative_control
from reckon.crew.promotion_evidence import _control_run_shows_a_failure
from reckon.crew.prompts import compose_prompt

DECLARATION = "the guard's early return was removed in a scratch copy"
REDDENED_CASE = "tests/test_guard.py::test_the_guard_refuses"
HEAD_RED_CASE = "tests/test_guard.py::test_a_case_the_head_arm_also_fails"
RUN_ID = "r-20261108T010000000000-control"


def _arm(failure_ids: list[str], *, revision: str) -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": revision,
        "command": "pytest -p no:cacheprovider -q tests/",
        "exit_status": 1 if failure_ids else 0,
        "log_path": f"logs/{revision}.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _gate(
    tmp_path: Path,
    *,
    log_text: str,
    baseline: dict | None = None,
    after: dict | None = None,
) -> dict:
    """Run the gate over one red log and the manifest that names it."""
    log = tmp_path / "control.log"
    log.write_text(log_text, encoding="utf-8")
    manifest: dict = {"negative_control_log": log.name}
    if baseline is not None:
        manifest["baseline_suite"] = baseline
    if after is not None:
        manifest["after_suite"] = after
    record = {
        "run_id": RUN_ID,
        "node": {
            "id": "node-a",
            "write_paths": ["tests/test_guard.py"],
            "negative_control": DECLARATION,
        },
    }
    return _require_declared_negative_control(
        RUN_ID,
        record,
        gate="passed",
        manifest=manifest,
        manifest_path=str(tmp_path / "manifest.md"),
    )


def _runner_red_log(failure_id: str) -> str:
    """A red run written by the runner with no capture shell's EXIT line."""
    return (
        f"{DECLARATION}\n"
        f"FAILED {failure_id} - AssertionError: no refusal\n"
        "1 failed, 4 passed in 0.19s\n"
    )


def test_a_runner_red_log_without_an_exit_line_is_admitted(tmp_path: Path) -> None:
    """The FAILED id and the runner's summary are the run's own failure record."""
    control = _gate(
        tmp_path,
        log_text=_runner_red_log(REDDENED_CASE),
        baseline=_arm([], revision="1111111"),
        after=_arm([], revision="2222222"),
    )

    assert control["verdict"] == "matched"
    assert control["control_run_failed"] is True
    assert control["control_exit_status"] is None
    assert control["added_failure_ids"] == [REDDENED_CASE]


def test_an_exit_line_without_runner_output_is_refused(tmp_path: Path) -> None:
    """A nonzero status says the command failed, not which test it broke."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=f"{DECLARATION}\nEXIT=1\n",
            baseline=_arm([], revision="1111111"),
            after=_arm([], revision="2222222"),
        )

    message = str(refusal.value)
    assert "names no failing test id" in message


def test_a_failing_id_the_head_arm_also_fails_is_refused(tmp_path: Path) -> None:
    """A control that reddens nothing the change broke is not a control."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_runner_red_log(HEAD_RED_CASE),
            baseline=_arm([], revision="1111111"),
            after=_arm([HEAD_RED_CASE], revision="2222222"),
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert "head arm" in message
    assert HEAD_RED_CASE in message


def test_the_failure_reader_admits_a_runner_record_and_refuses_a_bare_status() -> None:
    """The decision point itself: runner output admits, a bare nonzero status does not."""
    assert _control_run_shows_a_failure(_runner_red_log(REDDENED_CASE), None) is True
    assert _control_run_shows_a_failure(f"{DECLARATION}\nEXIT=1\n", 1) is True
    assert _control_run_shows_a_failure(f"{DECLARATION}\nEXIT=0\n", 0) is False
    assert (
        _control_run_shows_a_failure(f"{DECLARATION}\n5 passed in 0.1s\n", None)
        is False
    )


def test_a_composed_implement_prompt_names_the_control_contract() -> None:
    """A worker is told the EXIT line is optional when the runner shows the failure."""
    node = TaskNode(
        id="control-node",
        goal="read a negative control from the runner's own record",
        plan="plan-a",
        section="s2",
        role="implement",
        done_when="the control reader admits a runner-written red log",
        write_paths=["tests/test_reader.py", "reckon/crew/reader.py"],
        time_budget="20m",
        negative_control=DECLARATION,
    )
    prompt = compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktrees/control-run",
        working_directory="/repo/worktrees/control-run",
        manifest_path="/state/runs/control-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )

    flat = " ".join(prompt.split())
    assert "A terminal `EXIT=` line is one way the log shows the run failed" in flat
    assert "is not required" in flat
    assert "`FAILED <id>` list beside the runner's `N failed` summary" in flat
