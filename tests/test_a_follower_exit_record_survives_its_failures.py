"""A follower's exit record survives the two ways it can still be lost.

A record that only exists when everything else goes right is no record. Two
failures around the release were enough to erase it: restoring a displaced
SIGTERM handler that Python cannot name raises ``TypeError`` inside the
manager's ``finally``, skipping the release; and an unwritable crew home makes
the record write raise, replacing the follower's own reason with a crash.

Each case runs a real follower image under a temporary crew home, because both
failures sit in the manager's unwind path rather than in a helper a unit test
could call in isolation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import follower_registration

PROJECT = "exit-record-survives"
SESSION = "s-exit-record-survives"

REPO_ROOT = Path(__file__).resolve().parents[1]

# A follower image that takes one of the two failure paths. ``signal.signal``
# reads back ``None`` for a handler installed outside Python, and the real
# function refuses ``None`` on the way back, which is what the first driver
# reproduces: a fresh interpreter reports ``SIG_DFL``, so the state cannot be
# reached any other way.
_DRIVER = """
import signal
import sys

from reckon.crew import follower_registration as fr
from reckon.crew import runs
from reckon.crew.session_host import SETTLED_EXIT_CODE

mode, project, session = sys.argv[1], sys.argv[2], sys.argv[3]


def _unnameable_previous(signum, handler):
    if handler is None:
        raise TypeError(
            "signal handler must be signal.SIG_IGN, signal.SIG_DFL, or callable"
        )
    return None


if mode == "unrestorable-handler":
    signal.signal = _unnameable_previous
    with runs.follower_registration(project, session):
        sys.exit(SETTLED_EXIT_CODE)
elif mode == "record-write-fails":
    def _full_home(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    fr.write_json_atomically = _full_home
    with runs.follower_registration(project, session):
        sys.exit(SETTLED_EXIT_CODE)
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


def test_a_record_survives_a_handler_that_cannot_be_restored(home: Path) -> None:
    from reckon.crew.session_host import SETTLED_EXIT_CODE

    result = _run_follower(home, "unrestorable-handler")

    # The registration file is the positive control: this case would see "no
    # record" for the same reason an unused home would, so the home itself has
    # to be shown to have been used.
    assert _registration_file().exists(), result.stderr
    assert result.returncode == SETTLED_EXIT_CODE, result.stderr

    record = _exit_record(result.pid)
    assert record["reason"] == "settled"
    assert record["pid"] == result.pid


def test_a_follower_whose_record_write_fails_keeps_its_own_exit(home: Path) -> None:
    from reckon.crew.session_host import SETTLED_EXIT_CODE

    result = _run_follower(home, "record-write-fails")

    assert _registration_file().exists(), result.stderr
    # The follower's own reason is the exit, not the failed write.
    assert result.returncode == SETTLED_EXIT_CODE, result.stderr
    assert "exit record could not be written" in result.stderr
    assert not follower_registration.follower_exit_path(
        PROJECT, SESSION, result.pid
    ).exists(), "the failed write reported success"
