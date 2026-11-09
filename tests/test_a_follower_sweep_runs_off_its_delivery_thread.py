"""A follower's recovery sweep runs off its delivery thread, one at a time.

The sweep is the long call in the follower: it walks every registered worktree
and starts the launches it resumes, and each of those can block on a storage
wait. Run on the delivery thread it is paid by the reader, whose rows and next
tick queue behind it; joined against an arming's deadline it holds the arming
open for as long as the sweep walks. These tests pin the corrected behaviour
with a sweep that blocks on an event the test holds: rows keep arriving, the
loop keeps ticking, and a cadence that comes due during the held sweep starts
no second sweep.

The clock is the test's own and the sleeper is a no-op, so a pass costs no wall
time and the cadence boundaries are exact. The stream is a plain temporary
file and the crew home is a temporary directory, so nothing outside the test's
own tree is read or written.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import runs

FOLLOWER_SESSION = "s1"
PROJECT = "proj"


class _Clock:
    """A monotonic stand-in the test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _line(run_id: str) -> str:
    """One carried transition line: a limit reset reaches the pane as a row.

    What the row says does not matter here. It is a line the follower's own row
    path delivers, so it is a delivery the sweep can either hold back or not.
    """
    return (
        json.dumps(
            {
                "run_id": run_id,
                "from_state": "working",
                "to_state": "limit-reset",
                "session": FOLLOWER_SESSION,
                "node": "n1",
                "event": "transition",
                "working": 1,
                "blocked": 0,
                "unpromoted": 0,
            }
        )
        + "\n"
    )


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, so the follower writes nothing of the operator's."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


class _Follower:
    """Drive one follower against an injected stream, clock, and sweep.

    The follower blocks reading the stream, so it runs in its own thread. The
    test hands it lines and time, and holds the sweep on an event when a case
    needs a sweep that does not return.
    """

    CADENCE = 10.0
    POLL = 0.01

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        held: Callable[[str], dict] | None = None,
        lifetime: float | None = None,
    ) -> None:
        self.clock = _Clock(0.0)
        self.stop = threading.Event()
        self.emitted: list[dict] = []
        self.sweep_moments: list[float] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.stream_path = tmp_path / "watch-stream.jsonl"
        self.stream_path.touch()
        self._held = held
        self._lifetime = lifetime
        self._thread: threading.Thread | None = None

        monkeypatch.setattr(runs, "producer_live", lambda name: name == PROJECT)
        monkeypatch.setattr(
            runs,
            "watch_stream_cursor",
            lambda name: {
                "stream_path": str(self.stream_path),
                "offset": 0,
                "baseline": [],
                "producer": {},
            },
        )

    def _sleeper(self, seconds: float) -> None:
        # The test owns time; a real sleep would let the loop pace itself.
        return None

    def _sweep(self, project: str) -> dict:
        """The sweep entry point the follower calls, counted on every pass."""
        self.sweep_moments.append(self.clock())
        self.started.set()
        if self._held is not None:
            self.release.wait(10.0)
            return self._held(project)
        return {"resumed": [], "skipped": []}

    def start(self) -> _Follower:
        def reader() -> None:
            kwargs: dict = {}
            if self._lifetime is not None:
                kwargs["lifetime"] = self._lifetime
            for event in cli._follow_watch_lines(
                PROJECT,
                session=FOLLOWER_SESSION,
                poll_interval=self.POLL,
                sleeper=self._sleeper,
                stop=self.stop,
                sweep=self._sweep,
                sweep_interval=self.CADENCE,
                clock=self.clock,
                **kwargs,
            ):
                if event.get("event") in {"attached", "reattached"}:
                    continue
                self.emitted.append(event)

        self._thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()
        return self

    def append(self, *lines: str) -> None:
        with self.stream_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)
        return True

    def wait_sweeps(self, count: int) -> int:
        self.wait_for(lambda: len(self.sweep_moments) >= count)
        return len(self.sweep_moments)

    def wait_emissions(self, count: int) -> int:
        self.wait_for(lambda: len(self.emitted) >= count)
        return len(self.emitted)

    def stop_and_join(self, timeout: float = 15.0) -> None:
        self.release.set()
        self.stop.set()
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "a stopped follower must return"


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
    follower = _Follower(
        tmp_path,
        monkeypatch,
        held=lambda project: {"resumed": [], "skipped": []},
        lifetime=lifetime,
    ).start()
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
    follower = _Follower(
        tmp_path, monkeypatch, held=lambda project: {"resumed": [], "skipped": []}
    ).start()
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
