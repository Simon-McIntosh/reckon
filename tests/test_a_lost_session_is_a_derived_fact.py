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
from types import SimpleNamespace
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


def _flight_config(monkeypatch: pytest.MonkeyPatch, **block: Any) -> None:
    """Put a lost-session block in front of the derivation's flight read."""

    monkeypatch.setattr(
        lost_sessions.flight,
        "resolve",
        lambda: SimpleNamespace(config={"lost_session": dict(block)}),
    )


def test_the_boundary_comes_from_the_flight_layer(
    home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The threshold is read from the flight layer, not baked into the derivation.

    A derivation that stopped reading the layer and always returned the shipped
    default would pass every other case here, so this one moves the layer and
    requires the answer to move with it: 74 hours keeps the 73-hour lease live,
    70 hours makes the 71-hour lease lost, and zero is honoured as declared
    rather than folded back to the default.
    """

    assert lost_sessions.lost_session_after_hours() == 72.0
    assert lost_sessions.session_lost(PROJECT, LOST_SESSION, now=NOW)["lost"] is True

    _flight_config(monkeypatch, after_hours=74)
    assert lost_sessions.lost_session_after_hours() == 74.0
    assert lost_sessions.session_lost(PROJECT, LOST_SESSION, now=NOW)["lost"] is False

    _flight_config(monkeypatch, after_hours=70)
    assert lost_sessions.session_lost(PROJECT, LIVE_SESSION, now=NOW)["lost"] is True

    _flight_config(monkeypatch, after_hours=0)
    assert lost_sessions.lost_session_after_hours() == 0.0
    assert lost_sessions.session_lost(PROJECT, LIVE_SESSION, now=NOW)["lost"] is True


def test_a_broken_layer_falls_back_to_the_shipped_default(
    home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value the schema cannot hold must not silently change the horizon."""

    for broken in (-1, "72", True, None):
        _flight_config(monkeypatch, after_hours=broken)
        assert lost_sessions.lost_session_after_hours() == 72.0, broken


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


@pytest.fixture
def ten_day_process(home):
    """A real process carrying the long-lived session id, lease a minute old.

    A process in a test cannot actually be ten days old, so the start instant a
    process-age derivation would read is substituted where it is read: see
    ``test_a_ten_day_process_with_a_fresh_lease_reads_live``.
    """

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            LONG_LIVED_SESSION,
        ]
    )
    renew_follower_lease(PROJECT, LONG_LIVED_SESSION, now=(NOW.timestamp() - 60.0))
    try:
        yield process
    finally:
        process.kill()
    process.wait(timeout=10)


def _keyed_on_process_age(
    pid: int, *, now: datetime, horizon_hours: float = 72.0
) -> bool:
    """The declared negative control, executed: lost iff the process is old.

    This is the derivation the plan names as the mutation.

    A derivation keyed on process age reads the start instant of the process
    carrying the session id and compares that to the horizon. It is run here
    rather than described, so the control exercises the same evidence the real
    derivation would have to read to be wrong in the way the case forbids.
    """

    started = lost_sessions.process_started_at(pid)
    if started is None:
        return False
    return (now.timestamp() - started) > horizon_hours * HOUR


def test_a_ten_day_process_with_a_fresh_lease_reads_live(
    ten_day_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process's age is never read; only the instant the session was heard.

    The lease says a minute ago and the process carrying the id started ten days
    ago -- never restarted. The session is live because it was heard, and the
    control beside it shows why that assertion discriminates: the same evidence,
    read as a process's age, reads lost. The control is executed, not asserted
    about its own arithmetic, and the second half forbids the real derivation
    from reading a process start time at all.
    """

    started = NOW.timestamp() - 10 * 24 * HOUR
    monkeypatch.setattr(
        lost_sessions,
        "process_started_at",
        lambda pid, **_kwargs: started if pid == ten_day_process.pid else None,
    )

    assert _keyed_on_process_age(ten_day_process.pid, now=NOW) is True, (
        "the negative control must read the ten-day process as lost, or this "
        "case could not fail on the regression it names"
    )

    # The derivation under test must still read the session live, and must do so
    # without touching the start time: any read of it is a failure here.
    def never_read(*_arguments: Any, **_keywords: Any) -> float | None:
        raise AssertionError("the derivation read a process start time")

    monkeypatch.setattr(lost_sessions, "process_started_at", never_read)
    answer = lost_sessions.session_lost(PROJECT, LONG_LIVED_SESSION, now=NOW)

    assert answer["lost"] is False, answer
    assert answer["last_heard_at"] == NOW.isoformat(), answer


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
    result = _selection.admit_once(lock=False)

    assert len(result["lost_sessions"]) == 6
    receipt = {
        str(entry["read"]): int(entry["count"])
        for entry in result["reads"]["lost sessions"]
    }
    assert receipt.get("lost-session reads", 0) <= 6, result["reads"]
    assert receipt.get("lost-session reads", 0) > 0, (
        "the pass's own receipt must carry the derivation, or a zero here would "
        f"be an instrument that measured nothing: {result['reads']}"
    )
    assert len(pointers) == 40
