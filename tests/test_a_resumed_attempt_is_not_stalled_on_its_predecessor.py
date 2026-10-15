"""A resumed attempt's stall clock starts at its own attempt, not its predecessor's.

A resume advances a run by publishing the new attempt's identity before it
rewrites the pointer, so for a moment the pointer still dates the attempt that
already ended while ``attempt.json`` already names the new one. A producer on
another host cannot see the old process go, so liveness reads unknown and the
run reaches the stall check rather than the death branch: measured 2026-10-15,
a repair round started attempt 3 at 09:37:16Z and the producer drew ``stalled``
one second later, "liveness unknown, quiet 17m", where the 17 minutes were the
previous attempt's. Four seconds later the new attempt's stream moved and the
run read ``working``.

Quiet time is already capped by the attempt clock, but ``_attempt_started_seconds``
read the pointer's ``attempt_started_at`` first and fell back to ``attempt.json``
only when the pointer carried no such field — so in the resume window both terms
read the predecessor's 17 minutes. It now reads both and returns the later of the
two that parse, so a resumed attempt is never stalled on the quiet its
predecessor left and stalls only once it has itself been quiet for the window.

The declared negative control restores the pointer-first read, and the first
case must then read ``stalled``.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, recovery_liveness, recovery_watch, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "with the pointer-first read of attempt_started_at restored, the first "
    "case must read stalled"
)

MUTATION_ENV = "RECKON_RESUMED_ATTEMPT_START_NEGATIVE_CONTROL"

# The window a quiet run is judged against, in seconds.
STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)

# The predecessor's quiet: the pointer and the stream both date it 17 minutes
# ago, past the 15-minute window, which is what the reported row read.
STALE_SECONDS = 17 * 60

# A current attempt started a second ago.
FRESH_SECONDS = 1

RUN_ID = "r-resumed-attempt-start"
CURRENT_ATTEMPT = 3
PRIOR_ATTEMPT = 2

# A launching host that is provably not this one, so liveness reads unknown —
# the cross-host producer of the reported row, which cannot see the old pid go.
FOREIGN_HOST = "some-other-host"


def _pointer_first_reading(record: Mapping[str, Any]) -> float | None:
    """The reading the declared mutation restores: the pointer's start first."""
    raw = record.get("attempt_started_at")
    if not raw:
        return recovery_liveness._attempt_started_seconds(record)
    started = recovery.parse_utc(str(raw))
    return started.timestamp() if started is not None else None


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The crew home is a temporary directory; the operator's is never read."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if os.environ.get(MUTATION_ENV) == "1":
        # The declared mutation: read the pointer's start first, as the defect
        # did, so a just-resumed run dates from its predecessor again.
        monkeypatch.setattr(
            recovery_liveness, "_attempt_started_seconds", _pointer_first_reading
        )
        monkeypatch.setattr(
            recovery_watch, "_attempt_started_seconds", _pointer_first_reading
        )


def _at(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _write_attempt_record(
    run_id: str, *, attempt: int, started_seconds: float | None
) -> Path:
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / recovery_liveness.ATTEMPT_RECORD_NAME
    body: dict[str, Any] = {"run_id": run_id, "attempt": attempt}
    if started_seconds is not None:
        body["attempt_started_at"] = _at(started_seconds)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _stub_resumed_run(
    tmp_path: Path,
    run_id: str,
    *,
    pointer_started_seconds: float | None,
    attempt_started_seconds: float | None,
    stream_age_seconds: float,
    write_attempt_record: bool = True,
) -> tuple[dict, float]:
    """A cross-host run whose pointer and attempt record may date differently.

    ``pointer_started_seconds`` is the pointer's ``attempt_started_at`` (None to
    omit the field); ``attempt_started_seconds`` is the run directory's attempt
    record (ignored when ``write_attempt_record`` is false). The newest stream is
    written ``stream_age_seconds`` ago. Liveness reads unknown: the pointer
    names a foreign launching host, so no pid is probed here.
    """
    moment = time.time()
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid(),
        phase="working",
        manifest_body="node: a-resumed-attempt\nstatus: in-progress\n",
        stream_records=['{"type":"turn.started"}'],
    )
    # A producer on another host cannot see the written pid, so the run's
    # liveness reads unknown and the stall check is reached.
    pointer["launcher_host"] = FOREIGN_HOST
    pointer["process_alive"] = None
    pointer["attempt"] = CURRENT_ATTEMPT if pointer_started_seconds else PRIOR_ATTEMPT
    if pointer_started_seconds is not None:
        pointer["attempt_started_at"] = _at(pointer_started_seconds)

    # The stream this run's newest log holds, last written a while ago.
    stream = Path(str(pointer["log_path"]))
    stream_written = moment - stream_age_seconds
    os.utime(stream, (stream_written, stream_written))

    if write_attempt_record:
        _write_attempt_record(
            run_id, attempt=CURRENT_ATTEMPT, started_seconds=(attempt_started_seconds)
        )
    return pointer, moment


