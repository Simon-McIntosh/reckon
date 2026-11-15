"""A router that read full at one instant does not hold the queue until the next pass.

The lane dips with work queued because a pass reads the router's gate once: a
reading of full at that instant holds every queued request, and the next reading
comes a pass later, which on the fleet was 45 to 185 seconds on. The router holds
each request for about a quarter of a live run, so a full reading clears within
seconds. A pass therefore waits, bounded, for the lane to publish room again
before it records the hold and ends -- and does not wait at all on a paused lane,
whose verdict is a decision rather than an instant.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.admission import holds
from tests import test_each_admission_pass_records_its_verdict as fixtures

# The stand-in reads full for this long after the pass's first reading, then
# publishes a slot for every queued request.
FULL_FOR_SECONDS = 5.0

# The measure gives the whole cohort 60 seconds from the first open reading.
LAUNCH_BOUND_SECONDS = 60.0

# How long the pass is given. The wait under test is bounded at 30 seconds, so a
# budget near a minute would let a pass that never re-read the lane pass by luck.
PASS_BUDGET_SECONDS = 180.0

REQUESTS = 12


class _StandInRouter:
    """A lane that publishes its own reading before answering every read.

    It answers full for the first ``open_after`` seconds of the pass and open,
    with a slot for every queued request, from then on. The answer goes through
    the reader the dispatch path really uses, so the document this router
    publishes is parsed and weighted exactly as a live one is.
    """

    def __init__(
        self,
        path: Path,
        *,
        open_after: float,
        slots: float,
        paused: bool = False,
    ) -> None:
        self.path = path
        self.open_after = open_after
        self.slots = slots
        self.paused = paused
        self.started: float | None = None
        self.opened_at: float | None = None
        self.reads: list[tuple[float, str]] = []
        self._real = holds._dispatch_lane_allowance

    def _publish(self, *, full: bool) -> None:
        # The lane's verdict travels in the admission block beside the headroom
        # it was taken on: a block carrying no headroom publishes no verdict, and
        # this router must publish a reading the reader can tell apart from one
        # it could not take at all.
        if self.paused:
            document = fixtures._session_lane(0.0)
            document["headroom"] = 0
            document["admission"]["headroom"] = 0
            document["admission"]["verdict"] = "paused"
            document["admission"]["reason"] = "the operator stopped admission"
        elif full:
            document = fixtures._session_lane(0.0)
            document["headroom"] = 0
            document["admission"]["headroom"] = 0
            document["admission"]["verdict"] = "full"
            document["admission"]["reason"] = "the gate is full, at or over its width"
        else:
            document = fixtures._session_lane(self.slots)
            document["headroom"] = self.slots
            document["admission"]["headroom"] = self.slots
            document["admission"]["verdict"] = "open"
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def read(
        self, backend: Any, *, session: str, weights_configured: bool = False
    ) -> Any:
        now = time.monotonic()
        if self.started is None:
            self.started = now
        elapsed = now - self.started
        full = elapsed < self.open_after
        self._publish(full=full)
        if not full and self.opened_at is None:
            self.opened_at = now
        reading = "paused" if self.paused else "full" if full else "open"
        self.reads.append((round(elapsed, 3), reading))
        return self._real(
            backend, session=session, weights_configured=weights_configured
        )


def _queue(repository: Path, count: int) -> list[str]:
    """Store ``count`` distinct queued requests with disjoint write paths."""
    base = fixtures._queue(repository)
    run_ids: list[str] = []
    for index in range(count):
        pointer = copy.deepcopy(base)
        pointer["run_id"] = f"r-queued-{index}"
        pointer["node"]["id"] = f"queued-node-{index}"
        pointer["node"]["goal"] = f"admit queued node {index}"
        pointer["node"]["write_paths"] = [f"queued-node-{index}.py"]
        runs.queue_dispatch(pointer)
        run_ids.append(pointer["run_id"])
    return run_ids


def _instant_launcher(completed: list[tuple[str, float]]) -> Any:
    """A launch that occupies no time, so the measurement is the pass's.

    The real launch starts a worker, which the admission pass does not wait for;
    stubbing it keeps the measurement on the wait for the lane rather than on the
    harness.
    """

    def launch(pointer: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        run_id = str(pointer["run_id"])
        record = runs._mutate_pointer(run_id, lambda row: {**row, "phase": "working"})
        completed.append((run_id, time.monotonic()))
        return record

    return launch


def _recorded_pause(pauses: list[float]) -> Any:
    """The pass's own pause, recorded as it is taken.

    Every wait the pass takes between two readings of a full lane goes through
    this seam, so the list is the pass's own account of whether it waited: an
    empty list says it took one reading and moved on, and a short total on a
    short budget shows the wait never outlived the budget it was given. The
    pause itself still spends the time it names, because the stand-in lane
    publishes its next reading on the wall clock.
    """

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        time.sleep(seconds)

    return pause


def _admission_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared admission fixture: isolated crew home, launchable checkout."""
    return fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _admission_repository(tmp_path, monkeypatch)


