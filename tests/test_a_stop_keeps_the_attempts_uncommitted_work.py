"""A stop keeps the work its attempt had not committed yet.

``crew stop`` ends a run's process tree, and until it kept the tree it left the
attempt's uncommitted edit sitting in the worktree with no record of whose it
was: the next attempt committed it as its own work. The diff is written to the
run directory before anything is signalled, so the stop both ends the attempt
and keeps what that attempt had not yet committed.

Every case runs against a temporary git repository and a temporary crew home,
with a real stand-in worker process for the stop to end. The stand-in deletes
the file the attempt had modified when it receives SIGTERM, which is what makes
the timing observable: a read taken after the signal no longer sees the edit, so
a stop that saved its diff late would keep a deletion of that file instead of
the work in it. That is the declared negative control.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon import _store, crew
from reckon.crew import runs
from reckon.crew.dispatch_sessions import terminate
from reckon.crew.process_liveness import _process_start_time
from reckon.crew.process_signal import RUN_ID_ENV

NEGATIVE_CONTROL_MUTATION = (
    "writing the diff after the signal loses the stand-in's deleted file"
)

PROJECT = "sample"

# A stand-in that the stop failed to end is reaped here rather than inherited,
# so no case leaves a process behind.
_MARK_WAIT_SECONDS = 10.0

# The stand-in worker: it carries the run's environment mark, signals readiness
# outside the worktree so it dirties nothing, sleeps until it is signalled, and
# deletes the named file on SIGTERM so the moment of the read is observable in
# what the saved diff contains.
_STAND_IN = """
import os, signal, sys, time

_target, _ready = sys.argv[1], sys.argv[2]

def _end(_signum, _frame):
    if _target and os.path.exists(_target):
        os.unlink(_target)
    os._exit(0)

signal.signal(signal.SIGTERM, _end)
with open(_ready, "w", encoding="utf-8") as _handle:
    _handle.write("ready")
while True:
    time.sleep(0.05)
"""


def _unique_run_id() -> str:
    """A run id no other run on this machine or in this home carries."""
    return f"r-stop-work-{os.getpid()}-{os.urandom(6).hex()}"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _operator_crew_home() -> Path:
    """The crew home this machine really runs against, whatever a test set."""
    saved = os.environ.pop("RECKON_HOME", None)
    try:
        return _store._config_home() / "crew"
    finally:
        if saved is not None:
            os.environ["RECKON_HOME"] = saved


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8")


def _repository(tmp_path: Path) -> Path:
    """A committed, clean git repository to stop an attempt inside.

    The identity is set on the repository itself so the fixture works on a host
    with no global git configuration.
    """
    repository = tmp_path / "worktree"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    (repository / "tracked.txt").write_text("original\n", encoding="utf-8")
    (repository / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt", "kept.txt")
    _git(repository, "commit", "-q", "-m", "base")
    return repository


def _start_stand_in(
    repository: Path,
    *,
    run_id: str,
    home: Path,
    tmp_path: Path,
    deletes: Path | None,
) -> subprocess.Popen:
    """A live worker process for the stop to end, marked with the run's id."""
    ready = tmp_path / f"stand-in-ready-{os.getpid()}-{os.urandom(4).hex()}"
    environment = dict(os.environ)
    environment[RUN_ID_ENV] = run_id
    environment["RECKON_HOME"] = str(home)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _STAND_IN,
            str(deletes) if deletes is not None else "",
            str(ready),
        ],
        cwd=str(repository),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Its own session, so the stop's group signal reaches it and never this
        # test process.
        start_new_session=True,
    )
    deadline = time.monotonic() + _MARK_WAIT_SECONDS
    while not ready.exists() and time.monotonic() < deadline:
        if child.poll() is not None:
            break
        time.sleep(0.02)
    return child


