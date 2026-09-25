"""Recovery reads a finished worker's end from its own record.

A dispatched worker's supervisor writes ``exit.json`` beside the worker it
spawned, carrying the signal that ended it, the code it chose, and how many
stream records it produced. The pointer is written by the launcher and stops
being updated the moment the worker dies, so the record is the one account of
the end that survives — and a classification that infers death from a missing
pid alone cannot tell a worker that is mid-turn from one whose process left
hours ago.

These tests drive ``crew observe`` over run directories built in a temporary
config home, one per shape of end, and assert that each run classifies as its
record says. The pointers name a host this machine is not, which is the stale
claim a pointer leaves behind and the shape a run read from a peer login node
has: no process table here can answer for it, so the record — not the pid — is
what has to decide.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew.dispatch import observe as crew_observe

FOREIGN_HOST = "another-login-node"
SIGNAL = 15
SIGNAL_NAME = "SIGTERM"


def _pid_the_kernel_cannot_issue() -> int:
    """A pid no process can hold, so the liveness probe answers the same anywhere.

    Derived from the kernel's own ceiling rather than written down as a literal:
    a number that happens to be free on the machine the test was written on is
    a process someone else may hold on the machine it runs on, and the fixture
    would then be measuring the local process table instead of the record.
    """
    try:
        ceiling = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except (OSError, ValueError):
        ceiling = 2**22
    return ceiling + 1024


def _build_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    end: dict[str, object] | None,
    launcher_host: str = FOREIGN_HOST,
    pid: int | None = None,
    stored_alive: object = True,
    stream_lines: int = 4,
    recorded_run_id: str | None = None,
    exit_text: str | None = None,
    launch: str = "cli",
) -> dict:
    """Build one run directory and its live pointer under a temporary home."""
    home = tmp_path / run_id / "config"
    run_directory = home / "crew" / "runs" / run_id
    run_directory.mkdir(parents=True)
    stream = run_directory / "stream.jsonl"
    stream.write_text(
        "".join(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": f"turn {index}"},
                }
            )
            + "\n"
            for index in range(stream_lines)
        ),
        encoding="utf-8",
    )
    if end is not None:
        payload = {
            "run_id": recorded_run_id or run_id,
            "recorded_by": "supervisor",
            "worker_pid": 4242,
            "launched_at": "2026-09-25T04:10:00Z",
            "exited_at": "2026-09-25T04:30:00Z",
            "last_record_type": "item.completed" if stream_lines else None,
            **end,
        }
        (run_directory / recovery.EXIT_RECORD_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )
    if exit_text is not None:
        (run_directory / recovery.EXIT_RECORD_NAME).write_text(
            exit_text, encoding="utf-8"
        )
    worktree = tmp_path / run_id / "worktree"
    worktree.mkdir(parents=True)
    pointer = {
        "run_id": run_id,
        "project": "fixture-project",
        "phase": "working",
        "attempt": 1,
        "process_alive": stored_alive,
        "pid": pid if pid is not None else _pid_the_kernel_cannot_issue(),
        "launcher_host": launcher_host,
        "manifest_path": str(tmp_path / run_id / "manifest.md"),
        "log_path": str(stream),
        "stderr_path": str(run_directory / "stderr.log"),
        "worktree": str(worktree),
        "launch": launch,
        "command": "claude",
        "argv": ["claude", "-p", "carry the node to its manifest"],
        "backend": "fixture-lane",
        "session_id": "fixture-session",
        "node": {
            "id": run_id,
            "project": "fixture-project",
            "role": "implement",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{run_id}.json").write_text(json.dumps(pointer), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(home))
    return pointer


def _observed_classification(run_id: str) -> dict:
    """Drive the ``crew observe`` fold, then classify the observed pointer."""
    observed = crew_observe(run_id)
    return recovery.classify_pointer(observed)


CASES = (
    (
        "signalled",
        {
            "exit_code": None,
            "signal": SIGNAL,
            "signal_name": SIGNAL_NAME,
            "stream_records_seen": 4,
            "ended_during": "working",
        },
        recovery.INTERRUPTED_RUN_PHASE,
        f"ended by {SIGNAL_NAME}",
    ),
    (
        "clean-exit",
        {
            "exit_code": 0,
            "signal": None,
            "signal_name": None,
            "stream_records_seen": 4,
            "ended_during": "working",
        },
        "abandoned",
        "exited with code 0",
    ),
    (
        "launch-failure",
        {
            "exit_code": 1,
            "signal": None,
            "signal_name": None,
            "stream_records_seen": 0,
            "ended_during": "launch",
        },
        "launch-failed",
        "exited with code 1",
    ),
)


@pytest.mark.parametrize(
    ("tag", "end", "expected_classification", "expected_phrase"), CASES
)
def test_observe_classifies_a_finished_worker_from_its_exit_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
    end: dict[str, object],
    expected_classification: str,
    expected_phrase: str,
) -> None:
    run_id = f"r-exit-record-{tag}"
    stream_lines = int(end["stream_records_seen"])
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        end=end,
        stream_lines=stream_lines,
    )

    row = _observed_classification(run_id)

    assert row["classification"] == expected_classification
    assert expected_phrase in row["detail"]
    # The record decided this: the pid is on a host this machine cannot ask, so
    # the process table never answered and the exit record is the proof of the
    # end the stale "alive" claim on the pointer never was.
    assert row["process_alive"] is False
    assert row["liveness_proven"] is False
    assert row["exit_record"] is not None
    assert row["exit_record"]["exited_at"] == "2026-09-25T04:30:00Z"
    assert row["exit_record"]["stream_records_seen"] == end["stream_records_seen"]
    if expected_classification == recovery.INTERRUPTED_RUN_PHASE:
        assert row["effective_phase"] == recovery.INTERRUPTED_RUN_PHASE
        assert row["interruption"]["reason"] == "signal"
        assert row["interruption"]["signal_name"] == SIGNAL_NAME
        assert row["interruption"]["signal"] == SIGNAL
    else:
        assert row["interruption"] is None


def test_a_live_process_outranks_the_exit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed attempt reuses the run directory, so a record cannot kill it."""
    run_id = "r-exit-record-live-process"
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        end={
            "exit_code": None,
            "signal": SIGNAL,
            "signal_name": SIGNAL_NAME,
            "stream_records_seen": 4,
            "ended_during": "working",
        },
        launcher_host=socket.gethostname(),
        pid=os.getpid(),
    )

    row = _observed_classification(run_id)

    assert row["process_alive"] is True
    assert row["liveness_proven"] is True
    assert row["classification"] == "running"
    assert row["exit_record"] is None
    assert row["interruption"] is None