def test_a_full_reading_does_not_hold_the_queue(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    router = _StandInRouter(lane, open_after=FULL_FOR_SECONDS, slots=float(REQUESTS))
    _config, resolve = fixtures._config_with_lane(lane)
    monkeypatch.setattr(holds, "_dispatch_lane_allowance", router.read)
    queued = _queue(repository, REQUESTS)
    completed: list[tuple[str, float]] = []
    monkeypatch.setattr(queue_admission, "_launch_queued", _instant_launcher(completed))
    pauses: list[float] = []
    monkeypatch.setattr(queue_admission, "_pause", _recorded_pause(pauses))

    result = queue_admission.admit_once(
        config_for=resolve,
        budget_seconds=PASS_BUDGET_SECONDS,
    )

    assert router.reads[0][1] == "full", router.reads
    assert set(result["launched"]) == set(queued), result["verdicts"]
    assert pauses != [], "the pass did not re-read a lane that read full"
    assert router.opened_at is not None, router.reads
    assert len(completed) == REQUESTS
    latest = max(at for _run_id, at in completed)
    assert latest - router.opened_at < LAUNCH_BOUND_SECONDS, (
        f"the last launch was {latest - router.opened_at:.1f}s after the first "
        f"open reading"
    )
    assert all(runs.read_pointer(run_id)["phase"] == "working" for run_id in queued)


def test_a_paused_lane_is_not_waited_on(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lane = tmp_path / "lane.json"
    router = _StandInRouter(lane, open_after=0.0, slots=float(REQUESTS), paused=True)
    _config, resolve = fixtures._config_with_lane(lane)
    monkeypatch.setattr(holds, "_dispatch_lane_allowance", router.read)
    queued = _queue(repository, REQUESTS)
    completed: list[tuple[str, float]] = []
    monkeypatch.setattr(queue_admission, "_launch_queued", _instant_launcher(completed))
    pauses: list[float] = []
    monkeypatch.setattr(queue_admission, "_pause", _recorded_pause(pauses))

    result = queue_admission.admit_once(
        config_for=resolve, budget_seconds=PASS_BUDGET_SECONDS
    )

    assert result["launched"] == [], result["launched"]
    assert completed == []
    assert {reading for _at, reading in router.reads} == {"paused"}
    # A paused lane is a decision, not an instant: the pass takes its reading,
    # holds every request and moves on without waiting for a different one.
    assert pauses == [], f"the pass waited {sum(pauses):.1f}s on a paused lane"
    assert sorted(result["held"]) == sorted(queued)


def test_a_full_reading_waits_no_longer_than_the_pass_budget(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane that stays full costs the pass its budget, never the whole bound."""
    lane = tmp_path / "lane.json"
    router = _StandInRouter(lane, open_after=3600.0, slots=float(REQUESTS))
    _config, resolve = fixtures._config_with_lane(lane)
    monkeypatch.setattr(holds, "_dispatch_lane_allowance", router.read)
    run_ids = _queue(repository, REQUESTS)
    completed: list[tuple[str, float]] = []
    monkeypatch.setattr(queue_admission, "_launch_queued", _instant_launcher(completed))
    pauses: list[float] = []
    monkeypatch.setattr(queue_admission, "_pause", _recorded_pause(pauses))

    started = time.monotonic()
    result = queue_admission.admit_once(config_for=resolve, budget_seconds=2.0)
    elapsed = time.monotonic() - started

    assert result["launched"] == [], result["launched"]
    assert result["held"], "the pass held nothing it could not launch"
    assert set(result["held"]) <= set(run_ids)
    assert all(reading == "full" for _at, reading in router.reads), router.reads
    assert sum(pauses) <= 2.0, f"the pass waited {sum(pauses):.1f}s of a 2s budget"
    assert elapsed < 10.0, f"the pass spent {elapsed:.1f}s on a two-second budget"
