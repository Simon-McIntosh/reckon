"""A pid this host never issued cannot make a live worker read dead.

The crew home is shared across login nodes while a pid answers only on the host
that issued it, so a reading taken here and an answer carried from wherever the
pointer was written are different facts. The fleet read re-derived liveness with
a probe of the pointer's pid on the reading host and wrote the answer into the
field the classifier reads as the last observer's own account. A reader on a
host that never issued that pid found nothing holding it and called the worker
gone, so the retained-work interruption fired for a worker still running on its
own machine — the misreading a coordinator acts on by resuming the run and
duplicating work that is already in flight.

The mirror direction is the same mistake with the sign flipped. The run
directory's worker record carries a pid but no host, so a foreign run whose
number happened to be live here was handed proof of life this host cannot
support, and its row read alive with the reading marked proven.

The views share one narrowed reading rather than each taking a probe of its own:
the directory row is classified through it, and a sprint's liveness asks it
directly, so a pointer written on another machine cannot arrive alive in one
view and unreadable in another.

Each case below is paired with a control that differs in one fact, so what a
failure names is the fact rather than a neighbour: a run launched here whose pid
nothing holds must still read interrupted, and a foreign run whose end the
supervisor recorded must still read process gone, because a recorded end is the
account of a death rather than an inference from a missing process.

The real config home is fingerprinted before and after every case, so a fixture
for another host cannot write into a live fleet's own home.

The declared mutation restores the ungated ``list_live`` write in a scratch
copy; the run launched elsewhere must then fail on its own assertion, with the
retained-work clause back on a pid this host never issued.
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon.crew import directory, recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness
from tests import test_stalled_row_names_liveness as stalled

HOST = socket.gethostname()

# A host this machine is not: the pointer names somewhere else, so a reading
# taken here is no reading of the pid it carries.
FOREIGN_HOST = "another-login-node"

# The project these stub pointers belong to: the name the shared pointer
# fixture stamps, so the fleet read finds them.
PROJECT = "liveness-fixture"

# The window these stub runs are judged against, in seconds.
STALL_SECONDS = 900

IN_PROGRESS_MANIFEST = "node: {run_id}\nstatus: in-progress\n"

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "restore the ungated list_live liveness write in a scratch copy; case F "
    "must fail with dead-pid-with-retained-work"
)


def _home_fingerprint(home: Path) -> list[tuple[str, int]]:
    """The real config home's own entries, by name and mtime.

    One directory level only: the point is to catch a write that landed in the
    reader's own home, and a recursive walk of a live fleet's home on GPFS is
    the crawl this check must not itself become.
    """
    if not home.is_dir():
        return []
    return sorted((entry.name, entry.stat().st_mtime_ns) for entry in home.iterdir())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run against a temporary config home, and prove the real one untouched.

    The fixture is the receipt for its own isolation: these cases write pointers
    and run directories for runs that live on other machines, and one landing in
    the real home would both escape the case and collide with a live fleet, so
    the fingerprint is taken before the environment moves and the same home is
    re-read after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _run_launched_elsewhere(
    tmp_path: Path,
    run_id: str,
    *,
    launcher_host: str = FOREIGN_HOST,
    pid: int | None = None,
    worker_pid: int | None = None,
) -> dict:
    """One live pointer whose worktree carries a commit past its base.

    The pid is a real number beyond the kernel's ceiling, so the process table
    was asked on this host and answered that nothing holds it — the answer a
    fabricated death is made of. A worker record written here names a pid on
    this host, which is the record whose liveness the classifier may not turn
    into proof of life for a run launched elsewhere.
    """
    repo, base = liveness._worktree_with_commit(tmp_path, f"tree-{run_id}")
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=liveness._absent_pid() if pid is None else pid,
        phase="working",
        manifest_body=IN_PROGRESS_MANIFEST.format(run_id=run_id),
        worker_pid=worker_pid,
        worktree=repo,
        base_sha=base,
    )
    pointer["launcher_host"] = launcher_host
    # A resolvable session, so an interruption of this run offers the resume the
    # reported misreading produced rather than the redispatch an unresumable run
    # would be given for reasons this file is not testing.
    pointer["session_id"] = "session-held-by-the-worker"
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _as_the_fleet_read_hands_it_on(run_id: str) -> dict:
    """The record the follower's own path holds between its two steps.

    ``watch_ticker`` calls ``list_live`` and reduces each record with
    ``_watch_snapshot``, so this is the middle step's output: a probe written
    into the record here is read by the classifier as the last observer's own
    answer, on whatever host reads next.
    """
    records = runs.list_live(project=PROJECT)
    matching = [record for record in records if str(record.get("run_id")) == run_id]
    assert len(matching) == 1, [record.get("run_id") for record in records]
    return matching[0]


def _classify(record: dict, moment: float) -> tuple[dict, dict]:
    """The classifier's reading and the state a ticker compares."""
    row = recovery.classify_pointer(
        record, now_seconds=moment, stale_after_seconds=STALL_SECONDS
    )
    snapshot = recovery._watch_snapshot(
        record, moment=moment, stall_seconds=STALL_SECONDS
    )
    return row, snapshot


