"""A watch producer takes changed follower-side modules the way a follower does.

The follower re-executes itself into new code on every content-hash stamp
change, behind a throwaway import proof. The producer — the process that holds
the project watch seat and emits the transition stream — did not, so a fix that
landed after it was armed stayed inert on that seat until someone cycled it by
hand. Measured 2026-09-26: a discard fix had no effect until the producer was
restarted about an hour and a half later, and a discarded run went on rendering
as ``promoted`` in the interval.

These tests observe the real producer process, because the property is about an
image replacement and the seat's own record. The reload is forced through the
same trigger the follower uses — the content-hash stamp over the package's
source files — and its occurrence is confirmed by the replacement image's own
command line, so a run in which no reload happened cannot pass by measuring
something else. The seat record is asserted to carry the code stamp the
producer is actually running, which is the fact a reader needs to see a stale
producer: the version stamp names the install and holds still while commits
land in the checkout.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

PROJECT = "proj"
NODE = "node-1"

# The producer replacement is launched with a launcher that inserts its import
# root on ``sys.path`` before entering the command. That string is absent from
# the argv the test arms, so finding it on the process is direct evidence the
# image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# The producer is itself ``crew watch``, so its deferral line must name that
# command rather than the follower's.
DEFERRED_MARKER = "reckon crew watch deferred its reload"

# What a mid-merge module looks like: bytes that no longer compile, which the
# reload's throwaway import proof is what catches.
CONFLICT_MARKER = "\n<<<<<<< HEAD\n_render_watch_transition = None\n=======\n>>>>>>> other\n"

SEAT_WITHIN_SECONDS = 25.0
RELOAD_WITHIN_SECONDS = 15.0


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    """Keep registrations, pointers, and streams in temporary state."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _copy_source(tmp_path) -> tuple[Path, Path]:
    """Copy the package under test so the test may change its bytes."""
    source_package = Path(cli_module.__file__).resolve().parent
    root = tmp_path / "source"
    package = root / "reckon"
    shutil.copytree(source_package, package)
    return root, package


def _stamp_of(root: Path) -> str:
    """Compute the content-hash stamp over a copied tree, in its own process."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.crew import runs; print(runs.follower_code_stamp())",
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


def _seat_record(project: str) -> dict:
    """Read the seat's on-disk record, retrying a write caught mid-flight."""
    path = runs.watch_lock_path(project)
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


def _await_seat(project: str, expected_stamp: str) -> dict:
    deadline = time.monotonic() + SEAT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        record = _seat_record(project)
        if record.get("pid") and record.get("code_stamp") == expected_stamp:
            return record
        time.sleep(0.05)
    raise AssertionError(
        f"the producer never took the seat with stamp {expected_stamp!r}; "
        f"last record {_seat_record(project)!r}"
    )


def _launch_producer(root: Path, home: Path) -> tuple[subprocess.Popen, queue.Queue]:
    executable = Path(sys.executable).with_name("reckon")
    environment = {
        **os.environ,
        "PYTHONPATH": str(root),
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


def _write_live_run(home: Path, run_id: str) -> None:
    """A working run the producer can see on its next tick."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {NODE}\nstatus: in-progress\n")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": "s1",
            "node": {"id": NODE, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": runs._utc_now(),
            "manifest_path": str(manifest),
            "log_path": str(log),
        },
    )


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _await(predicate, timeout: float, message: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


def _read_until(lines: queue.Queue, marker: str, *, timeout: float = 25.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"the producer never printed a line with {marker!r}")
        row = lines.get(timeout=remaining)
        if marker in row:
            return row


def test_the_seat_records_the_code_stamp_the_producer_runs(
    isolated_home, tmp_path
) -> None:
    """The seat names the code the producer is executing, not just the install.

    ``reckon_version`` is read from the installed distribution's metadata and
    holds still while commits land in the checkout, so it cannot distinguish a
    producer running stale code from a current one. The content-hash stamp can,
    and it is written where a reader opens the seat.
    """
    root, _package = _copy_source(tmp_path)
    expected = _stamp_of(root)
    assert expected, "the copied tree must yield a stamp"
    process, _lines = _launch_producer(root, isolated_home)
    try:
        record = _await_seat(PROJECT, expected)
        assert record["code_stamp"] == expected
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_a_producer_reexecutes_onto_changed_code(isolated_home, tmp_path) -> None:
    """A content change to a follower module replaces the producer's image.

    The stamp moves, the replacement loads, and the seat record advances to the
    new stamp — so the same run that proved the reload happened also proves the
    new image took the seat and reported the code it is now running.
    """
    root, package = _copy_source(tmp_path)
    before = _stamp_of(root)
    process, _lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        # A comment, and one whose every prefix is a comment, so the module
        # still parses and the change is content-only.
        module = package / "crew" / "recovery.py"
        module.write_bytes(module.read_bytes() + b"\n# producer reload probe\n")
        after = _stamp_of(root)
        assert after != before, "a content change must move the stamp"

        _await(
            lambda: RELOAD_LAUNCHER_MARKER in _cmdline(process.pid),
            RELOAD_WITHIN_SECONDS,
            "the producer never re-executed onto the new image",
        )
        _await_seat(PROJECT, after)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_a_producer_defers_a_reload_onto_unimportable_code_and_keeps_producing(
    isolated_home, tmp_path
) -> None:
    """Code that does not compile keeps the current image alive and streaming.

    A merge resolved mid-module leaves conflict markers behind. Re-executing
    into that image would kill the producer and dark the pane, so the reload is
    proven in a throwaway interpreter first: the proof fails, one line says so,
    and the producer goes on delivering on the image it already holds.
    """
    root, package = _copy_source(tmp_path)
    process, lines = _launch_producer(root, isolated_home)
    try:
        before = _stamp_of(root)
        _await_seat(PROJECT, before)

        module = package / "crew" / "recovery.py"
        module.write_bytes(module.read_bytes() + CONFLICT_MARKER.encode())

        deferred = _read_until(lines, DEFERRED_MARKER)
        assert DEFERRED_MARKER in deferred

        # The current image is still delivering: a run it has not reported
        # before is rendered as a row.
        _write_live_run(isolated_home, "r-1")
        _read_until(lines, NODE)

        assert RELOAD_LAUNCHER_MARKER not in _cmdline(process.pid), (
            "the producer must not re-execute onto an image that does not import"
        )
    finally:
        process.terminate()
        process.wait(timeout=5)