"""The recovery sweep runs on elapsed time while a follower reads a live stream.

The sweep's cadence gate was only called from the follower's outer loop, which
iterates again only after a producer dies, so while a producer stays up the
follower lives inside the stream-read loop and the sweep ran exactly once, at
attach. That made the declared interval a lower bound that never took effect
for a live stream — every declared external wait became a manual operation.
These tests pin the corrected behaviour: the gate is called from the inner
loop's own passes, so a follower reading a live producer stream sweeps on its
declared interval. The clock and the sweep are injected, and the stream is a
plain temporary file, so no real follower and no real provider are involved.

The clock is advanced only by the test, not by the sleeper: the point under
test is the gate's behaviour on elapsed time, so the test owns time and the
loop's sleep is a no-op. That keeps every cadence boundary exact.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import cli
from reckon.crew import recovery, runs


class _Clock:
    """A monotonic stand-in the test advances by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _line(run_id: str, to_state: str, *, session: str = "s1") -> str:
    """One durable stream line carrying a single transition record."""
    return (
        json.dumps(
            {
                "run_id": run_id,
                "to_state": to_state,
                "session": session,
                "node": "n1",
                "event": "transition",
            }
        )
        + "\n"
    )


def _expected_emissions(
    lines: list[str], *, session: str | None = None, run_ids: tuple[str, ...] = ()
) -> list[dict]:
    """The follower's own selection and dedup, without a follower.

    Reuses :func:`reckon.cli._follow_selects` so the expected pane is derived
    by the same rules the generator applies, rather than by a mirror that can
    drift.
    """
    reported: dict[str, str] = {}
    selected: list[dict] = []
    for line in lines:
        event = runs.parse_stream_line(line)
        if event is None:
            continue
        if not cli._follow_selects(event, session=session, run_ids=run_ids):
            continue
        run_id = str(event.get("run_id") or "")
        state = str(event.get("to_state") or "")
        if run_id and not event.get("legacy"):
            if reported.get(run_id) == state:
                continue
            reported[run_id] = state
        selected.append(event)
    return selected


class _Follower:
    """Drive one follower against an injected stream, clock, and sweep.

    The follower blocks reading the stream, so it runs in its own thread; the
    test hands it lines and time and reads back what it emitted and how often
    it swept.
    """

    CADENCE = 10.0
    POLL = 0.05

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        sweep: Callable[[str], dict] | None,
        start: float = 0.0,
        cadence: float = CADENCE,
        project: str = "proj",
    ) -> None:
        self.clock = _Clock(start)
        self.cadence = cadence
        self.project = project
        self.stop = threading.Event()
        self.emitted: list[dict] = []
        self.sweep_moments: list[float] = []
        self.stream_path = tmp_path / "watch-stream.jsonl"
        self.stream_path.touch()
        self._extra = sweep
        self._thread: threading.Thread | None = None

        monkeypatch.setattr(
            runs,
            "producer_live",
            lambda name: name == self.project,
        )
        monkeypatch.setattr(
            runs,
            "watch_stream_cursor",
            lambda name: (
                {
                    "stream_path": str(self.stream_path),
                    "offset": 0,
                    "baseline": [],
                    "producer": {},
                }
                if name == self.project
                else pytest.fail("the follower must only watch its own project")
            ),
        )

    def _sleeper(self, seconds: float) -> None:
        # The test owns time; a real sleep would let the loop pace itself.
        return None

    def _sweep(self, project: str) -> dict:
        self.sweep_moments.append(self.clock())
        if self._extra is not None:
            return self._extra(project)
        return {"resumed": [], "skipped": []}

    def start(self) -> _Follower:
        def reader() -> None:
            for event in cli._follow_watch_lines(
                self.project,
                poll_interval=self.POLL,
                sleeper=self._sleeper,
                stop=self.stop,
                sweep=self._sweep,
                sweep_interval=self.cadence,
                clock=self.clock,
            ):
                if event.get("event") in {"attached", "reattached"}:
                    continue
                self.emitted.append(event)

        self._thread = threading.Thread(target=reader, daemon=True)
        self._thread.start()
        return self

    def append(self, *lines: str) -> None:
        payload = "".join(lines)
        with self.stream_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
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

    def stop_and_join(self, timeout: float = 3.0) -> None:
        self.stop.set()
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "a stopped follower must return"


