"""A worker's time fence states when its attempt started, not only how long it runs.

A fence that gives a duration and nothing to measure it against leaves the
worker to estimate elapsed time from the amount of work it has done. Measured
2026-10-01: a run launched at 18:32:03Z under a 60-minute fence wrote "Time
fence reached" at 18:46:23Z — fourteen minutes in — with its test file, its
negative control and its head arm unwritten. The dispatch prompt and every
resume now carry the attempt's launch instant and deadline in ISO-8601 UTC,
together with the command that checks them (``date -u``), and a resume
restates both for the resumed attempt.

These tests render a dispatch prompt and a resume advice for one fixed launch
instant and budget and assert that both carry the same deadline string, which
is the launch instant plus the budget. The instant is injected as data rather
than read from the clock, so the rendering is deterministic; a second instant
and budget pair proves the deadline is derived rather than matched.
"""

from __future__ import annotations

import re
from pathlib import Path

from reckon.crew.dispatch import _compose_dispatch_prompt, _restate_time_fence
from reckon.crew.node import TaskNode

FIXED_LAUNCH = "2026-10-01T18:32:03Z"
FIXED_BUDGET = "60m"
FIXED_DEADLINE = "2026-10-01T19:32:03Z"

OTHER_LAUNCH = "2026-01-02T03:04:05Z"
OTHER_BUDGET = "90m"
OTHER_DEADLINE = "2026-01-02T04:34:05Z"

CLOCK_COMMAND = "`date -u`"


def _node(*, time_budget: str = FIXED_BUDGET) -> TaskNode:
    return TaskNode(
        id="time-fence-node",
        goal="state the attempt's launch instant and deadline in the time fence",
        plan="plan-a",
        role="implement",
        done_when="the fence names the attempt's own clock",
        write_paths=["reckon/crew/prompts.py"],
        time_budget=time_budget,
        manifest_path="/state/runs/time-fence-node/manifest.md",
    )


def _dispatch_prompt(
    tmp_root: Path,
    *,
    launch_instant: str = FIXED_LAUNCH,
    time_budget: str = FIXED_BUDGET,
) -> str:
    """The prompt a dispatch composes, with the attempt's instant injected."""
    return _compose_dispatch_prompt(
        node=_node(time_budget=time_budget),
        project="proj",
        authority={},
        backend={"sandbox": "worktree-full"},
        repo_root=tmp_root,
        run_directory=tmp_root / "run",
        worktree=str(tmp_root),
        working_directory=str(tmp_root),
        launch_instant=launch_instant,
        needs_help_after_failures=2,
    )


def _resume_prompt(
    *, launch_instant: str = FIXED_LAUNCH, time_budget: str = FIXED_BUDGET
) -> str:
    """The prompt a resume launches: the advice with its own fence restated."""
    record = {"node": {"time_budget": time_budget}}
    return _restate_time_fence(
        "the coordinator's advice", record, attempt_started_at=launch_instant
    )


def _deadline_of(text: str) -> str | None:
    """The deadline the text states, or None when it states none."""
    match = re.search(
        r"deadline ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)", text
    )
    return match.group(1) if match else None


# ── The dispatch prompt and a resume carry one deadline ─────────────────────


def test_a_dispatch_prompt_and_a_resume_state_the_same_deadline(tmp_path: Path):
    prompt = _dispatch_prompt(tmp_path)
    resumed = _resume_prompt()

    assert _deadline_of(prompt) == FIXED_DEADLINE
    assert _deadline_of(resumed) == _deadline_of(prompt)


def test_the_deadline_is_the_launch_instant_plus_the_budget(tmp_path: Path):
    prompt = _dispatch_prompt(
        tmp_path, launch_instant=OTHER_LAUNCH, time_budget=OTHER_BUDGET
    )
    resumed = _resume_prompt(launch_instant=OTHER_LAUNCH, time_budget=OTHER_BUDGET)

    assert _deadline_of(prompt) == OTHER_DEADLINE
    assert _deadline_of(resumed) == OTHER_DEADLINE


# ── Both artifacts state the instant and the clock that checks it ───────────


def test_each_fence_states_the_launch_instant_and_the_clock_command(tmp_path: Path):
    prompt = _dispatch_prompt(tmp_path)
    resumed = _resume_prompt()

    assert FIXED_LAUNCH in prompt
    assert FIXED_LAUNCH in resumed
    assert CLOCK_COMMAND in prompt
    assert CLOCK_COMMAND in resumed


# ── A record declaring no budget keeps the resumed prompt unchanged ─────────


def test_a_resume_without_a_recorded_budget_restates_no_fence():
    prompt = _restate_time_fence(
        "the coordinator's advice", {"node": {}}, attempt_started_at=FIXED_LAUNCH
    )

    assert prompt == "the coordinator's advice"
