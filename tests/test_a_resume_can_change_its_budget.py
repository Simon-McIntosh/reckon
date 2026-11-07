"""A resume states the allowance its own attempt runs under.

A run stopped on budget used to be stuck with the allowance it was dispatched
under: ``crew resume`` re-read the stored budget, so the only way to give the
work more clock was to discard the run and dispatch it again, losing the
session that already held the context.

``reckon crew resume --time-budget`` now records a new allowance on the run, and
the resumed attempt composes its fence from that figure rather than from the
one the run was dispatched under. A figure above the resolved fence is refused
unless the caller says why with ``--over-fence``, exactly as a dispatch above
the fence is refused, and the reason is kept beside the change.

The declared negative control ignores the option when the test resumes, which
leaves the reader on the stored budget: the resumed attempt then composes the
first attempt's deadline and the first case must turn red.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_sessions as dispatch_sessions_module
import reckon.crew_run_commands as crew_run_commands_module
from reckon import cli as cli_module
from reckon import crew
from reckon.crew.runs import pointer_path
from tests import test_resume_and_lane_change_follow_their_own_attempt as lane
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    crew_home,
    operator_home,
    repo,
)

pytestmark = pytest.mark.arms_watch_producer

# The declared mutation, printed verbatim as the red log's first line.
DECLARED_MUTATION = (
    "ignore --time-budget when recording a resumed run's allowance; its test "
    "must turn red and the log is recorded"
)

MUTATION_ENV = "RECKON_RESUME_BUDGET_NEGATIVE_CONTROL"

# The launch instant is pinned so the fence the door composes is deterministic
# and can be read as 45 minutes rather than as wall clock.
ATTEMPT_LAUNCH = "2031-01-01T01:00:00+00:00"
STORED_BUDGET = "25m"
STORED_DEADLINE = "2031-01-01T01:25:00Z"
NEW_BUDGET = "45m"
NEW_DEADLINE = "2031-01-01T01:45:00Z"
RAISED_BUDGET = "90m"
RAISED_DEADLINE = "2031-01-01T02:30:00Z"
FENCE = "60m"
OVER_FENCE_REASON = "H200 gate measured at 3 h in run r-fixture-h200"

FENCE_HEADER = "FENCE — TIME (resumed attempt)"
ADVICE = "the budget is raised; continue the gate you were running"

# The fixture flight config with the project's own wall-clock fence declared, so
# the refusal can be exercised against a figure the run really is bounded by.
CONFIG = json.loads(json.dumps(lane.CONFIG))
CONFIG["fences"]["time_budget"] = FENCE


def _ignore_the_option(run_id: str, *, time_budget: str, over_fence: str) -> None:
    """The write the declared mutation removes: the allowance is never stored."""


@pytest.fixture(autouse=True)
def _declared_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation when its environment variable is set."""
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            crew_run_commands_module, "_record_resumed_time_budget", _ignore_the_option
        )


@pytest.fixture()
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the launch instant ``resume_plan`` composes the fence from."""
    monkeypatch.setattr(dispatch_sessions_module, "_utc_now", lambda: ATTEMPT_LAUNCH)


class _CapturingSpawn:
    """Stands in for the spawn, recording the prompt the worker would read."""

    def __init__(self) -> None:
        self.prompt_text = None

    def __call__(self, plan, *, log_path, stderr_path, prompt_path) -> int:
        self.prompt_text = Path(prompt_path).read_text(encoding="utf-8")
        return 4_194_303


def _stub_everything_outside_the_budget(
    monkeypatch: pytest.MonkeyPatch, spawn: _CapturingSpawn
) -> None:
    """Wire flight resolution and the spawn away from the subject.

    The subject is the allowance the resumed attempt is fenced by, not how the
    flight config resolves or how a worker is started, so both are replaced.
    """
    monkeypatch.setattr(
        crew_run_commands_module, "_resolved_flight", lambda *a, **k: CONFIG
    )
    monkeypatch.setattr(crew, "_spawn", spawn)


def _invoke(*arguments: str):
    return CliRunner().invoke(cli_module.main, ["crew", "resume", *arguments])


def _stored_budget(run_id: str) -> str:
    record = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    return str((record.get("node") or {}).get("time_budget") or "")


@pytest.mark.usefixtures("_pinned_clock")
def test_the_resumed_turn_is_fenced_from_the_new_allowance(
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """45m given to a run stored at 25m fences the resumed turn from 45m."""
    run_id = "r-resume-new-budget"
    lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    assert _stored_budget(run_id) == STORED_BUDGET
    spawn = _CapturingSpawn()
    _stub_everything_outside_the_budget(monkeypatch, spawn)

    result = _invoke("--run", run_id, "--advice", ADVICE, "--time-budget", NEW_BUDGET)

    assert result.exit_code == 0, result.output
    assert spawn.prompt_text is not None, "the resume launched nothing"
    assert spawn.prompt_text.count(FENCE_HEADER) == 1, spawn.prompt_text
    assert f"deadline {NEW_DEADLINE}" in spawn.prompt_text, spawn.prompt_text
    assert STORED_DEADLINE not in spawn.prompt_text, spawn.prompt_text
    assert _stored_budget(run_id) == NEW_BUDGET


@pytest.mark.usefixtures("_pinned_clock")
def test_the_change_is_kept_beside_the_reason_it_carries(
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowance above the fence needs a reason, which the run keeps."""
    run_id = "r-resume-over-fence"
    lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    spawn = _CapturingSpawn()
    _stub_everything_outside_the_budget(monkeypatch, spawn)

    result = _invoke(
        "--run",
        run_id,
        "--advice",
        ADVICE,
        "--time-budget",
        RAISED_BUDGET,
        "--over-fence",
        OVER_FENCE_REASON,
    )

    assert result.exit_code == 0, result.output
    assert spawn.prompt_text is not None, "the resume launched nothing"
    assert f"deadline {RAISED_DEADLINE}" in spawn.prompt_text, spawn.prompt_text
    record = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    change = record["budget_changes"][-1]
    assert change["from"] and change["to"] == RAISED_BUDGET, change
    assert change["reason"] == OVER_FENCE_REASON, change


@pytest.mark.usefixtures("_pinned_clock")
def test_an_allowance_above_the_fence_is_refused_without_a_reason(
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above the fence with no stated reason refuses and launches nothing."""
    run_id = "r-resume-unfenced-budget"
    lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    spawn = _CapturingSpawn()
    _stub_everything_outside_the_budget(monkeypatch, spawn)

    result = _invoke(
        "--run", run_id, "--advice", ADVICE, "--time-budget", RAISED_BUDGET
    )

    assert result.exit_code != 0, result.output
    assert "--over-fence" in result.output, result.output
    assert spawn.prompt_text is None, "a refused resume launched a worker"
    assert _stored_budget(run_id) == STORED_BUDGET


if __name__ == "__main__":  # pragma: no cover - names the red-log recipe
    import sys

    sys.stdout.write(
        f"{DECLARED_MUTATION}\n"
        "apply by setting the recording helper aside "
        "(restoring the stored-budget resume), then pytest this file\n"
    )
    os.environ[MUTATION_ENV] = "1"
