"""An idle producer backs its poll interval off to a cap, then recovers.

A per-project ``crew watch`` producer holds a lease, so an unattended one ends
between arms on its own. While it is up it still polls, and a project with no
live run pointer — nobody is watching it — must not keep polling at the armed
rate for as long as its lease lasts. These cases pin the back-off: an idle
producer doubles its recorded ``poll_interval_seconds`` from the armed base to a
30 s ceiling, a wake that sees a live run returns the interval to the base, and
however far it has backed off the producer still ends within a second of its
lease lapsing, because no sleep outlasts what remains of the lease.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs
from reckon.crew.recovery import IDLE_POLL_INTERVAL_CAP_SECONDS, watch_ticker

BASE_INTERVAL = 1.0
LEASE_SECONDS = 3.0


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Move all crew state into the test's temporary directory."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", str(int(LEASE_SECONDS)))
    return config_home


def _recorded_interval(project: str) -> float | None:
    """The producer's recorded poll interval, or ``None`` while it is unset."""
    value = runs.read_watch_registration(project).get("poll_interval_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _await(predicate, *, timeout: float = 20.0, message: str):
    """Poll ``predicate`` until it is truthy, or fail naming what never came."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError(message)


def _write_live_pointer(project: str, run_id: str) -> None:
    """Fabricate a live run pointer for the project in the temporary crew home."""
    log = runs.crew_home() / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    crew._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": project,
            "node": {"id": run_id, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": datetime.now(tz=UTC).isoformat(),
            "manifest_path": str(runs.crew_home() / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


class _ProducerStopError(Exception):
    """Raised from a fake sleeper to end the producer thread."""


def test_idle_producer_backs_off_to_the_cap_and_returns_to_base(home: Path) -> None:
    """The recorded interval doubles to the cap while idle, then returns to base."""
    del home  # requested for its environment, not its path
    project = "idle-backoff-sample"
    expected = [BASE_INTERVAL, 2.0, 4.0, 8.0, 16.0, IDLE_POLL_INTERVAL_CAP_SECONDS]
    observed: list[float] = []
    stop = threading.Event()

    def sleeper(interval: float) -> None:
        # The producer writes its interval before it sleeps, so the value read
        # here is the one registered for this pass, not a guess.
        recorded = _recorded_interval(project)
        if recorded is not None:
            observed.append(recorded)
        if stop.is_set():
            raise _ProducerStopError
        time.sleep(0.001)

    def drive() -> None:
        try:
            for _ in watch_ticker(
                project, poll_interval=BASE_INTERVAL, sleeper=sleeper
            ):
                pass
        except _ProducerStopError:
            pass

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    try:
        _await(
            lambda: observed[: len(expected)] == expected,
            message="an idle producer never backed its interval off to the 30 s cap",
        )
        assert observed[: len(expected)] == expected
        # A live run pointer for this project is seen on the next wake, so the
        # interval returns to the armed base rather than idling at the cap.
        _write_live_pointer(project, "live-run-1")
        _await(
            lambda: _recorded_interval(project) == BASE_INTERVAL,
            message="a wake that saw a live run did not return to the base interval",
        )
        assert _recorded_interval(project) == BASE_INTERVAL
    finally:
        stop.set()
        thread.join(timeout=10.0)
    assert not thread.is_alive(), "the producer thread did not stop on request"


def test_a_backed_off_producer_still_exits_at_its_lease(home: Path) -> None:
    """No sleep outlasts the lease, so a backed-off producer ends at the lapse."""
    del home  # requested for its environment, not its path
    project = "backed-off-exit"
    interval = 0.2
    observed: list[float] = []
    state: dict[str, object] = {}

    def sleeper(duration: float) -> None:
        recorded = _recorded_interval(project)
        if recorded is not None:
            observed.append(recorded)
        time.sleep(duration)

    def drive() -> None:
        try:
            for _ in watch_ticker(project, poll_interval=interval, sleeper=sleeper):
                pass
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion
            state["error"] = repr(exc)
            return
        state["ended_at"] = time.monotonic()

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    # Wait for the producer's lease seed, then time the exit against that
    # instant so the lapse is the producer's own, not the test's start.
    renewed = _await(
        lambda: runs.watch_lease_renewed_at(project),
        message="the producer never seeded its lease registration",
    )
    now_epoch = time.time()
    now_mono = time.monotonic()
    lapse_mono = now_mono + (renewed + LEASE_SECONDS - now_epoch)

    thread.join(timeout=LEASE_SECONDS + 2.0)
    assert state.get("error") is None, state.get("error")
    assert not thread.is_alive(), "a backed-off producer must still end at its lease"
    assert observed, "the producer never polled before its lease lapsed"
    assert max(observed) > interval, "the producer's interval never backed off"
    ended = time.monotonic()
    assert ended >= lapse_mono - 0.5, "the producer ended before its lease lapsed"
    assert ended <= lapse_mono + 1.0, "the producer outlived its lease by over a second"
