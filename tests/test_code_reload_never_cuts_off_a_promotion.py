"""A code reload never cuts off a promotion or a landing in flight.

The admission service replaces its own process image when the checkout's code
stamp moves. ``os.execve`` ends every thread the old image started, and both the
promotion threads and the landing thread live there. So on a moved stamp the
loop starts nothing new, waits for the work already running, and only then
re-executes.

Each case drives the real loop with the loop's own seams patched: a promotion
executor and a landing action that block on an event, a pause that moves the
stamp while that work runs, and a re-execution recorded rather than performed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from reckon.crew import promotion_requests, queue_admission, runs
from reckon.crew.promotion_requests import PromotionService

PROMOTION_THREAD_PREFIX = "promotion-request-"


def _queue_request(run_id: str) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "phase": "complete",
            "project": "proj",
            "session": "coordinator-a",
        },
    )
    promotion_requests.request_promotion(run_id)


def _live_promotion_threads() -> list[str]:
    return [
        thread.name
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(PROMOTION_THREAD_PREFIX)
    ]


class _LoopDriver:
    """Drive ``admit_until_stopped`` over a stamp move, with blocking work.

    ``pause`` is the interval between two passes, which is the window in which
    the checkout moves. It holds until the blocking work has started, so the
    stamp always moves while that work is still running.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        *,
        lander: Any = None,
        executor: Any = None,
    ) -> None:
        self.work_started = threading.Event()
        self.release = threading.Event()
        self.stamp_moved = threading.Event()
        self.starts: list[str] = []
        self.finishes: list[str] = []
        self.reexecs: list[dict[str, Any]] = []
        self.passes: list[int] = []
        self.observed_starts: list[str] = []
        self.reexecs_while_work_held = -1
        stamp = {"value": "old"}

        def pause(_seconds: float) -> None:
            if self.stamp_moved.is_set():
                return
            assert self.work_started.wait(10), "the blocking work never started"
            stamp["value"] = "new"
            self.stamp_moved.set()

        def reexec() -> None:
            # Recorded, not performed: ending the loop the way a replaced image
            # would lets the test read what happened before the replacement.
            self.reexecs.append(
                {
                    "promotion_threads": _live_promotion_threads(),
                    "stamp": stamp["value"],
                }
            )
            raise KeyboardInterrupt

        monkeypatch.setattr(
            queue_admission, "_admit_code_stamp", lambda: stamp["value"]
        )
        monkeypatch.setattr(queue_admission, "_checkout_code_is_trusted", lambda: True)
        monkeypatch.setattr(queue_admission, "_source_root", lambda: tmp_path)
        monkeypatch.setattr(queue_admission, "_reexec_into_current_code", reexec)
        monkeypatch.setattr(queue_admission, "_pause", pause)
        monkeypatch.setattr(queue_admission, "admit_once", self._admit_once)
        monkeypatch.setattr(
            "reckon.crew.dispatch_launch._adopt_launched_workers_from_reexec",
            lambda: None,
        )
        monkeypatch.setattr(
            "reckon.crew.resumption._promotion_settings", lambda _config: (1, 0.0)
        )
        monkeypatch.setattr("reckon.flight.resolve", lambda: SimpleNamespace(config={}))
        monkeypatch.setattr(
            promotion_requests,
            "PromotionService",
            lambda *, width: PromotionService(width=width, execute=executor),
        )

    def _admit_once(self, *, lane: str = "") -> dict[str, Any]:
        self.passes.append(len(self.passes) + 1)
        return {"queued": [], "launched": []}

    def drive(self) -> threading.Thread:
        """The thread that judges the loop while the blocking work still runs."""

        def run() -> None:
            assert self.stamp_moved.wait(10), "the stamp never moved"
            # Time for the loop to reach its drain, so a loop that re-executed
            # at once would already have recorded the replacement.
            time.sleep(0.3)
            self.observed_starts = list(self.starts)
            self.reexecs_while_work_held = len(self.reexecs)
            self.release.set()

        thread = threading.Thread(target=run)
        thread.start()
        return thread


def test_a_promotion_in_flight_finishes_before_the_service_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    _queue_request("run-a")
    _queue_request("run-b")
    state: dict[str, Any] = {}

    def executor(request: dict[str, Any]) -> dict[str, Any]:
        driver = state["driver"]
        driver.starts.append(str(request["run_id"]))
        driver.work_started.set()
        assert driver.release.wait(10), "the promotion was never released"
        driver.finishes.append(str(request["run_id"]))
        return {"ok": True, "promoted": True, "commits": []}

    driver = _LoopDriver(monkeypatch, tmp_path, executor=executor)
    state["driver"] = driver

    watcher = driver.drive()
    result = queue_admission.admit_until_stopped(interval=0.0)
    watcher.join(10)

    assert result["passes"] == 1
    # While the promotion ran, the loop had not replaced the image and had
    # started no second promotion, though run-b was pending throughout.
    assert driver.reexecs_while_work_held == 0
    assert driver.observed_starts == ["run-a"]
    # Once the promotion's own thread ended, the replacement followed in the
    # pass that noticed the moved stamp.
    assert len(driver.reexecs) == 1
    assert driver.reexecs[0]["promotion_threads"] == []
    assert driver.finishes == ["run-a"]
    assert driver.starts == ["run-a"]
    assert driver.passes == [1]


def test_a_landing_in_flight_finishes_before_the_service_reexecutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    state: dict[str, Any] = {}
    landing_runs: list[str] = []

    def lander() -> dict[str, Any]:
        driver = state["driver"]
        landing_runs.append("landing")
        driver.work_started.set()
        assert driver.release.wait(10), "the landing was never released"
        return {"landed": ["node"], "obligations": [], "deferred": []}

    driver = _LoopDriver(monkeypatch, tmp_path, lander=lander)
    state["driver"] = driver

    watcher = driver.drive()
    result = queue_admission.admit_until_stopped(interval=0.0, lander=lander)
    watcher.join(10)

    assert result["passes"] == 1
    assert driver.reexecs_while_work_held == 0
    assert len(driver.reexecs) == 1
    assert driver.reexecs[0]["promotion_threads"] == []
    # The landing reached its own end, and no second one began in its place.
    assert landing_runs == ["landing"]
    assert driver.passes == [1]


def test_a_draining_service_starts_no_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    _queue_request("run-a")
    started: list[str] = []
    service = PromotionService(
        width=1,
        execute=lambda request: (
            started.append(str(request["run_id"])) or {"ok": True, "promoted": True}
        ),
    )

    service.begin_drain()

    assert service.tick() == []
    service.wait_for_in_flight(timeout=5.0)
    assert started == []
