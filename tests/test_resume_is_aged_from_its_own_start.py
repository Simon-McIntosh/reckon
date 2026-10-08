"""A resumed run is aged from its own attempt, never a predecessor's stream.

A resumption leaves the superseded attempt's stream on disk and starts writing
its own log a moment later. Until that log exists the run has produced nothing
of its own, and reading the newest stream of any attempt then reports silence
that belongs to the attempt that already ended: measured 2026-09-26, a run went
``stalled`` with ``stream quiet for 956s`` 20 seconds after ``crew resume``,
because ``resume-1.jsonl`` did not exist yet and the classifier aged it from
the previous ``stream.jsonl``. It recovered on its own seconds later, when the
resumed worker wrote its first record.

The classifier now measures quiet time only from the current attempt's own log
and the moment the attempt began. Before that log exists there is no write to
read, so the run sits inside the launch window a fresh dispatch gets: it ages
from the attempt clock, or from the pointer a fresh dispatch falls back to when
no attempt clock was recorded. A second case has the attempt's own log last
written 1000s ago and asserts the run still reads stalled, so the guard narrows
which stream ages the run without hiding a genuinely quiet resume.

The declared negative control restores the old reading — quiet time from the
newest stream of any attempt — and the just-resumed case must then fail,
classifying stalled.
"""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_watch, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "age from the earliest stream again; the just-resumed case must fail"
)

MUTATION_ENV = "RECKON_RESUME_ATTEMPT_NEGATIVE_CONTROL"

# The window the stub runs are judged against, in seconds.
STALL_SECONDS = 900

# The age the fixtures declare for the predecessor's stream, and for a
# genuinely quiet resumed attempt whose own log has gone silent.
STALE_SECONDS = 1000

# How long ago the resumption launched, for the cases that must read working.
LAUNCH_SECONDS = 5


def _age_from_the_stream_alone(record: dict, *, now_seconds: float) -> int:
    """The reading the declared mutation restores: newest stream of any attempt."""
    found = recovery._record_newest_stream(record)
    if found is None:
        return recovery._stream_quiet_seconds(record, now_seconds=now_seconds)
    return max(0, int(now_seconds - found[1]))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            recovery_watch, "_run_stream_quiet_seconds", _age_from_the_stream_alone
        )


def _stub_resumed_run(
    tmp_path: Path,
    run_id: str,
    *,
    worker_pid: int,
    predecessor_age_seconds: float,
    own_log_age_seconds: float | None = None,
    launch_age_seconds: float | None = None,
    pointer_age_seconds: float | None = None,
    launch_in_record: bool = False,
) -> tuple[dict, float]:
    """A live run just resumed, with the previous attempt's stream still on disk.

    ``predecessor_age_seconds`` sets ``stream.jsonl``, the log the superseded
    attempt wrote. The current attempt's own log is ``resume-1.jsonl``: absent
    unless ``own_log_age_seconds`` names how long ago it was last written. The
    launch is recorded by the pointer's own mtime (``launch_age_seconds``, the
    launch window a fresh dispatch gets) or, when ``launch_in_record`` is set,
    only in the run directory's attempt record a supervisor publishes.
    """
    pointer: dict = {
        "run_id": run_id,
        "project": "resume-age-fixture",
        "session": "s21-coord",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(tmp_path / "manifests" / f"{run_id}.md"),
        "stderr_path": str(tmp_path / "stderr" / f"{run_id}.log"),
        "process_alive": None,
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "launcher_host": socket.gethostname(),
        "worktree": str(tmp_path / "tree"),
        "base_sha": "",
        "attempt": 2,
        "attempt_kind": "resume",
    }

    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    # The superseded attempt's stream, still on disk and far past the window.
    predecessor = directory / "stream.jsonl"
    predecessor.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    moment = time.time()
    predecessor_written = moment - predecessor_age_seconds
    os.utime(predecessor, (predecessor_written, predecessor_written))

    # The pointer names the current attempt's own log: resume-1.jsonl.
    own_log = directory / "resume-1.jsonl"
    pointer["log_path"] = str(own_log)
    if own_log_age_seconds is not None:
        own_log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
        own_written = moment - own_log_age_seconds
        os.utime(own_log, (own_written, own_written))

    if launch_in_record and launch_age_seconds is not None:
        started = datetime.now(tz=UTC) - timedelta(seconds=launch_age_seconds)
        (directory / recovery.ATTEMPT_RECORD_NAME).write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": 2,
                    "attempt_kind": "resume",
                    "attempt_started_at": started.isoformat(),
                }
            ),
            encoding="utf-8",
        )

    liveness._write_worker_record(run_id, worker_pid)

    # Publish the pointer. Its own mtime is the launch window a fresh dispatch
    # gets, so it carries the launch age when no attempt record does.
    pointer_file = runs.pointer_path(run_id)
    pointer_file.parent.mkdir(parents=True, exist_ok=True)
    pointer_file.write_text(json.dumps(pointer), encoding="utf-8")
    aged = (
        pointer_age_seconds if pointer_age_seconds is not None else launch_age_seconds
    )
    if aged is not None:
        pointer_written = moment - aged
        os.utime(pointer_file, (pointer_written, pointer_written))
    return pointer, moment


