"""A run cut off before its launch spawns a worker owes its session a duty.

A launch killed between composing its live pointer and spawning its own
supervisor leaves a record holding a pre-spawn phase with nothing beside it: no
pid, no worker record, no stream and no launch log. The classifier already reads
it as ``launch-failed`` — no model was reached and nothing recorded an end — but
that word carried no duty, so the pointer sat in the obligations view as silence
while it held whatever dispatch claim it had taken.

Two cases are measured through the view a coordinator reads. A pointer past the
stranded-launch bound appears as a ``launch-failed`` duty carrying the
classifier's own next command. The same pointer within the obligations does not,
because a launch that is merely young has not been shown to be cut off.

The two negative controls §4 names, each a mutation of the head revision, fail
one case each: dropping the ``launch-failed`` entry from
``RECOVERY_CLASSIFICATION_DUTY_KINDS`` leaves the first case absent from the
obligations, and lowering ``STRANDED_LAUNCH_BOUND_SECONDS`` to zero makes the
second case report. The bound is moved in-suite by the last case here, so the
second control's shape is demonstrated and not only described.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligations, recovery_classification, recovery_liveness
from reckon.crew.runs import _write_json, pointer_path, runs_dir

PROJECT = "stranded-launch-fixture"
SESSION = "stranded-launch-session"
STUCK_RUN_ID = "r-stranded-launch"
YOUNG_RUN_ID = "r-stranded-launch-young"
BOUND = recovery_classification.STRANDED_LAUNCH_BOUND_SECONDS
#: Twenty seconds: well inside a launch that is still composing its worker.
YOUNG_SECONDS = 20


def _stranded_pointer(run_id: str, *, started_seconds_ago: float) -> dict:
    """One pointer a launch left between composing it and spawning a worker.

    Every trace a spawned worker would leave is absent by construction: the run
    directory holds no attempt record and no worker record, the pointer names a
    stream and a stderr path that were never written, and no pid was ever
    recorded. Only the launch clock the launcher composes on the pointer before
    it spawns anything dates the attempt, which is what the classifier reads.
    """
    directory = runs_dir() / run_id
    directory.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC) - timedelta(seconds=started_seconds_ago)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(runs_dir()),
        "worktree": str(runs_dir() / f"{run_id}-tree"),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "codex",
        "role": "implement",
        "session": SESSION,
        "session_id": SESSION,
        "created_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempt_started_at": started.isoformat(),
        "attempt": 1,
        "phase": "starting",
        "log_path": str(directory / "stream.jsonl"),
        "stderr_path": str(directory / "stderr.log"),
        "manifest_path": str(directory / "manifest.md"),
        "node": {
            "id": run_id,
            "plan": "a-reexec-never-orphans-a-dispatch",
            "section": "list-a-run-stuck-before-launch-as-a-launchfailure-duty",
            "time_budget": "60m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)
    return record


@pytest.fixture(autouse=True)
def _the_real_pointer_directory_is_untouched() -> None:
    """No case may reach the workstation's own live-pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    mine = [real_live / f"{run_id}.json" for run_id in (STUCK_RUN_ID, YOUNG_RUN_ID)]

    assert not any(path.exists() for path in mine)
    yield
    assert not any(path.exists() for path in mine)


def _duties() -> list[dict]:
    return obligations.obligations(PROJECT, SESSION)["obligations"]


def _duty_for(run_id: str) -> dict | None:
    return next((item for item in _duties() if item.get("run_id") == run_id), None)


def test_a_pointer_past_the_bound_is_a_launch_failure_duty() -> None:
    """The launch was cut off; nothing about this pointer will ever change."""
    pointer = _stranded_pointer(STUCK_RUN_ID, started_seconds_ago=BOUND + 60)

    # The reading the duty rests on, asserted first so a case that reports for
    # some other reason cannot pass as this one.
    row = recovery_classification.classify_pointer(pointer)
    assert row["classification"] == "launch-failed"
    assert row["recovery_classification"] == "launch-failed"
    assert "stranded launch" in row["detail"]

    duty = _duty_for(STUCK_RUN_ID)

    assert duty is not None
    assert duty["kind"] == "launch-failed"
    assert duty["node"] == STUCK_RUN_ID
    assert duty["plan"] == "a-reexec-never-orphans-a-dispatch"
    # The command is the classifier's own, so the row names the release rather
    # than only the fact of the stall.
    assert duty["next_command"] == row["next_action"]
    assert f"compose run {STUCK_RUN_ID} again" in duty["next_command"]


def test_a_pointer_within_the_bound_owes_no_duty() -> None:
    """A launch this young may still be spawning its worker."""
    pointer = _stranded_pointer(YOUNG_RUN_ID, started_seconds_ago=YOUNG_SECONDS)

    row = recovery_classification.classify_pointer(pointer)
    assert row["classification"] != "launch-failed"

    assert _duty_for(YOUNG_RUN_ID) is None


def test_the_bound_is_read_from_its_one_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the bound at zero the young pointer reports, held by that constant.

    The second control's shape applied in-suite: the case above is held by the
    bound rather than by the fixture's own timing.
    """
    _stranded_pointer(YOUNG_RUN_ID, started_seconds_ago=YOUNG_SECONDS)
    monkeypatch.setattr(recovery_liveness, "STRANDED_LAUNCH_BOUND_SECONDS", 0)

    duty = _duty_for(YOUNG_RUN_ID)

    assert duty is not None
    assert duty["kind"] == "launch-failed"
