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
import socket
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
CONFLICT_MARKER = (
    "\n<<<<<<< HEAD\n_render_watch_transition = None\n=======\n>>>>>>> other\n"
)

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


def _plant_seat_record(
    project: str,
    *,
    pid: int,
    pid_start_time: float | None,
    host: str,
    started_at: str,
    code_stamp: str | None,
    reckon_version: str = "0.0.0",
) -> None:
    """Leave a seat record on disk, as an arming that never erased one would.

    The record is the only carrier of the facts a reader sees about a seat, so
    planting one is how a seat that this process is not running is observed:
    the fields are exactly those ``_project_watch_claim`` writes.
    """
    path = runs.watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "project": project,
        "pid": pid,
        "pid_start_time": pid_start_time,
        "host": host,
        "stall_window": runs.DEFAULT_WATCH_STALL_WINDOW,
        "started_at": started_at,
        "stream_path": str(runs.watch_stream_path(project)),
        "log_path": str(runs.watch_log_path(project)),
        "reckon_version": reckon_version,
    }
    if code_stamp is not None:
        record["code_stamp"] = code_stamp
    path.write_text(json.dumps(record))


def _self_identity() -> tuple[int, float, str]:
    """This test process, in the terms a seat record names a process."""
    return os.getpid(), runs._process_start_time(os.getpid()), socket.gethostname()


def _dead_pid() -> int:
    """A pid this host has issued and which is confirmed gone."""
    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=10)
    return victim.pid


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
    new stamp — so the same run that proved the seat's record carries the code
    the replacement runs. The record's start time stays where it was: the
    replacement is the same process, so the seat it holds is the same seat and
    has not been armed twice.
    """
    root, package = _copy_source(tmp_path)
    before = _stamp_of(root)
    process, _lines = _launch_producer(root, isolated_home)
    try:
        armed = _await_seat(PROJECT, before)

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
        reloaded = _await_seat(PROJECT, after)
        assert reloaded["pid"] == process.pid
        assert reloaded["started_at"] == armed["started_at"], (
            "a replacement is the same process holding the same seat, so the "
            "seat's start time must not be restamped"
        )
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


def test_fresh_arming_over_a_dead_seat_stamps_its_own_start(
    isolated_home, tmp_path
) -> None:
    """The seat's start time belongs to the process that wrote it.

    A producer that dies without erasing its record leaves the record behind,
    and the next arming opens the same file. Carrying the start time forward
    from whatever is there makes a producer armed a second ago read as one that
    has been up for hours, so the age a reader judges a stale producer by is a
    predecessor's. The record's identity — pid, kernel start time, host — is
    what tells the two apart, and only a seat's own replacement keeps all three.
    """
    root, _package = _copy_source(tmp_path)
    predecessor_start = "2001-01-01T00:00:00+00:00"
    _plant_seat_record(
        PROJECT,
        pid=_dead_pid(),
        pid_start_time=1.0,
        host=socket.gethostname(),
        started_at=predecessor_start,
        code_stamp="0" * 64,
        reckon_version=runs.__version__,
    )

    expected = _stamp_of(root)
    process, _lines = _launch_producer(root, isolated_home)
    try:
        record = _await_seat(PROJECT, expected)
        assert record["started_at"] != predecessor_start, (
            "a fresh arming must stamp its own start, not the dead predecessor's"
        )
        assert record["pid"] == process.pid
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_the_live_view_reports_a_seat_running_older_code(isolated_home) -> None:
    """A seat's staleness is a fact of the read model, not of one surface.

    The producer runs the image it was armed with, so a fix that landed since
    then is inert on that seat and every row it writes is composed by code the
    reader's is not. The read model answers it for any caller; a record naming
    no stamp is the older producer, since the field's absence predates it.
    """
    pid, pid_start, host = _self_identity()
    _plant_seat_record(
        PROJECT,
        pid=pid,
        pid_start_time=pid_start,
        host=host,
        started_at=runs._utc_now(),
        code_stamp="0" * 64,
        reckon_version=runs.__version__,
    )
    visibility = runs.project_watch_visibility(PROJECT)
    assert visibility["code_stale"] is True
    assert visibility["code_stamp"] == "0" * 64

    _plant_seat_record(
        PROJECT,
        pid=pid,
        pid_start_time=pid_start,
        host=host,
        started_at=runs._utc_now(),
        code_stamp=runs.follower_code_stamp(),
        reckon_version=runs.__version__,
    )
    assert runs.project_watch_visibility(PROJECT)["code_stale"] is False


def test_the_follower_names_a_producer_running_older_code(
    isolated_home, monkeypatch
) -> None:
    """The follower says so, on attach, where an operator is already looking.

    The seat's stamp is recorded where a reader opens the seat, and that is not
    where an operator watching a project looks. One dim line on attach names
    the gap and the command that cycles the seat; a seat running this
    follower's own code says nothing at all.
    """
    monkeypatch.setattr(runs, "producer_live", lambda project: True)
    pid, pid_start, host = _self_identity()

    def one_attach_pass(**extra) -> list[str]:
        captured: list[str] = []
        monkeypatch.setattr(cli_module, "_echo_follow_line", captured.append)
        stop = threading.Event()
        list(
            cli_module._follow_watch_lines(
                PROJECT,
                poll_interval=0.0,
                sleeper=lambda _seconds: None,
                stop=stop,
                on_poll=lambda _payload: stop.set(),
                sweep=lambda _project: None,
                **extra,
            )
        )
        return captured

    _plant_seat_record(
        PROJECT,
        pid=pid,
        pid_start_time=pid_start,
        host=host,
        started_at=runs._utc_now(),
        code_stamp="0" * 64,
        reckon_version=runs.__version__,
    )
    stale_lines = one_attach_pass()
    named = [line for line in stale_lines if "runs older code" in line]
    assert named, f"the follower did not name the stale producer: {stale_lines!r}"
    assert "reckon crew unwatch --project proj" in named[0]
    assert "reckon crew watch --ensure-service --project proj" in named[0]

    # The line is history rather than a worker transition, so it is dimmed on
    # the same path the follower's other remembered rows are.
    dimmed = [line for line in one_attach_pass(color=True) if "runs older code" in line]
    assert dimmed and cli_module.HISTORY_DIM in dimmed[0]

    _plant_seat_record(
        PROJECT,
        pid=pid,
        pid_start_time=pid_start,
        host=host,
        started_at=runs._utc_now(),
        code_stamp=runs.follower_code_stamp(),
        reckon_version=runs.__version__,
    )
    current_lines = one_attach_pass()
    assert not [line for line in current_lines if "runs older code" in line], (
        "a current seat must print nothing."
    )
