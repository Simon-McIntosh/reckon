"""A declared wait's facts reach the transition event a renderer reads.

The wait's probe verdict, its last observation, the horizon it was declared
against and the brief a resumed worker reads are computed on the classified
row, but the snapshot the transition composer reduces from dropped four of the
five and the composed event carried none of them. A reader was then handed a
row that cannot tell a probe which never ran from one still pending, because
the fact that separates them never arrived.

These tests bind the carriage at both boundaries: the snapshot must hold the
five, and the event built from it must hold the same values. Each figure is
None when the run declares no wait — the explicit unmeasured state — so a zero
never arrives in place of a measurement nobody took.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reckon.crew import recovery

PROJECT = "fixture-project"
WAIT_STARTED_AT = "2026-09-08T07:42:00+00:00"
# Derived from the fixture's own declaration rather than written as a literal:
# an age frozen into the test would have to be edited on a future date to stay
# true.
STARTED = datetime.fromisoformat(WAIT_STARTED_AT)
WAIT_EXPECTED_SECONDS = 3540

# The five facts this node makes reachable.
WAIT_FACTS = (
    "wait_condition_state",
    "wait_observed",
    "wait_overdue",
    "expected_horizon_seconds",
    "resume_brief",
)


def _pointer(tmp_path: Path, *, run_id: str, manifest_text: str) -> dict:
    worktree = tmp_path / "worktrees" / run_id
    worktree.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / f"{run_id}-manifest.md"
    manifest.write_text(manifest_text, encoding="utf-8")
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": f"session-{run_id}",
        "role": "implement",
        "node": {"id": run_id, "plan": "plan-a", "section": "s3", "role": "implement"},
        "phase": "working",
        "process_alive": False,
        "worktree": str(worktree),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "manifest_path": str(manifest),
    }


WAITING_MANIFEST = (
    "status: waiting\n"
    "wait_condition: scheduler job 42\n"
    'wait_probe: ["printf", "COMPLETED"]\n'
    'wait_terminal: ["COMPLETED", "FAILED"]\n'
    f"wait_started_at: {WAIT_STARTED_AT}\n"
    "wait_expected: 59m\n"
    "resume_brief: collect the scheduler result\n"
)

NO_WAIT_MANIFEST = "status: working\n"


def _snapshot(
    tmp_path: Path, *, run_id: str, manifest_text: str, moment: float
) -> dict:
    return recovery._watch_snapshot(
        _pointer(tmp_path, run_id=run_id, manifest_text=manifest_text),
        moment=moment,
        stall_seconds=3600,
    )


def _event(snapshot: dict) -> dict:
    """Build the persistent transition object the way the follower does."""
    return recovery._watch_transition(
        PROJECT,
        kind="transition",
        snapshot=snapshot,
        previous=None,
        current=str(snapshot.get("state") or ""),
        counts={"working": 0, "blocked": 0, "unpromoted": 0, "waiting": 1},
        spend_runs=[],
        rate_statuses={},
    )


def test_a_declared_wait_carries_all_five_facts_into_the_event(tmp_path: Path) -> None:
    """The measured wait's facts reach the event with the snapshot's values.

    The probe answers a terminal state, so all five are measured: the condition
    reads met, the observation is what the probe printed, the horizon is the
    declared expectation, the wait is overdue against a moment chosen past it,
    and the brief is the declared one.
    """
    moment = STARTED.timestamp() + WAIT_EXPECTED_SECONDS + 60
    snapshot = _snapshot(
        tmp_path,
        run_id="r-declared-wait",
        manifest_text=WAITING_MANIFEST,
        moment=moment,
    )

    assert snapshot["wait_condition_state"] == "met"
    assert snapshot["wait_observed"] == "COMPLETED"
    assert snapshot["expected_horizon_seconds"] == WAIT_EXPECTED_SECONDS
    assert snapshot["resume_brief"] == "collect the scheduler result"
    assert snapshot["wait_overdue"] is True

    event = _event(snapshot)

    for fact in WAIT_FACTS:
        assert fact in event, f"{fact} never reached the transition event"
        assert event[fact] == snapshot[fact], f"{fact} was altered in transit"


def test_an_undeclared_wait_carries_none_never_zero_or_absent(tmp_path: Path) -> None:
    """With no declared wait every fact is None in the snapshot and the event.

    The keys are present and None rather than absent or zero: a run that
    declares no wait has taken no measurement, and a zero would assert one.
    """
    snapshot = _snapshot(
        tmp_path,
        run_id="r-undeclared",
        manifest_text=NO_WAIT_MANIFEST,
        moment=STARTED.timestamp(),
    )

    for fact in WAIT_FACTS:
        assert snapshot[fact] is None, f"{fact} fabricated a measurement"
        assert snapshot[fact] != 0

    event = _event(snapshot)

    for fact in WAIT_FACTS:
        assert fact in event, f"{fact} is absent from the event, not None"
        assert event[fact] is None, f"{fact} fabricated a measurement"


def test_the_composer_copies_each_fact_by_name(tmp_path: Path) -> None:
    """A snapshot with distinct sentinels arrives field for field.

    Distinct values make a swapped copy fail, and a measured zero passes
    through as zero, so the unmeasured None is not a stand-in for every
    false-looking number.
    """
    snapshot = {
        "run_id": "r-sentinels",
        "node": "n-sentinels",
        "state": "waiting",
        "detail": "",
        "wait_condition_state": "pending",
        "wait_observed": "RUNNING",
        "wait_overdue": False,
        "expected_horizon_seconds": 0,
        "resume_brief": "re-probe the lane",
    }

    event = _event(snapshot)

    for fact, value in (
        ("wait_condition_state", "pending"),
        ("wait_observed", "RUNNING"),
        ("wait_overdue", False),
        ("expected_horizon_seconds", 0),
        ("resume_brief", "re-probe the lane"),
    ):
        assert fact in event, f"{fact} never reached the transition event"
        assert event[fact] == value, f"{fact} was altered in transit"
