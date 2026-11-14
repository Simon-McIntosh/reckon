"""A freed promotion slot takes the next waiting request without a pass.

An admission pass comes round every 60 to 80 seconds, and longer under load,
while one promotion holds its slot for minutes. A queue drained only by the
pass therefore leaves freed slots empty for the rest of the interval. These
tests drive the service with a promotion that takes a fixed time and a pass
that is held, and read how long the same five requests take either way.
"""

from __future__ import annotations

import threading
import time

import pytest

from reckon.crew import promotion_requests, runs
from reckon.crew.promotion_requests import PromotionService

# One promotion's fixed duration. Three times this is the most five requests
# can take at width 2: two batches of two and one of one.
PROMOTION_SECONDS = 1.0
# Bookkeeping around a promotion — two host leases, two pointer mutations and
# the receipt write on this filesystem — is about 0.15 s a request, so each
# batch pays it. It is a property of the store, not of the drain, and — short
# as it is beside a real promotion, which takes minutes.
SUITE_OVERHEAD_SECONDS = 1.0
# The control's promotion is shorter so that ten times it fits in the test.
CONTROL_PROMOTION_SECONDS = 0.5
PASS_SECONDS = 10 * CONTROL_PROMOTION_SECONDS
REQUESTS = 5


@pytest.fixture(autouse=True)
def fast_refill(monkeypatch):
    """Keep the idle poll short so a stamp test does not spend real seconds."""
    monkeypatch.setattr(promotion_requests, "REFILL_POLL_SECONDS", 0.05)


def _queue(run_id: str) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "phase": "complete",
            "project": "drain-fixture",
            "session": "drain-session",
        },
    )
    promotion_requests.request_promotion(run_id)


def _run_ids() -> list[str]:
    return [f"r-drain-{number}" for number in range(REQUESTS)]


def _executor(
    started: list[str], finished: list[str], duration: float = PROMOTION_SECONDS
):
    lock = threading.Lock()

    def perform(request):
        run_id = str(request["run_id"])
        with lock:
            started.append(run_id)
        time.sleep(duration)
        with lock:
            finished.append(run_id)
        return {"ok": True, "run_id": run_id, "record": {"commits": []}}

    return perform


def _wait_until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _all_promoted(run_ids: list[str]) -> bool:
    return all(
        promotion_requests.read_promotion(run_id).get("phase")
        == promotion_requests.PROMOTED
        for run_id in run_ids
    )


def test_a_freed_slot_takes_the_next_request(monkeypatch):
    run_ids = _run_ids()
    for run_id in run_ids:
        _queue(run_id)
    started: list[str] = []
    finished: list[str] = []
    service = PromotionService(width=2, execute=_executor(started, finished))
    service.start_refill()
    try:
        began = time.monotonic()
        # One admission pass, then a pass held far longer than the queue takes.
        service.tick()
        drained = _wait_until(
            lambda: _all_promoted(run_ids), 3 * PROMOTION_SECONDS + 5
        )
        elapsed = time.monotonic() - began
    finally:
        service.stop_refill()

    assert drained, f"{len(finished)} of {len(run_ids)} promotions finished"
    assert _all_promoted(run_ids)
    assert elapsed <= 3 * PROMOTION_SECONDS + SUITE_OVERHEAD_SECONDS, (
        f"five requests at width 2 took {elapsed:.2f} s, over three "
        f"{PROMOTION_SECONDS:g} s promotion times plus bookkeeping"
    )


def test_starting_only_from_the_pass_is_slow():
    """The negative control the head test is.

    Same five requests, same width, same promotion time: the only difference is
    that requests are taken up by the pass alone, which comes round once every
    ten promotion times.
    """
    run_ids = _run_ids()
    for run_id in run_ids:
        _queue(run_id)
    started: list[str] = []
    finished: list[str] = []
    service = PromotionService(
        width=2, execute=_executor(started, finished, CONTROL_PROMOTION_SECONDS)
    )
    try:
        began = time.monotonic()
        for _ in range(10):
            service.tick()
            if _all_promoted(run_ids):
                break
            time.sleep(PASS_SECONDS)
        elapsed = time.monotonic() - began
    finally:
        service.stop_refill()

    assert _all_promoted(run_ids)
    assert elapsed > PASS_SECONDS, (
        f"draining from the pass alone took {elapsed:.2f} s, no more than one "
        f"{PASS_SECONDS:g} s pass interval"
    )


def test_a_moved_code_stamp_starts_nothing_more():
    """No request starts between the stamp moving and the reload.

    A replacement image ends this process's threads, so a promotion started
    after the checkout's code has moved would be cut off by the reload that
    follows. The one already running finishes; nothing else begins until the
    replacement process takes the queue up.
    """
    run_ids = ["r-stamp-1", "r-stamp-2"]
    for run_id in run_ids:
        _queue(run_id)
    stamp = {"value": "old"}
    started: list[str] = []
    in_flight = threading.Event()
    release = threading.Event()

    def perform(request):
        run_id = str(request["run_id"])
        started.append(run_id)
        in_flight.set()
        assert release.wait(10), "the in-flight promotion was never released"
        return {"ok": True, "run_id": run_id, "record": {"commits": []}}

    service = PromotionService(
        width=1, execute=perform, code_stamp=lambda: stamp["value"]
    )
    service.start_refill()
    try:
        service.tick()
        assert in_flight.wait(5), "the first promotion never started"
        stamp["value"] = "new"
        release.set()
        # Several poll intervals and one slot release: the freed slot must stay
        # empty because this image is about to be replaced.
        _wait_until(lambda: False, 0.6)
        assert started == [run_ids[0]], started
        assert (
            promotion_requests.read_promotion(run_ids[1]).get("phase")
            == promotion_requests.REQUESTED
        )
        # The admission loop's move on a moved stamp: drain, wait, re-execute.
        service.begin_drain()
        assert service.wait_for_in_flight() == []
        assert started == [run_ids[0]]
    finally:
        service.stop_refill()

    reloaded = PromotionService(
        width=1, execute=perform, code_stamp=lambda: stamp["value"]
    )
    reloaded.start_refill()
    try:
        release.set()
        reloaded.tick()
        assert _wait_until(lambda: _all_promoted(run_ids), 5), started
    finally:
        reloaded.stop_refill()
    assert _all_promoted(run_ids)