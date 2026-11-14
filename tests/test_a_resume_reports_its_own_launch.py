"""A resume reports its launch while refusing an overlapping request for that run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main
from reckon.crew import resumption, runs
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)


@pytest.mark.parametrize("overlapping_request", [False, True])
def test_a_resume_reports_its_launch_and_refuses_only_another_call(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    overlapping_request: bool,
) -> None:
    """Keep the first call inside its launcher while issuing the second call.

    The launcher seam starts no worker. The request nested inside that seam
    overlaps the first request's lifetime, after its launch marker is written
    and before its attempt is recorded, without relying on thread scheduling.
    """
    repo_path = request.getfixturevalue("repo")
    run_id = "r-reports-own-resume"
    record = _stopped_pointer(tmp_path, repo_path, run_id, backend="alpha")
    monkeypatch.setattr("reckon.crew_run_commands._resolved_flight", lambda *a: CONFIG)
    monkeypatch.setattr(
        "reckon.crew.dispatch_watch._rearm_session_follower", lambda *a: (False, "")
    )
    launched = []
    refused = []
    advice = "inspect the solver receipt and continue"

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        marker = runs.read_pointer(run_id)[resumption.RESUME_LAUNCH_IN_FLIGHT]
        assert marker["pid"] == os.getpid()
        assert advice in plan.stdin_text
        assert advice in Path(prompt_path).read_text()
        launched.append(plan)
        if overlapping_request:
            result = CliRunner().invoke(
                main,
                ["crew", "resume", "--run", run_id, "--advice", "second request"],
            )
            assert result.exit_code != 0, result.output
            assert "already has a resume launching" in result.output, result.output
            assert run_id in result.output, result.output
            refused.append(result.output)
        return os.getpid()

    monkeypatch.setattr(crew, "_spawn", launcher)
    result = CliRunner().invoke(
        main, ["crew", "resume", "--run", run_id, "--advice", advice]
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["ok"] is True
    assert receipt["run_id"] == run_id
    assert receipt["pid"] == os.getpid()
    assert receipt["resumed_turn"] == 1
    assert len(launched) == 1
    assert len(refused) == int(overlapping_request)
    assert receipt["advice"] == advice
    after = runs.read_pointer(run_id)
    assert after["attempt"] > record["attempt"]
    assert after["phase"] == "working"
    assert resumption.RESUME_LAUNCH_IN_FLIGHT not in after
    assert run_id not in resumption._LAUNCHES_IN_THIS_PROCESS
