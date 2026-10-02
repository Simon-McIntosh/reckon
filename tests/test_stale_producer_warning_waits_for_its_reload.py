"""A stale-producer warning waits for the producer's own reload window.

A producer polls its code stamp every ``runs.FOLLOWER_FRESHNESS_SECONDS`` and
re-executes in place when the stamp moves, so a follower that re-executes itself
onto new code often reads the seat before the producer has caught up. Warning
then sends an operator to cycle by hand a seat that would have caught up on its
own within seconds. The mismatch is therefore deferred for a reload window when
the follower has just reloaded -- the one case where it has positive evidence
the code moved moments ago -- and the cycle advice is owed only by a mismatch
that outlasts the window. A fresh arming, which never saw the code move, still
reports at once.

The deferral is driven with an injected clock, so the short-lag and long-lag
arms are decided by the wait passes the loop makes rather than by how fast the
test process happens to run.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import runs

PROJECT = "proj"
SESSION = "session-a"

# The window granted to the producer before the cycle advice is owed, and the
# step the driven clock advances per wait pass. A step equal to the window makes
# "inside the window" one pass and "past the window" the next.
WINDOW = 1.0
STEP = 1.0

STALE_STAMP = "0" * 64


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    return home


def _plant_seat(*, code_stamp: str) -> None:
    """Leave a seat record on disk, as an external arming would."""
    path = runs.watch_lock_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "pid": os.getpid(),
                "pid_start_time": runs._process_start_time(os.getpid()),
                "host": socket.gethostname(),
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


def _events(
    monkeypatch, *, catch_up_on=None, stop_after=3, resume=None, reloaded_in_place=True
) -> list[dict]:
    """Drive a reload attach and return the events it yields.

    ``producer_live`` is pinned True so the seat is judged by its recorded
    stamp rather than by a probe of this test's own process table, and the
    owner is pinned to this process so the arming does not end for a gone
    consumer while the driven clock advances. ``catch_up_on`` names the wait
    pass at which the seat is rewritten to the follower's current stamp,
    standing in for a producer that has reloaded itself inside its window.
    ``reloaded_in_place`` is the fact a real replacement image carries; passing
    it True with ``resume={}`` drives a reload whose checkpoint came through
    empty, which is still a reload.
    """
    clock = _Clock()
    stop = threading.Event()
    sleeps = {"count": 0}

    def sleeper(_seconds: float) -> None:
        sleeps["count"] += 1
        if catch_up_on is not None and sleeps["count"] == catch_up_on:
            _plant_seat(code_stamp=runs.follower_code_stamp())
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
            resume={"offset": 0, "reported": {}} if resume is None else resume,
            reloaded_in_place=reloaded_in_place,
            producer_reload_window=WINDOW,
            poll_interval=STEP,
            sleeper=sleeper,
            clock=clock,
            stop=stop,
            on_poll=None,
            sweep=None,
        )
    )


def _kinds(events: list[dict]) -> list[str]:
    return [str(event.get("event")) for event in events]


def test_a_mismatch_inside_the_window_shows_a_reloading_note_not_the_advice(
    isolated_home, monkeypatch
) -> None:
    """A seat that catches up within the window never earns the cycle advice.

    This is the incident: a producer mid-reload, reported as stale by a follower
    that has just reloaded onto the same new code. One line says the producer is
    reloading and no cycle advice is printed.
    """
    _plant_seat(code_stamp=STALE_STAMP)
    events = _events(monkeypatch, catch_up_on=1)

    kinds = _kinds(events)
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT in kinds, kinds
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, (
        "a producer that reloaded inside its window was reported as stale"
    )
    note = next(
        event
        for event in events
        if event.get("event") == cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT
    )
    assert "runs older code" in note["line"]
    assert "cycle it with" not in note["line"]
    assert "remedy" not in note, "a reloading note carries no remedy to act on"
    assert note["code_stamp"] == STALE_STAMP
    assert note["current_stamp"] == runs.follower_code_stamp()


def test_a_mismatch_past_the_window_shows_the_cycle_advice(
    isolated_home, monkeypatch
) -> None:
    """A seat that does not catch up past the window is reported as stale.

    The window is not a licence to stay silent: once it has passed with the
    mismatch still standing, the follower carries the cycle remedy exactly as
    the immediate path does.
    """
    _plant_seat(code_stamp=STALE_STAMP)
    events = _events(monkeypatch, catch_up_on=None)

    kinds = _kinds(events)
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT in kinds, kinds
    assert kinds.count(cli_module.FOLLOWER_STALE_PRODUCER_EVENT) == 1, kinds
    note_index = kinds.index(cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT)
    advise_index = kinds.index(cli_module.FOLLOWER_STALE_PRODUCER_EVENT)
    assert note_index < advise_index, "the reloading note precedes the advice"
    advice = events[advise_index]
    assert "runs older code" in advice["line"]
    assert advice["remedy"] == runs.watch_cycle_line(PROJECT)
    assert "cycle it with:" in advice["line"]
    assert advice["code_stamp"] == STALE_STAMP


def test_a_current_seat_says_nothing(isolated_home, monkeypatch) -> None:
    """A producer running this follower's own code is not a subject.

    The deferral must not turn a current seat into a note: the comparison's
    equality is what keeps the pane quiet when a producer is already current.
    """
    _plant_seat(code_stamp=runs.follower_code_stamp())
    events = _events(monkeypatch)

    kinds = _kinds(events)
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT not in kinds, kinds
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, kinds
    assert kinds.count(cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT) == 0


def test_the_default_window_covers_a_slow_reload() -> None:
    """The shipped window exceeds the worst case a healthy reload can take.

    A producer notices the code on its stamp poll and then proves the
    replacement importable before it re-executed, and it can be asleep for its
    idle poll interval cap in between. The shipped default must exceed both
    bounds, or a slow but healthy reload would still be reported as stale and
    cycled by hand. The mirrored cap is asserted against the producer's own so
    the derivation cannot drift from the figure it is built on.
    """
    from reckon.crew import recovery

    worst_case = (
        cli_module._FOLLOWER_RELOAD_PROBE_TIMEOUT
        + recovery.IDLE_POLL_INTERVAL_CAP_SECONDS
    )
    assert cli_module.PRODUCER_POLL_INTERVAL_CAP_SECONDS == (
        recovery.IDLE_POLL_INTERVAL_CAP_SECONDS
    ), "the mirrored poll interval cap has drifted from the producer's"
    assert worst_case < cli_module.PRODUCER_RELOAD_WINDOW_SECONDS, (
        "the default reload window must exceed a slow reload's worst case"
    )


def test_an_empty_checkpoint_reload_still_defers(isolated_home, monkeypatch) -> None:
    """A reload whose checkpoint came through empty still defers.

    The reloader marks a replacement by setting the checkpoint variable, and the
    checkpoint inside it can be empty. The deferral keys on the reload having
    happened, not on the checkpoint's contents, so the mismatch inside a
    producer's window is still shown as a reloading note rather than reported
    as stale at once.
    """
    _plant_seat(code_stamp=STALE_STAMP)
    events = _events(monkeypatch, catch_up_on=1, resume={})

    kinds = _kinds(events)
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT in kinds, kinds
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, (
        "an empty-checkpoint reload reported the producer as stale at once"
    )


def _reload_environment(monkeypatch, *, checkpoint=None) -> dict:
    """Run the reloader's own re-exec hand-off and return the environment it passes.

    A real ``_FollowerReloader`` for a non-seat follower composes the
    replacement's environment inside ``poll``. The import proof and ``os.execve``
    are stubbed so that composition runs without replacing this process, and the
    mapping the reloader would have handed to ``execve`` is returned. The stamp
    the constructor fixed is moved first, so ``poll`` reads a code gap and
    proceeds to the hand-off rather than deciding the code is already current.
    """
    captured: dict = {}
    monkeypatch.setattr(
        cli_module._FollowerReloader, "_replacement_imports", lambda self: (True, "")
    )
    monkeypatch.setattr(
        os, "execve", lambda path, argv, env: captured.update(env=dict(env))
    )
    reloader = cli_module._FollowerReloader(PROJECT, None, seat=False)
    reloader.code_stamp = STALE_STAMP
    reloader.poll({} if checkpoint is None else checkpoint)
    assert captured.get("env"), "the reloader never reached the image replacement"
    return captured["env"]


def _pin_follower_to_this_process(monkeypatch) -> None:
    """Keep the follower alive by naming this live test process as its owner."""
    monkeypatch.setattr(runs, "producer_live", lambda project: True)
    monkeypatch.setattr(
        runs,
        "follower_owner",
        lambda: (os.getpid(), runs._process_start_time(os.getpid())),
    )


def _follow_events() -> list[dict]:
    """Drive the real ``crew follow`` command and return the objects it emits.

    The command is invoked end to end through its click entry point, so the
    staleness deferral is decided by ``crew_follow`` reading its own environment
    rather than by a value a test injected. JSON mode is used so each line is one
    object whose ``event`` field can be inspected.
    """
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "follow",
            "--project",
            PROJECT,
            "--json",
            "--lifetime",
            "1s",
            "--no-color",
            "--width",
            "240",
        ],
    )
    assert result.exit_code == 0, result.output
    return [json.loads(line) for line in result.output.splitlines() if line.strip()]


def test_a_reload_handed_through_crew_follow_defers_the_mismatch(
    isolated_home, monkeypatch
) -> None:
    """A reload defers when the deferral is read by crew_follow's own wiring.

    The reloader marks a replacement by putting the checkpoint variable into the
    environment ``execve`` passes, and ``crew_follow`` reads whether that
    variable is present before it pops the checkpoint. This case drives the
    reloader's own hand-off into that computation and through the real command,
    so the deferral is exercised where it lives rather than by a flag the test
    handed the loop. The checkpoint it carries is empty, because the deferral
    keys on the reload having happened, not on the checkpoint's contents.
    """
    _plant_seat(code_stamp=STALE_STAMP)
    _pin_follower_to_this_process(monkeypatch)
    environment = _reload_environment(monkeypatch)
    # The replacement image is this process, handed exactly the mapping the
    # reloader composed for execve; only the checkpoint variable is adopted.
    monkeypatch.setenv(
        cli_module._FOLLOWER_CHECKPOINT_ENV,
        environment[cli_module._FOLLOWER_CHECKPOINT_ENV],
    )

    kinds = [event.get("event") for event in _follow_events()]
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT in kinds, kinds
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT not in kinds, (
        "crew_follow reported a reloaded follower's mismatch as stale at once"
    )


def test_a_fresh_arming_through_crew_follow_reports_the_mismatch_at_once(
    isolated_home, monkeypatch
) -> None:
    """A fresh arming has no reload to vouch for the mismatch, so it reports it.

    A follower that never replaced an image cannot say the stale producer is
    merely catching up, so the cycle advice is owed on the first mismatch. This
    is the opposite arm of the same computation: with no checkpoint variable in
    the environment, ``crew_follow`` computes a fresh arming and the advice
    follows at once rather than after a window.
    """
    _plant_seat(code_stamp=STALE_STAMP)
    _pin_follower_to_this_process(monkeypatch)
    monkeypatch.delenv(cli_module._FOLLOWER_CHECKPOINT_ENV, raising=False)

    kinds = [event.get("event") for event in _follow_events()]
    assert cli_module.FOLLOWER_STALE_PRODUCER_EVENT in kinds, kinds
    assert cli_module.FOLLOWER_PRODUCER_RELOADING_EVENT not in kinds, (
        "a fresh arming carried a reload's deferral"
    )
