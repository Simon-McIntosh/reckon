"""A watch producer keeps producing when a merge breaks the config under it.

The producer composes a transition on every tick, and composing one prices the
run against the resolved configuration. A merge can therefore land a config
that does not validate while the seat is already armed and holding the stream
open; if the config error is allowed to end the tick, it ends the process, and
the pane goes dark for a file that is usually fixed moments later.

These tests drive the real producer process. The broken layer is written into
the isolated config home *after* the seat is armed, so the failure is the one a
merge causes rather than the one a bad arm-time config would. What is shown is
that the tick is deferred: one line names it, the producer keeps delivering
rows for work it can see, and the transitions it owed are published once the
config validates rather than dropped.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon.crew import runs

PROJECT = "proj"
NODE_PREFIX = "node"

# The producer names a deferred tick with this marker; the tick it stands in
# for is retried, so the line must appear once and not on every retry. Resolved
# through the module when it publishes one, so a tree that lacks the deferral
# altogether still runs this test to its behavioural failure rather than
# stopping at the import.
DEFERRAL_MARKER = getattr(
    runs, "WATCH_TICK_DEFERRAL_MARKER", "reckon crew watch deferred its tick"
)

# A layer that does not load: an unclosed flow sequence is invalid YAML, which
# is what a merge that resolves a config badly leaves behind.
BROKEN_CONFIG = "backends: [\n"

SEAT_WITHIN_SECONDS = 25.0
ROW_WITHIN_SECONDS = 20.0
STREAM_WITHIN_SECONDS = 20.0


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    """Keep registrations, pointers, and streams in temporary state."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _repo_root() -> Path:
    """The checkout the imported package came from, so the child runs it too."""
    return Path(cli_module.__file__).resolve().parent.parent


def _launch_producer(home: Path) -> tuple[subprocess.Popen, queue.Queue]:
    """Start the real ``crew watch`` producer against the isolated home."""
    executable = Path(sys.executable).with_name("reckon")
    environment = {
        **os.environ,
        "PYTHONPATH": str(_repo_root()),
        "RECKON_HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [
            str(executable),
            "crew",
            "watch",
            "--project",
            PROJECT,
            "--no-color",
            "--width",
            "240",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
        start_new_session=True,
    )
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line.rstrip("\n")) for line in process.stdout],
        daemon=True,
    )
    reader.start()
    return process, lines


def _seat_record() -> dict:
    path = runs.watch_lock_path(PROJECT)
    for _ in range(20):
        if not path.is_file():
            time.sleep(0.02)
            continue
        text = path.read_text()
        if not text.strip():
            time.sleep(0.02)
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            time.sleep(0.02)
    return {}


def _await_seat() -> dict:
    deadline = time.monotonic() + SEAT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        record = _seat_record()
        if record.get("pid"):
            return record
        time.sleep(0.05)
    raise AssertionError(f"the producer never took the seat; last {_seat_record()!r}")


def _write_measured_run(home: Path, run_id: str, node: str) -> None:
    """A working run whose stream carries usage, so a tick prices it.

    Pricing is what reads the resolved configuration, so a run without a
    measured stream would never reach the read this test breaks.
    """
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 0,
                    "output_tokens": 40,
                    "reasoning_output_tokens": 4,
                },
            }
        )
        + "\n"
    )
    manifest = runs.crew_home() / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {node}\nstatus: in-progress\n")
    pointer = runs.pointer_path(run_id)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": "s1",
                "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
                "phase": "working",
                "created_at": runs._utc_now(),
                "manifest_path": str(manifest),
                "log_path": str(log),
                "agent": {"backend": "local", "model": "m-1", "effort": "medium"},
            }
        )
    )


def _read_until(lines: queue.Queue, marker: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"the producer never printed a line with {marker!r}")
        try:
            row = lines.get(timeout=remaining)
        except queue.Empty:
            raise AssertionError(
                f"the producer never printed a line with {marker!r}"
            ) from None
        if marker in row:
            return row


def _drained(lines: queue.Queue) -> list[str]:
    """Every line the producer has emitted so far, without blocking."""
    seen: list[str] = []
    while True:
        try:
            seen.append(lines.get_nowait())
        except queue.Empty:
            return seen


def _await_stream_runs(run_ids: set[str], *, timeout: float) -> None:
    """Wait until the durable stream carries a transition for every run id."""
    path = runs.watch_stream_path(PROJECT)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            events = list(runs.read_stream_events(path))
            carried = {str(event.get("run_id") or "") for event in events}
            if run_ids <= carried:
                return
        time.sleep(0.1)
    events = list(runs.read_stream_events(path)) if path.is_file() else []
    carried = {str(event.get("run_id") or "") for event in events}
    raise AssertionError(
        f"the deferred transitions were not retried; stream carries {carried}, "
        f"wanted {run_ids}; events={events!r}"
    )


def test_the_producer_defers_a_broken_config_and_recovers(
    isolated_home, tmp_path
) -> None:
    """A config that fails validation under an armed seat defers the tick.

    The seat is armed against a valid config and a first run is published
    normally. A broken layer is then written as a merge would leave it, and two
    more runs appear. The producer keeps delivering their rows, says once that
    the tick is deferred, and — once the layer loads again — publishes the
    transitions it owed, so a transient config error costs one observation
    rather than the producer and its stream.
    """
    process, lines = _launch_producer(isolated_home)
    try:
        _await_seat()
        _write_measured_run(isolated_home, "r-1", f"{NODE_PREFIX}-1")
        _read_until(lines, f"{NODE_PREFIX}-1", timeout=ROW_WITHIN_SECONDS)
        # The first run is published durably before the config breaks, so the
        # stream's baseline is established and what follows is a real arrival.
        _await_stream_runs({"r-1"}, timeout=STREAM_WITHIN_SECONDS)

        # A merge leaves a layer that does not load, under the live seat.
        (isolated_home / "flight.yaml").write_text(BROKEN_CONFIG)

        _write_measured_run(isolated_home, "r-2", f"{NODE_PREFIX}-2")
        deferred = _read_until(lines, DEFERRAL_MARKER, timeout=ROW_WITHIN_SECONDS)
        assert DEFERRAL_MARKER in deferred

        # A second run while the config is still broken: the producer must keep
        # producing, and must not repeat the deferral for every retry.
        _write_measured_run(isolated_home, "r-3", f"{NODE_PREFIX}-3")
        _read_until(lines, f"{NODE_PREFIX}-3", timeout=ROW_WITHIN_SECONDS)
        all_rows = [deferred, *_drained(lines)]
        assert sum(DEFERRAL_MARKER in row for row in all_rows) == 1, all_rows

        assert process.poll() is None, (
            "the producer exited on a config error instead of deferring the tick"
        )

        # The layer loads again; the transitions it owed are retried, not lost.
        (isolated_home / "flight.yaml").unlink()
        _await_stream_runs({"r-2", "r-3"}, timeout=STREAM_WITHIN_SECONDS)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