def _verdict(pointer: dict, moment: float) -> dict:
    row = recovery.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )
    return row["fleet_verdict"]


def test_a_just_resumed_run_is_not_stalled_on_its_predecessors_stream(
    tmp_path: Path,
) -> None:
    """The reported defect: resume-1.jsonl absent, stream.jsonl 1000s old.

    The current attempt's own log does not exist yet, so the run is inside the
    launch window the just-written pointer records and reads working. Nothing
    about the superseded attempt's stream may enter the reading.
    """
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_resumed_run(
            tmp_path,
            "r-just-resumed",
            worker_pid=worker_pid,
            predecessor_age_seconds=STALE_SECONDS,
            launch_age_seconds=LAUNCH_SECONDS,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert not Path(pointer["log_path"]).exists()
    assert abs(quiet - LAUNCH_SECONDS) <= 2, quiet
    assert verdict["state"] != "stalled", verdict["detail"]
    assert verdict["state"] == "working", verdict["detail"]


def test_a_resumed_run_with_a_recorded_attempt_start_is_not_stalled(
    tmp_path: Path,
) -> None:
    """The launch clock in the attempt record, with the own log still absent."""
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_resumed_run(
            tmp_path,
            "r-attempt-clock",
            worker_pid=worker_pid,
            predecessor_age_seconds=STALE_SECONDS,
            launch_age_seconds=LAUNCH_SECONDS,
            launch_in_record=True,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert "attempt_started_at" not in pointer
    assert abs(quiet - LAUNCH_SECONDS) <= 2, quiet
    assert verdict["state"] == "working", verdict["detail"]


def test_a_resumed_run_whose_own_log_is_quiet_still_stalls(tmp_path: Path) -> None:
    """The dead control: the attempt's own log is 1000s old, so it stalls.

    The guard narrows which stream ages the run; it must not hide a resume
    whose own attempt has genuinely gone silent.
    """
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_resumed_run(
            tmp_path,
            "r-own-log-quiet",
            worker_pid=worker_pid,
            predecessor_age_seconds=STALE_SECONDS,
            own_log_age_seconds=STALE_SECONDS,
            launch_age_seconds=STALE_SECONDS,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert abs(quiet - STALE_SECONDS) <= 2, quiet
    assert verdict["state"] == "stalled", verdict["detail"]
    assert verdict["detail"].startswith("alive, quiet "), verdict["detail"]


def test_the_attempt_record_clock_beats_an_old_pointer(tmp_path: Path) -> None:
    """No stream of its own, an old pointer, but a live attempt record."""
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_resumed_run(
            tmp_path,
            "r-record-beats-pointer",
            worker_pid=worker_pid,
            predecessor_age_seconds=STALE_SECONDS,
            launch_age_seconds=LAUNCH_SECONDS,
            pointer_age_seconds=STALE_SECONDS,
            launch_in_record=True,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert abs(quiet - LAUNCH_SECONDS) <= 2, quiet
    assert verdict["state"] == "working", verdict["detail"]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    os.environ[MUTATION_ENV] = "1"
    raise SystemExit(pytest.main(["-p", "no:cacheprovider", "-q", str(Path(__file__))]))
