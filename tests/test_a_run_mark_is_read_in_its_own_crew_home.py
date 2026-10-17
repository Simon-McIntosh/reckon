"""A run's environment mark is read only within its own crew home.

``_processes_owning_run`` names every process of this user whose environment
carries ``RECKON_RUN_ID`` equal to a run id. Two doors read it: the environment
reach ``local_liveness`` falls back to for a reparented worker, and the stop
path that ends a run's whole process tree. Without a crew-home comparison a
child launched under one home's run id is read as the worker of another home's
run, so a liveness read counts it and a stop could signal it. The homes are
independent: nine test files use run ids of the form ``r-resume-*``, each under
its own temporary crew home, and one test's child was read as another test's
worker under ``-n``.

Every case starts a real, live child carrying a run id under a known
``RECKON_HOME`` and asks the doors which processes they name. The home a process
holds is resolved from its own environment by ``_config_home``, the one
resolver, so a trailing slash or a symlinked spelling of the same place is
still the same home.

The declared negative control removes the crew-home comparison from
``_processes_owning_run``: the scan must then name the child of the other home
and fail here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import _store
from reckon.crew import recovery_liveness, runs
from reckon.crew.process_signal import (
    RUN_ID_ENV,
    _processes_owning_run,
    _run_process_pids,
)
from tests import test_a_live_run_never_reads_dead as liveness

NEGATIVE_CONTROL_MUTATION = (
    "remove the crew-home comparison from _processes_owning_run; the scan "
    "must then name the child of the other home and fail here"
)

# Far longer than any single read waits; every case reaps the child it starts.
_CHILD_SLEEP_SECONDS = 300.0
_MARK_WAIT_SECONDS = 10.0


def _unique_run_id() -> str:
    """A run id no other process on this machine carries."""
    return f"r-envscan-{os.getpid()}-{os.urandom(6).hex()}"


def _start_child(run_id: str, home: str | None) -> subprocess.Popen:
    """A live child carrying ``run_id`` and, when given, ``RECKON_HOME``."""
    env = dict(os.environ)
    env.pop("RECKON_HOME", None)
    env[RUN_ID_ENV] = run_id
    if home is not None:
        env["RECKON_HOME"] = home
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({_CHILD_SLEEP_SECONDS})"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reap(child: subprocess.Popen) -> None:
    """Stop a child and wait for it, escalating only if it will not end."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def _named_for(run_id: str, home: Path) -> list[int]:
    """``_processes_owning_run`` for ``run_id`` against ``home``'s crew."""
    return _processes_owning_run(run_id, str(home / "crew"))


def _wait_for_mark(run_id: str, home: Path, pid: int) -> list[int]:
    """Wait until the scan names ``pid`` for ``home``, or return the last read.

    The positive control each assertion rests on: a child spawned but not yet
    executed is not yet readable through ``/proc``, so a scan that does not
    name it proves nothing. Waiting for the mark shows the instrument sees a
    child known to be present before any absence is believed.
    """
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    named = _named_for(run_id, home)
    while pid not in named and time.monotonic() < deadline:
        time.sleep(0.05)
        named = _named_for(run_id, home)
    return named


def _wait_for_both(run_id: str, home: Path, first: int, second: int) -> list[int]:
    """Wait until the scan names both pids for ``home``, or return the last read."""
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    named = _named_for(run_id, home)
    while not {first, second} <= set(named) and time.monotonic() < deadline:
        time.sleep(0.05)
        named = _named_for(run_id, home)
    return named


@pytest.fixture
def two_homes(
    tmp_path: Path,
) -> Iterator[tuple[str, Path, Path, subprocess.Popen, subprocess.Popen]]:
    """One run id carried by a live child in each of two crew homes."""
    run_id = _unique_run_id()
    home_one = tmp_path / "home-one"
    home_two = tmp_path / "home-two"
    home_one.mkdir()
    home_two.mkdir()
    child_one = _start_child(run_id, str(home_one))
    try:
        child_two = _start_child(run_id, str(home_two))
    except BaseException:
        _reap(child_one)
        raise
    try:
        assert child_one.pid in _wait_for_mark(run_id, home_one, child_one.pid)
        assert child_two.pid in _wait_for_mark(run_id, home_two, child_two.pid)
        yield run_id, home_one, home_two, child_one, child_two
    finally:
        _reap(child_one)
        _reap(child_two)


