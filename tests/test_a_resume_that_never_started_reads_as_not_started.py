"""A resume whose worker never begins a turn is a failed launch, not a working run.

A resume records its launcher's pid and the working phase as soon as the spawn
returns. When the launcher blocks before starting any worker — an srun client
waiting for an allocation its step never receives — the pointer's pid is a live
process, so the classifier answers "alive" about the launcher rather than about
any work, and the run reads as working for as long as it waits. Nothing releases
it, and stopping it leaves the client behind because a placed run's step is held
to be the scheduler's to end.

The cases below take the one reading that answers for a resumed attempt — a turn
in its own stream — and the one condition that makes a placed run's client a
live launch: the step record the step writes on the node that runs it. Before
that record exists there is no step work to leave to the scheduler, so the
client and everything under it are the launch's own local tree and a stop must
end them.

The properties, each on the reading it names:

* within the bound, a resumed attempt whose stream holds no turn does not read
  as a failed launch;
* past the bound, the same attempt reads as ``launch-failed``, carries the
  launcher's own waiting reason, and is offered for resume again;
* a resumed attempt whose stream holds an assistant turn reads as working past
  the bound, so a run that has actually begun is never released as failed.

A stop of a placed run whose step never started ends the client and the process
under it, names every pid it signalled, and leaves nothing alive.

Red when this change is reverted: the blocked run reads as working, and the stop
leaves the stand-in launcher alive.

Run directly, this module prints the mutation and the command that produced the
red log beneath it. It starts the processes its stop case needs and ends them.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import crew
from reckon.cli import main
from reckon.crew import recovery_classification, resumption, runs
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    CONFIG,
    _stopped_pointer,
    crew_home,
    operator_home,
    repo,
)

dispatch_sessions = importlib.import_module("reckon.crew.dispatch_sessions")
recovery_liveness = importlib.import_module("reckon.crew.recovery_liveness")

REASON = "srun: job 999999 queued and waiting for resources"

# The bound a resumed attempt may hold before a turn it never began is read as
# a failed launch. Written here as the clock a case reasons about, and checked
# against the classifier's own figure where that figure exists, so a case does
# not pass merely because it and the code drifted apart together.
BOUND_SECONDS = 900

# The mutation this module's cases are red on, printed as the declared negative
# control's first line.
NEGATIVE_CONTROL_MUTATION = (
    "at the base revision the blocked run reads as working, and the stop leaves "
    "the stand-in launcher alive"
)

# A stand-in launcher: it never starts a worker, it spawns a child of its own,
# and it says in its own log what it is waiting for, as a scheduler client does.
BLOCKING_CLIENT = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
    "open(os.environ['PIDFILE'], 'w').write(f'{os.getpid()} {child.pid}')\n"
    "sys.stderr.write(os.environ['WAITING_REASON'] + '\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(300)\n"
)

ASSISTANT_RECORD = {
    "type": "assistant",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
}


def _running(pid: int | None) -> bool:
    """Whether a pid is a live (non-zombie) process."""
    if not pid:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rindex(")") + 2 :].split()[0] != "Z"


def _wait_until(predicate: Callable[[], Any], *, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{detail} not met within {timeout:g}s")


def _kill(pid: int | None) -> None:
    if not pid:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def _started_seconds(record: dict[str, Any]) -> float:
    stamp = str(record.get("attempt_started_at") or "")
    return datetime.fromisoformat(stamp).timestamp()


def _resume_through_a_stand_in(
    tmp_path: Path,
    repo_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reason: str = "",
    begins_a_turn: bool = False,
) -> dict[str, Any]:
    """Drive a resume whose launcher never starts a worker.

    The launcher seam returns the test process's own pid, so the pointer names a
    live process that is not a worker — exactly the reading a resume records
    when a launcher is merely waiting. It writes its reason to the attempt's own
    stderr log, and an assistant record to the attempt's own stream when the case
    asks for a turn that has begun.
    """
    run_id = "r-resume-never-started"
    _stopped_pointer(tmp_path, repo_path, run_id, backend="alpha")
    monkeypatch.setattr("reckon.crew_run_commands._resolved_flight", lambda *a: CONFIG)
    monkeypatch.setattr(
        "reckon.crew.dispatch_watch._rearm_session_follower", lambda *a: (False, "")
    )
    launched = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        Path(stderr_path).write_text(reason, encoding="utf-8")
        if begins_a_turn:
            Path(log_path).write_text(
                json.dumps(ASSISTANT_RECORD) + "\n", encoding="utf-8"
            )
        launched.append(plan)
        return os.getpid()

    monkeypatch.setattr(crew, "_spawn", launcher)
    result = CliRunner().invoke(
        main, ["crew", "resume", "--run", run_id, "--advice", "continue"]
    )
    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    # Name a live process for the attempt, as a launcher that returned from its
    # spawn does. Without one the record reads as starting before any bound is
    # consulted, and no case here would be asking the bound a question.
    record = runs.read_pointer(run_id)
    record["pid"] = os.getpid()
    record["pid_start_time"] = dispatch_sessions._process_start_time(os.getpid())
    return record


def _blocked_stop_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """A resumed run whose placed client waits for a step that never started.

    ``supervisor.json`` and ``worker.json`` name the client's argv as a step
    launch, so the run is placed; ``step-task.txt`` is absent, which is what a
    step that never started looks like. The client spawned a child of its own,
    so the launch tree holds more than the pid the pointer names.
    """
    run_id = "r-resume-never-started-stop"
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    pidfile = tmp_path / "pids"
    environment = dict(
        os.environ,
        PIDFILE=str(pidfile),
        WAITING_REASON=REASON,
        RECKON_RUN_ID=run_id,
        RECKON_PROJECT="resume-never-started",
    )
    client = subprocess.Popen(
        [sys.executable, "-c", BLOCKING_CLIENT],
        env=environment,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_until(pidfile.is_file, timeout=20, detail="the stand-in client's pidfile")
    client_pid, child_pid = (int(word) for word in pidfile.read_text().split())
    argv = ["srun", "--jobid=999999", sys.executable, "-m", "worker"]
    (directory / "worker.json").write_text(
        json.dumps(
            {
                "pid": client_pid,
                "attempt": 2,
                "pid_start_time": dispatch_sessions._process_start_time(client_pid),
                "argv": argv,
            }
        ),
        encoding="utf-8",
    )
    (directory / "supervisor.json").write_text(
        json.dumps({"plan": {"argv": argv}}), encoding="utf-8"
    )
    stderr_path = directory / "resume-2.stderr.log"
    stderr_path.write_text(REASON + "\n", encoding="utf-8")
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": "resume-never-started",
        "repo": str(worktree),
        "worktree": str(worktree),
        "phase": "working",
        "pid": client_pid,
        "pid_start_time": dispatch_sessions._process_start_time(client_pid),
        "attempt": 2,
        "attempt_kind": "resume",
        "attempt_started_at": "2026-11-22T00:00:00Z",
        "created_at": "2026-11-22T00:00:00Z",
        "placement": {"srun": True},
        "job_id": "999999",
        "stderr_path": str(stderr_path),
        "log_path": str(directory / "resume-2.jsonl"),
    }
    return {
        "run_id": run_id,
        "client": client_pid,
        "child": child_pid,
        "client_process": client,
        "run_directory": directory,
        "pointer": pointer,
    }


def _cleanup(fixture: dict[str, Any]) -> None:
    for key in ("client", "child"):
        _kill(fixture[key])
    with contextlib.suppress(Exception):
        fixture["client_process"].wait(timeout=5)


def test_before_the_bound_a_resume_that_has_begun_no_turn_is_not_a_failed_launch(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = request.getfixturevalue("repo")
    record = _resume_through_a_stand_in(tmp_path, repo_path, monkeypatch, reason=REASON)
    started = _started_seconds(record)

    row = recovery_classification.classify_pointer(record, now_seconds=started + 60)

    assert row["classification"] != "launch-failed", row
    assert REASON not in str(row.get("detail") or "")


def test_past_the_bound_the_same_resume_reads_as_a_failed_launch_offering_resume(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = request.getfixturevalue("repo")
    record = _resume_through_a_stand_in(tmp_path, repo_path, monkeypatch, reason=REASON)
    started = _started_seconds(record)

    bound = getattr(resumption, "RESUME_TURN_BOUND_SECONDS", BOUND_SECONDS)
    assert bound == BOUND_SECONDS
    if hasattr(resumption, "RESUME_TURN_BOUND_SECONDS"):
        # The bound is the launch reading's own clock, not a second copy of its
        # value: a copy would drift the moment the owner's bound moved.
        assert bound is recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS
    row = recovery_classification.classify_pointer(
        record, now_seconds=started + BOUND_SECONDS + 60
    )

    assert row["classification"] == "launch-failed", row
    assert row["launch_error"] == REASON, row
    assert REASON in str(row["detail"]), row
    assert f"{BOUND_SECONDS}s" in str(row["detail"]), row
    # No end was recorded, so the remedy is the resume that carries the attempt
    # on rather than a discard of a run that still holds its session.
    assert row["ended_during_launch"] is False, row
    assert "crew resume" in str(row["next_action"]).lower(), row


def test_a_resume_is_dated_from_the_later_of_its_two_attempt_records(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer still dating the predecessor must not age the resumed attempt.

    A resume publishes the attempt record before it rewrites the pointer, so in
    that window the pointer dates the attempt that ended while ``attempt.json``
    already names the one running. Reading only the pointer would measure the
    bound from the predecessor and release a run that has just begun.
    """
    repo_path = request.getfixturevalue("repo")
    record = _resume_through_a_stand_in(tmp_path, repo_path, monkeypatch, reason=REASON)
    now = datetime.now(tz=UTC)
    stale = now.timestamp() - (BOUND_SECONDS * 4)
    record["attempt_started_at"] = datetime.fromtimestamp(stale, tz=UTC).isoformat()
    # The attempt names a live process, so the bound is the only question left:
    # a launcher merely waiting is alive, and being alive is not a begun turn.
    record["pid"] = os.getpid()
    record["pid_start_time"] = dispatch_sessions._process_start_time(os.getpid())
    runs._write_json(runs.pointer_path(record["run_id"]), record)
    (runs.run_dir(record["run_id"]) / recovery_liveness.ATTEMPT_RECORD_NAME).write_text(
        json.dumps(
            {"attempt": record["attempt"], "attempt_started_at": now.isoformat()}
        ),
        encoding="utf-8",
    )

    reading = resumption.resume_without_a_started_turn(
        record, now_seconds=stale + BOUND_SECONDS + 60
    )

    # The pointer says the attempt is an hour and a half old while its own
    # record says it began a moment ago, so there is no stalled launch to
    # report and the reading leaves the run to the arms that own it.
    assert reading is None, reading


