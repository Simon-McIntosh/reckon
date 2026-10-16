"""A resume's first seconds are read from its own attempt, not its predecessor.

A resume advances a run in three steps: it publishes the new attempt's identity
in attempt.json and retires the previous attempt's canonical worker and exit
records, then its supervisor spawns the new worker, then that worker registers.
For the seconds between the first and the last, the run directory holds an
attempt record naming attempt N, no worker record at all, and the previous
attempt's exit record still under its canonical name, while the pointer still
carries attempt N-1's pid and phase because nothing has rewritten them yet.

Read from that evidence, the predecessor's recorded end is drawn as this
attempt's. The classifier's ``exited_unfinished`` arm asks four questions — is
the pointer's process gone, is there an exit record, does the manifest still
carry a non-terminal word, and does the worktree hold commits past its base —
and every one of them is answered by the attempt that already ran: its dead
pid, its exit record, the status word a resume's manifest reset left behind, and
its committed work. So a run that is only starting is drawn ``exited-unfinished``,
which the pane renders halted, and a reader is sent to repair work that is
already resuming.

The reading below defers to the attempt's own clock instead, exactly as the
producer's death branch already does for the same window: an attempt that has
recorded no process and no end of its own is read as starting, and the
predecessor's stream does not decide its liveness. Past
STRANDED_LAUNCH_BOUND_SECONDS the deferral expires through the stranded-launch
rule the death branch already carries, so a resume that never launches is still
reported. A manifest that carries a verdict word of its own outranks the
deferral, so a delivered run is read as delivered exactly as before.

The declared negative control restores the arm to its base behaviour, and the
first case must then read halted again.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import (
    obligations,
    recovery,
    recovery_classification,
    recovery_liveness,
    recovery_watch,
    runs,
)
from reckon.crew.ticker import PANE_STATES
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "with the branch the node named restored to its base behaviour, the first "
    "case reads halted again"
)

NEGATIVE_CONTROL = os.environ.get("RECKON_RESUME_HALTED_NEGATIVE_CONTROL", "").strip()

STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)
ATTEMPT = 7
PREVIOUS = 6
RUN_ID = "r-resume-halted"

# The words the pane draws halted. Taken from the pane's own vocabulary so the
# case states its claim in the reader's terms rather than in a second list.
HALTED_STATES = PANE_STATES["halted"]

# What a resume leaves when it advances a run: the delivered verdict's status
# word replaced with a non-terminal one, the delivered text kept beside it.
DELIVERED_BODY = "node: a-resume-halted\nstatus: complete\ncommits: []\n"
RESET_BODY = "node: a-resume-halted\nstatus: in-progress\ncommits:\nblockers:\n"

# The previous attempt's stream, which ends as a finished worker's does: a turn
# that ran to its own result record.
PRIOR_STREAM_TAIL = (
    '{"type":"turn.started"}',
    '{"type":"assistant","message":{"content":[]}}',
    '{"type":"result","subtype":"success"}',
)


@pytest.fixture(autouse=True)
def _isolated_crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The crew home is a temporary directory; the operator's is never read."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    if NEGATIVE_CONTROL:
        # The declared mutation: the arm reads from the pointer's evidence
        # again, so a predecessor's recorded end is drawn as this attempt's.
        monkeypatch.setattr(
            recovery_classification,
            "_attempt_awaits_its_worker",
            lambda record: False,
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


def _write_previous_exit_record(run_id: str, *, attempt: int) -> None:
    """The exit record a resume has not yet retired from its canonical name.

    It is the previous attempt's, and it is what makes the classifier read this
    attempt's pointer as a worker that exited with its work committed.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / recovery.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "worker_pid": None,
                "ended_during": "working",
                "exit_code": 0,
                "launched_at": _at(time.time() - 300),
                "exited_at": _at(time.time() - 240),
                "stream_records_seen": 3,
            }
        ),
        encoding="utf-8",
    )


def _write_previous_stream(run_id: str) -> Path:
    """The previous attempt's stream, under the name the run directory gives it."""
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text("\n".join(PRIOR_STREAM_TAIL) + "\n", encoding="utf-8")
    return stream


def _resumed_pointer(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    attempt_started_seconds: float | None = None,
    with_previous_exit_record: bool = True,
) -> dict:
    """A run between a resume's attempt write and its new worker's registration.

    The shape the observed run carried: the pointer still names the previous
    attempt's exited process, the previous attempt's stream and exit record are
    still in the run directory, the manifest is the delivered one with its
    status word reset, and the worktree holds a commit past the recorded base.
    """
    repo, base = liveness._worktree_with_commit(tmp_path, f"tree-{run_id}")
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid(),
        phase="working",
        manifest_body=RESET_BODY,
        worktree=repo,
        base_sha=base,
    )
    pointer["log_path"] = str(_write_previous_stream(run_id))
    pointer["attempt"] = PREVIOUS
    if with_previous_exit_record:
        _write_previous_exit_record(run_id, attempt=PREVIOUS)
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