def test_the_scan_names_only_the_child_of_the_given_home(
    two_homes: tuple[str, Path, Path, subprocess.Popen, subprocess.Popen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's home decides the read, not the calling process's own.

    The calling process's own ``RECKON_HOME`` names the second home, and the
    run whose home is the first must still name only the first home's child.
    """
    run_id, home_one, home_two, child_one, child_two = two_homes
    monkeypatch.setenv("RECKON_HOME", str(home_two))

    named = _named_for(run_id, home_one)

    assert named == [child_one.pid]
    assert child_two.pid not in named


def test_the_stop_path_reaches_only_the_child_of_this_home(
    two_homes: tuple[str, Path, Path, subprocess.Popen, subprocess.Popen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop path inherits the narrowing: it ends only this home's process."""
    run_id, home_one, _home_two, child_one, child_two = two_homes
    monkeypatch.setenv("RECKON_HOME", str(home_one))

    reached = _run_process_pids(None, run_id=run_id, run_dir=None)

    assert child_one.pid in reached
    assert child_two.pid not in reached


def test_a_child_without_a_reckon_home_matches_the_resolved_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mark with no ``RECKON_HOME`` resolves to this process's fallback home.

    The child carries no ``RECKON_HOME``, so its home is whatever
    ``_config_home`` resolves without one — the XDG location when it exists.
    It is named for that home and for no other.
    """
    decoy = tmp_path / "operator-home"
    (decoy / ".config" / "reckon").mkdir(parents=True)
    monkeypatch.delenv("RECKON_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: decoy))

    resolved = _store._config_home()
    assert resolved == decoy / ".config" / "reckon"
    run_id = _unique_run_id()
    child = _start_child(run_id, None)
    try:
        assert _wait_for_mark(run_id, resolved, child.pid) == [child.pid]
        assert _processes_owning_run(run_id, str(tmp_path / "elsewhere" / "crew")) == []
    finally:
        _reap(child)


def test_a_child_without_a_reckon_home_falls_back_to_the_legacy_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no XDG home on disk the fallback is the legacy ``~/docs-server``."""
    decoy = tmp_path / "operator-home"
    decoy.mkdir()
    monkeypatch.delenv("RECKON_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: decoy))

    resolved = _store._config_home()
    assert resolved == decoy / "docs-server"
    run_id = _unique_run_id()
    child = _start_child(run_id, None)
    try:
        assert _wait_for_mark(run_id, resolved, child.pid) == [child.pid]
        assert _processes_owning_run(run_id, str(tmp_path / "elsewhere" / "crew")) == []
    finally:
        _reap(child)


def test_a_trailing_slash_and_a_symlinked_spelling_still_match(
    tmp_path: Path,
) -> None:
    """The same home spelled with a slash or through a symlink still matches."""
    home = tmp_path / "home"
    home.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(home, target_is_directory=True)
    run_id = _unique_run_id()
    trailing = _start_child(run_id, str(home) + "/")
    try:
        linked = _start_child(run_id, str(link))
    except BaseException:
        _reap(trailing)
        raise
    try:
        named = _wait_for_both(run_id, home, trailing.pid, linked.pid)
        assert set(named) == {trailing.pid, linked.pid}
    finally:
        _reap(trailing)
        _reap(linked)


def test_a_record_read_in_one_home_reads_dead_for_a_mark_in_another(
    two_homes: tuple[str, Path, Path, subprocess.Popen, subprocess.Popen],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose own pids are gone stays dead while only another home lives.

    The record names dead pids, the first home's child is reaped, and only the
    second home's child still carries the run id. Read from the first home the
    marker is not this run's worker, so the run gains no life it has not got.
    Without the home comparison the other home's child would be named and the
    run would read alive.
    """
    run_id, home_one, home_two, child_one, child_two = two_homes
    _reap(child_one)
    monkeypatch.setenv("RECKON_HOME", str(home_one))

    run_directory = runs.run_dir(run_id)
    run_directory.mkdir(parents=True, exist_ok=True)
    dead_pid = liveness._absent_pid()
    (run_directory / "worker.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": dead_pid,
                "pid_start_time": None,
                "launched_at": "2026-10-17T00:00:00Z",
            }
        )
    )
    record = {
        "run_id": run_id,
        "pid": dead_pid,
        "pid_start_time": None,
        "launcher_host": socket.gethostname(),
    }

    # The instrument still sees the second home's live child for its own home,
    # so the not-alive answer below is the narrowing at work rather than a scan
    # that has gone blind.
    assert _named_for(run_id, home_two) == [child_two.pid]

    alive, proven = recovery_liveness.local_liveness(record)
    assert alive is False, (alive, proven)
