"""One newest-stream reader for the stall classifier and the session lookup.

A resumed or lane-changed run keeps writing to a new stream file —
``resume-N.jsonl`` and ``lane-change-N.jsonl`` land beside the ``stream.jsonl``
the pointer first named. A reader that consults only the first stream reads a
run's oldest output as its present state: a healthy resumed run ages as if
nothing had happened, and a session that is sitting in its newest stream reads
as absent. Both questions are answered from whichever stream was written last,
through one reader, so the two readers cannot disagree about which is current.
"""

from __future__ import annotations

import os
from pathlib import Path

from reckon import _backends, crew
from reckon.crew import recovery, resumption

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)
FIXTURE_SESSION = "01a0635f-62a3-7283-a81b-61cd39bedb60"


def _write(path: Path, *, text: str, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _record(run_id: str, run_id_directory: Path, **overrides) -> dict:
    record = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "the-node", "plan": "plan-a", "time_budget": "20m"},
        "phase": "working",
        "process_alive": True,
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex", "exec"],
        "manifest_path": str(run_id_directory / "manifest.md"),
        "log_path": str(run_id_directory / "stream.jsonl"),
    }
    record.update(overrides)
    return record


def _run_directory(run_id: str) -> Path:
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# -- the reader itself ------------------------------------------------------


def test_the_newest_non_empty_stream_wins(isolated_reckon_home: Path) -> None:
    """A fresh resume outranks the first stream the pointer named."""
    directory = _run_directory("r-resumed")
    _write(directory / "stream.jsonl", text="old\n", mtime=1000.0)
    fresh = _write(directory / "resume-1.jsonl", text="new\n", mtime=2000.0)

    found = recovery.newest_stream(directory)

    assert found is not None
    assert found[0] == fresh
    assert found[1] == 2000.0


def test_a_lane_change_stream_counts(isolated_reckon_home: Path) -> None:
    """A run that changed lane is writing to lane-change-N.jsonl, and that is
    the stream its activity must be read from."""
    directory = _run_directory("r-lane")
    _write(directory / "stream.jsonl", text="old\n", mtime=1000.0)
    _write(directory / "resume-1.jsonl", text="resumed\n", mtime=1500.0)
    lane = _write(directory / "lane-change-2.jsonl", text="lane\n", mtime=3000.0)

    found = recovery.newest_stream(directory)

    assert found is not None
    assert found[0] == lane
    assert found[1] == 3000.0


def test_an_empty_newer_stream_is_skipped(isolated_reckon_home: Path) -> None:
    """A stream file opened but not yet written is not activity, so the reader
    falls back to the newest non-empty one."""
    directory = _run_directory("r-empty")
    _write(directory / "stream.jsonl", text="old\n", mtime=1000.0)
    earlier = _write(directory / "resume-1.jsonl", text="resumed\n", mtime=2000.0)
    _write(directory / "resume-2.jsonl", text="", mtime=4000.0)

    found = recovery.newest_stream(directory)

    assert found is not None
    assert found[0] == earlier


def test_nothing_written_is_no_measurement(isolated_reckon_home: Path) -> None:
    """No non-empty stream at all answers None rather than an infinitely old
    one."""
    directory = _run_directory("r-quiet")
    _write(directory / "stream.jsonl", text="", mtime=1000.0)

    assert recovery.newest_stream(directory) is None


# -- the classifier reads through it ----------------------------------------


def test_a_stale_first_stream_with_a_fresh_resume_is_not_stalled(
    isolated_reckon_home: Path,
) -> None:
    """The incident: the pointer still names stream.jsonl while resume-1.jsonl
    is being written, so a reader of the first stream alone ages a live run as
    stalled."""
    directory = _run_directory("r-classify-resume")
    now = 100_000.0
    _write(directory / "stream.jsonl", text="old\n", mtime=now - 5_000.0)
    _write(directory / "resume-1.jsonl", text="new\n", mtime=now - 5.0)

    record = _record("r-classify-resume", directory)
    snapshot = recovery._watch_snapshot(record, moment=now, stall_seconds=600)

    assert snapshot["state"] != "stalled"


def test_a_fresh_lane_change_keeps_a_run_from_stalling(
    isolated_reckon_home: Path,
) -> None:
    """A lane-changed run is producing output on its lane stream; the stale
    first stream must not stall it."""
    directory = _run_directory("r-classify-lane")
    now = 100_000.0
    _write(directory / "stream.jsonl", text="old\n", mtime=now - 5_000.0)
    _write(directory / "lane-change-1.jsonl", text="lane\n", mtime=now - 5.0)

    record = _record("r-classify-lane", directory)
    snapshot = recovery._watch_snapshot(record, moment=now, stall_seconds=600)

    assert snapshot["state"] != "stalled"


def test_a_genuinely_quiet_run_still_stalls(isolated_reckon_home: Path) -> None:
    """The reader must still find a stall: with every stream quiet past the
    window the classification is unchanged."""
    directory = _run_directory("r-real-stall")
    now = 100_000.0
    _write(directory / "stream.jsonl", text="old\n", mtime=now - 5_000.0)
    _write(directory / "resume-1.jsonl", text="older\n", mtime=now - 4_000.0)

    record = _record("r-real-stall", directory)
    snapshot = recovery._watch_snapshot(record, moment=now, stall_seconds=600)

    assert snapshot["state"] == "stalled"


# -- the session lookup reads through it ------------------------------------


def test_an_empty_newest_resume_still_resolves_the_session_from_an_earlier_stream(
    isolated_reckon_home: Path,
) -> None:
    """A resumed turn writes resume-2.jsonl, empty until its first event; the
    session it is continuing is in resume-1.jsonl, and the lookup must fall
    back to it rather than report an absence."""
    run_id = "r-session-fallback"
    directory = _run_directory(run_id)
    _write(
        directory / "resume-1.jsonl",
        text=FIXTURE.read_text(encoding="utf-8"),
        mtime=1_000.0,
    )
    newest = _write(directory / "resume-2.jsonl", text="", mtime=2_000.0)

    record = _record(run_id, directory, log_path=str(newest))

    resolved = resumption.resolve_session(run_id, record=record)

    expected = _backends.observe_log(
        backend_name="alpha",
        backend={"launch": "cli", "command": "codex"},
        log_path=directory / "resume-1.jsonl",
    ).session_id
    assert expected == FIXTURE_SESSION
    assert resolved["resolved"] is True
    assert resolved["source"] == "stream"
    assert resolved["session_id"] == FIXTURE_SESSION