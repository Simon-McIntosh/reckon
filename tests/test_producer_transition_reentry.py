"""The producer's transition write classifies its pointers without re-entering.

A transition is composed from the live pointers, and composing one classifies
each pointer. The classification's scoring branch asks whether a review is
already in flight, and with no pointer snapshot handed to it that reader lists
the project's live runs — which publishes the watch stream again. The producer
therefore re-entered its own transition write once per scoring run: one complete
run with no review attached and no live review recursed until the recursion
limit ended the producer, leaving the project's stream, the session obligation
snapshots and the review reflex to a seat that was no longer running.

The guard is a thread-local around the transition write, on the same mechanism
the obligations sweep uses: a publish entered from inside a running transition
on the same thread is left to that transition rather than re-entered.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery, runs

PROJECT = "a-scoring-transition"
NOW = datetime(2026, 11, 5, 8, 0, 0, tzinfo=UTC)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every case at a temporary crew home, and prove the real one is untouched."""
    real_stream = runs.watch_stream_path(PROJECT)
    before = _fingerprint(real_stream)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    assert runs.crew_home().is_relative_to(tmp_path)
    yield config_home
    assert runs.crew_home().is_relative_to(tmp_path)
    assert _fingerprint(real_stream) == before


def _fingerprint(path: Path) -> tuple[bool, int, int] | None:
    """A file's identity, or None when it is absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (True, stat.st_size, stat.st_mtime_ns)


def _write_scoring_pointer(run_id: str) -> dict[str, Any]:
    """A live pointer that classifies as scoring.

    Its manifest reports completion, no review is attached and no review run is
    live, so the classifier's scoring branch asks the review reader whether a
    review is in flight and that reader lists the project's live pointers.
    """
    directory = runs.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    log = directory / "stream.jsonl"
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "a-plan", "time_budget": "20m"},
        "phase": "complete",
        "created_at": (NOW - timedelta(hours=2)).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(log),
        "process_alive": False,
        # Another host, so the classifier never probes this host's process table.
        "launcher_host": "a-host-that-is-not-this-one",
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _published_rows(pointer: dict[str, Any]) -> list[dict[str, Any]]:
    """Publish one transition over the pointer and return the rows written.

    The producer is registered with the run already standing at ``working``, so
    this tick owes a transition rather than a baseline — the state a run reaches
    when its manifest lands — and the composed row is the one a follower reads.
    """
    run_id = str(pointer["run_id"])
    producer = runs._WatchStreamProducer(
        path=runs.watch_stream_path(PROJECT),
        known={run_id: {"state": "working"}},
        stall_window="1h",
    )
    producer.fleet_seen = True
    runs._WATCH_STREAM_PRODUCERS[PROJECT] = producer
    try:
        runs._publish_watch_stream(PROJECT, [pointer])
        if producer.sweep_thread is not None:
            producer.sweep_thread.join(timeout=10)
    finally:
        runs._WATCH_STREAM_PRODUCERS.pop(PROJECT, None)
    return list(runs.read_stream_events(producer.path))


def test_a_transition_over_a_scoring_pointer_publishes_without_recursing(home) -> None:
    """A complete, unreviewed run reaches the transition write and lands a row."""
    pointer = _write_scoring_pointer("r-scoring")

    # The precondition this case exists to hold: the pointer classifies as
    # scoring, which is the branch that asks the review reader whether a review
    # is in flight from inside the transition write.
    assert recovery.classify_pointer(pointer)["classification"] == "scoring"

    rows = _published_rows(pointer)

    assert rows, "the transition write published no row"
    assert rows[-1]["run_id"] == "r-scoring", rows
    assert rows[-1]["event"] == "transition", rows
    assert rows[-1]["from_state"] == "working", rows
    # The fleet's word for a scoring classification.
    assert rows[-1]["to_state"] == "completed_unpromoted", rows


def test_the_guard_clears_after_the_transition_write(home) -> None:
    """The thread-local is cleared, so the next tick publishes normally."""
    pointer = _write_scoring_pointer("r-scoring-again")

    _published_rows(pointer)
    assert not getattr(runs._SWEEP_LOCAL, "in_transition", False)

    # A second, independent publish on this thread still writes its row.
    rows = _published_rows(pointer)
    assert rows, "the publish after the transition write wrote nothing"
    assert rows[-1]["run_id"] == "r-scoring-again", rows