def test_a_resumed_attempt_with_no_worker_yet_is_not_drawn_halted(
    tmp_path: Path,
) -> None:
    """The first case: the observed run, whose predecessor's end is not its own.

    Every fact the halted reading rests on belongs to attempt 6 — its dead pid,
    its exit record, its committed work — while attempt 7 has recorded neither a
    process nor an end. The run is starting, and the pane draws starting as
    working.
    """
    pointer = _resumed_pointer(tmp_path)

    snapshot = _snapshot(pointer)

    # The arm the base reading came from, named so a red log says which reading
    # the fixture reaches when the deferral is dropped.
    assert snapshot["classification"] != "exited-unfinished", snapshot
    assert snapshot["state"] not in HALTED_STATES, (
        "the pointer's dead process and the exit record beside it are attempt "
        f"{PREVIOUS}'s, so attempt {ATTEMPT} must not be drawn halted; "
        f"it read {snapshot['state']!r} with {snapshot!r}"
    )
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES, snapshot["state"]
    assert snapshot["process_alive"] is False, snapshot["process_alive"]


def test_the_fixture_is_the_shape_that_read_halted(tmp_path: Path) -> None:
    """The fixture's own properties, so the deciding branch is not a guess.

    The halted reading needs all four answers to point at the attempt that
    already ran: the canonical exit record must still name it, the manifest must
    still carry a non-terminal word, the worktree must hold a commit past base,
    and the pointer must be silent about the attempt now starting.
    """
    pointer = _resumed_pointer(tmp_path)
    directory = runs.run_dir(RUN_ID)
    previous_exit = json.loads(
        (directory / recovery.EXIT_RECORD_NAME).read_text(encoding="utf-8")
    )

    assert previous_exit["attempt"] == PREVIOUS, previous_exit
    assert previous_exit["exit_code"] == 0, previous_exit
    assert "status: in-progress" in Path(pointer["manifest_path"]).read_text(
        encoding="utf-8"
    ), "the reset manifest is what keeps the predecessor's report from being dropped"
    assert recovery_liveness._current_attempt_number(pointer) == ATTEMPT
    assert recovery_watch._attempt_awaits_its_worker(pointer) is True
    assert recovery._commits_beyond_base(pointer) > 0, (
        "the predecessor's committed work is the fourth fact the halted reading uses"
    )


def test_a_resumed_attempt_without_the_predecessors_exit_record_is_not_halted(
    tmp_path: Path,
) -> None:
    """The same run where a resume had already retired the exit record.

    Without that record the base reading is the interrupted arm instead, which
    carries a duty rather than a repair word. Both are halted, and neither is
    this attempt's end.
    """
    pointer = _resumed_pointer(tmp_path, with_previous_exit_record=False)

    snapshot = _snapshot(pointer)

    assert snapshot["state"] not in HALTED_STATES, snapshot
    assert snapshot["state"] in recovery.FLEET_WORKING_STATES, snapshot["state"]


def test_a_resumed_attempt_past_the_bound_reads_as_today(tmp_path: Path) -> None:
    """The third case: the deferral carries the launch bound and then expires.

    Past STRANDED_LAUNCH_BOUND_SECONDS with no stream and no worker record of
    its own, nothing is coming, so the run reads as it does today: through the
    stranded-launch rule the death branch already applies to a resumed attempt.
    Within the bound it is starting, so the reading below is the clock's and not
    the fixture's.
    """
    inside = _snapshot(
        _resumed_pointer(
            tmp_path,
            run_id=RUN_ID + "-inside",
            attempt_started_seconds=time.time() - 60,
        )
    )
    past = _resumed_pointer(
        tmp_path,
        run_id=RUN_ID + "-past",
        attempt_started_seconds=(
            time.time() - recovery_liveness.STRANDED_LAUNCH_BOUND_SECONDS - 60
        ),
    )

    snapshot = _snapshot(past)

    assert inside["state"] == "starting", inside["state"]
    assert snapshot["state"] in HALTED_STATES, snapshot["state"]
    assert snapshot["recovery_classification"] == "launch-failed", snapshot
    assert "stranded launch" in str(snapshot["detail"]), snapshot["detail"]


def test_the_resumed_attempt_owes_no_duty(tmp_path: Path) -> None:
    """The fourth case: nothing is owed for work that is already resuming.

    Both shapes are published to the live directory, so the duty projection
    reads the same pointers the pane does. The run whose predecessor's exit
    record is still in place is the observed one; the run without it is the
    same window one step earlier, and read from that record's absence the
    predecessor's interrupted reading is the one carrying a turn-ended-early
    duty. Neither duty belongs to the attempt that is only starting.
    """
    live = runs.live_dir()
    live.mkdir(parents=True, exist_ok=True)
    published = []
    for suffix, exit_record in (("", True), ("-retired", False)):
        pointer = _resumed_pointer(
            tmp_path,
            run_id=RUN_ID + suffix,
            with_previous_exit_record=exit_record,
        )
        (live / f"{RUN_ID}{suffix}.json").write_text(
            json.dumps(pointer), encoding="utf-8"
        )
        published.append(pointer)

    duties = obligations.obligations(
        str(published[0]["project"]), str(published[0]["session"])
    )

    raised = [item for item in duties["obligations"] if item.get("run_id")]
    assert raised == [], raised
