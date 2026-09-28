"""A turn that ended without a manifest is a row at once, and the row says so.

A print-mode worker whose turn ends has finished, whether or not it delivered.
Its stream's last record is then a ``result`` record and its process exits,
while the manifest it never replaced still reads ``in-progress``. Measured on
one such run, the row reached the pane fifteen minutes late as a stalled row
naming the wrong cause: what happened is that the turn ended, whose remedy is a
resume, not that the worker hung mid-turn, whose cause a reader has to see
before choosing one.

Three cases pin that reading, each separating it from the reading it could be
confused with.

* The run's stream ends in ``result`` beside a manifest reading ``in-progress``
  and a gone process: one watcher snapshot lands it as a single transition
  whose reason says ``turn ended``, with the quiet time well under the stall
  window, so a reading held behind that window would fail here.
* The same run whose stream ends in an ``assistant`` record instead is a death
  mid-turn, and must get the death reading rather than the ended-turn one —
  different ends, different remedies. Both arms are read on one run, so the
  case compares two readings rather than asserting an absence.
* The same run whose manifest word is outside the reader's vocabulary is
  refused as unreadable: a file no supported reader can parse is neither a
  delivery nor a silence, so that reading outranks the ended-turn one and the
  classification, not the reason, is what separates them.

The declared mutation deletes the turn-ended branch. A ``result`` tail then
falls to the death reading, so the first case's snapshot emits no ended-turn
transition and the case fails naming what the stream holds.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon import crew
from reckon.crew import recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "delete the turn-ended branch near reckon/crew/recovery.py:5185-5193 in a "
    "scratch copy; the first case's snapshot must emit no ended-turn transition"
)

STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)
# The words the ended-turn reading composes, and the section's own name for the
# end it reports.
ENDED_TURN_CLAUSE = "turn ended"
# The phrase only the death reading composes: it names the stream's tail.
STREAM_TAIL_CLAUSE = "the stream's last record is"

# The ages, in seconds, the fixture's clocks are set to before it is read. The
# run is placed decisively past the launch window a fresh dispatch gets, and its
# manifest is its newer activity rather than its stream, so the report is read
# as the worker's own last word. The stream is minutes quiet against a
# fifteen-minute stall window, so a reading that waited for the window would
# move on a different snapshot than the one asserted here.
DISPATCH_AGE_SECONDS = 3600
STREAM_AGE_SECONDS = 300
MANIFEST_AGE_SECONDS = 120

IN_PROGRESS_BODY = "node: a-stub-node\nstatus: in-progress\ncommits:\nblockers:\n"
READY_FOR_REVIEW_BODY = (
    "node: a-stub-node\nstatus: ready-for-review\ncommits:\nblockers:\n"
)

ASSISTANT_TAIL = (
    '{"type":"turn.started"}',
    '{"type":"assistant","message":{"content":[]}}',
)
RESULT_TAIL = (*ASSISTANT_TAIL, '{"type":"result","subtype":"success"}')


def _start_worker() -> subprocess.Popen:
    """A real process to kill, so liveness is the process table's answer."""
    return subprocess.Popen(["sleep", "120"])


def _kill(worker: subprocess.Popen) -> None:
    worker.kill()
    worker.wait()


def _set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _dispatch(
    tmp_path: Path,
    run_id: str,
    *,
    project: str,
    pid: int | None,
    manifest_body: str | None,
    stream_records: tuple[str, ...],
) -> dict:
    """One stub run as the pointer is written, aged past the transient windows."""
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=pid,
        phase="working",
        manifest_body=manifest_body,
        stream_records=list(stream_records),
    )
    pointer["project"] = project
    pointer["created_at"] = (
        datetime.now(tz=UTC) - timedelta(seconds=DISPATCH_AGE_SECONDS)
    ).isoformat()
    _write_stream(pointer, stream_records)
    if manifest_body is not None:
        _set_mtime(
            Path(str(pointer["manifest_path"])), time.time() - MANIFEST_AGE_SECONDS
        )
    return pointer


