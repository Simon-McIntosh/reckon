"""A wait probe that can never report a terminal state is caught.

A waiting run lifts when its probe's observation equals one of its declared
terminal states. Two declarations make that impossible and are read here:

* a ``sacct`` probe that prints a structured document (``--json``,
  ``--parsable``, ``--parsable2``), whose last line is never a state name;
* at classification, any probe whose observation carries no word-like state
  token — empty, or punctuation alone such as the ``}`` that closes a JSON
  document.

A probe that reports a real non-terminal state such as ``RUNNING`` is an
ordinary wait, and one that reports a terminal state still lifts.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_liveness

NOW = 1_761_500_000.0


def _absent_pid() -> int:
    """A pid the kernel will never allocate: beyond the pid_max ceiling."""
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _waiting_pointer(
    tmp_path: Path,
    *,
    probe: str,
    terminal: str,
    alive: bool = False,
    observed_end: bool = False,
) -> dict:
    run_id = "r-wait-probe"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "status: waiting\n"
        "wait_condition: scheduler job 1284803\n"
        f"wait_probe: {probe}\n"
        f"wait_terminal: {terminal}\n"
        "wait_started_at: 2026-10-13T16:05:00+00:00\n"
        "wait_expected: 3h\n"
        "resume_brief: collect the scheduler result\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "fixture-project",
        "process_alive": alive,
        "phase": "working",
        "manifest_path": str(manifest),
        "log_path": str(tmp_path / "stream.jsonl"),
        "stderr_path": str(tmp_path / "stderr.log"),
        "worktree": str(worktree),
        "launch": "cli",
        "argv": ["fixture-agent", "exec"],
        "backend": "fixture-lane",
        "session_id": "fixture-session",
        "node": {
            "id": run_id,
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    if observed_end:
        # The end a lift rests on: this host checks the pid and finds it gone.
        record["launcher_host"] = socket.gethostname()
        record["pid"] = _absent_pid()
    return record


def _observation(state: str, observed: str):
    def observe(_pointer, _wait):
        return {"state": state, "observed": observed}

    return observe


@pytest.mark.parametrize("flag", ["--json", "--parsable2"])
def test_a_structured_sacct_probe_is_a_wait_key_defect(
    tmp_path: Path, flag: str
) -> None:
    pointer = _waiting_pointer(
        tmp_path,
        probe=f'["sacct", "{flag}", "-j", "1284803"]',
        terminal='["COMPLETED", "FAILED"]',
    )

    row = recovery.classify_pointer(
        pointer,
        now_seconds=NOW,
        condition_test=_observation("unknown", "}"),
    )

    defect = row["wait_key_defect"]
    assert defect is not None
    # The refusal names the form the probe should take, job id and all.
    assert "sacct -n -X -o State -j" in defect


def test_a_structured_sacct_probe_is_rejected_as_invalid(tmp_path: Path) -> None:
    pointer = _waiting_pointer(
        tmp_path,
        probe='["sacct", "--json", "-j", "1284803"]',
        terminal='["COMPLETED", "FAILED"]',
    )

    declaration = recovery_liveness._manifest_wait(
        {
            "status": "waiting",
            "wait_condition": "scheduler job 1284803",
            "wait_probe": '["sacct", "--json", "-j", "1284803"]',
            "wait_terminal": '["COMPLETED", "FAILED"]',
            "wait_started_at": "2026-10-13T16:05:00+00:00",
            "wait_expected": "3h",
            "resume_brief": "collect the scheduler result",
        },
        Path(pointer["manifest_path"]),
        now_seconds=NOW,
        stale_after_seconds=1800,
    )

    assert declaration is not None
    assert declaration["valid"] is False
    assert "sacct -n -X -o State -j" in declaration["error"]
    assert "sacct -n -X -o State -j" in declaration["wait_key_defect"]


def test_a_punctuation_observation_is_a_wait_key_defect(tmp_path: Path) -> None:
    pointer = _waiting_pointer(
        tmp_path,
        probe='["sacct", "-n", "-X", "-o", "State", "-j", "1284803"]',
        terminal='["COMPLETED", "FAILED"]',
    )

    row = recovery.classify_pointer(
        pointer,
        now_seconds=NOW,
        condition_test=_observation("unknown", "}"),
    )

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["wait_observed"] == "}"
    defect = row["wait_key_defect"]
    assert defect is not None
    assert "'}'" in defect


def test_a_running_observation_stays_an_ordinary_wait(tmp_path: Path) -> None:
    pointer = _waiting_pointer(
        tmp_path,
        probe='["sacct", "-n", "-X", "-o", "State", "-j", "1284803"]',
        terminal='["COMPLETED", "FAILED"]',
    )

    row = recovery.classify_pointer(
        pointer,
        now_seconds=NOW,
        condition_test=_observation("pending", "RUNNING"),
    )

    assert row["classification"] == recovery.WAITING_STATUS
    assert row["wait_observed"] == "RUNNING"
    assert row["wait_key_defect"] is None


def test_a_completed_observation_still_lifts(tmp_path: Path) -> None:
    pointer = _waiting_pointer(
        tmp_path,
        probe='["sacct", "-n", "-X", "-o", "State", "-j", "1284803"]',
        terminal='["COMPLETED", "FAILED"]',
        observed_end=True,
    )

    row = recovery.classify_pointer(
        pointer,
        now_seconds=NOW,
        condition_test=_observation("met", "COMPLETED"),
    )

    assert row["wait_observed"] == "COMPLETED"
    assert row["wait_key_defect"] is None
    assert row["recovery"] == "resume"