def test_a_record_naming_another_run_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-exit-record-other-run"
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        end={
            "exit_code": None,
            "signal": SIGNAL,
            "signal_name": SIGNAL_NAME,
            "stream_records_seen": 4,
            "ended_during": "working",
        },
        recorded_run_id="r-some-other-run",
    )

    row = _observed_classification(run_id)

    # A record naming another run is not this run's end. The missing process is
    # then the only evidence there is, and the run keeps the verdict inference
    # from a bare pid has always produced.
    assert row["exit_record"] is None
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert row["interruption"]["reason"] == "dead-pid-no-exit"


def test_an_unreadable_record_falls_back_to_the_pid_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-exit-record-unreadable"
    _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        end=None,
        exit_text="{not json at all",
    )

    row = _observed_classification(run_id)

    # Nothing readable, nothing read: the inference from the missing pid is the
    # only verdict left, and it is the one the record exists to replace.
    assert row["exit_record"] is None
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert row["interruption"]["reason"] == "dead-pid-no-exit"


def test_a_chosen_exit_is_not_an_interruption_for_an_orphaned_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Death is inferred from the missing pid only when no record exists."""
    chosen = "r-exit-record-orphan-chosen"
    pointer = _build_run(
        tmp_path,
        monkeypatch,
        run_id=chosen,
        end={
            "exit_code": 0,
            "signal": None,
            "signal_name": None,
            "stream_records_seen": 4,
            "ended_during": "working",
        },
        launch="in-harness",
    )
    _rewrite_phase(chosen, pointer, "orphaned")

    row = _observed_classification(chosen)

    assert row["classification"] == "abandoned"
    assert row["classification"] != recovery.INTERRUPTED_RUN_PHASE
    assert "exited with code 0" in row["detail"]


def test_an_orphaned_pointer_with_no_record_is_still_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the pair above: the inference stays where it belongs."""
    run_id = "r-exit-record-orphan-no-record"
    pointer = _build_run(
        tmp_path,
        monkeypatch,
        run_id=run_id,
        end=None,
        stored_alive=False,
        launch="in-harness",
    )
    _rewrite_phase(run_id, pointer, "orphaned")

    row = _observed_classification(run_id)

    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE
    assert row["interruption"]["reason"] == "dead-pid-no-exit"
    assert row["exit_record"] is None


def _rewrite_phase(run_id: str, pointer: dict, phase: str) -> None:
    """Record the phase a pointer held when its process was last seen."""
    updated = {**pointer, "phase": phase, "process_alive": pointer["process_alive"]}
    path = Path(os.environ["RECKON_HOME"]) / "crew" / "live" / f"{run_id}.json"
    path.write_text(json.dumps(updated), encoding="utf-8")