def _write_stream(pointer: dict, stream_records: tuple[str, ...]) -> None:
    """Rewrite the run's stream and age it, so its tail is the run's older news."""
    stream = Path(str(pointer["log_path"]))
    stream.write_text(
        "".join(record + "\n" for record in stream_records), encoding="utf-8"
    )
    _set_mtime(stream, time.time() - STREAM_AGE_SECONDS)


def _write_pointer(run_id: str, pointer: dict) -> None:
    crew._write_json(crew.pointer_path(run_id), pointer)


def _events(stream_path: Path, run_id: str) -> list[dict]:
    """Every watch event the stream holds for one run, in order."""
    return [
        event
        for event in runs.read_stream_events(stream_path)
        if event.get("run_id") == run_id
    ]


def _kinds(events: list[dict]) -> list[tuple]:
    """Event kind with its state move, its reading and the reason it composed.
    The reason travels in the failure message because a red arm is about the
    words the row carried: a reader must see the clause the reader composed
    rather than infer it from a classification."""
    return [
        (
            str(event.get("event")),
            event.get("from_state"),
            event.get("to_state"),
            event.get("recovery_classification"),
            str(event.get("detail") or ""),
        )
        for event in events
    ]


def _ended_turn_rows(events: list[dict]) -> list[dict]:
    return [
        event for event in events if ENDED_TURN_CLAUSE in str(event.get("detail") or "")
    ]


def test_a_turn_that_ended_without_a_manifest_is_a_row_at_once(tmp_path: Path) -> None:
    """The end is the news, and the row names which end this was.

    The run is built alive so the publisher records a baseline of it working;
    the process then ends between two snapshots, which is the only arrangement
    in which a transition can be asked for at all. The manifest still reads
    ``in-progress`` and the stream's last record is ``result``, so the end is a
    concluded turn rather than a death mid-turn.
    """
    run_id = "r-turn-ended"
    project = "proj-turn-ended"
    worker = _start_worker()
    pointer = _dispatch(
        tmp_path,
        run_id,
        project=project,
        pid=worker.pid,
        manifest_body=IN_PROGRESS_BODY,
        stream_records=RESULT_TAIL,
    )
    _write_pointer(run_id, pointer)

    try:
        with runs._project_watch_claim(project, "1h") as (acquired, registration):
            assert acquired is True
            stream_path = Path(str(registration["stream_path"]))
            opened = _events(stream_path, run_id)
            assert [kind[:4] for kind in _kinds(opened)] == [
                ("baseline", None, "working", "running")
            ], _kinds(opened)
            # The positive control for the baseline: a live worker behind a
            # non-terminal manifest reads working, so the row asserted below is
            # the end the next snapshot observes rather than a state the run
            # arrived in.
            assert opened[0]["process_alive"] is True, opened[0]["process_alive"]

            _kill(worker)
            quiet = recovery._run_stream_quiet_seconds(pointer, now_seconds=time.time())
            assert quiet < STALL_SECONDS, quiet

            crew.list_live(project=project)
            events = _events(stream_path, run_id)
    finally:
        if worker.poll() is None:
            _kill(worker)

    ended = _ended_turn_rows(events)
    assert len(ended) == 1, (
        "the turn ended without a manifest, so the snapshot that saw the "
        "process end owes one row whose reason says so; the stream holds "
        f"{_kinds(events)}"
    )
    row = ended[0]
    assert row["event"] == "transition", row["event"]
    assert row["from_state"] == "working", row["from_state"]
    assert row["to_state"] == "blocked", row["to_state"]
    assert row["recovery_classification"] == "ended-without-manifest", row[
        "recovery_classification"
    ]
    assert row["recovery"] == "resume", row["recovery"]
    assert row["process_alive"] is False, row["process_alive"]
    # It is not the stall row: the window has not elapsed and the row does not
    # claim it has.
    assert "stream quiet for" not in str(row["detail"]), row["detail"]
    # Neither is it the death reading, which names the record the death
    # interrupted rather than the end that concluded.
    assert STREAM_TAIL_CLAUSE not in str(row["detail"]), row["detail"]


