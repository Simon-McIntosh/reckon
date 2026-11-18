"""The admission pass bounds how many launches it sets up at once."""

from __future__ import annotations

import threading
import time

import pytest

from reckon.crew import placement, runs
from reckon.crew.admission import attempts


def test_the_pool_runs_no_more_setups_at_once_than_its_declared_width():
    batch = attempts.LaunchBatch({}, {})
    running = 0
    peak = 0
    guard = threading.Lock()

    def setup():
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with guard:
            running -= 1

    futures = [batch.pool.submit(setup) for _ in range(attempts.LAUNCH_POOL_WIDTH * 5)]
    for future in futures:
        future.result()
    batch.pool.shutdown(wait=True)

    assert batch.width == attempts.LAUNCH_POOL_WIDTH
    assert peak == attempts.LAUNCH_POOL_WIDTH


def test_the_declared_width_keeps_setup_memory_below_the_workers_share():
    # A setup has been measured at a few gigabytes; the pool must stay narrow
    # enough that a full pass of them is a small part of a shared allowance.
    assert 1 <= attempts.LAUNCH_POOL_WIDTH <= 8


@pytest.mark.parametrize("free_seats", [1, 2])
def test_pending_placed_setups_share_the_reservations_free_seats(
    monkeypatch, free_seats
):
    """Setups that have not published a worker cannot all spend one free seat."""
    running = 0
    peak = 0
    guard = threading.Lock()

    def setup(pointer, **kwargs):
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with guard:
            running -= 1
        return {"launched": [pointer["run_id"]]}

    monkeypatch.setattr(attempts, "_attempt_launch", setup)
    monkeypatch.setattr(
        placement, "read_reservation", lambda _project: {"job_id": "fixture"}
    )
    monkeypatch.setattr(runs, "list_live", list)
    monkeypatch.setattr(
        placement,
        "roster_occupants",
        lambda _points: [{}] * (placement.RESERVATION_ROSTER_LIMIT - free_seats),
    )
    config = {
        "backends": {"worker": {"placement": {"scheduler": "srun", "options": []}}}
    }
    key = ("worker", "session")
    result = {}
    with attempts.LaunchBatch(result, {key: None}) as batch:
        for index in range(attempts.LAUNCH_POOL_WIDTH * 2):
            batch.wait_for_capacity()
            batch.submit(
                key,
                {"run_id": str(index), "backend": "worker", "project": "fixture"},
                config_for=lambda _pointer: config,
            )
    assert peak == free_seats
    assert len(result["launched"]) == attempts.LAUNCH_POOL_WIDTH * 2
