"""A run's quiet time is measured from its current attempt, not its old stream.

A run resumed or redispatched writes a new attempt while its superseded
attempt's stream stays on disk. Reading quiet time from the stream alone then
reports silence that belongs to the attempt that already ended: measured
2026-09-25, a run went ``stalled`` with ``stream quiet for 1919s`` 27 seconds
after ``crew resume`` started a new attempt whose own log was a second old.

Each attempt now records ``attempt_started_at``. The stall clock is the earlier
of the stream's silence and the age of the attempt now running, so a fresh
attempt cannot inherit its predecessor's silence. The declared negative control
restores the old reading — quiet time from the stream alone — and the freshly
resumed case must then fail, classifying stalled.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "quiet time ignores the attempt start and reads the stream alone as today; "
    "the freshly resumed case must fail classifying stalled"
)

MUTATION_ENV = "RECKON_STALL_CLOCK_NEGATIVE_CONTROL"

# The window the stub runs are judged against, in seconds.
STALL_SECONDS = 900


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if os.environ.get(MUTATION_ENV) == "1":
        # The declared mutation: the attempt start is dropped, so quiet time
        # reads the stream alone exactly as it did before this change.
        monkeypatch.setattr(recovery, "_attempt_started_seconds", lambda record: None)


def _stub_pointer(
    tmp_path: Path,
    run_id: str,
    *,
    worker_pid: int,
    stream_age_seconds: float,
    attempt_age_seconds: float | None = None,
    attempt_in_record: bool = False,
) -> tuple[dict, float]:
    """A live run whose stream is old, optionally with a newer attempt start.

    ``attempt_age_seconds`` on the pointer is the common case a supervisor
    writes. ``attempt_in_record`` writes the same moment only into the run
    directory's current attempt record, exercising the fallback a pointer that
    predates the field relies on.
    """
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid(),
        phase="working",
        worker_pid=worker_pid,
        write_stream=False,
    )
    moment = time.time()
    stream = Path(pointer["log_path"])
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    written = moment - stream_age_seconds
    os.utime(stream, (written, written))
    if attempt_age_seconds is not None:
        started = datetime.now(tz=UTC) - timedelta(seconds=attempt_age_seconds)
        if attempt_in_record:
            directory = runs.run_dir(run_id)
            directory.mkdir(parents=True, exist_ok=True)
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
        else:
            pointer["attempt_started_at"] = started.isoformat()
    return pointer, moment


def _verdict(pointer: dict, moment: float) -> dict:
    row = recovery.classify_pointer(
        pointer, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )
    return row["fleet_verdict"]


def test_a_fresh_attempt_beside_an_old_stream_is_working_not_stalled(
    tmp_path: Path,
) -> None:
    """The reported defect: a resume 30 seconds old with a 1919s-old stream.

    The stream's last record is far past the stall window, but the attempt now
    running began 30 seconds ago, so the run is working.
    """
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_pointer(
            tmp_path,
            "r-fresh-attempt",
            worker_pid=worker_pid,
            stream_age_seconds=1919,
            attempt_age_seconds=30,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert abs(quiet - 30) <= 1, quiet
    assert verdict["state"] == "working", verdict["detail"]
    assert verdict["state"] != "stalled"


def test_a_stale_attempt_with_no_newer_record_still_stalls(tmp_path: Path) -> None:
    """The dead control: silence past the window, under an attempt just as old.

    With the attempt start at or before the last stream record the floor adds
    nothing, so a genuinely quiet run still reads stalled at its measured age.
    """
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_pointer(
            tmp_path,
            "r-stale-attempt",
            worker_pid=worker_pid,
            stream_age_seconds=1919,
            attempt_age_seconds=1919,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert abs(quiet - 1919) <= 1, quiet
    assert verdict["state"] == "stalled", verdict["detail"]
    assert verdict["detail"].startswith("stream quiet for ")


def test_a_pointer_with_no_attempt_start_behaves_as_today(tmp_path: Path) -> None:
    """No attempt clock anywhere: quiet time reads the stream alone."""
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_pointer(
            tmp_path,
            "r-no-attempt-clock",
            worker_pid=worker_pid,
            stream_age_seconds=1919,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert abs(quiet - 1919) <= 1, quiet
    assert verdict["state"] == "stalled", verdict["detail"]


def test_the_current_attempt_record_supplies_the_start(tmp_path: Path) -> None:
    """A pointer that predates the field still finds the moment beside it."""
    with liveness._live_child() as worker_pid:
        pointer, moment = _stub_pointer(
            tmp_path,
            "r-attempt-record",
            worker_pid=worker_pid,
            stream_age_seconds=1919,
            attempt_age_seconds=30,
            attempt_in_record=True,
        )
        quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=moment)
        verdict = _verdict(pointer, moment)

    assert "attempt_started_at" not in pointer
    assert abs(quiet - 30) <= 1, quiet
    assert verdict["state"] == "working", verdict["detail"]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    os.environ[MUTATION_ENV] = "1"
    raise SystemExit(
        pytest.main(
            [
                "-p",
                "no:cacheprovider",
                "-q",
                str(Path(__file__)),
                "-k",
                "fresh_attempt",
            ]
        )
    )