def test_a_stream_that_ended_mid_turn_is_a_death_not_a_concluded_turn(
    tmp_path: Path,
) -> None:
    """The two ends read differently, and the tail is what separates them.

    One run is read twice and only its stream's last record differs, so each
    reading is the control for the other: the ``result`` arm is what the
    ended-turn reading is, and the ``assistant`` arm must not be it. The death
    remedy is chosen from the record the death interrupted, so the case is an
    assertion about which reading a reader acts on rather than about phrasing.
    """
    run_id = "r-turn-ended-death"
    pointer = _dispatch(
        tmp_path,
        run_id,
        project="proj-turn-ended-death",
        pid=liveness._absent_pid(),
        manifest_body=IN_PROGRESS_BODY,
        stream_records=RESULT_TAIL,
    )

    ended = recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=STALL_SECONDS
    )
    _write_stream(pointer, ASSISTANT_TAIL)
    death = recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=STALL_SECONDS
    )

    # The paired control: the same pointer, read with a result tail, is the
    # ended turn — so the death reading below is the tail's effect and not a
    # reader that reports no end at all.
    assert ended["recovery_classification"] == "ended-without-manifest", ended
    assert ENDED_TURN_CLAUSE in str(ended["detail"]), ended["detail"]

    assert death["recovery_classification"] == recovery.INTERRUPTED_RUN_PHASE, death
    assert death["recovery"] == "redispatch", death["recovery"]
    assert ENDED_TURN_CLAUSE not in str(death["detail"]), death["detail"]
    # A death names the record it interrupted, because the cause is what a
    # reader needs before choosing a recovery.
    assert STREAM_TAIL_CLAUSE in str(death["detail"]), death["detail"]
    assert "assistant" in str(death["detail"]), death["detail"]


def test_a_manifest_word_outside_the_vocabulary_outranks_the_ended_turn(
    tmp_path: Path,
) -> None:
    """A file no reader can parse is refused, whatever its stream went on to do.

    The run is the first case's, dead process and ``result`` tail included, and
    the only difference is the manifest's status word. The word is repaired
    before the run can be judged, so its refusal is the reading the row carries
    rather than the end the stream would otherwise report.
    """
    run_id = "r-turn-ended-unreadable"
    pointer = _dispatch(
        tmp_path,
        run_id,
        project="proj-turn-ended-unreadable",
        pid=liveness._absent_pid(),
        manifest_body=READY_FOR_REVIEW_BODY,
        stream_records=RESULT_TAIL,
    )
    row = recovery.classify_pointer(
        pointer, now_seconds=time.time(), stale_after_seconds=STALL_SECONDS
    )
    snapshot = recovery._watch_snapshot(
        pointer, moment=time.time(), stall_seconds=STALL_SECONDS
    )

    # The positive control: the file was present, read, and refused by name, so
    # the classification asserted below is a refusal of this word rather than a
    # reader that never reached the file.
    assert row["manifest_present"] is True, row["manifest_present"]
    assert "ready-for-review" in str(row["manifest_error"] or ""), row["manifest_error"]
    assert row["process_alive"] is False, row["process_alive"]

    assert snapshot["classification"] == "unreadable", snapshot["classification"]
    assert snapshot["recovery_classification"] == "unreadable", snapshot[
        "recovery_classification"
    ]
    assert snapshot["recovery"] == "repair", snapshot["recovery"]
    assert snapshot["recovery_classification"] != "ended-without-manifest", snapshot[
        "recovery_classification"
    ]
    # The end the stream reports is still there to be read once the file is
    # repaired; it is the row's classification that the refusal replaces.
    assert ENDED_TURN_CLAUSE not in str(snapshot["detail"]), snapshot["detail"]
