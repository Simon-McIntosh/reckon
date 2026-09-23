"""A review that has delivered stops holding the lane it was granted.

A reviewer stores one record, and its manifest reaches one verdict. Once both
exist there is nothing left to produce, so a review that keeps streaming past
its delivery is not still working: it spends a lane the fleet needs. These
tests drive the watcher tick itself, because the stopping is the watcher's own
behaviour rather than a property of any one run.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, review
from reckon.crew.recovery import watch_ticker

PROJECT = "sample"
REVIEWED_RUN = "source-run"
REVIEW_RUN = "reviewer-run"
GRACE = 300.0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


class _Signals:
    """Record the pid identity the watcher signalled."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str | None]] = []

    def __call__(self, pid: int, expected_start_time: str | None = None) -> None:
        self.calls.append((pid, expected_start_time))


def _parsed_review() -> dict:
    """A review record scoring every dimension, ready to store."""
    emitted = "\n".join(
        f"SCORE {dimension}: 18" for dimension in review.REVIEW_DIMENSIONS
    )
    record = review.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": REVIEWED_RUN,
            "review_run_id": REVIEW_RUN,
        }
    )
    return record


def _write_stream(home: Path, mtime: float) -> Path:
    path = home / "streams" / f"{REVIEW_RUN}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _write_manifest(home: Path, status: str, mtime: float) -> Path:
    path = home / "manifests" / f"{REVIEW_RUN}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"node: {REVIEW_RUN}\nstatus: {status}\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))
    return path


def _reviewer(
    home: Path,
    *,
    delivered: bool,
    stream_offset: float,
    manifest_status: str = "complete",
    role: str = "review",
    node_id: str = "review-of-source-node",
) -> dict:
    """A live reviewer pointer with its delivery and stream dated as asked.

    ``stream_offset`` is the stream's mtime measured forward from the instant
    the review delivered: positive means the stream kept growing after both
    writes landed, negative means its newest write precedes them.
    """
    delivered_at = time.time() - 3600.0
    record_path = review.review_path(PROJECT, REVIEWED_RUN)
    if delivered:
        review.store_review(_parsed_review())
        os.utime(record_path, (delivered_at, delivered_at))
    manifest = _write_manifest(home, manifest_status, delivered_at)
    stream = _write_stream(home, delivered_at + stream_offset)
    return {
        "run_id": REVIEW_RUN,
        "project": PROJECT,
        "session": "sample-session",
        "repo": str(home / "repo"),
        "phase": "active",
        "pid": 4321,
        "pid_start_time": "start-time",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(stream),
        "role": role,
        "node": {
            "id": node_id,
            "plan": "a-review-dies-where-nobody-is-looking",
            "section": "s4",
            "time_budget": "20m",
            "write_paths": [str(record_path)],
        },
    }


def _source_pointer(home: Path) -> dict:
    """A completed run awaiting review, for the composed-brief test."""
    manifest = home / "manifests" / "source-run.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: source-node\nstatus: complete\ncommits: abc123\n",
        encoding="utf-8",
    )
    return {
        "run_id": REVIEWED_RUN,
        "project": PROJECT,
        "session": "sample-session",
        "node": {
            "id": "source-node",
            "plan": "a-review-dies-where-nobody-is-looking",
            "section": "s4",
        },
        "phase": "complete",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(home / "streams" / "source-run.jsonl"),
        "process_alive": False,
    }


def _tick(home: Path, pointer: dict) -> tuple[_Signals, dict]:
    """Park the pointer, run one watcher tick, and read the pointer back."""
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
    signals = _Signals()
    ticker = watch_ticker(
        PROJECT,
        stall_window="1h",
        poll_interval=0,
        sleeper=lambda _seconds: None,
        signal_run=signals,
    )
    next(ticker)
    return signals, recovery.read_pointer(pointer["run_id"])


def test_a_delivered_review_still_streaming_is_stopped(home: Path) -> None:
    pointer = _reviewer(home, delivered=True, stream_offset=GRACE + 300.0)
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)

    signals, stored = _tick(home, pointer)

    assert signals.calls == [(4321, "start-time")]
    assert stored["phase"] == "stopped"
    ended = stored["ended_after_delivery"]
    assert ended["grace_seconds"] == GRACE
    assert ended["stopped_at"]
    assert ended["record_path"] == str(review.review_path(PROJECT, REVIEWED_RUN))
    assert ended["manifest_path"] == pointer["manifest_path"]


def test_review_delivered_answers_the_delivered_facts(home: Path) -> None:
    pointer = _reviewer(home, delivered=True, stream_offset=GRACE + 300.0)

    facts = recovery.review_delivered(pointer)

    assert facts is not None
    assert facts["record_path"] == str(review.review_path(PROJECT, REVIEWED_RUN))
    assert facts["manifest_path"] == pointer["manifest_path"]
    assert facts["delivered_at"] == max(facts["record_mtime"], facts["manifest_mtime"])


def test_a_delivered_review_inside_the_grace_is_left_running(home: Path) -> None:
    pointer = _reviewer(home, delivered=True, stream_offset=60.0)

    signals, stored = _tick(home, pointer)

    assert signals.calls == []
    assert stored["phase"] == "active"
    assert "ended_after_delivery" not in stored


def test_a_review_with_no_stored_record_is_never_stopped(home: Path) -> None:
    pointer = _reviewer(home, delivered=False, stream_offset=-3 * 86400.0)

    signals, stored = _tick(home, pointer)

    assert signals.calls == []
    assert "ended_after_delivery" not in stored


def test_an_incomplete_manifest_review_is_left_running(home: Path) -> None:
    pointer = _reviewer(
        home,
        delivered=True,
        stream_offset=3 * 86400.0,
        manifest_status="blocked",
    )

    signals, stored = _tick(home, pointer)

    assert signals.calls == []
    assert "ended_after_delivery" not in stored


def test_a_run_that_is_not_a_review_is_never_stopped(home: Path) -> None:
    pointer = _reviewer(
        home,
        delivered=True,
        stream_offset=3 * 86400.0,
        role="implement",
        node_id="implement-node",
    )

    signals, stored = _tick(home, pointer)

    assert signals.calls == []
    assert "ended_after_delivery" not in stored


def test_the_composed_review_brief_states_where_the_turn_ends(home: Path) -> None:
    pointer = _source_pointer(home)
    crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)

    row = recovery.classify_pointer(pointer)

    assert row["classification"] == "scoring"
    assert (
        "the turn ends once that record is stored and the manifest reads complete"
        in row["next_action"]
    )
