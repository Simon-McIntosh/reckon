"""An unreadable manifest names the word it could not read.

A manifest whose status word is outside the reader's vocabulary is a file no
supported reader can parse, so it is neither a delivery nor an absence: it is
refused, and the refusal quotes the rejected word. The word is what makes the
row actionable. "The manifest cannot be read" sends a coordinator to the
format documentation; "ready-for-review is not a recognised manifest status"
names the one line to repair.

Only a gone process lets the file be judged at all. While the worker lives the
manifest is a condition of work in flight — the worker can still rewrite the
word — so the row stays quiet and the run reads live.

Three observations pin that emission through the published watch stream. A
``ready-for-review`` manifest beside a gone process lands as exactly one
transition into ``unreadable`` whose reason carries the word. A recognised
non-terminal word beside a live process emits nothing, and neither does the
unrecognised one while its process lives. The live cases carry their own
positive control — each asserts the file was read and the process table
answered alive — because an absence claim that also passes for a reader which
never opened the file states nothing.

Every fixture is dispatched an hour before it is read, so the reader's grace
for a file that has just moved — the launch window on a run, the rewrite
window on a manifest whose mtime just changed — never applies, and no reading
here depends on the instant the case runs. The manifest of the first case
arrives between its two observations, so that case is about the reading its
arrival changes rather than about the clock.

The declared mutation deletes the unreadable arm; the first case then emits
nothing, because a file whose arrival changes no reading changes no row.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs
from tests import test_a_live_run_never_reads_dead as liveness

# The declared mutation, verbatim: the string the promotion audit matches
# against the red log's first line.
DECLARED_MUTATION = (
    "delete the unreadable arm at reckon/crew/recovery.py:4282; the "
    "ready-for-review case must then fail with no transition emitted"
)

READY_FOR_REVIEW_BODY = (
    "node: a-stub-node\nstatus: ready-for-review\ncommits:\nblockers:\n"
)

IN_PROGRESS_BODY = "node: a-stub-node\nstatus: in-progress\ncommits:\nblockers:\n"

# The ages, in seconds, each fixture's three clocks are set to before it is
# read. The reader withholds a verdict on a run that has just appeared, reads a
# manifest whose mtime has just moved as a rewrite in progress, and calls a
# non-terminal report superseded when the run's stream is the newer activity —
# so every fixture is placed decisively outside all three windows: an hour-old
# dispatch, a last stream record minutes old, and a manifest written after that
# record and well before the rewrite window closes.
DISPATCH_AGE_SECONDS = 3600
STREAM_AGE_SECONDS = 300
MANIFEST_AGE_SECONDS = 120

# The window these runs are judged against. The runs are quiet, so it only
# fixes the horizon their age is read against.
STALL_SECONDS = recovery.parse_duration(recovery.DEFAULT_WATCH_STALL_WINDOW)


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
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against a temporary config home, and prove the real one untouched.

    These cases publish into a project's watch stream and write run pointers,
    so a path that resolved the real fleet's home would both collide with live
    runs and be corrupted by the test. The fingerprint is the receipt for the
    isolation: the real home is read before the environment moves and re-read
    after the case ends.
    """
    real_home = Path(os.path.expanduser("~")) / ".config" / "reckon"
    before = _home_fingerprint(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield
    assert _home_fingerprint(real_home) == before


def _set_mtime(path: Path, when: float) -> None:
    os.utime(path, (when, when))


def _age_manifest(path: Path) -> None:
    """Backdate a manifest past the reader's window for a file that just moved."""
    _set_mtime(path, time.time() - MANIFEST_AGE_SECONDS)


def _dispatch(
    tmp_path: Path,
    run_id: str,
    *,
    project: str,
    pid: int | None,
    manifest_body: str | None = None,
) -> dict:
    """One stub run as the pointer is written, aged past the transient windows.

    The pointer's ``created_at`` is an hour old, and where a manifest is present
    the file is the run's newer activity rather than its stream, so the report
    is read as the worker's own last word instead of as superseded by a stream
    write that came after it.
    """
    pointer = liveness._pointer(
        tmp_path,
        run_id,
        pid=pid,
        phase="working",
        manifest_body=manifest_body,
    )
    pointer["project"] = project
    dispatched = datetime.now(tz=UTC) - timedelta(seconds=DISPATCH_AGE_SECONDS)
    pointer["created_at"] = dispatched.isoformat()
    now = time.time()
    stream = Path(str(pointer["log_path"]))
    if stream.is_file():
        _set_mtime(stream, now - STREAM_AGE_SECONDS)
    manifest = Path(str(pointer["manifest_path"]))
    if manifest.exists():
        _set_mtime(manifest, now - MANIFEST_AGE_SECONDS)
    return pointer


def _write_pointer(run_id: str, pointer: dict) -> None:
    crew._write_json(crew.pointer_path(run_id), pointer)


def _events(stream_path: Path, run_id: str) -> list[dict]:
    """Every watch event the stream holds for one run, in order."""
    return [
        event
        for event in runs.read_stream_events(stream_path)
        if event.get("run_id") == run_id
    ]


def _kinds(events: list[dict]) -> list[tuple[str, str | None, str]]:
    """Event kinds with their state move, for a failure that names what ran."""
    return [
        (str(event.get("event")), event.get("from_state"), event.get("to_state"))
        for event in events
    ]


def test_an_unreadable_manifest_beside_a_gone_process_names_its_word(
    tmp_path: Path,
) -> None:
    """The file a dead worker left behind is read once; its word reaches the row.

    The pointer's worker is gone before any manifest landed, so the file's
    arrival is the only change between the two observations and the transition
    is the run's entry into the unreadable state rather than a state the
    baseline already held.
    """
    run_id = "r-word-lands"
    project = "proj-word-lands"
    pointer = _dispatch(tmp_path, run_id, project=project, pid=liveness._absent_pid())
    _write_pointer(run_id, pointer)
    manifest = Path(str(pointer["manifest_path"]))

    with runs._project_watch_claim(project, "1h") as (acquired, registration):
        assert acquired is True
        stream = Path(str(registration["stream_path"]))
        opened = _events(stream, run_id)
        assert _kinds(opened) == [("baseline", None, "abandoned")], _kinds(opened)
        # The positive control for this case: the baseline is a run the reader
        # looked at, found dead with proof, and read as having delivered
        # nothing. The state the manifest lands into is therefore one the
        # arrival alone moves.
        assert opened[0]["process_alive"] is False, opened[0]["process_alive"]
        assert opened[0]["liveness_proven"] is True, opened[0]["liveness_proven"]
        assert not manifest.exists(), manifest

        # The manifest the dying worker left behind arrives, and the next
        # observation is the one that has to read it.
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(READY_FOR_REVIEW_BODY, encoding="utf-8")
        _age_manifest(manifest)

        crew.list_live(project=project)
        events = _events(stream, run_id)

    landed = [event for event in events if event["event"] == "transition"]
    assert len(landed) == 1, (
        "the manifest's word must land as one transition into unreadable; "
        f"the stream holds {_kinds(events)}"
    )
    row = landed[0]
    assert row["from_state"] == "abandoned", row["from_state"]
    assert row["to_state"] == "unreadable", row["to_state"]
    assert row["classification"] == "unreadable", row["classification"]
    assert row["process_alive"] is False, row["process_alive"]
    assert row["recovery_classification"] == "unreadable", row[
        "recovery_classification"
    ]
    assert row["recovery"] == "repair", row["recovery"]
    # The reason clause a coordinator reads: the event persists it under
    # ``detail`` and the ticker composes the row's reason from that field. The
    # assertion is the word itself, because a clause that says only "could not
    # be read" leaves the file's problem unidentified.
    reason = str(row["detail"])
    assert "ready-for-review" in reason, reason


def test_a_recognised_non_terminal_manifest_beside_a_live_worker_is_silent(
    tmp_path: Path,
) -> None:
    """The worker's own last word, read while the worker is still writing.

    ``in-progress`` is the status a live worker writes while orienting, so the
    reader takes it as the working word it is and nothing about the run moves:
    no transition into unreadable, and no transition at all.
    """
    run_id = "r-word-known"
    project = "proj-word-known"
    with liveness._live_child() as pid:
        pointer = _dispatch(
            tmp_path,
            run_id,
            project=project,
            pid=pid,
            manifest_body=IN_PROGRESS_BODY,
        )
        _write_pointer(run_id, pointer)
        row = recovery.classify_pointer(
            pointer, now_seconds=time.time(), stale_after_seconds=STALL_SECONDS
        )

        with runs._project_watch_claim(project, "1h") as (acquired, registration):
            assert acquired is True
            stream = Path(str(registration["stream_path"]))
            opened = _events(stream, run_id)
            assert _kinds(opened) == [("baseline", None, "working")], _kinds(opened)
            assert opened[0]["process_alive"] is True, opened[0]["process_alive"]
            # A working run has nothing to say beyond that it is working: the
            # clause the classifier composed for it is blanked on this surface:
            # the row a pane renders is the state and nothing else.
            assert opened[0]["detail"] == "", opened[0]["detail"]

            crew.list_live(project=project)
            folded = _events(stream, run_id)

    # The positive control: the file was present, its word was taken, and the
    # process table answered alive — so the reading is what a reader says about
    # a live run holding this word, not what a reader says about a file it
    # never reached.
    assert row["manifest_present"] is True, row["manifest_present"]
    assert row["manifest_status"] == "in-progress", row["manifest_status"]
    assert row["process_alive"] is True, row["process_alive"]
    assert row["classification"] == "running", row["classification"]
    assert row["recovery_classification"] == "running", row["recovery_classification"]
    assert "in-progress" in str(row["detail"]), row["detail"]
    assert _kinds(folded) == [("baseline", None, "working")], _kinds(folded)


def test_an_unreadable_manifest_beside_a_live_worker_is_silent(tmp_path: Path) -> None:
    """A live worker can still rewrite the word, so nothing is read from it.

    The file is the one the first case reads as unreadable and the only
    difference is the process table's answer: while the worker lives the run
    has not written a verdict yet, the word is carried on the row for a reader
    to see, and no unreadable row is emitted.
    """
    run_id = "r-word-refused"
    project = "proj-word-refused"
    with liveness._live_child() as pid:
        pointer = _dispatch(
            tmp_path,
            run_id,
            project=project,
            pid=pid,
            manifest_body=READY_FOR_REVIEW_BODY,
        )
        _write_pointer(run_id, pointer)
        row = recovery.classify_pointer(
            pointer, now_seconds=time.time(), stale_after_seconds=STALL_SECONDS
        )

        with runs._project_watch_claim(project, "1h") as (acquired, registration):
            assert acquired is True
            stream = Path(str(registration["stream_path"]))
            opened = _events(stream, run_id)
            assert _kinds(opened) == [("baseline", None, "running")], _kinds(opened)
            assert opened[0]["process_alive"] is True, opened[0]["process_alive"]

            crew.list_live(project=project)
            folded = _events(stream, run_id)

    # The positive control: the file was read and refused, and the refusal
    # names the word — so the absence asserted below is a reader that reached
    # the file and chose not to emit, not one that never opened it.
    assert row["manifest_present"] is True, row["manifest_present"]
    assert "ready-for-review" in str(row["manifest_error"] or ""), row["manifest_error"]
    assert row["process_alive"] is True, row["process_alive"]
    assert row["classification"] == "running", row["classification"]
    assert row["recovery_classification"] == "unwritten", row["recovery_classification"]
    assert row["recovery"] == "resume", row["recovery"]
    assert _kinds(folded) == [("baseline", None, "running")], _kinds(folded)