def _snapshot(pointer: dict, moment: float) -> dict:
    return recovery._watch_snapshot(pointer, moment=moment, stall_seconds=STALL_SECONDS)


def test_a_resumed_attempt_is_not_stalled_on_its_predecessors_clock(
    tmp_path: Path,
) -> None:
    """The first case: the pointer's 17 minutes are the previous attempt's.

    Liveness reads unknown, the pointer still carries the predecessor's start
    from 17 minutes ago, and the newest stream was last written 17 minutes ago —
    but the attempt record names an attempt started a second ago, so the run is
    alive on its own attempt and must not read stalled.
    """
    started = time.time()
    pointer, moment = _stub_resumed_run(
        tmp_path,
        RUN_ID,
        pointer_started_seconds=started - STALE_SECONDS,
        attempt_started_seconds=started - FRESH_SECONDS,
        stream_age_seconds=STALE_SECONDS,
    )

    snapshot = _snapshot(pointer, moment)

    # The declared mutation's arm reads here first, so a red log names the
    # reading the pointer-first clock composes from the predecessor.
    assert snapshot["state"] != "stalled", (
        "the attempt record names an attempt started a second ago, so the run "
        f"must not be stalled on the predecessor's quiet; it read {snapshot!r}"
    )
    assert snapshot["state"] == "working", snapshot["state"]
    assert snapshot["recovery_classification"] != "stalled", snapshot
    # The quiet the reading was taken from is the attempt's own second, not the
    # stream's 17 minutes: min(1020, 1) == 1.
    assert snapshot["quiet_seconds"] <= FRESH_SECONDS + 2, snapshot["quiet_seconds"]


def test_an_attempt_quiet_for_the_window_still_stalls(tmp_path: Path) -> None:
    """The dead control: both starts are old and the stream is quiet.

    The guard narrows which clock dates the run; it must not hide a resume whose
    own attempt has genuinely gone silent. Both the pointer and the attempt
    record are older than the window, so the later of them is too.
    """
    started = time.time()
    pointer, moment = _stub_resumed_run(
        tmp_path,
        RUN_ID + "-both-old",
        pointer_started_seconds=started - STALE_SECONDS - 300,
        attempt_started_seconds=started - STALE_SECONDS,
        stream_age_seconds=STALE_SECONDS,
    )

    snapshot = _snapshot(pointer, moment)

    assert snapshot["state"] == "stalled", snapshot["state"]
    assert snapshot["recovery_classification"] == "stalled", snapshot


def test_a_run_with_no_attempt_record_stalls_as_today(tmp_path: Path) -> None:
    """No attempt record, a stream quiet past the window: stalled as today."""
    started = time.time()
    pointer, moment = _stub_resumed_run(
        tmp_path,
        RUN_ID + "-no-record",
        pointer_started_seconds=started - STALE_SECONDS,
        attempt_started_seconds=None,
        stream_age_seconds=STALE_SECONDS,
        write_attempt_record=False,
    )

    snapshot = _snapshot(pointer, moment)

    assert snapshot["state"] == "stalled", snapshot["state"]


