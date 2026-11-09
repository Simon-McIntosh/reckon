"""A follower's recovery sweep runs off its delivery thread, one at a time.

The sweep is the long call in the follower: it walks every registered worktree
and starts the launches it resumes, and each of those can block on a storage
wait. Run on the delivery thread it is paid by the reader, whose rows and next
tick queue behind it; joined against an arming's deadline it holds the arming
open for as long as the sweep walks. These tests pin the corrected behaviour
with a sweep that blocks on an event the test holds: rows keep arriving, the
loop keeps ticking, and a cadence that comes due during the held sweep starts
no second sweep.

The clock, the stream, the row collector and the stop are the cadence suite's
own harness, imported rather than restated: ``_Clock``, ``_line`` and
``_Follower`` from ``tests.test_follow_sweep_cadence`` drive a follower exactly
as those cases do. Two things are added here — a sweep the case holds until it
releases it, and an arming lifetime, so both branches the gate used to take are
driven through one driver.
"""

from __future__ import annotations

import threading

import pytest

from reckon import cli
from tests.test_follow_sweep_cadence import Follower_session, _Follower, _line

# How long the held sweep may block before the case gives up on it. Generous:
# a case that releases late is still a case that measured something, while one
# that never releases would otherwise hang the run.
SWEEP_HOLD_SECONDS = 10.0


class _HeldSweepFollower(_Follower):
    """The cadence suite's follower, with its sweep held on an event.

    ``_Follower`` already owns the temporary stream, the injected clock, the
    row collector and the stop; this adds the held sweep and the arming
    lifetime the cadence cases do not pass.
    """

    def __init__(self, tmp_path, monkeypatch, *, lifetime: float | None = None):
        super().__init__(tmp_path, monkeypatch, sweep=None)
        self._lifetime = lifetime
        self.started = threading.Event()
        self.release = threading.Event()

    def _sweep(self, project: str) -> dict:
        """The sweep entry point the follower calls, counted on every pass."""
        self.sweep_moments.append(self.clock())
        self.started.set()
        self.release.wait(SWEEP_HOLD_SECONDS)
        return {"resumed": [], "skipped": []}

    def start(self) -> _HeldSweepFollower:
        """Start the arming, handing the loop this case's lifetime.

        The parent's driver with one argument added, because a lifetime is the
        one thing its call site does not offer.
        """

        def reader() -> None:
            for event in cli._follow_watch_lines(
                self.project,
                session=Follower_session,
                poll_interval=self.POLL,
                sleeper=self._sleeper,
                stop=self.stop,
                sweep=self._sweep,
                sweep_interval=self.cadence,
                clock=self.clock,
                lifetime=self._lifetime,
            ):
                if event.get("event") in {"attached", "reattached"}:
                    continue
                self.emitted.append(event)

        self._thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()
        return self

    def stop_and_join(self, timeout: float = 15.0) -> None:
        # Release the held sweep first, or the follower's pass never returns
        # and the join is what the case measures instead of the guard.
        self.release.set()
        super().stop_and_join(timeout)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> None:
    """A temporary crew home, so the follower writes nothing of the operator's."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))


@pytest.mark.parametrize(
    "lifetime",
    [None, 600.0],
    ids=["no-arming-deadline", "arming-deadline"],
)
def test_a_held_sweep_leaves_the_delivery_thread_free(
    tmp_path, monkeypatch, crew_home, lifetime
) -> None:
    """Rows are delivered and the following tick completes while the sweep holds.

    The sweep does not return until the test releases it, so a follower that ran
    it on its delivery thread could neither print a row nor reach the tick that
    follows the gate. Both are asserted while it is held, on both branches the
    gate used to take: the fresh arming that ran the sweep inline and the arming
    with a deadline that joined it.
    """
    follower = _HeldSweepFollower(tmp_path, monkeypatch, lifetime=lifetime).start()
    try:
        assert follower.wait_sweeps(1) == 1, "the attach sweep starts"
        assert follower.started.is_set(), "the sweep is held inside its pass"

        follower.append(_line("r1"))
        assert follower.wait_emissions(1) == 1, (
            "a row must reach the pane while the sweep is still walking"
        )

        # A following tick: the loop reaches the cadence gate again and returns
        # from it, then reads the next line. On the delivery thread both the
        # gate and the read are behind the held sweep.
        follower.clock.advance(follower.CADENCE)
        follower.append(_line("r2"))
        assert follower.wait_emissions(2) == 2, (
            "the tick after a held sweep must complete and deliver"
        )
        assert follower.sweep_moments == [0.0], (
            "the held sweep is still the only one that started"
        )
    finally:
        follower.stop_and_join()


def test_a_cadence_during_a_held_sweep_starts_no_second_sweep(
    tmp_path, monkeypatch, crew_home
) -> None:
    """A cadence that comes due while a sweep walks starts nothing.

    ``swept_at`` is stamped when a sweep starts, so it says only when the last
    one began. A sweep can outlive its cadence, and the count here is taken on
    the sweep entry point the follower calls: the held pass stays the only one
    until it returns, and the cadence is honoured again after it does.
    """
    follower = _HeldSweepFollower(tmp_path, monkeypatch).start()
    try:
        assert follower.wait_sweeps(1) == 1, "the attach sweep starts"

        # Five cadences pass while the first sweep is still held. Each carries
        # a row, so the panes they pass through are rows this loop delivered
        # rather than ticks that never happened.
        for step in range(1, 6):
            follower.clock.advance(follower.CADENCE)
            follower.append(_line(f"r{step}"))
            assert follower.wait_emissions(step) == step, (
                "each cadence delivered its own row while the sweep was held"
            )

        assert follower.sweep_moments == [0.0], (
            "a cadence due during a held sweep must start no second sweep"
        )

        # The guard is a single flight, not a latch: once the held sweep
        # returns, a later cadence starts the next one.
        follower.release.set()
        assert follower.wait_sweeps(2) == 2, (
            "a cadence after the held sweep returns starts the next sweep"
        )
    finally:
        follower.stop_and_join()
