"""A resumed attempt is read through its own records, not its predecessor's.

A resume reuses its run directory, so the manifest beside the pointer may be
the judgement an earlier turn left. The run's own records decide: an attempt
that has recorded an end of its own is read from that end, and an attempt whose
own word is the last thing written is read from that word once nothing has
written its stream for longer than the stall window.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.ticker import PANE_STATES

# The words the pane draws halted, taken from the pane's own vocabulary.
HALTED_STATES = PANE_STATES["needs you"]

# A pid nothing runs under, for the predecessor's own process.
DEAD_PID = 99999999

# A launching host that is provably not this one, so no pid is probed here.
FOREIGN_HOST = "some-other-host"

# The repaired manifest and the predecessor's stream are old — quiet past the
# stall window — so the run is not already moving on the stream alone.
STALE_SECONDS = 1200.0


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The crew home is a temporary directory; the operator's is never read."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _write_run(
    *,
    run_id: str,
    attempt: int,
    manifest_status: str = "blocked",
    manifest_age_seconds: float = STALE_SECONDS,
    status_repair: bool = False,
    exit_attempt: int | None = None,
    exit_age_seconds: float = 300.0,
    stream_age_seconds: float = STALE_SECONDS,
) -> dict:
    """Write one resumed run's own records and return its pointer.

    ``attempt`` names the attempt whose exit record is written: the pointer
    still carries the predecessor's pid and phase because nothing has rewritten
    them yet. The launcher is another host, so the pid is unprobeable here,
    which is the reading a producer on a peer host makes.
    """
    now = time.time()
    directory = directory_of(run_id)
    directory.mkdir(parents=True, exist_ok=True)

    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: {manifest_status}\ncommits:\nblockers:\n",
        encoding="utf-8",
    )
    written = now - manifest_age_seconds
    os.utime(manifest, (written, written))

    if status_repair:
        (directory / "status-repair.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "manifest": str(manifest),
                    "status": manifest_status,
                    "previous_status": "complete",
                    "reason": "one folded finding is answered in a short round",
                    "repaired_at": _at(now - manifest_age_seconds),
                }
            ),
            encoding="utf-8",
        )

    if exit_attempt is not None:
        exit_path = directory / recovery.EXIT_RECORD_NAME
        exit_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "attempt": exit_attempt,
                    "worker_pid": None,
                    "ended_during": "working",
                    "exit_code": 0,
                    "launched_at": _at(now - exit_age_seconds),
                    "exited_at": _at(now - exit_age_seconds + 60),
                    "stream_records_seen": 5,
                }
            ),
            encoding="utf-8",
        )
        stamp = now - exit_age_seconds
        os.utime(exit_path, (stamp, stamp))

    stream = directory / f"resume-{attempt}.jsonl"
    stream.write_text(
        '{"type":"assistant","message":{"content":[]}}\n', encoding="utf-8"
    )
    quiet = now - stream_age_seconds
    os.utime(stream, (quiet, quiet))

    pointer = {
        "run_id": run_id,
        "project": "resumed-record-fixture",
        "session": "coordinator-fixture",
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["seed.txt"],
        },
        "phase": "working",
        "attempt": attempt - 1,
        "attempt_kind": "resume",
        "pid": DEAD_PID,
        "launcher_host": FOREIGN_HOST,
        "process_alive": None,
        "manifest_path": str(manifest),
        "log_path": str(stream),
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def directory_of(run_id: str) -> Path:
    """The run's directory, through the same resolver the reader uses."""
    return runs.run_dir(run_id)


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def test_the_current_attempts_own_end_still_governs() -> None:
    """An attempt that recorded its own end is read from it.

    The exit record names attempt 2, the attempt the repaired manifest also
    describes, so the run reads halted from the word the resume left in place.
    """
    run_id = "run-own-exit"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        status_repair=True,
        exit_attempt=2,
        exit_age_seconds=300.0,
    )

    snapshot = _snapshot(pointer)

    assert snapshot["state"] in HALTED_STATES, (
        "the attempt recorded its own end, so the repaired word governs; "
        f"it read {snapshot['state']!r} with {snapshot!r}"
    )


def test_arm_v_the_same_word_holds_once_nothing_writes_it() -> None:
    """The attempt's own word governs once its stream has gone quiet.

    Nothing has written the run's stream for longer than the stall window, so
    the word is not a checkpoint in progress: the run has really stopped and the
    attempt's own verdict governs.
    """
    run_id = "run-own-word-quiet-stream"
    pointer = _write_run(
        run_id=run_id,
        attempt=2,
        manifest_age_seconds=120.0,
        stream_age_seconds=STALE_SECONDS + 600.0,
    )

    snapshot = _snapshot(pointer)

    assert snapshot["state"] in HALTED_STATES, (
        "arm v quiet: nothing has written the run's stream inside the stall "
        f"window, so the attempt's own word governs; it read "
        f"{snapshot['state']!r}"
    )
    assert snapshot["state"] == "blocked", snapshot
