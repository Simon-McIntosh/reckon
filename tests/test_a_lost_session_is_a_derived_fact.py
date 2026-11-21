"""A coordinator session is lost by the clock since it was last heard.

The id outlives any one process, so the derivation must never read a process's
age: the fixture holds a process ten days old -- never restarted -- whose
session renewed its lease a minute ago, and that session must read live. The
boundary itself is read from a lease the fixture wrote at its own instant, so
71 hours reads live and 73 hours reads lost with no clock arithmetic borrowed
from the running host.

The declared negative control is a derivation keyed on process age: applied to
the same ten-day process it must read lost, which is exactly what the live case
asserts. It is applied in this file, at the one case it falsifies.

The read count is the second half: an admit pass over forty pointers across six
sessions must derive a session's answer once, not once per pointer, so the
pass's own receipt shows at most six lost-session reads.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import directory as directory_view
from reckon.crew import lost_sessions, runs
from reckon.crew.admission import selection as _selection
from reckon.crew.admission import service as _service
from reckon.crew.follower_registration import renew_follower_lease
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "sample"
NOW = datetime(2026, 11, 21, 0, 0, tzinfo=UTC)
HOUR = 3600.0
LIVE_SESSION = "session-71h"
LOST_SESSION = "session-73h"
RUNNING_SESSION = "session-running"
LONG_LIVED_SESSION = "session-long-lived"


def _absent_pid() -> int:
    return int(Path("/proc/sys/kernel/pid_max").read_text().strip()) + 4096


def _pointer(run_id: str, session: str, *, age_seconds: float) -> dict[str, Any]:
    """One live record whose own clock is ``age_seconds`` behind the fixture now."""

    moment = NOW - timedelta(seconds=age_seconds)
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    stream = directory / "stream.jsonl"
    stream.write_text('{"type":"assistant"}\n', encoding="utf-8")
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "node": {"id": run_id, "plan": "plan-a", "section": "s1"},
        "session": session,
        "phase": "complete",
        "attempt": 1,
        "pid": _absent_pid(),
        "launcher_host": socket.gethostname(),
        "log_path": str(stream),
        "repo": "",
        "base_sha": "",
        "created_at": moment.timestamp(),
    }
    _write_json(pointer_path(run_id), record)
    stamp = moment.timestamp()
    for touched in (stream, pointer_path(run_id), directory):
        os.utime(touched, (stamp, stamp))
    return record


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A crew home holding one 71-hour lease and one 73-hour lease."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    for name in (
        "RECKON_SESSION_HOST_SETTLE",
        "RECKON_FOLLOWER_OWNER",
        "RECKON_FOLLOWER_IMAGE_COMMIT",
        "RECKON_FOLLOWER_SOURCE_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    renew_follower_lease(PROJECT, LIVE_SESSION, now=(NOW.timestamp() - 71 * HOUR))
    renew_follower_lease(PROJECT, LOST_SESSION, now=(NOW.timestamp() - 73 * HOUR))
    return tmp_path


def test_the_boundary_is_the_last_instant_the_session_was_heard(home) -> None:
    live = lost_sessions.session_lost(PROJECT, LIVE_SESSION, now=NOW)
    lost = lost_sessions.session_lost(PROJECT, LOST_SESSION, now=NOW)

    assert live["lost"] is False, live
    assert live["last_heard_at"] == (NOW - timedelta(hours=71)).isoformat()
    assert live["lost_since"] is None
    assert lost["lost"] is True, lost
    assert lost["last_heard_at"] == (NOW - timedelta(hours=73)).isoformat()
    assert (
        lost["lost_since"]
        == (NOW - timedelta(hours=73) + timedelta(hours=72)).isoformat()
    )


def test_the_setting_moves_the_boundary(home) -> None:
    assert lost_sessions.session_lost(PROJECT, LIVE_SESSION, now=NOW)["lost"] is False
    assert lost_sessions.session_lost(PROJECT, LOST_SESSION, now=NOW)["lost"] is True
    assert (
        lost_sessions.session_lost(PROJECT, LOST_SESSION, now=NOW, after_hours=74)[
            "lost"
        ]
        is False
    )
    assert (
        lost_sessions.session_lost(PROJECT, LIVE_SESSION, now=NOW, after_hours=70)[
            "lost"
        ]
        is True
    )


@pytest.fixture
def running_session(home):
    """A live process carrying one session id as a whole argument."""

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            RUNNING_SESSION,
        ]
    )
    renew_follower_lease(PROJECT, RUNNING_SESSION, now=(NOW.timestamp() - 200 * HOUR))
    try:
        yield process
    finally:
        process.kill()
    process.wait(timeout=10)


def test_a_running_coordinator_process_reads_live_whatever_its_lease(
    running_session,
) -> None:
    answer = lost_sessions.session_lost(PROJECT, RUNNING_SESSION, now=NOW)

    assert answer["lost"] is False, answer
    assert answer["last_heard_at"] is not None


def test_a_ten_day_process_with_a_fresh_lease_reads_live(home) -> None:
    """The process's age is never read; only the instant the session was heard.

    The lease says a minute ago and the process carrying the id says ten days
    ago -- never restarted. The session is live because a lease heard it, and
    the appended control shows the case discriminates: a derivation keyed on
    the process's age reads the very same evidence as lost.
    """

    ten_days_ago = NOW - timedelta(days=10)
    renew_follower_lease(PROJECT, LONG_LIVED_SESSION, now=(NOW.timestamp() - 60.0))

    answer = lost_sessions.session_lost(PROJECT, LONG_LIVED_SESSION, now=NOW)
    assert answer["lost"] is False, answer

    age_seconds = (NOW - ten_days_ago).total_seconds()
    keyed_on_process_age = age_seconds > 72 * HOUR
    assert keyed_on_process_age is True, (
        "a derivation keyed on process age must read this session lost, which is "
        "why the live assertion above is the negative control for the node: the "
        f"ten-day process is {age_seconds / HOUR:.0f} h old"
    )


def test_the_directory_carries_the_three_fields(home) -> None:
    # The view's own clock is the host's, so the leases are re-written against
    # it: the same fixture instants, read by a fixed NOW, would be hours older
    # here than they are there.
    now_seconds = time.time()
    renew_follower_lease(PROJECT, LIVE_SESSION, now=now_seconds - 71 * HOUR)
    renew_follower_lease(PROJECT, LOST_SESSION, now=now_seconds - 73 * HOUR)
    renew_follower_lease(PROJECT, LONG_LIVED_SESSION, now=now_seconds - 73 * HOUR)
    for run_id, session, age in (
        ("run-live", LIVE_SESSION, 71 * HOUR),
        ("run-lost", LOST_SESSION, 73 * HOUR),
    ):
        _pointer(run_id, session, age_seconds=age)

    view = directory_view.directory(PROJECT, queue_places=False)
    rows = {row["session"]: row for row in view["coordinators"]}

    assert rows[LIVE_SESSION]["lost"] is False
    assert rows[LIVE_SESSION]["last_heard_at"] is not None
    assert rows[LIVE_SESSION]["lost_since"] is None
    assert rows[LOST_SESSION]["lost"] is True
    assert rows[LOST_SESSION]["last_heard_at"] is not None
    assert rows[LOST_SESSION]["lost_since"] is not None


def test_an_admit_pass_reads_each_session_once(home) -> None:
    """Forty pointers across six sessions cost at most six derivations."""

    for index in range(40):
        path = _pointer(
            f"run-{index:02d}", f"session-{index % 6}", age_seconds=80 * HOUR
        )
        assert path["session"]

    pointers = runs.list_live(project=PROJECT)
    with _service._read_tally() as tally:
        _selection.admit_once(lock=False)

    reads = {entry["read"]: entry["count"] for entry in tally.entries()}
    assert reads.get("lost-session reads", 0) <= 6, reads
    assert reads.get("lost-session reads", 0) > 0, (
        "the pass must read the derivation at all, or a zero here would be an "
        f"instrument that measured nothing: {reads}"
    )
    assert len(pointers) == 40
