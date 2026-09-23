"""A dispatched run records its session id, or names why it captured none.

Resume is chosen by a human who can read that a run needs resuming, and a
pointer carrying a bare null said nothing: a run whose stream had not been read
and a run whose stream genuinely had no id to give both reported the same
absence, and five resumable runs were promoted on that reading. The capture
path therefore answers either with the id or with the point the capture
reached, so the absence is a measurement rather than a silence.

The stream is a recorded shape rather than a live model: a session id exists
only once a backend announces one, and the two cases that matter are a stream
that carries one and a stream that never does. The first is a real codex
stream's opening event; the second is the same stream with the announcement
removed, which is what a run that ends before the backend reports its session
looks like on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.dispatch import observe

CODEX_SESSION = "019ff509-8a60-7723-94fd-65942a6d8faa"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    (home / "crew" / "live").mkdir(parents=True)
    (home / "crew" / "runs").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _write_stream(home: Path, run_id: str, lines: list[dict]) -> Path:
    path = home / "crew" / "runs" / run_id / "stream.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _stub_run(home: Path, run_id: str, stream: Path) -> None:
    """Write the minimum live pointer an observation reads."""
    record = {
        "run_id": run_id,
        "project": "proj",
        "backend": "probe",
        "launch": "cli",
        "command": "codex",
        "manifest_path": str(home / "crew" / "runs" / run_id / "manifest.md"),
        "log_path": str(stream),
        "worktree": "",
        "phase": "working",
        "pid": None,
    }
    (home / "crew" / "live" / f"{run_id}.json").write_text(json.dumps(record))


def test_a_stream_carrying_a_session_id_records_it(crew_home: Path) -> None:
    """The announcement the backend writes into its stream reaches the pointer."""
    stream = _write_stream(
        crew_home,
        "r-carries",
        [
            {"thread_id": CODEX_SESSION, "type": "thread.started"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {}},
        ],
    )
    _stub_run(crew_home, "r-carries", stream)

    record = observe("r-carries")

    assert record["session_id"] == CODEX_SESSION
    assert record["session_source"] == "stream"
    # A resolved session is recorded as the id; a leftover absence beside it
    # would tell a reader to distrust a pointer that in fact has an id.
    assert not record.get("session_id_absent")


def test_a_stream_that_never_emits_a_session_records_a_named_reason(
    crew_home: Path,
) -> None:
    """A run the backend never announced a session for names where capture stopped."""
    stream = _write_stream(
        crew_home,
        "r-silent",
        [
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {}},
        ],
    )
    _stub_run(crew_home, "r-silent", stream)

    record = observe("r-silent")

    assert not record.get("session_id")
    absence = record["session_id_absent"]
    assert absence["point"] == "stream-without-id"
    assert absence["reason"]


def test_an_unreadable_stream_names_the_point_that_blocked_capture(
    crew_home: Path,
) -> None:
    """A stream path with nothing behind it is a different absence from a silent one."""
    missing = crew_home / "crew" / "runs" / "r-missing" / "absent.jsonl"
    _stub_run(crew_home, "r-missing", missing)

    record = observe("r-missing")

    assert not record.get("session_id")
    assert record["session_id_absent"]["point"] == "stream-unreadable"


def test_a_harness_launch_names_its_own_absence(crew_home: Path) -> None:
    """An in-harness task writes no stream, so nothing will ever capture one."""
    stream = crew_home / "crew" / "runs" / "r-task" / "stream.jsonl"
    _stub_run(crew_home, "r-task", stream)
    pointer = crew_home / "crew" / "live" / "r-task.json"
    record = json.loads(pointer.read_text())

    record["launch"] = "in-harness"
    pointer.write_text(json.dumps(record))

    observed = observe("r-task")

    assert not observed.get("session_id")
    assert observed["session_id_absent"]["point"] == "harness-launch"
