"""A deadline lets a worker finish a bounded step it named, up to a hard stop.

A worker stops at its deadline even when its remaining work is one bounded
step — a declared gate, its base arm, a named solver batch — and writes a
partial manifest to wait for a resume. The prompt was the only thing stopping
it, and it stopped unconditionally. The rule now states the same instant at
every surface: at the deadline the checkpoint goes to disk, a step the manifest
named before the deadline may be finished, and the hard stop is the launch
instant plus the budget times the configured grace multiple.

These tests compose a real prompt through ``compose_prompt`` and restate a
resumed attempt's fence through the ``dispatch_sessions`` path, and check the
opt-in watchdog signals at the same hard stop. The instant is injected as data
rather than read from the clock, so the rendering is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reckon.crew import recovery, recovery_stream
from reckon.crew.dispatch_sessions import _restate_time_fence
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.crew.routing import resolved_grace_multiple

LAUNCH = "2026-10-19T16:00:00Z"
BUDGET = "60m"
DEADLINE = "2026-10-19T17:00:00Z"
HARD_STOP = "2026-10-19T17:30:00Z"

WRITE_CHECKPOINT = "Write the checkpoint at the deadline"
BOUNDED_STEP = (
    "Continue only through a bounded step the manifest named before the deadline"
)
CONTINUATION_MARKER = "Continue only through a bounded step"
NO_CONTINUATION = "no step is continued past it"
PUSH_ON = "never push on"
SPENT_BUDGET = "the time budget is spent"


def _node() -> TaskNode:
    return TaskNode(
        id="deadline-node",
        goal="finish a bounded step the manifest named before the deadline",
        plan="plan-a",
        section="s3",
        role="implement",
        done_when="the fence names the hard stop and the bounded step",
        write_paths=["reckon/crew/prompts.py"],
        time_budget=BUDGET,
    )


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompt(*, grace_multiple: float | str | None = 1.5) -> str:
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/deadline-node",
        working_directory="/repo/worktrees/deadline-node",
        manifest_path="/state/runs/deadline-node/manifest.md",
        time_budget=BUDGET,
        launch_instant=LAUNCH,
        needs_help_after_failures=2,
        grace_multiple=grace_multiple,
    )


def _resumed(*, config: dict[str, Any] | None = None) -> str:
    record = {"node": {"time_budget": BUDGET}}
    return _restate_time_fence(
        "the coordinator's advice",
        record,
        attempt_started_at=LAUNCH,
        config=config,
    )


# ── The composed prompt states the deadline and the hard stop ───────────────


def test_the_prompt_states_the_deadline_and_the_hard_stop():
    prompt = _flat(_prompt(grace_multiple=1.5))

    assert DEADLINE in prompt
    assert HARD_STOP in prompt


def test_the_fence_names_the_checkpoint_and_the_bounded_step():
    prompt = _flat(_prompt(grace_multiple=1.5))

    assert WRITE_CHECKPOINT in prompt
    assert BOUNDED_STEP in prompt


def test_the_prompt_never_says_never_push_on():
    assert PUSH_ON not in _flat(_prompt(grace_multiple=1.5))
    assert PUSH_ON not in _flat(_prompt(grace_multiple=1.0))
    assert PUSH_ON not in _flat(_prompt(grace_multiple=None))


def test_the_stop_list_names_the_hard_stop_and_the_unnamed_work():
    prompt = _flat(_prompt(grace_multiple=1.5))

    assert "the hard stop is reached" in prompt
    assert "remaining work that is not a named bounded step" in prompt
    assert SPENT_BUDGET not in prompt


# ── A resumed attempt states the hard stop from its own launch ──────────────


def test_a_resumed_fence_states_the_hard_stop_from_its_own_launch():
    resumed = _flat(_resumed(config={"fences": {"budget_grace_multiple": 1.5}}))

    assert DEADLINE in resumed
    assert HARD_STOP in resumed
    assert BOUNDED_STEP in resumed


# ── At or below one, and absent, the hard stop is the deadline ──────────────


def test_a_multiple_of_one_offers_no_continuation():
    prompt = _flat(_prompt(grace_multiple=1.0))

    assert DEADLINE in prompt
    assert HARD_STOP not in prompt
    assert NO_CONTINUATION in prompt
    assert CONTINUATION_MARKER not in prompt


def test_an_absent_multiple_offers_no_continuation():
    prompt = _flat(_prompt(grace_multiple=None))

    assert HARD_STOP not in prompt
    assert NO_CONTINUATION in prompt
    assert CONTINUATION_MARKER not in prompt


# ── The resolver reads one owner for the multiple ──────────────────────────


def test_resolved_grace_multiple_reads_configured_absent_and_invalid():
    assert resolved_grace_multiple({"fences": {"budget_grace_multiple": 1.5}}) == 1.5
    assert resolved_grace_multiple({}) == 1.0
    assert resolved_grace_multiple({"fences": {}}) == 1.0
    assert (
        resolved_grace_multiple({"fences": {"budget_grace_multiple": "soon"}}) is None
    )
    assert resolved_grace_multiple(None) == 1.0


def test_an_invalid_multiple_offers_no_continuation():
    prompt = _flat(_prompt(grace_multiple="soon"))

    assert NO_CONTINUATION in prompt
    assert CONTINUATION_MARKER not in prompt


# ── The watchdog signals at the same hard stop the fence states ─────────────


def _watchdog_record(started: datetime) -> dict[str, Any]:
    return {
        "run_id": "r-deadline-watchdog",
        "launch": "cli",
        "backend": "alpha",
        "argv": ["claude"],
        "pid": 4242,
        "pid_start_time": "start",
        "phase": "working",
        "created_at": started.isoformat(),
        "attempt_started_at": started.isoformat(),
        "node": {"id": "deadline-node", "plan": "plan-a", "time_budget": BUDGET},
        "process_alive": True,
        "log_path": str(Path("/nonexistent/stream.jsonl")),
    }


def test_the_watchdog_signals_at_the_hard_stop_not_before(monkeypatch) -> None:
    config = {"fences": {"enforce_budget_watchdog": True, "budget_grace_multiple": 1.5}}
    signalled: list[tuple[int, str]] = []
    monkeypatch.setattr(
        recovery_stream,
        "_signal_process_group",
        lambda pid, started_at, *, reason="", **kwargs: signalled.append((pid, reason)),
    )

    under = _watchdog_record(datetime.now(tz=UTC) - timedelta(minutes=89))
    recovery._apply_budget_watchdog(under, config)
    assert signalled == []

    over = _watchdog_record(datetime.now(tz=UTC) - timedelta(minutes=91))
    recovery._apply_budget_watchdog(over, config)
    assert signalled == [(4242, "budget-watchdog")]
