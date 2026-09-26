"""A run whose manifest reads complete never renders as dispatched.

Measured 2026-09-26:
``r-20260926T193548240312-review-of-follower-survives-a-config-change`` emitted
``working -> dispatched`` at 19:58:33Z, one second after its supervisor wrote
the worker's exit record, and ``dispatched -> complete`` six seconds later. The
run had already been reported working, so its phase moved backwards: the
dispatch happened after the work was delivered.

The input shape, every part of it read from that run's own records:

* the pointer still carried a pre-spawn phase (``starting``), written by the
  launcher and never advanced past it;
* ``launcher_host`` named a different machine, so the stored
  ``process_alive: true`` was kept as an unproven answer rather than
  re-derived from this host's process table;
* the run directory's ``worker.json`` named a pid that had already exited;
* the manifest had reached ``complete``.

The phase arbiter returned the pre-spawn label for that shape, so the row
rendered dispatched. These tests bind the correction: a delivered report is
itself evidence the launch got past starting, and so is the presence of a
worker record whatever the process table now says, so a run observed working
never falls back to a pre-spawn label.

Every crew directory is environment-resolved into ``tmp_path`` through
``RECKON_HOME``, so the fixtures describe a fleet of their own and touch no
live run.
"""

from __future__ import annotations

import json
import os

import pytest

from reckon import crew
from reckon.crew import recovery, runs

PROJECT = "proj"
# A pid no process can hold, so a "worker has ended" fixture never depends on
# which processes this machine happens to be running.
GONE_PID = 999_999_999


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point every crew directory at a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _pointer(
    run_id: str,
    *,
    phase: str = "starting",
    process_alive: bool | None = True,
    launcher_host: str | None = "another-login-node",
) -> dict:
    """A pointer shaped like the measured row.

    ``launcher_host`` names a foreign machine by default, which is what keeps
    the stored ``process_alive`` as an unproven answer instead of re-deriving it
    here — the exact pairing the measured run carried.

    The pointer's ``pid`` is this process, because the ticker re-derives
    liveness from the process table on every read (``runs.list_live``) before
    the snapshot is taken. A pid this process holds keeps the pointer's answer
    "alive" through that path, which is what a launch still running looks like;
    the worker record names its own pid separately, and that is what the
    fixtures vary to model a launch that has ended.
    """
    runs.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    stream = runs.run_dir(run_id) / "stream.jsonl"
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "s23-coord",
        "role": "review",
        "agent": {
            "backend": "clive",
            "model": "deepseek-v4.1-flash",
            "effort": "xhigh",
        },
        "node": {"id": run_id, "plan": "plan-a", "section": "s9"},
        "worktree": str(runs.run_dir(run_id) / "tree"),
        "log_path": str(stream),
        "stderr_path": str(runs.run_dir(run_id) / "stderr.log"),
        "manifest_path": str(runs.run_dir(run_id) / "manifest.md"),
        "phase": phase,
        "pid": os.getpid(),
        "process_alive": process_alive,
        "created_at": "2026-09-26T19:35:57Z",
    }
    if launcher_host is not None:
        record["launcher_host"] = launcher_host
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _worker(run_id: str, pid: int) -> None:
    """The supervisor's worker record: its presence means the worker spawned."""
    (runs.run_dir(run_id) / "worker.json").write_text(
        json.dumps({"run_id": run_id, "pid": pid}), encoding="utf-8"
    )


def _manifest(run_id: str, status: str) -> None:
    (runs.run_dir(run_id) / "manifest.md").write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: abc1234\nblockers: none\n",
        encoding="utf-8",
    )


def _snapshot(run_id: str, *, moment: float = 1_800_000.0) -> dict:
    return recovery._watch_snapshot(
        runs.read_pointer(run_id), moment=moment, stall_seconds=3600
    )


def _rendered(previous: dict, current: dict) -> list[str]:
    """The phase sequence the follower renders for one observation step."""
    events, _ = recovery.fleet_transitions(previous, current)
    return [state for _snapshot, _previous, state, _counts in events]


def _dispatched_after_working(sequence: list[str]) -> bool:
    """Whether any dispatched phase follows a working phase in ``sequence``."""
    seen_working = False
    for state in sequence:
        if state in ("working", "complete", "completed_unpromoted"):
            seen_working = True
        elif state == "dispatched" and seen_working:
            return True
    return False