def _reap(child: subprocess.Popen) -> None:
    """Stop a stand-in and wait for it, escalating only if it will not end."""
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def _live_record(run_id: str, worktree: Path, child: subprocess.Popen) -> dict:
    """The live pointer of a run whose recorded pid is the stand-in."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "attempt": 1,
        "phase": "working",
        "worktree": str(worktree),
        "pid": child.pid,
        "pid_start_time": _process_start_time(child.pid),
    }


def _park(record: dict) -> None:
    """Publish the run's live pointer where ``terminate`` reads it."""
    crew._write_json(crew.pointer_path(record["run_id"]), record)


def _run_directory(run_id: str) -> Path:
    """The run directory the stop writes its kept work into."""
    return runs.run_dir(run_id)


def test_a_dirty_tree_is_saved_before_the_signal(home: Path, tmp_path: Path) -> None:
    """The attempt's uncommitted work is in the run directory before the stop."""
    run_id = _unique_run_id()
    repository = _repository(tmp_path)
    # The tracked file the attempt edited is the one the stand-in deletes on
    # SIGTERM, so a diff read after the signal would record a deletion instead.
    (repository / "tracked.txt").write_text(
        "original\nattempt work\n", encoding="utf-8"
    )
    (repository / "attempt-notes.txt").write_text("a note\n", encoding="utf-8")
    child = _start_stand_in(
        repository,
        run_id=run_id,
        home=home,
        tmp_path=tmp_path,
        deletes=repository / "tracked.txt",
    )
    try:
        record = _live_record(run_id, repository, child)
        _park(record)

        result = terminate(run_id)

        assert child.wait(timeout=10) == 0
        work = result["stopped_attempt_work"]
        assert work["state"] == "saved"
        saved = Path(work["path"])
        assert saved.is_relative_to(_run_directory(run_id))
        assert saved.name == "stopped-attempt-1.diff"
        text = saved.read_text(encoding="utf-8")
        assert "+attempt work" in text
        assert "deleted file mode" not in text
        assert "attempt-notes.txt" in text
        # The path is in the result the command prints and in the record read
        # back from disk, so neither has to be opened to find the work.
        assert str(saved) in result["detail"]
        assert runs.read_pointer(run_id)["stopped_attempt_work"]["path"] == str(saved)
        operator = _operator_crew_home()
        assert not (operator / "live" / f"{run_id}.json").exists()
        assert not (operator / "runs" / run_id).exists()
    finally:
        _reap(child)


def test_a_clean_tree_writes_no_diff_and_says_so(home: Path, tmp_path: Path) -> None:
    """Nothing to keep writes no file, and the result says the tree was clean."""
    run_id = _unique_run_id()
    repository = _repository(tmp_path)
    child = _start_stand_in(
        repository, run_id=run_id, home=home, tmp_path=tmp_path, deletes=None
    )
    try:
        record = _live_record(run_id, repository, child)
        _park(record)

        result = terminate(run_id)

        assert child.poll() is not None  # the stop still ended it
        work = result["stopped_attempt_work"]
        assert work["state"] == "clean"
        assert work["path"] is None
        assert "no uncommitted changes" in work["detail"]
        assert "no uncommitted changes" in result["detail"]
        assert not list(_run_directory(run_id).glob("stopped-attempt-*.diff"))
    finally:
        _reap(child)


def test_an_unreadable_worktree_is_named_and_the_run_still_ends(
    home: Path, tmp_path: Path
) -> None:
    """A worktree that cannot be read is named, and the run still ends."""
    run_id = _unique_run_id()
    repository = _repository(tmp_path)
    missing = tmp_path / "moved-away-worktree"
    child = _start_stand_in(
        repository, run_id=run_id, home=home, tmp_path=tmp_path, deletes=None
    )
    try:
        record = _live_record(run_id, missing, child)
        _park(record)

        result = terminate(run_id)

        assert child.poll() is not None
        work = result["stopped_attempt_work"]
        assert work["state"] == "unreadable"
        assert work["worktree"] == str(missing)
        assert str(missing) in work["detail"]
        assert "the stop proceeded" in work["detail"]
        assert "the stop proceeded" in result["detail"]
        assert not list(_run_directory(run_id).glob("stopped-attempt-*.diff"))
    finally:
        _reap(child)
