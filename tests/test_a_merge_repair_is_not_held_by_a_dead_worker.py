"""A merge repair is not held by a worker that died mid-turn.

The repair reflex reads a reviewed run's newest stream to decide whether a
resumed turn is still under way: a resume stream carrying an assistant record
and no result line is the shape a turn in progress leaves. That stream is only
evidence about a process that is still there. A worker that died mid-turn left
the same stream behind and no process, so reading the stream alone holds the
repair for ever — a recorded pid that the kernel proves gone must not.

Two directions are asserted. A recorded worker pid that is not alive releases
the round: the repair is reached, not held. A live pid still holds it: the
turn really is producing work, and a second worker onto it is what the liveness
deferral exists to prevent. A run that records no process on an unfinished
stream keeps its old reading, because silence about a process is not proof of
its death.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon.crew import recovery_repair_dispatch, runs
from tests.test_the_reflex_resumes_an_unpromoted_run import (  # noqa: F401
    CONFIG,
    FINDING,
    RUN_ID,
    _completed_pointer,
    _store_review,
    _stub_dispatch,
    _stub_resume,
    _sweep,
    isolated_project,
)

pytestmark = pytest.mark.arms_watch_producer

# A pid no process on this host holds. The kernel answers a zero signal to it
# with ESRCH, which is the observation the release rests on.
DEAD_PID = 4_194_303

HELD_REASON = "the reviewed run has a resumed turn in progress"


def _write_unfinished_resume_stream() -> Path:
    """The stream a worker leaves when it dies mid-turn: no result line."""
    directory = Path(runs.run_dir(RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "resume-1.jsonl"
    stream.write_text(
        json.dumps({"type": "assistant", "message": {"content": "working"}}) + "\n",
        encoding="utf-8",
    )
    return stream


def _busy(record: dict) -> str:
    """The busy reason the repair reflex reads for this run, or empty."""
    return recovery_repair_dispatch._reviewed_run_is_busy(record)


def test_a_dead_recorded_pid_does_not_hold_the_round(
    request: pytest.FixtureRequest,
) -> None:
    """A resume stream without a result, and a recorded pid that is gone.

    The stream alone would read as a live turn. The recorded pid proves the
    process that wrote it has exited, so the turn cannot still be producing
    work and the round is free.
    """
    config_home, repo, _head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, repo)
    record["pid"] = DEAD_PID
    runs._write_json(runs.pointer_path(RUN_ID), record)
    _write_unfinished_resume_stream()

    assert _busy(record) == ""


def test_a_live_recorded_pid_still_holds_the_round(
    request: pytest.FixtureRequest,
) -> None:
    """The same unfinished stream, with the worker still running.

    This is the half that must not regress: a second worker dispatched onto a
    live turn is what the deferral prevents. The reading that answers is the
    live worker's own, so the hold is asserted rather than the sentence naming
    it.
    """
    config_home, repo, _head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, repo)
    record["pid"] = os.getpid()
    runs._write_json(runs.pointer_path(RUN_ID), record)
    _write_unfinished_resume_stream()

    assert _busy(record) != "", "a live worker's turn was released"


def test_a_run_recording_no_process_on_an_unfinished_stream_still_holds(
    request: pytest.FixtureRequest,
) -> None:
    """No recorded process is not a stopped worker: silence is not death.

    A pointer written before the worker registered names no pid, so the liveness
    read answers unknown. The stream is then the only evidence there is, and it
    keeps its old reading.
    """
    config_home, repo, _head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, repo)
    record.pop("pid", None)
    runs._write_json(runs.pointer_path(RUN_ID), record)
    _write_unfinished_resume_stream()

    assert _busy(record) == HELD_REASON


def test_a_dead_worker_does_not_hold_the_repair_from_dispatch(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full reflex: the dead turn's round is repaired, not held.

    The first sweep resumes the reviewed run, whose stub leaves the unfinished
    stream behind. The worker recorded on the pointer is gone. The second sweep
    must reach the retry and spend a second resume rather than reading the dead
    turn's stream as work in progress.
    """
    config_home, repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, repo)
    record["pid"] = DEAD_PID
    runs._write_json(runs.pointer_path(RUN_ID), record)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch, finished=False)
    dispatched = _stub_dispatch(monkeypatch)

    _sweep()
    released = _busy(runs.read_pointer(RUN_ID))
    _sweep()

    assert released == "", "the dead worker's stream still held the round"
    assert len(resumed) == 2, "the dead worker's round was not repaired"
    assert dispatched == []
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["attempt"] == 2