def test_a_completed_run_never_renders_dispatched_after_working(home) -> None:
    """The measured shape: reported working, then the manifest and the worker end.

    The step where the worker record answers "gone" is the one that used to
    fall back to the pre-spawn label and render dispatched.
    """
    _pointer("r-delivered")
    _worker("r-delivered", os.getpid())
    first = _snapshot("r-delivered")
    assert first["state"] == "working", "a spawned worker is working, not dispatched"

    _manifest("r-delivered", "complete")
    _worker("r-delivered", GONE_PID)
    second = _snapshot("r-delivered")

    sequence = [
        first["state"],
        *_rendered({"r-delivered": first}, {"r-delivered": second}),
    ]
    assert sequence[0] == "working"
    assert "dispatched" not in sequence, sequence
    assert not _dispatched_after_working(sequence), sequence


def test_a_dead_workers_in_progress_manifest_never_renders_dispatched(home) -> None:
    """The same fall-back with no verdict yet: the launch still happened.

    A run whose report never reached a terminal status must not lose the
    evidence that it ran; reading it as dispatched would erase work in flight.
    """
    _pointer("r-in-progress")
    _worker("r-in-progress", os.getpid())
    first = _snapshot("r-in-progress")
    assert first["state"] == "working"

    _manifest("r-in-progress", "in-progress")
    _worker("r-in-progress", GONE_PID)
    second = _snapshot("r-in-progress")

    sequence = [
        first["state"],
        *_rendered({"r-in-progress": first}, {"r-in-progress": second}),
    ]
    assert not _dispatched_after_working(sequence), sequence
    assert second["state"] == "working"


def test_the_measured_row_is_not_dispatched(home) -> None:
    """The classifier row for the exact measured input, asserted directly.

    This is the input that produced the dispatched word: a pre-spawn label, a
    foreign launcher's unproven live answer, a dead worker record and a
    complete manifest. The effective phase must be past starting.
    """
    _pointer("r-measured")
    _worker("r-measured", GONE_PID)
    _manifest("r-measured", "complete")

    row = recovery.classify_pointer(
        runs.read_pointer("r-measured"), now_seconds=1_800_000.0
    )
    assert row["effective_phase"] != "starting"
    assert row["fleet_verdict"]["state"] != "dispatched"


def test_a_prespawn_run_with_no_worker_record_still_renders_dispatched(home) -> None:
    """Control: a run that never spawned a worker keeps its launch word.

    Nothing has happened yet, so the launcher's label stands. A fix that made
    every pre-spawn run read working would hide the state a coordinator acts on.
    """
    _pointer("r-prespawn")
    snapshot = _snapshot("r-prespawn")
    assert snapshot["state"] == "dispatched"


def test_a_worker_that_ended_without_a_report_reads_working_not_dispatched(
    home,
) -> None:
    """Control: the worker record alone is evidence the launch happened.

    A worker that spawned and exited before delivering anything is not a run
    still waiting to launch, and it is not a completed one either.
    """
    _pointer("r-abandoned")
    _worker("r-abandoned", GONE_PID)
    snapshot = _snapshot("r-abandoned")
    assert snapshot["state"] == "working"


def test_a_terminal_report_from_a_live_process_still_reads_working(home) -> None:
    """Control: a live worker's terminal report is a deferred outcome.

    The classifier already reads a live process against a terminal report as an
    outcome not yet in force, and the phase arbiter must agree — reading it as
    complete here would report a verdict the process has not stood behind.
    """
    _pointer("r-deferred")
    _worker("r-deferred", os.getpid())
    _manifest("r-deferred", "complete")

    row = recovery.classify_pointer(
        runs.read_pointer("r-deferred"), now_seconds=1_800_000.0
    )
    assert row["effective_phase"] == "working"
    assert row["fleet_verdict"]["state"] == "working"
    assert row["fleet_verdict"]["state"] != "dispatched"


def test_the_follower_renders_no_dispatched_after_working(home) -> None:
    """The end-to-end read: the ticker's own emitted sequence for one run.

    The stream is driven by a sleeper that ages the run from launched to
    delivered and then removes the pointer, so the whole phase sequence the
    follower emits is asserted rather than one fold step.
    """
    _pointer("r-streamed")
    sleeps = 0

    def advance(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            _worker("r-streamed", os.getpid())
        elif sleeps == 2:
            _manifest("r-streamed", "complete")
            _worker("r-streamed", GONE_PID)
        elif sleeps == 3:
            crew.pointer_path("r-streamed").unlink()
        else:
            pytest.fail("the ticker did not end after the last pointer was reconciled")

    stream = recovery.watch_ticker(
        PROJECT, stall_window="1h", poll_interval=0, sleeper=advance
    )
    try:
        baseline = next(stream)
        transitions = list(stream)
    finally:
        stream.close()

    sequence = [baseline["to_state"]] + [event["to_state"] for event in transitions]
    assert not _dispatched_after_working(sequence), sequence
    assert sequence.count("dispatched") <= 1, sequence
    assert sequence[0] == "dispatched", "a fresh pre-spawn run is dispatched"