def _directory_row(run_id: str) -> dict:
    """The directory view's own row for one run, wherever the read placed it."""
    result = directory.directory(project=PROJECT)
    rows = [
        row for coordinator in result["coordinators"] for row in coordinator["runs"]
    ] + list(result["unowned_runs"])
    matches = [row for row in rows if row["run_id"] == run_id]
    assert len(matches) == 1, matches
    return matches[0]


def test_a_run_launched_elsewhere_is_not_read_interrupted_with_retained_work(
    tmp_path: Path,
) -> None:
    """Retained work on another host is not an interruption this host can see.

    Every ingredient the clause fires on is present and is asserted here: the
    manifest is the worker's own non-terminal last word, the worktree carries a
    commit beyond the dispatch base, and no exit was recorded. The only fact
    that separates this run from the control below is the host that launched it,
    so a row that reads interrupted here is reading a pid the reading host never
    issued as a death.
    """
    run_id = "r-launched-elsewhere"
    _run_launched_elsewhere(tmp_path, run_id)
    record = _as_the_fleet_read_hands_it_on(run_id)

    assert recovery._commits_beyond_base(record) == 1, (
        "the fixture stopped carrying the retained work the clause fires on"
    )

    row, snapshot = _classify(record, time.time())

    # The reading a coordinator acts on comes first: a row that says the worker
    # is gone with work in hand is the harm, however the reading was arrived at.
    assert row["interruption"] is None, row["interruption"]
    assert "dead-pid-with-retained-work" not in json.dumps(row)
    assert row["classification"] != recovery.INTERRUPTED_RUN_PHASE, row["detail"]
    assert snapshot["classification"] != recovery.INTERRUPTED_RUN_PHASE, snapshot[
        "detail"
    ]
    assert snapshot["recovery"] != "resume", snapshot["detail"]

    # And the injection that produced it, so a row that reads right for the
    # wrong reason is still caught.
    assert record.get("process_alive") is None, (
        "the fleet read added a probe of its own to the record"
    )
    assert row["liveness_proven"] is False, row["detail"]
    assert row["process_alive"] is not False, row["detail"]


def test_a_foreign_worker_record_cannot_borrow_a_local_process_for_life(
    tmp_path: Path,
) -> None:
    """A number live here is not a worker of a run launched somewhere else.

    The worker record names a pid and carries no host of its own, so the pid is
    asked on whatever machine reads it. A foreign run whose number happens to be
    held here would then be handed proof of life, and its row would read alive
    with the reading marked proven — the same fabrication as a death read from a
    foreign pid, pointing the other way.
    """
    run_id = "r-foreign-with-a-local-number"
    with liveness._live_child() as local_pid:
        _run_launched_elsewhere(tmp_path, run_id, worker_pid=local_pid)
        record = _as_the_fleet_read_hands_it_on(run_id)
        assert runs.process_alive(local_pid) is True, (
            "the worker record's pid is not live on this host, so the "
            "borrowed-life case is not under test"
        )
        row, snapshot = _classify(record, time.time())

    assert row["process_alive"] is not True, row["detail"]
    assert row["liveness_proven"] is False, row["detail"]
    assert snapshot["process_alive"] is not True, snapshot["detail"]
    assert snapshot["liveness_proven"] is False, snapshot["detail"]