def test_a_quiet_live_stream_sweeps_once_per_interval(tmp_path, monkeypatch) -> None:
    """A follower attached to a live stream that emits no lines calls the sweep
    once per cadence interval, not once in total."""
    follower = _Follower(tmp_path, monkeypatch, sweep=None).start()
    assert follower.wait_sweeps(1) == 1, "the attach sweep runs once"

    assert follower.clock() == 0.0
    for step in (1, 2, 3):
        follower.clock.advance(follower.CADENCE)
        assert follower.wait_sweeps(step + 1) == step + 1
    follower.stop_and_join()

    assert follower.sweep_moments == [0.0, 10.0, 20.0, 30.0], (
        "one sweep at attach and one per elapsed interval, no more"
    )


def test_line_traffic_does_not_starve_but_does_not_repeat(
    tmp_path, monkeypatch
) -> None:
    """A stream that emits lines continuously still sweeps on the cadence: the
    line traffic neither starves the sweep nor multiplies it."""
    follower = _Follower(tmp_path, monkeypatch, sweep=None).start()
    follower.append(
        _line("r1", "working"), _line("r2", "working"), _line("r3", "working")
    )
    assert follower.wait_emissions(3) == 3
    assert follower.sweep_moments == [0.0], "a busy stream does not sweep more"

    for step in (1, 2):
        follower.append(_line(f"r{step + 3}", "working"))
        follower.clock.advance(follower.CADENCE)
        assert follower.wait_sweeps(step + 1) == step + 1
    assert follower.wait_emissions(5) == 5, "the line traffic still reached the pane"
    follower.stop_and_join()

    assert follower.sweep_moments == [0.0, 10.0, 20.0], (
        "one sweep per interval while lines keep arriving"
    )


def test_the_rate_guard_holds_across_many_calls_in_one_interval(
    tmp_path, monkeypatch
) -> None:
    """Two or more gate calls inside one interval produce one sweep, so the
    time-based guard still bounds the sweep from the inner loop."""
    follower = _Follower(tmp_path, monkeypatch, sweep=None).start()
    assert follower.wait_sweeps(1) == 1

    follower.append(*[_line("r1", f"step{i}") for i in range(6)])
    assert follower.wait_emissions(6) == 6
    follower.clock.advance(follower.CADENCE / 2)
    assert follower.sweep_moments == [0.0], (
        "half an interval and a burst of lines must not add a sweep"
    )

    follower.clock.advance(follower.CADENCE / 2)
    assert follower.wait_sweeps(2) == 2
    follower.stop_and_join()
    assert follower.sweep_moments == [0.0, 10.0]


def test_a_raising_sweep_does_not_end_the_follower(tmp_path, monkeypatch) -> None:
    """A sweep that raises is swallowed for that pass, and a later cadence
    sweep still runs; the follower keeps reading its stream throughout."""
    raised: list[bool] = []

    def flaky(project: str) -> dict:
        if not raised:
            raised.append(True)
            raise RuntimeError("recovery failed on this pass")
        return {"resumed": [], "skipped": []}

    follower = _Follower(tmp_path, monkeypatch, sweep=flaky).start()
    assert follower.wait_sweeps(1) == 1
    assert raised == [True], "the first sweep attempt raised"

    follower.append(_line("r1", "working"))
    follower.clock.advance(follower.CADENCE)
    assert follower.wait_sweeps(2) == 2, "the cadence still runs after a raise"
    assert follower.wait_emissions(1) == 1, "a raising sweep must not kill the pane"
    follower.stop_and_join()


def test_a_stopped_follower_performs_no_sweep_after_stop(tmp_path, monkeypatch) -> None:
    """Once the follower is stopped it performs no sweep after the stop."""
    follower = _Follower(tmp_path, monkeypatch, sweep=None).start()
    assert follower.wait_sweeps(1) == 1

    follower.stop_and_join()
    assert follower.sweep_moments == [0.0], (
        "no sweep fires after the follower observes the stop"
    )


def test_pane_output_is_unchanged_by_the_sweep(tmp_path, monkeypatch) -> None:
    """The pane for a given sequence of stream lines is byte-identical with the
    sweep running: a sweep emits nothing of its own onto the transition
    stream."""
    lines = [
        _line("r1", "working"),
        _line("r2", "blocked"),
        _line("r1", "blocked"),
        _line("r1", "blocked"),
        "",
    ]
    follower = _Follower(
        tmp_path, monkeypatch, sweep=lambda project: {"resumed": [], "skipped": []}
    ).start()
    follower.append(*lines)
    assert follower.wait_emissions(3) == 3
    follower.stop_and_join()

    expected = _expected_emissions(lines)
    assert expected == follower.emitted, "the sweep changed the transition stream"
    rendered_expected = [recovery.format_watch_transition(event) for event in expected]
    rendered_actual = [
        recovery.format_watch_transition(event) for event in follower.emitted
    ]
    assert rendered_actual == rendered_expected, "the pane bytes changed"
