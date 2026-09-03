"""A watch producer must declare which code it is running.

A watcher imports its detection module once at startup and then runs for
hours, so a fix that lands after it started is inert on that seat and nothing
distinguishes it from a seat running current code. These tests assert that the
seat stamps its version and start time where a follower can read them, and that
a seat whose stamp is stale or missing is replaced rather than reused when the
replacement is asked for.

They also record what the stamp does not do. It names the install rather than
the code, so it is constant across exactly the transition above, and nothing in
the package asks for the replacement — both asserted below, so a reader finds
the limitation measured instead of inferring the mechanism is live.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from reckon import __version__
from reckon.crew import recovery, runs


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move watcher registration into an isolated configuration home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def watcher_processes():
    """Terminate only watcher subprocesses started by the current test."""
    processes: list[subprocess.Popen[str]] = []
    yield processes
    for process in processes:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _arm_watcher(
    project: str, processes: list[subprocess.Popen[str]]
) -> subprocess.Popen[str]:
    """Hold the real seat lock in a child process, using the real claim path.

    A hermetic in-process fake cannot exercise the flock this behaviour
    depends on: the seat is judged held or free by whether a separate process
    still owns the lock, so the test needs a separate process.
    """
    source_root = Path(__file__).parents[1]
    script = f"""
import time
from reckon.crew import _project_watch_claim
with _project_watch_claim({project!r}, '1h') as (acquired, _record):
    print('ready' if acquired else 'refused', flush=True)
    if acquired:
        time.sleep(30)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=source_root,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    processes.append(process)
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def _rewrite_seat_record(project: str, record: dict) -> None:
    """Overwrite the seat's on-disk record without disturbing its holder.

    The advisory lock guards concurrent *claims*, not raw writes, so a
    second process may rewrite the file's content underneath the process
    that holds the flock -- this is exactly how a test simulates a seat
    armed before the version stamp existed, or armed by a different build.
    """
    path = runs.watch_lock_path(project)
    path.write_text(json.dumps(record, sort_keys=True) + "\n")


def _read_seat_record(project: str) -> dict:
    """Read the seat's raw on-disk record, the way a fresh reader would."""
    path = runs.watch_lock_path(project)
    text = path.read_text() if path.is_file() else ""
    return json.loads(text) if text.strip() else {}


def test_no_seat_reports_no_identity_and_no_replacement_need(home) -> None:
    assert runs.watch_producer_identity("unarmed") == {}
    assert runs.watch_seat_version_current("unarmed") is False
    assert runs.watch_seat_needs_replacement("unarmed") is False


def test_identity_names_the_installed_version_and_start_time(
    home, watcher_processes
) -> None:
    _arm_watcher("proj", watcher_processes)

    identity = runs.watch_producer_identity("proj")

    assert identity["reckon_version"] == __version__
    assert identity["started_at"]
    assert __version__ in identity["line"]
    assert identity["started_at"] in identity["line"]

    cursor = runs.watch_stream_cursor("proj")
    assert cursor["producer"] == identity


def test_a_current_seat_is_reused_rather_than_replaced(home, watcher_processes) -> None:
    _arm_watcher("proj", watcher_processes)

    assert runs.watch_seat_version_current("proj") is True
    assert runs.watch_seat_needs_replacement("proj") is False
    assert runs.replace_stale_watch_seat("proj") is None

    # Reused, not torn down: the seat is still live afterwards.
    assert runs.watch_state("proj")["watcher_live"] is True


def test_a_version_mismatched_seat_is_replaced_at_the_next_arming(
    home, watcher_processes
) -> None:
    process = _arm_watcher("proj", watcher_processes)
    stale_record = dict(runs.watch_state("proj")["watcher"])
    stale_record["reckon_version"] = "0.0.0-stale"
    _rewrite_seat_record("proj", stale_record)

    assert runs.watch_seat_version_current("proj") is False
    assert runs.watch_seat_needs_replacement("proj") is True

    result = runs.replace_stale_watch_seat("proj")

    assert result is not None
    assert result["stopped"] is True
    assert result["registration_released"] is True
    process.wait(timeout=5)
    assert runs.watch_state("proj")["watcher_live"] is False


def test_a_seat_with_no_recorded_version_is_replaced_not_trusted(
    home, watcher_processes
) -> None:
    """Absence stays absence: a seat that predates the stamp is stale, not current."""
    process = _arm_watcher("proj", watcher_processes)
    unstamped_record = dict(runs.watch_state("proj")["watcher"])
    del unstamped_record["reckon_version"]
    _rewrite_seat_record("proj", unstamped_record)

    assert runs.watch_seat_version_current("proj") is False
    assert runs.watch_seat_needs_replacement("proj") is True

    result = runs.replace_stale_watch_seat("proj")

    assert result is not None
    assert result["stopped"] is True
    process.wait(timeout=5)
    assert runs.watch_state("proj")["watcher_live"] is False