def test_a_pointer_only_start_is_measured_from_that_one(tmp_path: Path) -> None:
    """A run carrying only the pointer's start is measured from it, as today."""
    started = time.time()
    pointer, moment = _stub_resumed_run(
        tmp_path,
        RUN_ID + "-pointer-only",
        pointer_started_seconds=started - STALE_SECONDS,
        attempt_started_seconds=None,
        stream_age_seconds=STALE_SECONDS,
        write_attempt_record=False,
    )

    assert recovery_liveness._attempt_started_seconds(pointer) == pytest.approx(
        started - STALE_SECONDS, abs=2
    )
    assert _snapshot(pointer, moment)["state"] == "stalled"


def test_an_attempt_only_start_reads_fresh_and_is_not_stalled(tmp_path: Path) -> None:
    """A run carrying only the attempt record's start is measured from it.

    The reported row's own shape: the pointer predates the field, so the
    attempt record is the only clock and the run is young on it.
    """
    started = time.time()
    pointer = liveness._pointer(
        tmp_path,
        RUN_ID + "-record-only",
        pid=liveness._absent_pid(),
        phase="working",
        manifest_body="node: a-resumed-attempt\nstatus: in-progress\n",
        stream_records=['{"type":"turn.started"}'],
    )
    pointer["launcher_host"] = FOREIGN_HOST
    pointer["process_alive"] = None
    stream = Path(str(pointer["log_path"]))
    written = started - STALE_SECONDS
    os.utime(stream, (written, written))
    _write_attempt_record(
        RUN_ID + "-record-only",
        attempt=CURRENT_ATTEMPT,
        started_seconds=started - FRESH_SECONDS,
    )

    snapshot = _snapshot(pointer, started)

    assert _attempt_start(pointer) == pytest.approx(started - FRESH_SECONDS, abs=2)
    assert snapshot["state"] == "working", snapshot["state"]


def _attempt_start(pointer: Mapping[str, Any]) -> float | None:
    return recovery_liveness._attempt_started_seconds(pointer)


def test_attempt_start_is_the_later_of_the_two_recorded_starts(
    tmp_path: Path,
) -> None:
    """``_attempt_started_seconds`` returns the later parseable start.

    Both records are read; the current attempt is the one that began later, and
    a record whose stamp will not parse names no start and cannot date the run.
    """
    older = time.time() - STALE_SECONDS
    newer = time.time() - FRESH_SECONDS
    pointer = {
        "run_id": RUN_ID + "-both",
        "attempt_started_at": _at(older),
    }
    runs.run_dir(pointer["run_id"]).mkdir(parents=True, exist_ok=True)
    _write_attempt_record(
        pointer["run_id"], attempt=CURRENT_ATTEMPT, started_seconds=newer
    )

    assert _attempt_start(pointer) == pytest.approx(newer, abs=2)

    # A pointer older than the attempt record's is still the earlier one.
    pointer["attempt_started_at"] = _at(newer)
    _write_attempt_record(
        pointer["run_id"], attempt=CURRENT_ATTEMPT, started_seconds=older
    )
    assert _attempt_start(pointer) == pytest.approx(newer, abs=2)

    # Neither parses: no attempt clock, so the fresh-dispatch window applies.
    pointer["attempt_started_at"] = "not-a-timestamp"
    _write_attempt_record(
        pointer["run_id"], attempt=CURRENT_ATTEMPT, started_seconds=None
    )
    (
        runs.run_dir(pointer["run_id"]) / recovery_liveness.ATTEMPT_RECORD_NAME
    ).write_text(
        json.dumps(
            {
                "run_id": pointer["run_id"],
                "attempt": CURRENT_ATTEMPT,
                "attempt_started_at": "also-not-a-timestamp",
            }
        ),
        encoding="utf-8",
    )
    assert _attempt_start(pointer) is None
