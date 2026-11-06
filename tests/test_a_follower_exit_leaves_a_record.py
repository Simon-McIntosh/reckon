"""A follower that exits leaves a record of why.

A follower used to end silently: a settle, a termination signal and a crash all
left the same thing on disk, which was nothing. A reader could not tell a
deliberate end from a kill, so a pane that stopped was indistinguishable from
one that settled.

Each case runs a real follower image under a temporary crew home, because the
reason is derived from the exception that unwinds that image's loop. A child is
used for the signal case in particular: with no handler installed, SIGTERM
would kill the test process rather than the follower it names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from reckon.crew import follower_registration

PROJECT = "exit-record-proj"
SESSION = "s-exit-record"

REPO_ROOT = Path(__file__).resolve().parents[1]

# A follower image that takes one of the paths this module records. The
# station arm drives the branch a station-attached pane takes, so the two
# delivery paths are measured against the same registration release.
_DRIVER = """
import os
import signal
import sys

from reckon.crew import runs
from reckon.crew.session_host import SETTLED_EXIT_CODE
from reckon.crew_follow_commands import _StationRegistration

mode, project, session = sys.argv[1], sys.argv[2], sys.argv[3]

if mode == "settled":
    with runs.follower_registration(project, session):
        sys.exit(SETTLED_EXIT_CODE)
elif mode == "raise":
    with runs.follower_registration(project, session):
        raise RuntimeError("the follower loop failed")
elif mode == "sigterm":
    with runs.follower_registration(project, session):
        os.kill(os.getpid(), signal.SIGTERM)
elif mode == "station-settled":
    class _Subscriber:
        persist = False

        def register(self, **_kwargs):
            pass

        def detach(self):
            pass

    registration = _StationRegistration(_Subscriber(), project=project, session=session)
    registration.acquire()
    try:
        sys.exit(SETTLED_EXIT_CODE)
    finally:
        registration.release()
else:
    raise SystemExit(f"unknown mode {mode}")
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


class _FollowerRun:
    """One follower image's exit, with the pid its record is named for."""

    def __init__(self, pid: int, returncode: int, stderr: str) -> None:
        self.pid = pid
        self.returncode = returncode
        self.stderr = stderr


def _run_follower(home: Path, mode: str) -> _FollowerRun:
    env = dict(os.environ)
    env["RECKON_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO_ROOT)
    process = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, mode, PROJECT, SESSION],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _out, err = process.communicate(timeout=120)
    return _FollowerRun(process.pid, process.returncode, err or "")


def _registration_file() -> Path:
    return follower_registration.follower_lock_path(PROJECT, SESSION)


def _exit_record(pid: int) -> dict:
    path = follower_registration.follower_exit_path(PROJECT, SESSION, pid)
    assert path.exists(), f"no exit record at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_follower_that_settles_leaves_a_record(home: Path) -> None:
    from reckon.crew.session_host import SETTLED_EXIT_CODE

    result = _run_follower(home, "settled")

    # The registration file is the positive control: this case would see "no
    # record" for the same reason a broken home would, so the home itself has
    # to be shown to have been used.
    assert _registration_file().exists(), result.stderr
    assert result.returncode == SETTLED_EXIT_CODE, result.stderr

    record = _exit_record(result.pid)
    assert record["reason"] == "settled"
    assert record["pid"] == result.pid
    assert record["project"] == PROJECT
    assert record["session"] == SESSION
    datetime.fromisoformat(record["exited_at"])
    assert record["host"]


def test_a_follower_ended_by_sigterm_names_the_signal(home: Path) -> None:
    result = _run_follower(home, "sigterm")

    assert _registration_file().exists(), result.stderr
    record = _exit_record(result.pid)
    assert record["reason"] == "SIGTERM"
    assert record["pid"] == result.pid


def test_a_follower_whose_loop_raises_names_the_exception(home: Path) -> None:
    result = _run_follower(home, "raise")

    assert _registration_file().exists(), result.stderr
    record = _exit_record(result.pid)
    assert record["reason"] == "RuntimeError"
    assert record["pid"] == result.pid


def test_a_station_attached_follower_that_settles_leaves_a_record(home: Path) -> None:
    from reckon.crew.session_host import SETTLED_EXIT_CODE

    result = _run_follower(home, "station-settled")

    assert _registration_file().exists(), result.stderr
    assert result.returncode == SETTLED_EXIT_CODE, result.stderr
    record = _exit_record(result.pid)
    assert record["reason"] == "settled"
    assert record["pid"] == result.pid