def test_a_run_launched_here_with_an_absent_pid_still_reads_interrupted(
    tmp_path: Path,
) -> None:
    """The control the narrowing is measured against.

    The same fixture, the same fleet read and the same worktree, with the one
    fact changed: this host launched the run. Its pid was asked here and nothing
    holds it, which is an observation rather than an inference, so the
    interruption is the right reading and must still be emitted.
    """
    run_id = "r-launched-here-with-no-process"
    _run_launched_elsewhere(tmp_path, run_id, launcher_host=HOST)
    record = _as_the_fleet_read_hands_it_on(run_id)

    row, snapshot = _classify(record, time.time())

    assert row["process_alive"] is False, row["detail"]
    assert row["liveness_proven"] is True, row["detail"]
    assert row["classification"] == recovery.INTERRUPTED_RUN_PHASE, row["detail"]
    assert row["interruption"]["reason"] == "dead-pid-with-retained-work"
    assert row["recovery"] == "resume", row["detail"]
    assert snapshot["classification"] == recovery.INTERRUPTED_RUN_PHASE


def test_a_foreign_run_with_a_recorded_exit_still_reads_process_gone(
    tmp_path: Path,
) -> None:
    """A recorded end is the cross-host reading, and the narrowing keeps it.

    The supervisor's exit record outlives a pointer nobody updates and a pid no
    other machine can look up, so it is how a death on another host is read
    without a probe here. The row must still carry the process state in words,
    and must not read interrupted: a run whose end was recorded is not an
    inference from a missing process.
    """
    run_id = "r-foreign-with-a-recorded-exit"
    pointer, moment = stalled._stub_pointer(
        tmp_path, run_id, pid=liveness._absent_pid(), launcher_host=FOREIGN_HOST
    )
    liveness._write_exit_record(run_id)

    row, snapshot, line = stalled._render_stalled_row(pointer, moment)

    assert row["liveness_proven"] is False, row["detail"]
    assert row["process_alive"] is False, row["detail"]
    assert row["exit_record"] is not None, row["exit_record"]
    assert row["interruption"] is None, row["interruption"]
    assert "process gone" in line, line
    assert "liveness unknown" not in line, line
    assert snapshot["recovery"] != "resume", snapshot["detail"]


def test_a_death_read_elsewhere_is_not_enough_for_the_retained_work_clause(
    tmp_path: Path,
) -> None:
    """The clause's own gate, driven with both provances of one reading.

    The same record and the same reading, differing only in where the reading
    was taken: unproven, it emits nothing, and proven here it still emits with
    the work it counted. A case that asserted only the first half could be
    passed by a clause that never fires at all.
    """
    run_id = "r-death-read-elsewhere"
    _run_launched_elsewhere(tmp_path, run_id)
    record = runs.read_pointer(run_id)

    unproven, unproven_commits = recovery._interruption_evidence(
        record,
        phase="working",
        process_alive=False,
        liveness_proven=False,
    )
    proven, proven_commits = recovery._interruption_evidence(
        record,
        phase="working",
        process_alive=False,
        liveness_proven=True,
    )

    assert unproven is None, unproven
    assert unproven_commits == 0, unproven_commits
    assert proven is not None, "the clause no longer fires on a proven death"
    assert proven["reason"] == "dead-pid-with-retained-work", proven
    assert proven_commits == 1, proven_commits


def test_the_directory_row_does_not_read_a_foreign_pid_as_alive(
    tmp_path: Path,
) -> None:
    """The directory view reports liveness too, and reads it under the same gate.

    The row is built from the pointer alone, so a probe taken here becomes the
    run's liveness in it. The pid is a process this host really holds, so
    nothing in the pointer's own facts keeps the row from reading alive; the one
    fact that does is the host the run was launched on, which the pointer names
    and this host's process table cannot answer for. The same pointer with the
    host set to this one is the control: it reads alive, so the gate narrows the
    reading rather than removing it.
    """
    run_id = "r-foreign-row-with-a-local-number"
    with liveness._live_child() as local_pid:
        pointer = _run_launched_elsewhere(tmp_path, run_id, pid=local_pid)
        assert runs.process_alive(local_pid) is True, (
            "the pointer's pid is not a live process on this host, so the "
            "borrowed-life reading is not under test"
        )
        foreign_row = _directory_row(run_id)
        pointer["launcher_host"] = HOST
        runs._write_json(runs.pointer_path(run_id), pointer)
        local_row = _directory_row(run_id)

    assert foreign_row["process_alive"] is not True, foreign_row
    assert foreign_row["process_alive"] is None, foreign_row
    assert local_row["process_alive"] is True, local_row
