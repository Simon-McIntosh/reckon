"""A declared wait is classified from condition state and worker liveness."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, resumption


def _waiting_pointer(tmp_path: Path, *, alive: bool) -> dict:
    run_id = "r-waiting-condition"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "status: waiting\n"
        "wait_condition: scheduler job 42\n"
        'wait_probe: ["scheduler-status", "--job", "42"]\n'
        'wait_terminal: ["COMPLETED", "FAILED"]\n'
        "wait_started_at: 2026-09-08T07:42:00+00:00\n"
        "wait_expected: 59m\n"
        "resume_brief: collect the scheduler result\n",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "worktree": str(worktree),
        "backend": "fixture-lane",
        "session_id": "fixture-session",
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }


def _condition(state: str):
    def observe(_pointer, _wait):
        return {
            "state": state,
            "observed": {
                "pending": "RUNNING",
                "met": "COMPLETED",
                "unknown": "UNRECOGNISED",
            }[state],
        }

    return observe


@pytest.mark.parametrize(
    ("alive", "condition_state", "expected_recovery"),
    [
        (True, "pending", "waiting"),
        (False, "pending", "waiting"),
        (True, "met", "ready"),
        (False, "met", "ready"),
        (True, "unknown", "waiting"),
        (False, "unknown", "waiting"),
    ],
)
def test_wait_classification_ranges_over_liveness_and_condition_state(
    tmp_path: Path,
    alive: bool,
    condition_state: str,
    expected_recovery: str,
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=alive)

    row = recovery.classify_pointer(
        pointer,
        condition_test=_condition(condition_state),
        now_seconds=1_788_853_920.0,
    )

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["recovery_classification"] == expected_recovery
    assert row["wait_condition_state"] == condition_state
    if condition_state == "met":
        assert row["recovery"] == "resume"
    else:
        assert row["recovery"] == "wait"
    if not alive:
        assert row["classification"] != "abandoned"
        assert row["recovery_classification"] != "abandoned"


def test_a_met_dead_wait_is_walked_by_the_resume_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=False)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(resumption, "list_live", lambda **_kwargs: [pointer])
    monkeypatch.setattr(resumption, "_claimed_write_paths", lambda _pointer: [])

    report = resumption.sweep(
        "fixture-project",
        dry_run=True,
        condition_test=lambda _pointer, _wait: {
            "terminal": True,
            "observed": "COMPLETED",
        },
    )

    assert report["checked"] == 1
    assert [row["run_id"] for row in report["resumed"]] == [pointer["run_id"]]
    assert report["resumed"][0]["would_resume"] is True


def test_a_successful_terminal_probe_is_ready_to_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=False)
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["scheduler-status"],
            returncode=0,
            stdout="COMPLETED\n",
            stderr="",
        ),
    )

    row = recovery.classify_pointer(pointer, now_seconds=1_788_853_920.0)

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["recovery_classification"] == "ready"
    assert row["recovery"] == "resume"
    assert row["wait_condition_state"] == "met"
    assert row["recovery_classification"] != "abandoned"


def test_a_probe_error_is_unknown_and_does_not_abandon(
    tmp_path: Path,
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=False)

    def raises(_pointer, _wait):
        raise OSError("probe unavailable")

    row = recovery.classify_pointer(
        pointer,
        condition_test=raises,
        now_seconds=1_788_853_920.0,
    )

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["recovery_classification"] == "waiting"
    assert row["wait_condition_state"] == "unknown"
    assert row["classification"] != "abandoned"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "COMPLETED\n"),
        (0, ""),
        (0, "UNRECOGNISED\n"),
    ],
)
def test_an_untrustworthy_probe_result_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=False)
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["scheduler-status"],
            returncode=returncode,
            stdout=stdout,
            stderr="",
        ),
    )

    row = recovery.classify_pointer(pointer, now_seconds=1_788_853_920.0)

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["recovery_classification"] == "waiting"
    assert row["wait_condition_state"] == "unknown"
    assert row["classification"] != "abandoned"


def test_the_classifier_bounds_a_probe_that_would_otherwise_hang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _waiting_pointer(tmp_path, alive=False)
    observed_timeouts: list[float] = []

    def would_hang(_argv, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None:
            raise AssertionError("the unbounded probe would not return")
        observed_timeouts.append(float(timeout))
        raise subprocess.TimeoutExpired(cmd="scheduler-status", timeout=timeout)

    monkeypatch.setattr(recovery.subprocess, "run", would_hang)

    row = recovery.classify_pointer(pointer, now_seconds=1_788_853_920.0)

    assert observed_timeouts
    assert max(observed_timeouts) <= 1.0
    assert row["classification"] == recovery.WAITING_STATUS
    assert row["recovery_classification"] == "waiting"
    assert row["wait_condition_state"] == "unknown"
    assert row["classification"] != "abandoned"
