"""``crew stop`` ends the worker's scheduler step, or names the one it left.

A placed worker runs as a scheduler step on the node that holds the allocation,
so a signal sent from the coordinator's host cannot reach it and the ``srun``
client waits for that step rather than ending with the client. A stop that only
signalled the local tree therefore reported a settled stop while the step still
ran, and the next resume was refused until the step ended by itself.

The step is ended where the scheduler holds it, by the id the step wrote, and
the stop waits a bounded time for that id to leave the listing. When the bound
passes, the result names the surviving step and the command that ends it, and
the run records that reason so a later resume reads it instead of a generic
refusal.

The declared negative control removes the cancel: the step stays listed, and
the first case fails on that reading.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew.node import CrewError

dispatch_sessions = importlib.import_module("reckon.crew.dispatch_sessions")
process_signal = importlib.import_module("reckon.crew.process_signal")
resumption = importlib.import_module("reckon.crew.resumption")
runs = importlib.import_module("reckon.crew.runs")

RUN = "r-stop-ends-the-step"
JOB = "1281970"
STEP = "17"
STEP_ID = f"{JOB}.{STEP}"
CANCEL_COMMAND = f"scancel {STEP_ID}"
NEGATIVE_CONTROL_MUTATION = (
    "With the step cancel removed from the stop, the first case must fail with "
    "the step still listed."
)


class FakeScheduler:
    """A step listing that drops a step only when the scheduler is asked to."""

    def __init__(self, *, honour_cancel: bool = True) -> None:
        self.listed = {STEP_ID}
        self.calls: list[list[str]] = []
        self.honour_cancel = honour_cancel

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"squeue", "scancel"} else None

    def run(self, argv, **_kwargs) -> SimpleNamespace:
        self.calls.append([str(word) for word in argv])
        verb = Path(str(argv[0])).name
        if verb == "squeue":
            return SimpleNamespace(
                returncode=0,
                stdout="".join(f"{step}\n" for step in sorted(self.listed)),
                stderr="",
            )
        if verb == "scancel":
            assert len(argv) == 2, f"a cancel names one id: {argv}"
            if self.honour_cancel:
                self.listed.discard(str(argv[1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"the stop reached an unexpected verb: {argv}")

    def cancels(self) -> list[list[str]]:
        return [call for call in self.calls if Path(call[0]).name == "scancel"]


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A run whose own record names a step, with no real process behind it."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    run_directory = runs.run_dir(RUN)
    run_directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (run_directory / "worker.json").write_text(
        json.dumps(
            {
                "pid": 101,
                "pid_start_time": "77",
                "argv": ["/usr/bin/srun", f"--jobid={JOB}"],
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "step-task.txt").write_text(
        f"{JOB} {STEP} 303 88\n", encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": "stop-ends-the-step fixture",
            "repo": str(worktree),
            "worktree": str(worktree),
            "launch": "cli",
            "phase": "working",
            "pid": 101,
            "pid_start_time": "77",
            "attempt": 1,
            "attempt_kind": "dispatch",
            "attempt_started_at": "2026-11-21T00:00:00Z",
            "created_at": "2026-11-21T00:00:00Z",
        },
    )

    monkeypatch.setattr(
        dispatch_sessions,
        "_preserve_stopped_worktree",
        lambda *args, **kwargs: {"detail": "worktree kept"},
    )
    monkeypatch.setattr(dispatch_sessions, "process_alive", lambda pid: False)
    monkeypatch.setattr(
        dispatch_sessions, "signal_worker", lambda pid, *args, **kwargs: [303]
    )
    monkeypatch.setattr(
        dispatch_sessions, "_await_run_processes_gone", lambda *args, **kwargs: []
    )
    return run_directory


def _stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheduler: FakeScheduler
) -> tuple[dict, list[int]]:
    """Run one stored stop over the fixture, with the scheduler substituted."""
    run_directory = _fixture(tmp_path, monkeypatch)
    assert run_directory.is_dir()
    monkeypatch.setattr(process_signal.shutil, "which", scheduler.which)
    monkeypatch.setattr(process_signal.subprocess, "run", scheduler.run)
    # The bound is what the fixture controls: a zero bound asks the scheduler
    # once after the cancel, so a step that survives is reported at once and a
    # step that left is reported as gone.
    monkeypatch.setattr(process_signal, "STEP_STOP_SETTLE_SECONDS", 0.0)
    exits: list[int] = []

    def record_exit(*_args, **_kwargs: object) -> bool:
        exits.append(1)
        return True

    monkeypatch.setattr(dispatch_sessions, "_record_stopped_attempt_exit", record_exit)
    return dispatch_sessions.terminate(RUN), exits


def _run_record() -> dict:
    return dict(runs.read_pointer(RUN))


def test_a_stop_ends_the_step_and_leaves_the_run_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = FakeScheduler()
    record, exits = _stop(tmp_path, monkeypatch, scheduler)

    assert STEP_ID not in scheduler.listed, (
        "the step is ended by the stop, not left to end by itself"
    )
    assert scheduler.cancels() == [["/usr/bin/scancel", STEP_ID]]
    assert record["step_stop"] == {
        "step": STEP_ID,
        "command": CANCEL_COMMAND,
        "surviving": False,
        "cancel_exit": 0,
        "cancel_output": "",
        "detail": f"step {STEP_ID} left the scheduler after it was cancelled",
    }
    assert "stop_exit_recorded" in record
    assert exits == [1], "a settled step is an observed end"
    # The reason a resume reads is absent, and the door's own guard agrees.
    assert "stop_reason" not in record
    assert resumption.surviving_step_refusal(record) is None
    assert _run_record()["phase"] == "stopped"


def test_a_stop_whose_step_outlives_the_bound_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = FakeScheduler(honour_cancel=False)
    record, exits = _stop(tmp_path, monkeypatch, scheduler)

    assert scheduler.cancels() == [["/usr/bin/scancel", STEP_ID]]
    assert STEP_ID in scheduler.listed, "the fake scheduler kept the step"
    assert record["phase"] == "stopped"
    assert record["step_stop"]["surviving"] is True
    assert record["step_stop"]["step"] == STEP_ID
    assert record["step_stop"]["command"] == CANCEL_COMMAND
    assert STEP_ID in record["stop_reason"]
    assert CANCEL_COMMAND in record["stop_reason"]
    assert record["stop_exit_recorded"] is False
    assert exits == [], "an unsettled step is not an observed end"


def test_a_resume_of_a_run_whose_step_survived_is_refused_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = FakeScheduler(honour_cancel=False)
    record, _ = _stop(tmp_path, monkeypatch, scheduler)

    with pytest.raises(CrewError) as refused:
        dispatch_sessions.resume_plan(RUN, "continue")
    assert STEP_ID in str(refused.value)
    assert CANCEL_COMMAND in str(refused.value)

    # The dry-run prediction refuses with the same message, so a preview cannot
    # offer the resume the real door refuses.
    predicted = resumption._launcher_refusal(record, config=None)
    assert predicted is not None
    assert STEP_ID in str(predicted)
    assert str(predicted) == str(refused.value)
