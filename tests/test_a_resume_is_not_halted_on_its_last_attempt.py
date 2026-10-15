"""A resume is read from its own attempt, not from the process it replaced.

A resume advances a run in three steps: it publishes the new attempt's identity
and retires the previous attempt's canonical worker and exit records, then its
supervisor spawns the new worker, then that worker registers. For the seconds
between the first and the last, the run directory holds an attempt record naming
attempt N and no worker record at all, while the pointer still carries attempt
N-1's pid and phase — nothing has rewritten them yet.

Read from that pointer, the earlier attempt's exited process looks like the
current attempt dead before a manifest, so the producer's death branch read the
newest stream, found the previous attempt's ``result`` tail and drew the run
``ended-without-manifest``; seconds later the new attempt's stream existed
holding only its start record and the same branch drew it ``interrupted``. Both
are drawn halted, and each sent a reader to resume work that was already
resuming.

The reading below reserves death for the process of the attempt the run is
actually on. The case that pins it is a run whose attempt record names attempt 6
started a moment ago, with no worker and no exit record, whose pointer still
outnames attempt 5's exited process, whose manifest carries no verdict and whose
newest stream is attempt 5's ending in ``result``: it reads ``starting`` — the
state a launch with no worker record is already given, and the one the pane
draws as working — rather than ``ended-without-manifest``. The last case is the
same run with no attempt record at all, and reads exactly as it did before: the
death branch still owns a pointer whose attempt it can speak for.

The declared negative control drops the attempt check, and the first case must
then read ``ended-without-manifest`` again.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_liveness, recovery_watch, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "with the death branch applied without the attempt check, the first case "
    "must read ended-without-manifest"
)

NEGATIVE_CONTROL = os.environ.get(
    "RECKON_ATTEMPT_DEFERRAL_NEGATIVE_CONTROL", ""
).strip()

STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)
ATTEMPT = 6
PRIOR_ATTEMPT = 5

RUN_ID = "r-resumed-attempt"
# The manifest a resume leaves behind: the delivered verdict's status word is
# replaced with a non-terminal one and nothing else is touched, so the file
# reads a run in progress rather than a delivery.
CLEARED_MANIFEST_BODY = (
    "node: a-resumed-attempt\nstatus: in-progress\ncommits:\nblockers:\n"
)
PRIOR_STREAM_TAIL = (
    '{"type":"turn.started"}',
    '{"type":"assistant","message":{"content":[]}}',
    '{"type":"result","subtype":"success"}',
)
SYSTEM_ONLY_TAIL = ('{"type":"system","subtype":"init"}',)


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The crew home is a temporary directory; the operator's is never read."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if NEGATIVE_CONTROL:
        # The declared mutation: the attempt check is dropped, so the death
        # branch applies to a pointer whose attempt it cannot speak for.
        monkeypatch.setattr(
            recovery_watch, "_attempt_awaits_its_worker", lambda record: False
        )


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _write_attempt_record(run_id: str, *, attempt: int, started_seconds: float) -> None:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery_liveness.ATTEMPT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "attempt_kind": "resume",
                "attempt_started_at": _at(started_seconds),
            }
        ),
        encoding="utf-8",
    )


def _write_worker_record(run_id: str, pid: int, *, attempt: int) -> None:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.WORKER_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "pid": pid,
                "pid_start_time": recovery._process_start_time(pid),
                "backend": "claude",
            }
        ),
        encoding="utf-8",
    )


def _write_exit_record(run_id: str, *, attempt: int, stream_records_seen: int) -> None:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "worker_pid": None,
                "ended_during": "launch",
                "launched_at": _at(time.time() - 120),
                "exited_at": _at(time.time() - 60),
                "stream_records_seen": stream_records_seen,
            }
        ),
        encoding="utf-8",
    )


def _write_attempt_stream(run_id: str, records: tuple[str, ...]) -> Path:
    """The stream a resumed attempt writes, named as the run directory's own."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / f"resume-{run_id}.jsonl"
    stream.write_text("".join(records), encoding="utf-8")
    return stream


