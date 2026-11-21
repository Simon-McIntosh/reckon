"""A run that never reached a model resumes with a plain resume command.

A launch that dies before its first turn leaves the run in phase
``launch-failed``. The lift loop must never relaunch it — repeating an exec that
already failed is the loop the phase exists to stop — but a person who has
repaired the command must be able to relaunch it with the plain resume command.

The scene is driven end to end: a run whose backend exits at exec is recorded as
a launch failure by the launcher's own recorder, the classifier is read as the
live view reads it, and the plain resume then launches a fresh attempt on the
run's own backend.

The declared negative control is the base revision: the plain resume is refused
with the launch-failed refusal, and the view's next action names no resume
command. Run with ``RECKON_LAUNCH_FAILED_RESUME_NEGATIVE_CONTROL=1`` the two
cases below must fail.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.crew import dispatch_launch, recovery, resumption, runs
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)

RUN_ID = "r-launch-failed-resumes"
# A backend that cannot run exits 127, exactly as the measured fence fault did.
EXEC_FAILURE_STATUS = 127


def _run_that_failed_to_launch(tmp_path: Path, repo_path: Path) -> dict:
    """A run the launcher marked launch-failed, as the live directory holds it.

    The backend script exits at exec, so the launcher's own recorder is what
    writes the failure: the pointer receives the phase, the failure list and the
    cause the classifier quotes.
    """
    record = _stopped_pointer(tmp_path, repo_path, RUN_ID, backend="alpha")
    directory = runs.run_dir(RUN_ID)
    # A stream of zero bytes is what "no turn ran" means; the reason is in
    # stderr, where the launcher's recorder quotes it.
    Path(record["log_path"]).write_bytes(b"")
    stderr = directory / "stderr.log"
    stderr.write_text("exec: cannot run the backend\n", encoding="utf-8")
    record["stderr_path"] = str(stderr)
    # The launch died before the worker wrote a verdict, so no manifest exists
    # yet; the phase and the failure list are the whole account.
    Path(record["manifest_path"]).unlink(missing_ok=True)
    runs._write_json(runs.pointer_path(RUN_ID), record)

    dispatch_launch._record_launch_failure(
        {
            "run_id": RUN_ID,
            "stream_path": record["log_path"],
            "stderr_path": str(stderr),
            "backend": record["backend"],
            "argv": [str(stderr)],
        },
        exit_status=EXEC_FAILURE_STATUS,
    )
    return runs.read_pointer(RUN_ID)


def test_a_launch_failed_run_resumes_on_its_own_backend(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The plain resume launches a fresh attempt on the run's recorded backend."""
    record = _run_that_failed_to_launch(tmp_path, request.getfixturevalue("repo"))
    assert record["phase"] == dispatch_launch.LAUNCH_FAILED_PHASE
    assert record.get("launch_failures"), "the failure was not recorded"

    launched: list[dict] = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched.append({"plan": plan, "log_path": str(log_path)})
        return os.getpid()

    resumption._resume(
        RUN_ID,
        record,
        config=CONFIG,
        launcher=launcher,
        advice="continue",
        coordinator=True,
    )

    assert launched, "the plain resume launched nothing"
    resumed = runs.read_pointer(RUN_ID)
    assert resumed["backend"] == "alpha", (
        f"the resume did not launch on the run's own backend: {resumed['backend']!r}"
    )
    assert resumed["phase"] == "working"
    assert resumed["attempt"] > int(record["attempt"])
    # The failure the new attempt supersedes is cleared, so the run no longer
    # reads as the launch that failed and the lift may act on it again.
    assert not resumed.get("launch_failures")
    assert list(runs.run_dir(RUN_ID).glob("resume-*-advice.txt")), (
        "no record of the launched resume was written"
    )


def test_the_view_names_the_plain_resume_command(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The live view's next action is the plain resume command."""
    record = _run_that_failed_to_launch(tmp_path, request.getfixturevalue("repo"))

    row = recovery.classify_pointer(record)

    assert row["classification"] == "launch-failed"
    assert f"reckon crew resume --run {RUN_ID}" in row["next_action"], row[
        "next_action"
    ]
    assert "--backend" not in row["next_action"], (
        "the view still names the lane-change form for a plain resume: "
        f"{row['next_action']!r}"
    )


def test_the_lift_still_refuses_a_launch_failed_run(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Only a person's resume relaunches it; the sweep must still refuse."""
    record = _run_that_failed_to_launch(tmp_path, request.getfixturevalue("repo"))
    launched: list[dict] = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):  # pragma: no cover
        launched.append({"plan": plan})
        return os.getpid()

    from reckon import crew

    with pytest.raises(crew.CrewError) as refusal:
        resumption._resume(
            RUN_ID, record, config=CONFIG, launcher=launcher, advice="continue"
        )

    assert dispatch_launch.LAUNCH_FAILED_PHASE in str(refusal.value)
    assert launched == []
