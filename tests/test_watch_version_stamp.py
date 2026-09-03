"""A watch producer must declare which code it is running.

A watcher imports its detection module once at startup and then runs for
hours, so a fix that lands after it started is inert on that seat and nothing
distinguishes it from a seat running current code. These tests assert that
the seat stamps its version and start time where a follower can read them,
and that the arming path replaces a seat whose stamp is stale or missing
rather than reusing it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
