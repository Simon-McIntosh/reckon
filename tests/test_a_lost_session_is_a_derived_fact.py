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
from reckon.crew import gc, lost_sessions, process_liveness, runs
from reckon.crew.admission import selection as _selection
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

    A process a test starts is seconds old, so the ten-day age the case names is
    put on it through the repository's own reader: the start instant the sweep
    would report for this pid is backdated, which is the same seam any
    process-age-keyed derivation reads. The process itself is real, it really
    carries the id, and its lease is really a minute old.
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


def _backdate(monkeypatch: pytest.MonkeyPatch, pid: int, days: float) -> float:
    """Make the repository's start reader report ``pid`` as ``days`` old."""

    started = NOW.timestamp() - days * 24 * HOUR

    def reported(target: Any) -> float | None:
        return started if int(target) == pid else None

    monkeypatch.setattr(gc, "_process_start_epoch", reported)
    return started


def _keyed_on_process_age(session: str, *, now: datetime) -> bool:
    """The derivation the plan names as the mutation, executed.

    It reads the same evidence a wrong implementation would read -- the start
    instant of the process carrying the session id -- through the repository's
    own reader, and compares that instant to the horizon.
    """

    carried = [entry for entry in Path("/proc").iterdir() if str(entry.name).isdigit()]
    for entry in carried:
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if session.encode() in raw.split(b"\0"):
            started = gc._process_start_epoch(int(entry.name))
            if started is not None:
                return (now.timestamp() - started) > 72 * HOUR
    return False


def test_a_ten_day_process_with_a_fresh_lease_reads_live(
    ten_day_process, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process's age is never read; only the instant the session was heard.

    The process carrying the id is backdated ten days through the repository's
    own start reader, and never restarted; its lease says a minute ago. Under
    that evidence the process-age derivation reads the session lost, while the
    derivation under test reads it live off the lease. The two answers differ on
    one fixture, which is what makes this case able to fail. The start readers
    are then made to raise, so a derivation that reaches for a process's age at
    all fails here rather than passing by accident.
    """

    _backdate(monkeypatch, ten_day_process.pid, 10)

    started = gc._process_start_epoch(ten_day_process.pid)
    assert started == NOW.timestamp() - 10 * 24 * HOUR

    assert _keyed_on_process_age(LONG_LIVED_SESSION, now=NOW) is True, (
        "the process-age derivation must read the ten-day process as lost, or "
        "this case could not fail on the regression it names"
    )

    answer = lost_sessions.session_lost(PROJECT, LONG_LIVED_SESSION, now=NOW)
    assert answer["lost"] is False, answer
    assert answer["last_heard_at"] == NOW.isoformat(), answer

    def never_read(*_arguments: Any, **_keywords: Any) -> float | None:
        raise AssertionError("the derivation read a process start time")

    monkeypatch.setattr(gc, "_process_start_epoch", never_read)
    monkeypatch.setattr(process_liveness, "_process_start_time", never_read)
    assert (
        lost_sessions.session_lost(PROJECT, LONG_LIVED_SESSION, now=NOW)["lost"]
        is False
    )


def test_the_derivation_carries_no_second_process_start_reader() -> None:
    """One reader for a process's start instant, and it is not this module's.

    A second copy drifts silently: the one this derivation first carried split
    the stat record on whitespace, so a command name holding a space shifted
    every later field and the instant read wrong for exactly the case its own
    docstring claimed to handle. The repository already owns the reader, so this
    module must not grow another.
    """

    assert not hasattr(lost_sessions, "process_started_at")
    source = Path(lost_sessions.__file__).read_text()
    assert "btime" not in source
    assert "SC_CLK_TCK" not in source


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
