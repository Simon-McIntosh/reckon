"""A watch producer holds a lease, so a follower's renewal keeps it alive.

The producer is spawned detached and reparented to init, so nothing ends it
when its session does. These cases pin the lease that replaces a lifetime: an
unattended producer ends one interval after its last renewal, a follower that
keeps renewing holds it up, and the two writers of the shared registration —
the producer's pid and the follower's renewal — never drop each other's field.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.recovery import watch_ticker

LEASE_SECONDS = 3.0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", str(int(LEASE_SECONDS)))
    return config_home


def _run_producer(project: str) -> tuple[threading.Thread, dict[str, float]]:
    """Drive the producer loop on a thread, recording when it ends."""
    state: dict[str, float] = {}

    def drive() -> None:
        try:
            for _ in watch_ticker(project, poll_interval=0.2):
                pass
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion
            state["error"] = repr(exc)  # type: ignore[assignment]
            return
        state["ended_at"] = time.monotonic()

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    return thread, state


def _await_lease(
    project: str, state: dict[str, float], *, timeout: float = 20.0
) -> float:
    """Return the producer's first recorded renewal, once it has taken the seat.

    The wait is generous because taking the seat writes through shared storage:
    under contention a seed that normally lands in milliseconds can take
    seconds, and a short bound would read that load as a producer that never
    started. The error the producer thread recorded is surfaced rather than
    buried, so a refusal to start is not mistaken for a slow one.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        renewed = runs.watch_lease_renewed_at(project)
        if renewed is not None:
            return renewed
        if state.get("error"):
            raise AssertionError(f"the producer failed to start: {state['error']}")
        time.sleep(0.05)
    raise AssertionError("the producer never seeded its lease registration")


def test_unattended_producer_exits_after_its_lease_lapses(home: Path) -> None:
    """With no follower renewing, the producer ends within a second of a lapse."""
    project = "unattended"
    thread, state = _run_producer(project)
    renewed = _await_lease(project, state)
    # The exit is timed against the lease instant the producer recorded, not
    # against the start of the test, so a slow first poll cannot read as early
    # expiry and a fast one cannot read as a late one.
    now_epoch = time.time()
    now_mono = time.monotonic()
    lapse_mono = now_mono + (renewed + LEASE_SECONDS - now_epoch)

    thread.join(timeout=LEASE_SECONDS + 2.0)
    assert state.get("error") is None, state.get("error")
    assert not thread.is_alive(), "an unattended producer must end on its own lease"
    ended = time.monotonic()
    assert ended >= lapse_mono - 0.5, "the producer ended before its lease lapsed"
    assert ended <= lapse_mono + 1.0, "the producer outlived its lease by over a second"
    assert runs.watch_lease_renewed_at(project) == renewed


def test_a_renewed_producer_outlives_three_lease_intervals(home: Path) -> None:
    """A follower renewing at least once per half interval holds the producer up."""
    project = "renewed"
    thread, state = _run_producer(project)
    fresh = _await_lease(project, state)

    # A renewal counts only when the follower's write advanced the instant
    # recorded in the registration. renew_producer_lease also returns the
    # existing record on a throttled call, so a non-None return alone would
    # count a no-op write as a renewal and the assertion would pass without a
    # single write having landed.
    renewals = 0
    previous = fresh
    deadline = time.monotonic() + 3 * LEASE_SECONDS
    while time.monotonic() < deadline:
        runs.renew_producer_lease(project)
        renewed = runs.watch_lease_renewed_at(project)
        if renewed is not None and renewed > previous:
            renewals += 1
            previous = renewed
        time.sleep(LEASE_SECONDS / 2)

    assert state.get("error") is None, state.get("error")
    assert thread.is_alive(), "a renewed producer must stay up past three intervals"
    # At least one observed write per half interval over three intervals is
    # five; require the floor the cadence promises rather than the exact count.
    assert renewals >= 5
    advanced = runs.watch_lease_renewed_at(project)
    assert advanced is not None and advanced > fresh

    # Let the lease lapse so the seat is released before the test ends.
    thread.join(timeout=LEASE_SECONDS + 2.0)
    assert not thread.is_alive()


def test_concurrent_writers_keep_both_registration_fields(home: Path) -> None:
    """The producer's pid and the follower's renewal each survive the other."""
    project = "concurrent"
    runs.update_watch_registration(project, pid=1, lease_renewed_at=1000.0)

    def write_pid() -> None:
        for _ in range(50):
            runs.update_watch_registration(project, pid=os.getpid())

    def write_renewal() -> None:
        for step in range(50):
            runs.update_watch_registration(project, lease_renewed_at=2000.0 + step)

    writers = [
        threading.Thread(target=write_pid),
        threading.Thread(target=write_renewal),
    ]
    for writer in writers:
        writer.start()
    # Joined without a timeout: each write renames a file on shared storage, and
    # a thread abandoned mid-write would outlive the test's RECKON_HOME and then
    # resolve the registration against the operator's own crew home.
    for writer in writers:
        writer.join()

    record = runs.read_watch_registration(project)
    assert record.get("pid") == os.getpid()
    assert record.get("lease_renewed_at") is not None