def test_the_launcher_reason_is_the_recorded_tail_when_one_was_recorded(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason is read the way a launch failure reads it: the tail first."""
    repo_path = request.getfixturevalue("repo")
    record = _resume_through_a_stand_in(tmp_path, repo_path, monkeypatch, reason=REASON)
    started = _started_seconds(record)
    record["stderr_tail"] = "srun: waiting for the partition to free a node\n"
    record["stderr_path"] = str(tmp_path / "absent.stderr.log")

    row = recovery_classification.classify_pointer(
        record, now_seconds=started + BOUND_SECONDS + 60
    )

    assert row["classification"] == "launch-failed", row
    assert row["launch_error"] == "srun: waiting for the partition to free a node", row


def test_a_resumed_attempt_whose_stream_begins_a_turn_reads_as_working(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = request.getfixturevalue("repo")
    record = _resume_through_a_stand_in(
        tmp_path, repo_path, monkeypatch, reason=REASON, begins_a_turn=True
    )
    started = _started_seconds(record)

    row = recovery_classification.classify_pointer(
        record, now_seconds=started + BOUND_SECONDS + 3_600
    )

    assert row["classification"] == "running", row
    assert row["launch_error"] is None, row


def test_a_stop_ends_a_never_started_client_tree_and_names_every_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _blocked_stop_fixture(tmp_path, monkeypatch)
    runs._write_json(runs.pointer_path(fixture["run_id"]), fixture["pointer"])
    try:
        record = dispatch_sessions.terminate(fixture["run_id"])

        # The behavioural claim comes first: nothing the launch started is left,
        # whatever the record says.
        assert not _running(fixture["client"]), record.get("detail")
        assert not _running(fixture["child"]), record.get("detail")
        assert record["alive_after"] == [], record.get("detail")

        # Every pid signalled is named, and the resolved tree holds the client
        # and the process under it.
        assert record["ended_pids"], record.get("detail")
        assert set(record["ended_pids"]) >= {
            fixture["client"],
            fixture["child"],
        }
        printable = json.dumps(record)
        for pid in record["ended_pids"]:
            assert str(pid) in printable
    finally:
        _cleanup(fixture)


if __name__ == "__main__":  # pragma: no cover
    print(NEGATIVE_CONTROL_MUTATION)
    print("pytest -q tests/test_a_resume_that_never_started_reads_as_not_started.py")
