"""The station's sweeps run under a fixed bound, so they never starve its tick.

Every session's sweep ran on a thread of its own, all on the station's one
interpreter. On 14 November about eighteen ran at once; the tick thread had about
one minute of CPU in thirty-eight and sat in one pass for over ten minutes, so
no session was woken by a transition in that time.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from reckon.crew import station

MOMENT = datetime(2026, 11, 14, 19, 0, tzinfo=UTC)


class _Sweeps:
    """A sweep that holds each pass until the case releases it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.passes: list[tuple[str, str, datetime]] = []
        self.release = threading.Event()
        self.finished = threading.Semaphore(0)

    def __call__(self, project, *, session, now, writer):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.passes.append((project, session, now))
        assert self.release.wait(10), "the sweep was never released"
        with self.lock:
            self.running -= 1
        self.finished.release()
        return {}


def _threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "station-sweep"]


def _await_passes(sweeps: _Sweeps, count: int) -> None:
    for _ in range(count):
        assert sweeps.finished.acquire(timeout=10), "a queued sweep never ran"


def test_no_more_than_the_bound_run_at_once_and_every_sweep_runs():
    sweeps = _Sweeps()
    beat = station.SweepBeat(sweep=sweeps, concurrency=2)
    before = len(_threads())
    for index in range(6):
        assert beat.request("p", f"s{index}", moment=MOMENT, changed=True)
    assert len(_threads()) - before <= 2
    assert len(beat.waiting()) == 4
    sweeps.release.set()
    _await_passes(sweeps, 6)
    assert sweeps.peak <= 2
    assert sorted(session for _, session, _ in sweeps.passes) == [
        f"s{index}" for index in range(6)
    ]
    assert beat.waiting() == []


def test_queued_sweeps_run_in_arrival_order():
    sweeps = _Sweeps()
    beat = station.SweepBeat(sweep=sweeps, concurrency=1)
    for name in ("first", "second", "third"):
        beat.request("p", name, moment=MOMENT, changed=True)
    assert beat.waiting() == [("p", "second"), ("p", "third")]
    sweeps.release.set()
    _await_passes(sweeps, 3)
    assert [session for _, session, _ in sweeps.passes] == [
        "first",
        "second",
        "third",
    ]


def test_a_change_to_a_queued_session_is_read_by_its_pass():
    sweeps = _Sweeps()
    beat = station.SweepBeat(sweep=sweeps, concurrency=1)
    later = MOMENT + timedelta(seconds=30)
    beat.request("p", "busy", moment=MOMENT, changed=True)
    beat.request("p", "queued", moment=MOMENT, changed=True)
    assert beat.request("p", "queued", moment=later, changed=True) is False
    sweeps.release.set()
    _await_passes(sweeps, 2)
    assert ("p", "queued", later) in sweeps.passes
    assert len(sweeps.passes) == 2


def test_a_change_during_a_running_pass_still_gets_its_follow_up():
    sweeps = _Sweeps()
    beat = station.SweepBeat(sweep=sweeps, concurrency=1)
    later = MOMENT + timedelta(seconds=30)
    beat.request("p", "busy", moment=MOMENT, changed=True)
    assert beat.request("p", "busy", moment=later, changed=True) is False
    sweeps.release.set()
    _await_passes(sweeps, 2)
    assert [now for _, _, now in sweeps.passes] == [MOMENT, later]


def test_the_default_bound_is_the_named_constant():
    assert station.SweepBeat().concurrency == station.SWEEP_CONCURRENCY
