"""A missing snapshot beside a live foreign seat names that host, not a producer.

A session's snapshot is published only while that session has a live follower,
so a session that never armed one has no snapshot at all. When the producer
runs on another host and its seat names that host with a fresh stream, the
absence is this session's, not the producer's: the not-current line must say so,
name the host the seat is on, and offer this session's follower arming. The old
line read "no producer" and offered ``watch --ensure``, a command that arms a
watcher on this host -- where the seat is held elsewhere, so it changes nothing.

Every case builds a temporary configuration home: a mount for the fixture
repository, a registered session, no snapshot, and a seat record naming another
host whose transition stream is fresh or stalled. The drives run as subprocesses
against that home, so no case reads or writes the operator's own state.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, watch_unit
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "missing-snapshot-fixture"
SESSION = "missing-snapshot-session"
FOREIGN_HOST = "seat-elsewhere.invalid"
FOLLOW_REMEDY = f"reckon crew follow --project {PROJECT} --session {SESSION}"
ENSURE_REMEDY = f"reckon crew watch --ensure --project {PROJECT}"
STALL_WINDOW = "10m"

# A pid no process can hold, so the drive's session resolves through the
# registration's own recorded name rather than an ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed missing snapshot fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _register_the_session() -> None:
    """Register the session so the hook resolves it by the name it registered.

    The registration is released immediately, which is the shape a session that
    never armed a follower holds: the name is on record, so the hook answers for
    it, but nothing is publishing a snapshot for it.
    """
    with runs.follower_registration(PROJECT, SESSION):
        pass


def _write_seat(*, host: str) -> None:
    """Record a seat naming ``host`` as the project's producer's."""
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "host": host,
                "pid": 1,
                "stall_window": STALL_WINDOW,
            }
        ),
        encoding="utf-8",
    )


def _touch_stream(*, age_seconds: float) -> None:
    """Create the seat's transition stream and stamp it ``age_seconds`` back."""
    path = runs.watch_stream_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    written = time.time() - age_seconds
    os.utime(path, (written, written))


def _write_session_host(config_home: Path, *, host_pid: int) -> None:
    owner_pid = os.getpid()
    start = obligation_snapshot.process_start_time(owner_pid)
    assert start is not None
    path = config_home / "crew" / "session-hosts" / f"{owner_pid}-{start}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"owner": {"pid": owner_pid, "start_time": start}, "pid": host_pid}),
        encoding="utf-8",
    )


def _drive(
    repository: Path, *, owner_pid: int = _ABSENT_HARNESS_PID
) -> subprocess.CompletedProcess[str]:
    """Run the prompt hook as the harness does, against the temporary home."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(owner_pid)
    payload = {
        "session_id": SESSION,
        "cwd": str(repository),
        "hook_event_name": "UserPromptSubmit",
    }
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _line(completed: subprocess.CompletedProcess[str]) -> str:
    """The single line one drive emitted."""
    assert completed.returncode == 0
    assert completed.stderr == "", completed.stderr
    text = json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]
    assert text.strip(), "a not-fresh drive must speak its line"
    assert "\n" not in text, text
    return text


def test_a_missing_snapshot_beside_a_live_foreign_seat_names_the_host(
    config_home: Path, repository: Path
) -> None:
    """A session with no snapshot, beside a seat held elsewhere, says where.

    The producer stands on another host whose stream is moving, and this session
    has no snapshot because it has no follower, so the line must name that host
    and offer this session's follower arming. The old reading -- "no producer"
    with ``watch --ensure`` -- is wrong here: it names an absence where a
    producer is running, and offers a command that changes nothing on this host.
    The reading is asserted positively as well, so the case can never pass by
    falling into the no-producer branch.
    """
    _register_the_session()
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=0)

    line = _line(_drive(repository))

    assert FOREIGN_HOST in line, line
    assert FOLLOW_REMEDY in line, line
    assert "no producer" not in line, line
    assert "watch --ensure" not in line, line


def test_a_missing_snapshot_names_its_live_session_host(
    config_home: Path, repository: Path
) -> None:
    _register_the_session()
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=0)
    _write_session_host(config_home, host_pid=os.getpid())

    line = _line(_drive(repository, owner_pid=os.getpid()))

    assert FOREIGN_HOST in line, line
    assert "session host" in line, line
    assert "re-arms this session's follower with the session's next dispatch" in line, (
        line
    )
    assert "reckon crew follow" not in line, line


def test_a_missing_snapshot_ignores_a_dead_session_host(
    config_home: Path, repository: Path
) -> None:
    _register_the_session()
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=0)
    _write_session_host(config_home, host_pid=_ABSENT_HARNESS_PID)

    line = _line(_drive(repository, owner_pid=os.getpid()))

    assert FOLLOW_REMEDY in line, line


def test_a_missing_snapshot_with_no_seat_reads_no_producer(
    config_home: Path, repository: Path
) -> None:
    """Absent snapshot and no foreign seat names no producer, as it always did."""
    _register_the_session()

    line = _line(_drive(repository))

    assert "no producer" in line, line
    assert ENSURE_REMEDY in line, line
    assert "crew follow" not in line, line


def test_a_missing_snapshot_beside_a_stalled_seat_reads_no_producer(
    config_home: Path, repository: Path
) -> None:
    """A foreign seat whose stream has stalled names no live producer."""
    _register_the_session()
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=3_600)

    line = _line(_drive(repository))

    assert "no producer" in line, line
    assert ENSURE_REMEDY in line, line
    assert FOREIGN_HOST not in line, line
    assert "crew follow" not in line, line


def test_the_seat_host_reader_agrees_with_the_boolean_judgement(
    config_home: Path, repository: Path
) -> None:
    """One judgement: the host reader names the seat, and the boolean tests it.

    ``seat_names_a_live_foreign_producer`` is the boolean wrap of
    :func:`watch_unit.live_foreign_seat_host`, so a live foreign seat must make
    the host reader name its host and the boolean true together, and a stalled
    one must empty the host and read false together.
    """
    assert socket.gethostname() != FOREIGN_HOST
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=0)
    assert watch_unit.live_foreign_seat_host(PROJECT) == FOREIGN_HOST
    assert watch_unit.seat_names_a_live_foreign_producer(PROJECT) is True

    _touch_stream(age_seconds=3_600)
    assert watch_unit.live_foreign_seat_host(PROJECT) == ""
    assert watch_unit.seat_names_a_live_foreign_producer(PROJECT) is False


def test_the_host_named_is_the_seats_own_host(
    config_home: Path, repository: Path
) -> None:
    """The line names the seat's recorded host, whatever it is.

    Pinning the host to a second value keeps the case from passing against a
    line that names this host, which is the reading the seat judgement exists to
    rule out.
    """
    _register_the_session()
    _write_seat(host=FOREIGN_HOST)
    _touch_stream(age_seconds=0)

    line = _line(_drive(repository))

    assert re.search(re.escape(FOREIGN_HOST), line), line
    assert socket.gethostname() not in line, line
