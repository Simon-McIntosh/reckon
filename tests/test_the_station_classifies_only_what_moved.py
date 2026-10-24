"""The station re-derives a verdict only when what a verdict depends on moved.

Three live runs sit in a fixture crew home: two launched on this node and one
launched elsewhere. The full classifier is counted through a patch, and the
liveness probe through the process-table reader the liveness gate calls, so
each case measures the work a tick actually did rather than the answer it
happened to reach. A run whose inputs did not move must take only the quiet-beat
reading -- its silence recomputed from the clock -- while a run whose pointer,
stream, manifest or resume stream moved is classified exactly once.

Two readings move with no file at all, and both are measured here: a worker
that exits while every file stays still, and a working run whose silence grows
past its window between two ticks. The second is the one a station that trusted
its stored verdict would report forever as working.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from reckon.crew import recovery_watch, runs, station_store
from reckon.crew.run_paths import crew_home

THIS_HOST = socket.gethostname()
OTHER_HOST = "another-node"
STALL_SECONDS = 60


def _stream_records(wait_seconds: int | None = None) -> list[dict]:
    records = [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "working"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    if wait_seconds is not None:
        records.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": f"sleep {wait_seconds}"},
                        }
                    ]
                },
            }
        )
    return records


def _write_stream(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item) for item in records) + "\n")


def _pointer(
    home: Path,
    run_id: str,
    *,
    launcher_host: str = THIS_HOST,
    stream: Path,
    phase: str = "working",
) -> Path:
    manifest = home / "runs" / run_id / "manifest.md"
    tree = home / "trees" / run_id
    tree.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "project": "fixture",
        "session": "s",
        "phase": phase,
        "launcher_host": launcher_host,
        "pid": os.getpid(),
        "created_at": "2026-10-24T12:00:00Z",
        "log_path": str(stream),
        "manifest_path": str(manifest),
        "worktree": str(tree),
        "repo": str(tree),
        "node": {},
    }
    path = home / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    return path


def _touch_pointer(path: Path) -> None:
    record = json.loads(path.read_text())
    record["last_heartbeat"] = "2026-10-24T12:30:00Z"
    path.write_text(json.dumps(record))


@pytest.fixture()
def fleet(home: Path) -> dict[str, Path]:
    """A crew home with three live runs and nothing yet classified."""
    base = crew_home()
    streams: dict[str, Path] = {}
    pointers: dict[str, Path] = {}
    for run_id, launcher in (
        ("r-one", THIS_HOST),
        ("r-two", THIS_HOST),
        ("r-three", OTHER_HOST),
    ):
        stream = base / "runs" / run_id / "stream.jsonl"
        _write_stream(stream, _stream_records())
        streams[run_id] = stream
        pointers[run_id] = _pointer(base, run_id, launcher_host=launcher, stream=stream)
    return {"home": base, "streams": streams, "pointers": pointers}


class _Counters:
    def __init__(self) -> None:
        self.classified: list[str] = []
        self.probed: list[str] = []
        self.alive: dict[str, bool] = {}


@pytest.fixture()
def counters(monkeypatch: pytest.MonkeyPatch) -> _Counters:
    state = _Counters()

    def counting_classifier(record, **kwargs):
        state.classified.append(str(record.get("run_id")))
        return REAL_CLASSIFIER(record, **kwargs)

    def counting_probe(record, *args, **kwargs):
        run_id = str((record or {}).get("run_id") or "")
        state.probed.append(run_id)
        return state.alive.get(run_id, True)

    monkeypatch.setattr(recovery_watch, "classify_pointer", counting_classifier)
    monkeypatch.setattr(runs, "record_process_alive", counting_probe)
    return state


REAL_CLASSIFIER = recovery_watch.classify_pointer


def _store(tmp_path: Path) -> station_store.StationStore:
    return station_store.StationStore(tmp_path / "station")


def _tick(store, fleet, *, moment=None, classify=True):
    store.tick(home=fleet["home"])
    assert classify
    return store.classify_tick(now_seconds=moment, stall_seconds=STALL_SECONDS)


def test_an_unchanged_tick_classifies_no_run(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    assert sorted(counters.classified) == ["r-one", "r-three", "r-two"]
    counters.classified.clear()

    store.tick(home=fleet["home"])
    store.classify_tick(stall_seconds=STALL_SECONDS)

    assert counters.classified == []


def test_a_pointer_move_classifies_exactly_that_run(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    counters.classified.clear()

    _touch_pointer(fleet["pointers"]["r-one"])
    _tick(store, fleet)

    assert counters.classified == ["r-one"]


def test_a_stream_move_classifies_exactly_that_run(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    counters.classified.clear()

    with fleet["streams"]["r-two"].open("a") as handle:
        handle.write(json.dumps(_stream_records()[1]) + "\n")
    _tick(store, fleet)

    assert counters.classified == ["r-two"]


def test_a_manifest_move_classifies_exactly_that_run(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    counters.classified.clear()

    manifest = fleet["home"] / "runs" / "r-three" / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("---\nstatus: in-progress\n---\n")

    _tick(store, fleet)

    assert counters.classified == ["r-three"]


def test_a_resume_stream_classifies_that_run(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    counters.classified.clear()

    resumed = fleet["home"] / "runs" / "r-one" / "resume-1.jsonl"
    _write_stream(resumed, _stream_records())
    later = time.time() + 5
    os.utime(resumed, (later, later))

    _tick(store, fleet)

    assert counters.classified == ["r-one"]


def test_liveness_is_probed_only_for_runs_launched_on_this_node(
    fleet, counters, tmp_path
):
    store = _store(tmp_path)
    _tick(store, fleet)
    counters.probed.clear()

    store.tick(home=fleet["home"])
    store.classify_tick(stall_seconds=STALL_SECONDS)

    assert set(counters.probed) == {"r-one", "r-two"}
    assert OTHER_HOST not in counters.probed


def test_a_worker_exit_with_every_file_still_reads_a_new_verdict(
    fleet, counters, tmp_path
):
    store = _store(tmp_path)
    first = _tick(store, fleet)["r-one"]
    counters.classified.clear()

    counters.alive["r-one"] = False
    second = _tick(store, fleet)["r-one"]

    assert counters.classified == ["r-one"]
    assert (second["process_alive"], second["state"]) != (
        first["process_alive"],
        first["state"],
    )


def _silence(fleet, monkeypatch, *, waiting: bool):
    base = fleet["home"]
    stream = base / "runs" / "r-two" / "stream.jsonl"
    os.utime(stream, (time.time() - 300, time.time() - 300))
    start = time.time() - 300
    if waiting:
        _write_stream(stream, _stream_records(wait_seconds=300))
        os.utime(stream, (start, start))
    return stream, start


def test_silence_past_the_window_reads_stalled_with_no_file_moving(
    fleet, counters, tmp_path
):
    store = _store(tmp_path)
    _tick(store, fleet)
    stream = fleet["streams"]["r-two"]
    quiet_until = time.time()
    os.utime(stream, (quiet_until, quiet_until))
    store.tick(home=fleet["home"])
    store.classify_tick(now_seconds=quiet_until, stall_seconds=STALL_SECONDS)
    counters.classified.clear()

    # The clock advances and no file moves: only the stall clock is re-derived.
    verdict = store.classify_tick(
        now_seconds=quiet_until + STALL_SECONDS * 4, stall_seconds=STALL_SECONDS
    )["r-two"]

    assert counters.classified == []
    assert verdict["state"] == "stalled"


def test_a_pause_reason_reads_waiting_with_no_file_moving(fleet, counters, tmp_path):
    store = _store(tmp_path)
    _tick(store, fleet)
    stream = fleet["streams"]["r-two"]
    _write_stream(stream, _stream_records(wait_seconds=300))
    quiet_until = time.time()
    os.utime(stream, (quiet_until, quiet_until))
    store.tick(home=fleet["home"])
    store.classify_tick(now_seconds=quiet_until, stall_seconds=STALL_SECONDS)
    counters.classified.clear()

    verdict = store.classify_tick(
        now_seconds=quiet_until + STALL_SECONDS * 4, stall_seconds=STALL_SECONDS
    )["r-two"]

    assert counters.classified == []
    assert verdict["state"] == "waiting"
