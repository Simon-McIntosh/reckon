"""The stale-producer notice names a remedy its reader's host can run.

A follower reads a project's seat from the shared home every node mounts, so it
routinely reads a seat armed on another node. The notice used to carry the cycle
pair -- ``reckon crew unwatch`` then ``watch --ensure`` -- for every seat alike.
``unwatch`` refuses a seat whose record names a foreign host, so a reader on
``98dci4-srv-1006`` following the advice for a seat on ``98dci4-clu-2002`` hit a
refusal and had no way to act on the line at all.

The fix keeps the cycle pair for a seat on the reader's own host, and for a
foreign seat names the host and says what is true of it: a producer runs this
follower's own image held as a seat, and that image re-executes in place when
the code stamp moves, so a foreign seat catches up on its own. The reader is
sent to no command their own ``unwatch`` would refuse.

Every case is isolated in a throwaway configuration home, and the seat is
planted on disk exactly as an external arming leaves it.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon.crew import runs

PROJECT = "proj"
SESSION = "session-a"
STEP = 1.0
STALE_STAMP = "0" * 64
OTHER_HOST = "98dci4-clu-2002"


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    return home


def _plant_seat(*, host: str, code_stamp: str) -> None:
    """Leave a seat record on disk, as an external arming on ``host`` would."""
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "pid": os.getpid(),
                "pid_start_time": runs._process_start_time(os.getpid()),
                "host": host,
                "stall_window": runs.DEFAULT_WATCH_STALL_WINDOW,
                "started_at": runs._utc_now(),
                "stream_path": str(runs.watch_stream_path(PROJECT)),
                "log_path": str(runs.watch_log_path(PROJECT)),
                "reckon_version": runs.__version__,
                "code_stamp": code_stamp,
            }
        )
    )


class _Clock:
    """A monotonic stand-in whose value only the wait passes advance."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _events(monkeypatch, *, stop_after=2) -> list[dict]:
    """Drive a fresh arming against a stale seat and return the events it yields.

    A fresh arming (``reloaded_in_place`` False) has no reload window to grant,
    so a mismatch is reported at once -- the path the reader acts on. The seat
    is judged by its recorded stamp rather than by a probe of this test's own
    process table (``producer_live`` pinned True), and the owner is pinned to
    this process so the arming does not end for a gone consumer while the
    driven clock advances.
    """
    clock = _Clock()
    stop = threading.Event()
    sleeps = {"count": 0}

    def sleeper(_seconds: float) -> None:
        sleeps["count"] += 1
        clock.value += STEP
        if sleeps["count"] >= stop_after:
            stop.set()

    monkeypatch.setattr(runs, "producer_live", lambda project: True)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )

    return list(
        cli_module._follow_watch_lines(
            PROJECT,
            session=SESSION,
            resume={"offset": 0, "reported": {}},
            reloaded_in_place=False,
            poll_interval=STEP,
            sleeper=sleeper,
            clock=clock,
            stop=stop,
            on_poll=None,
            sweep=None,
        )
    )


def _stale_event(events: list[dict]) -> dict:
    stale = [
        event
        for event in events
        if event.get("event") == cli_module.FOLLOWER_STALE_PRODUCER_EVENT
    ]
    assert stale, [event.get("event") for event in events]
    return stale[0]


def _unwatch_refusal(project: str) -> str:
    """Return the refusal ``unwatch`` gives for the planted seat, or empty.

    The command the notice must not print is the one this raises on: proving the
    refusal here is what makes "prints no command ``unwatch`` would refuse" a
    measured fact rather than an assertion about a string.
    """
    from reckon.crew import recovery
    from reckon.crew.node import CrewError

    try:
        recovery.unwatch(project)
    except CrewError as exc:
        return str(exc)
    return ""


def test_a_foreign_seat_names_the_host_and_the_producers_self_reload(
    isolated_home, monkeypatch
) -> None:
    """A seat on another host is named, with no command its reader cannot run.

    The producer reloads itself, so the line says it will catch up rather than
    telling the reader to cycle a seat their own ``unwatch`` refuses.
    """
    _plant_seat(host=OTHER_HOST, code_stamp=STALE_STAMP)
    event = _stale_event(_events(monkeypatch))

    assert "runs older code" in event["line"]
    assert OTHER_HOST in event["line"], event["line"]
    assert "reloads itself" in event["line"], event["line"]
    assert "cycle it with" not in event["line"], event["line"]
    assert "reckon crew unwatch" not in event["line"], event["line"]
    assert "remedy" not in event, "a foreign seat offers no command to act on"
    assert event["host"] == OTHER_HOST
    assert event["code_stamp"] == STALE_STAMP


def test_the_omitted_command_is_the_one_unwatch_refuses(
    isolated_home, monkeypatch
) -> None:
    """The cycle command would be refused here, so the notice rightly omits it.

    Without this, "prints no refused command" could not be distinguished from a
    string the notice merely happened to leave out.
    """
    _plant_seat(host=OTHER_HOST, code_stamp=STALE_STAMP)
    # Hold the seat lock as the foreign producer does: unwatch refuses a
    # foreign holder only where a holder is seen at all, so a record with no
    # lock behind it reads as no producer rather than as a refused seat.
    handle = runs.watch_lock_path(PROJECT).open("a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
        refusal = _unwatch_refusal(PROJECT)
    finally:
        handle.close()
    assert "refusing to unwatch" in refusal, refusal
    assert OTHER_HOST in refusal, refusal


def test_a_seat_on_the_readers_own_host_keeps_the_cycle_pair(
    isolated_home, monkeypatch
) -> None:
    """A local seat is cycled in place exactly as before the fix.

    The remedy for a seat this host issued is unchanged: the pair of commands
    ``watch_cycle_line`` composes, carried on the entity and in the line.
    """
    _plant_seat(host=socket.gethostname(), code_stamp=STALE_STAMP)
    event = _stale_event(_events(monkeypatch))

    expected = runs.watch_cycle_line(PROJECT)
    assert event["remedy"] == expected, event
    assert "cycle it with:" in event["line"], event["line"]
    assert expected in event["line"], event["line"]
    assert "host" not in event, "the cycle notice keeps its original shape"


def test_a_current_seat_says_nothing(isolated_home, monkeypatch) -> None:
    """A producer running this follower's own code is still not a subject."""
    _plant_seat(host=OTHER_HOST, code_stamp=runs.follower_code_stamp())
    kinds = [event.get("event") for event in _events(monkeypatch)]
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, kinds