def _resumed_pointer(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    attempt_started_seconds: float | None = None,
) -> dict:
    """A run between a resume's attempt write and its new worker's registration.

    The pointer is attempt 5's: it still names that attempt's exited process and
    its phase, which is all a resume leaves behind before its supervisor starts.
    """
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid(),
        phase="working",
        manifest_body=CLEARED_MANIFEST_BODY,
        stream_records=list(PRIOR_STREAM_TAIL),
    )
    pointer["attempt"] = ATTEMPT
    _write_attempt_record(
        run_id,
        attempt=ATTEMPT,
        started_seconds=(
            time.time() if attempt_started_seconds is None else attempt_started_seconds
        ),
    )
    return pointer


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=STALL_SECONDS
    )


def test_a_resumed_attempt_whose_worker_has_not_registered_reads_starting(
    tmp_path: Path,
) -> None:
    """The first case: attempt 6's own launch is what the pointer is read for.

    The pointer names attempt 5's exited process and its stream still ends in
    ``result`` — the two facts the death branch reads today as a turn that
    ended. Attempt 6 has written no worker record, so neither fact speaks for
    it, and the run reads as the launch it is.
    """
    pointer = _resumed_pointer(tmp_path)

    snapshot = _snapshot(pointer)

    # The declared mutation's arm reads here first, so a red log names the
    # reading the death branch composes for a pointer it does not own.
    assert snapshot["recovery_classification"] != "ended-without-manifest", (
        "the pointer's dead process belongs to the attempt before this one, so "
        f"the run must not read ended-without-manifest; it read {snapshot!r}"
    )
    assert snapshot["state"] == "starting", snapshot["state"]
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES, snapshot["state"]
    assert snapshot["process_alive"] is False, snapshot["process_alive"]
    # The earlier attempt's tail is still the newest stream, so the reading is
    # about which process owns it rather than about the stream being absent.
    assert recovery_liveness._record_newest_stream(pointer)[0] == Path(
        str(pointer["log_path"])
    )


def test_a_resumed_attempt_stream_with_only_a_start_record_still_reads_starting(
    tmp_path: Path,
) -> None:
    """The second case: attempt 6's own stream is young, not a death mid-turn."""
    pointer = _resumed_pointer(tmp_path)
    _write_attempt_stream(RUN_ID, SYSTEM_ONLY_TAIL)

    snapshot = _snapshot(pointer)

    assert snapshot["state"] == "starting", snapshot["state"]
    assert snapshot["recovery_classification"] != recovery.INTERRUPTED_RUN_PHASE, (
        snapshot
    )


def test_a_registered_worker_of_the_current_attempt_reads_working(
    tmp_path: Path,
) -> None:
    """The third case: once attempt 6's worker record exists it answers."""
    pointer = _resumed_pointer(tmp_path)

    with liveness._live_child() as worker_pid:
        _write_worker_record(RUN_ID, worker_pid, attempt=ATTEMPT)
        snapshot = _snapshot(pointer)

    assert snapshot["process_alive"] is True, snapshot["process_alive"]
    assert snapshot["state"] == "working", snapshot["state"]
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES, snapshot["state"]


def test_the_death_of_the_current_attempt_reads_as_today(tmp_path: Path) -> None:
    """The fourth case: attempt 6's own worker died, and that is a death.

    Its worker record names attempt 6 and its process is gone, so the death the
    pointer reports is this attempt's. The stream still ends in ``result``, so
    the reading is the resumable turn the branch already gives.
    """
    pointer = _resumed_pointer(tmp_path)
    _write_worker_record(RUN_ID, liveness._absent_pid(), attempt=ATTEMPT)

    snapshot = _snapshot(pointer)

    assert snapshot["process_alive"] is False, snapshot["process_alive"]
    assert snapshot["recovery_classification"] == "ended-without-manifest", snapshot
    assert snapshot["recovery"] == "resume", snapshot["recovery"]


