"""A follower reloads only onto code that changed and will import.

Two independent guards are exercised here, both from the same live incident: a
merge resolving in the shared checkout left conflict markers in a follower
module for about a second, and every armed follower re-executed into the broken
image, raised SyntaxError and exited. Beside it, a module whose mtime moved with
no content change reloaded the whole fleet for nothing.
"""

from __future__ import annotations

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
from reckon.crew import runs

# The reload's own line, written to the pane. It is about the follower rather
# than the fleet, so the measured row reads below skip it — counting it would
# drop a genuine transition from the measure.
DEFERRED_MARKER = "deferred its reload"

PROBE_NODE_PREFIX = "reload-probe-"


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _event(number: int) -> dict:
    return {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-09-04T04:00:00+00:00",
        "run_id": f"run-{number}",
        "node": f"node-{number}",
        "session": "session-a",
        "role": "implement",
        "backend": "local",
        "model": "model",
        "alias": "model",
        "effort": "medium",
        "from_state": "starting",
        "to_state": "working",
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "detail": "",
        "needs_help_complete": False,
    }


def _probe_event(number: int) -> dict:
    event = _event(number)
    event["run_id"] = f"{PROBE_NODE_PREFIX}run-{number}"
    event["node"] = f"{PROBE_NODE_PREFIX}{number}"
    return event


def _is_probe_row(line: str) -> bool:
    return PROBE_NODE_PREFIX in line


def _is_pane_line(line: str) -> bool:
    return _is_probe_row(line) or DEFERRED_MARKER in line


def _drain_probe_rows(lines: queue.Queue) -> None:
    while True:
        try:
            row = lines.get_nowait()
        except queue.Empty:
            return
        assert _is_pane_line(row), f"a measured row arrived before the measure: {row!r}"


def _wait_for_registration(project: str, session: str) -> dict:
    deadline = time.monotonic() + 8
    state = runs.follower_state(project, session)
    while time.monotonic() < deadline and not state["registered"]:
        time.sleep(0.02)
        state = runs.follower_state(project, session)
    assert state["registered"], "the running follower never registered"
    return state


def _await_attached(
    stream_path: Path, lines: queue.Queue, *, attempts: int = 20
) -> str:
    for number in range(attempts):
        runs._append_watch_lines(stream_path, [_probe_event(number)])
        try:
            row = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        if _is_probe_row(row):
            _drain_probe_rows(lines)
            return row
    raise AssertionError("the follower never rendered a transition")


def _read_until(lines: queue.Queue, marker: str, *, timeout: float = 20.0) -> str:
    """Return the first line carrying ``marker``, skipping the probe rows."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"the follower never printed a line with {marker!r}")
        row = lines.get(timeout=remaining)
        if marker in row:
            return row


def _measured_rows(
    lines: queue.Queue, count: int, *, timeout: float = 8.0
) -> list[str]:
    rows: list[str] = []
    deadline = time.monotonic() + timeout
    while len(rows) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"only {len(rows)} of {count} measured rows arrived")
        row = lines.get(timeout=remaining)
        if _is_pane_line(row):
            continue
        rows.append(row)
    return rows


def test_touching_a_follower_module_without_changing_it_does_not_move_the_stamp(
    monkeypatch, tmp_path
) -> None:
    """A touched file is not new code, so the stamp a follower reloads on holds.

    ``follower_code_stamp`` is keyed on each module's content; the mtime and
    size stay only as a pre-check. A tool that rewrites a file with identical
    bytes — a formatter, a checkout restoring the same revision — moves the
    mtime alone, and a stamp that moved with it reloaded every follower for
    nothing. The observed case was a module whose mtime moved between 16:49:51
    and 16:49:52 with no content change.
    """
    package = tmp_path / "reckon"
    (package / "crew").mkdir(parents=True)
    (package / "cli.py").write_text("cli = 1\n")
    touched = package / "crew" / "paid_lanes.py"
    touched.write_text("lane = 1\n")
    monkeypatch.setattr(runs, "__file__", str(package / "crew" / "runs.py"))

    before = runs.follower_code_stamp()

    # The same bytes, a later mtime: a touch, not a change.
    later = time.time() + 5
    os.utime(touched, (later, later))
    assert runs.follower_code_stamp() == before, (
        "a module touched without a content change is not new code"
    )

    # Real new content is new code, and the stamp must move.
    touched.write_text("lane = 2\n")
    assert runs.follower_code_stamp() != before, "changed content must move the stamp"


def test_syntax_error_in_a_follower_module_defers_the_reload(
    isolated_home, tmp_path
) -> None:
    """A reload onto code that does not import keeps the current image alive.

    The reload is proven safe in a throwaway interpreter before the process
    image is replaced. A syntax error — the conflict-marker case observed when
    a merge resolved mid-module — fails that proof, so the follower prints one
    dim deferred line and goes on delivering on the image it already holds.
    Without the check the replacement dies at import and the pane goes dark.
    """
    source_package = Path(cli_module.__file__).resolve().parent
    copied_root = tmp_path / "source"
    copied_package = copied_root / "reckon"
    shutil.copytree(source_package, copied_package)

    executable = Path(sys.executable).with_name("reckon")
    environment = {
        **os.environ,
        "PYTHONPATH": str(copied_root),
        "RECKON_HOME": str(isolated_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [
            str(executable),
            "crew",
            "follow",
            "--project",
            "proj",
            "--session",
            "session-a",
            "--no-color",
            "--width",
            "240",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line.rstrip("\n")) for line in process.stdout],
        daemon=True,
    )
    reader.start()

    try:
        with runs._project_watch_claim("proj", "1h") as (acquired, registration):
            assert acquired
            _wait_for_registration("proj", "session-a")
            stream_path = Path(registration["stream_path"])
            _await_attached(stream_path, lines)

            # A merge resolved mid-module leaves conflict markers behind: the
            # module no longer parses, and the stamp moves because the bytes
            # changed.
            broken = copied_package / "crew" / "recovery.py"
            broken.write_text(
                broken.read_text()
                + "\n<<<<<<< HEAD\n_render_watch_transition = None\n=======\n>>>>>>> other\n"
            )

            deferred = _read_until(lines, DEFERRED_MARKER)
            assert deferred.startswith(DEFERRED_MARKER) or DEFERRED_MARKER in deferred

            # The current image is still delivering: a transition appended after
            # the deferral is rendered.
            runs._append_watch_lines(stream_path, [_event(101)])
            rows = _measured_rows(lines, 1)
            assert "node-101" in rows[0]

        assert process.poll() is None, "the follower kept running on its current image"
    finally:
        process.terminate()
        process.wait(timeout=5)