def test_replacement_goes_through_the_existing_unwatch_path(
    home, watcher_processes, monkeypatch
) -> None:
    """The version criterion reuses `unwatch` rather than adding a second teardown."""
    _arm_watcher("proj", watcher_processes)
    stale_record = dict(runs.watch_state("proj")["watcher"])
    stale_record["reckon_version"] = "0.0.0-stale"
    _rewrite_seat_record("proj", stale_record)

    calls: list[str] = []
    real_unwatch = recovery.unwatch

    def spy(project: str):
        calls.append(project)
        return real_unwatch(project)

    monkeypatch.setattr(recovery, "unwatch", spy)

    result = runs.replace_stale_watch_seat("proj")

    assert calls == ["proj"]
    assert result is not None
    assert result["registration_released"] is True


def test_the_version_stamp_names_the_install_and_not_the_code(
    home, watcher_processes, monkeypatch
) -> None:
    """The stamp is constant across the transition it exists to detect.

    ``reckon.__version__`` is read from the installed distribution's metadata,
    written once at install time, so the writer of a seat record and every
    later reader of it resolve one string however far the checkout has moved in
    between. The seat therefore still reads as current once the fix it predates
    lands -- the case the stamp was added to catch. Measured on one workstation
    install: the stamp stayed at its installed value while eighty-five commits
    reached the package, ``reckon/crew/runs.py`` among them.
    """
    _arm_watcher("proj", watcher_processes)
    recorded = runs.watch_state("proj")["watcher"]["reckon_version"]

    assert recorded == metadata.version("reckon-plans")
    assert recorded == __version__
    assert runs.watch_seat_version_current("proj") is True

    # No edit to the checkout moves either side of that comparison, so nothing
    # short of a reinstall under a live seat flips the answer -- an install
    # event, not a code change. Standing in for one shows the installed version
    # is the signal's only input beyond the record itself.
    monkeypatch.setattr(runs, "__version__", f"{__version__}+reinstalled")

    assert runs.watch_seat_version_current("proj") is False
    assert runs.watch_seat_needs_replacement("proj") is True


def test_producer_live_erases_a_confirmed_dead_seat_record(
    home, watcher_processes
) -> None:
    """A liveness check that finds the process gone must not leave the lie behind."""
    process = _arm_watcher("proj", watcher_processes)
    assert runs.producer_live("proj") is True

    process.terminate()
    process.wait(timeout=5)

    assert runs.producer_live("proj") is False
    assert runs.watch_producer_identity("proj") == {}
    assert _read_seat_record("proj") == {}


def test_producer_live_erases_a_seat_whose_pid_was_recycled(
    home, watcher_processes
) -> None:
    """A pid that is alive but under a disagreeing start time is confirmed dead too."""
    _arm_watcher("proj", watcher_processes)
    recycled = dict(runs.watch_state("proj")["watcher"])
    recycled["pid_start_time"] = "0"
    _rewrite_seat_record("proj", recycled)

    assert runs.producer_live("proj") is False
    assert runs.watch_producer_identity("proj") == {}
    assert _read_seat_record("proj") == {}


def test_producer_live_leaves_a_live_seat_record_untouched(
    home, watcher_processes
) -> None:
    _arm_watcher("proj", watcher_processes)
    before = dict(runs.watch_state("proj")["watcher"])

    assert runs.producer_live("proj") is True

    assert dict(runs.watch_state("proj")["watcher"]) == before


def test_producer_live_leaves_a_record_replaced_mid_check_alone(
    home, watcher_processes, monkeypatch
) -> None:
    """Erasure is best-effort: a record replaced between the read and the write
    must survive it rather than be clobbered back to empty. Content written by
    a concurrent arming or teardown after the confirming read is stood in for
    here by rewriting the file from inside the very ``process_alive`` call the
    check makes, landing squarely between the read that confirmed death and
    the erase's own re-read.
    """
    process = _arm_watcher("proj", watcher_processes)
    stale = dict(runs.watch_state("proj")["watcher"])
    process.terminate()
    process.wait(timeout=5)

    newer = dict(stale)
    newer["pid"] = os.getpid()
    newer["pid_start_time"] = runs._process_start_time(os.getpid())
    assert newer != stale

    real_process_alive = runs.process_alive

    def racing_process_alive(pid):
        _rewrite_seat_record("proj", newer)
        return real_process_alive(pid)

    monkeypatch.setattr(runs, "process_alive", racing_process_alive)

    assert runs.producer_live("proj") is False
    assert _read_seat_record("proj") == newer


def test_the_replacement_records_that_nothing_calls_it() -> None:
    """A helper may not claim a caller the package does not give it.

    Its account of itself and the source have to move together. While no module
    reaches ``replace_stale_watch_seat`` the docstring says so, and wiring one
    in fails this until that sentence is rewritten -- which is the moment to
    check that the condition it gates on can now become true.
    """
    package = Path(__file__).parents[1] / "reckon"
    definition = Path(runs.__file__).resolve()
    callers = sorted(
        path.resolve().relative_to(package.resolve()).as_posix()
        for path in package.rglob("*.py")
        if path.resolve() != definition
        and "replace_stale_watch_seat" in path.read_text()
    )
    doc = inspect.getdoc(runs.replace_stale_watch_seat) or ""
    records_the_gap = "Nothing calls it" in doc

    assert callers == []
    assert records_the_gap == (callers == []), (
        f"docstring records the gap: {records_the_gap}; callers found: {callers}"
    )
