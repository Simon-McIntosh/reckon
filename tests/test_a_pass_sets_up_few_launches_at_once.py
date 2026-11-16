"""The admission pass bounds how many launches it sets up at once."""

from __future__ import annotations

import threading
import time

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