def test_a_resumed_attempt_past_the_bound_reads_as_a_stranded_launch(
    tmp_path: Path,
) -> None:
    """The fifth case: the deferral carries the bound a fresh launch is given.

    Past STRANDED_LAUNCH_BOUND_SECONDS with no worker record written, nothing is
    coming, so the pointer holds a claim its attempt never backed. The reading
    is the stranded launch the producer already names, reached here through the
    deferral's own clock — ``_stranded_launch`` cannot see it, because the
    pointer names the earlier attempt's pid.
    """
    started = time.time() - recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS - 60
    pointer = _resumed_pointer(tmp_path, attempt_started_seconds=started)
    # The positive control for the bound: the same run inside it is not
    # stranded, so the reading below is the clock's and not the fixture's.
    inside = _snapshot(
        _resumed_pointer(
            tmp_path,
            run_id=RUN_ID + "-inside",
            attempt_started_seconds=time.time() - 60,
        )
    )
    assert recovery._stranded_launch(pointer, now_seconds=time.time()) is False, (
        "the producer's pre-spawn stranded reading must not see a pointer that "
        "names a pid; this is the gap the deferral's own bound covers"
    )

    snapshot = _snapshot(pointer)

    assert inside["state"] == "starting", inside["state"]
    assert snapshot["state"] == "launch-failed", snapshot["state"]
    assert snapshot["recovery_classification"] == "launch-failed", snapshot
    assert snapshot["recovery"] == "resume", snapshot["recovery"]
    assert "stranded launch" in str(snapshot["detail"]), snapshot["detail"]


def test_a_recorded_launch_failure_reads_as_that_failure_at_once(
    tmp_path: Path,
) -> None:
    """The sixth case: a recorded end outranks the deferral's own clock.

    A launch that failed before it spawned a worker wrote an exit record and no
    manifest, so the run reads the failure rather than being held at starting.
    The companion arm adds the cleared manifest a resumed run does carry: the
    deferral must yield there too, because an end the attempt itself recorded is
    evidence where a pointer it does not own is not.
    """
    failed_id = RUN_ID + "-failed"
    pointer = liveness._pointer(
        tmp_path,
        failed_id,
        pid=liveness._absent_pid(),
        phase="launch-failed",
    )
    _write_attempt_record(failed_id, attempt=ATTEMPT, started_seconds=time.time())
    _write_exit_record(failed_id, attempt=ATTEMPT, stream_records_seen=0)

    snapshot = _snapshot(pointer)

    assert snapshot["classification"] == "launch-failed", snapshot["classification"]
    assert snapshot["state"] != "starting", snapshot["state"]
    assert "no model was reached" in str(snapshot["detail"]), snapshot["detail"]

    cleared = _resumed_pointer(tmp_path, run_id=RUN_ID + "-cleared")
    _write_exit_record(RUN_ID + "-cleared", attempt=ATTEMPT, stream_records_seen=0)
    held = _snapshot(cleared)
    assert held["state"] != "starting", held["state"]


def test_a_run_with_no_attempt_record_reads_as_today(tmp_path: Path) -> None:
    """The seventh case, and the module's control on the death branch.

    This run is the first case's fixture with one difference: nothing records an
    attempt, so the pointer's dead process is the only process there is. It
    reads ``ended-without-manifest`` — the reading the death branch composes,
    and the one the first case must not get.
    """
    pointer = liveness._pointer(
        tmp_path,
        RUN_ID,
        pid=liveness._absent_pid(),
        phase="working",
        manifest_body=CLEARED_MANIFEST_BODY,
        stream_records=list(PRIOR_STREAM_TAIL),
    )

    snapshot = _snapshot(pointer)

    assert snapshot["recovery_classification"] == "ended-without-manifest", snapshot
    assert snapshot["state"] == "blocked", snapshot["state"]
