"""A session's sweep runs its promotions beside itself, not in front of itself.

A promotion takes minutes on a session's shared checkout. A sweep that ran them
one after another held every review it owed behind the last of them, so a run
delivered while four reviews were being promoted waited tens of minutes for a
reviewer to be named. The sweep now dispatches the reviews it owes first and
hands each promotion and acceptance to a bounded worker of its own.

The four behaviours asserted here are the ones the section names:

- a sweep holding four finished reviews and one newly delivered run dispatches
  the new run's review before any promotion completes;
- with the width set to two, no more than two promotions run at once and all
  four complete;
- a promotion that fails is reported on its run, and the next sweep retries it;
- two sweeps never promote the same run at once.

The negative control is the older ordering: with the promotions run inside the
sweep's own pass, the newly delivered run's review is dispatched after every
promotion has completed, and the first assertion fails naming it.

The fixture runs a temporary crew home and asserts the workstation's real crew
pointer directory is untouched afterwards, because an isolated read does not
prove an isolated write.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import recovery_review_acceptance as acceptance
from reckon.crew import resumption
from reckon.crew.runs import crew_home

PROJECT = "sweep-promotion-fixture"
SESSION = "session-sweep-promotion"
REVIEWED_RUNS = [f"r-20261112T12000{i}000000-finished-review-{i}" for i in range(4)]
DELIVERED_RUN = "r-20261112T120005000000-newly-delivered"
REVIEW_RUN_ID = "r-20261112T120006000000-review-of-newly-delivered"

# The promotion's own duration. Long enough that four of them run one after
# another cannot be mistaken for four run at once, short enough that the whole
# module stays inside a gate's minutes.
PROMOTION_SECONDS = 0.4


class PromotionRecorder:
    """A stubbed promotion that takes a configured time and records its runs."""

    def __init__(self, *, seconds: float = PROMOTION_SECONDS) -> None:
        self.seconds = seconds
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: list[tuple[float, str]] = []
        self.finished: list[tuple[float, str]] = []
        self.failures: set[str] = set()
        self.calls: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def promotion(self, pointer, *, config=None):
        run_id = str(pointer.get("run_id") or "")
        with self.lock:
            self.calls.append(run_id)
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.started.append((time.monotonic(), run_id))
        try:
            self.release.wait(timeout=10.0)
            time.sleep(self.seconds)
            if run_id in self.failures:
                raise RuntimeError(f"stubbed promotion failed for {run_id}")
            with self.lock:
                self.finished.append((time.monotonic(), run_id))
            return {
                "run_id": run_id,
                "accepted": True,
                "promoted": True,
            }
        finally:
            with self.lock:
                self.running -= 1

    def first_completion(self) -> float:
        with self.lock:
            return min(stamp for stamp, _ in self.finished)


class DispatchRecorder:
    """A stubbed review dispatch that records when it named a reviewer."""

    def __init__(self, *, seconds: float = 0.05) -> None:
        self.seconds = seconds
        self.lock = threading.Lock()
        self.at: dict[str, float] = {}

    def dispatch(self, pointer, *, config=None, launcher=None):
        run_id = str(pointer.get("run_id") or "")
        time.sleep(self.seconds)
        with self.lock:
            self.at[run_id] = time.monotonic()
        return {
            "run_id": run_id,
            "dispatched": True,
            "review_run_id": REVIEW_RUN_ID,
        }


def _pointer(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": run_id, "role": "implement"},
        "phase": "delivered",
        "repo": "/nonexistent",
        "worktree": "/nonexistent",
    }


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    real_pointer = crew_home() / "pointers" / PROJECT
    before = sorted(str(path) for path in real_pointer.glob("*.json"))
    yield
    assert sorted(str(path) for path in real_pointer.glob("*.json")) == before


@pytest.fixture()
def stubs(isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Stub every pass but the promotions, and return both recorders."""
    promotions = PromotionRecorder()
    dispatches = DispatchRecorder()
    monkeypatch.setattr(acceptance, "accept_clean_review", promotions.promotion)
    monkeypatch.setattr(acceptance, "promote_review_run_itself", promotions.promotion)
    monkeypatch.setattr(acceptance, "dispatch_review_for_run", dispatches.dispatch)
    monkeypatch.setattr(
        acceptance, "dispatch_repair_for_run", lambda *a, **k: {"dispatched": False}
    )
    monkeypatch.setattr(acceptance, "withdraw_superseded_review", lambda *a, **k: None)
    monkeypatch.setattr(
        acceptance, "review_settle_seconds_remaining", lambda *a, **k: 0
    )
    monkeypatch.setattr(acceptance, "_sweep_review_tier", lambda *a, **k: "light")
    monkeypatch.setattr(
        acceptance,
        "classify_pointer",
        lambda pointer, **kwargs: {
            "classification": (
                "scoring" if pointer.get("run_id") == DELIVERED_RUN else "promotable"
            )
        },
    )
    monkeypatch.setattr(resumption, "external_wait", lambda *a, **k: None)
    monkeypatch.setattr(resumption, "_stream_refusal_block", lambda *a, **k: None)
    monkeypatch.setattr(
        resumption.plan_review,
        "compose_owed_plan_reviews",
        lambda *a, **k: {},
    )
    return promotions, dispatches


def _pointers() -> list[dict]:
    return [_pointer(run_id) for run_id in [*REVIEWED_RUNS, DELIVERED_RUN]]


def _sweep(*, config: dict | None = None, pointers: list[dict] | None = None) -> dict:
    return resumption.sweep(
        PROJECT,
        session=SESSION,
        pointers=_pointers() if pointers is None else pointers,
        config=config,
        launcher=lambda *args, **kwargs: 0,
    )


def test_a_promotion_beside_the_sweep_never_holds_the_review_it_owed(
    stubs,
) -> None:
    """The delivered run's review is dispatched before any promotion completes."""
    promotions, dispatches = stubs

    report = _sweep(config={"promotion": {"width": 2, "wait_seconds": 60}})

    assert report["reviews"]["in_flight"] == [], report["reviews"]["in_flight"]
    assert report["reviews"]["accepted"] == REVIEWED_RUNS, report["reviews"]
    assert report["reviews"]["failed"] == [], report["reviews"]["failed"]
    assert dispatches.at.keys() == {DELIVERED_RUN}, dispatches.at
    assert DELIVERED_RUN in dispatches.at, (
        "the newly delivered run's review was never dispatched"
    )
    dispatched_at = dispatches.at[DELIVERED_RUN]
    assert promotions.finished, "no promotion completed, so nothing was overlapped"
    assert dispatched_at < promotions.first_completion(), (
        "the delivered run's review was dispatched at "
        f"{dispatched_at:.3f} but a promotion had already completed at "
        f"{promotions.first_completion():.3f}: the sweep waited on a promotion "
        "before naming the reviewer it owed"
    )


def test_the_width_bounds_the_promotions_running_at_once(stubs) -> None:
    """Two at a time, and all four still complete."""
    promotions, _dispatches = stubs

    report = _sweep(config={"promotion": {"width": 2, "wait_seconds": 60}})

    assert sorted(report["reviews"]["accepted"]) == sorted(REVIEWED_RUNS)
    assert sorted(run_id for _, run_id in promotions.finished) == sorted(REVIEWED_RUNS)
    assert promotions.peak == 2, (
        f"peak concurrent promotions was {promotions.peak}, not the width of 2"
    )


def test_a_failed_promotion_is_reported_and_the_next_sweep_retries_it(stubs) -> None:
    """A promotion that raised names its run, and the run is tried again."""
    promotions, _dispatches = stubs
    failed_run = REVIEWED_RUNS[0]
    promotions.failures = {failed_run}

    first = _sweep(config={"promotion": {"width": 2, "wait_seconds": 60}})

    assert [entry["run_id"] for entry in first["reviews"]["failed"]] == [failed_run], (
        first["reviews"]
    )
    assert "stubbed promotion failed" in first["reviews"]["failed"][0]["error"]
    assert failed_run not in first["reviews"]["accepted"]

    promotions.failures = set()
    second = _sweep(config={"promotion": {"width": 2, "wait_seconds": 60}})

    assert failed_run in second["reviews"]["accepted"], second["reviews"]
    assert promotions.calls.count(failed_run) == 2, promotions.calls


def test_two_sweeps_never_promote_one_run_at_once(stubs) -> None:
    """A second sweep reaching a run already being promoted leaves it alone."""
    promotions, _dispatches = stubs
    promotions.seconds = 0.6
    promotions.release.clear()
    # Only one reviewed run reaches acceptance, so the second sweep's whole
    # question is whether it promotes that run beside the first.
    pointers = [_pointer(REVIEWED_RUNS[0]), _pointer(DELIVERED_RUN)]
    other = _pointer(REVIEWED_RUNS[0])
    other["session"] = SESSION

    first: dict = {}

    def run_first_sweep() -> None:
        first["report"] = _sweep(
            config={"promotion": {"width": 1, "wait_seconds": 60}},
            pointers=pointers,
        )

    worker = threading.Thread(target=run_first_sweep)
    worker.start()
    try:
        deadline = time.monotonic() + 10.0
        while not promotions.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert promotions.started, "the first sweep never reached its promotion"

        second = _sweep(
            config={"promotion": {"width": 1, "wait_seconds": 60}},
            pointers=[other, _pointer(DELIVERED_RUN)],
        )
    finally:
        promotions.release.set()
        worker.join(timeout=30)

    assert second["reviews"]["in_flight"], second["reviews"]
    assert second["reviews"]["in_flight"][0]["run_id"] == REVIEWED_RUNS[0]
    assert promotions.calls.count(REVIEWED_RUNS[0]) == 1, (
        "the same run was promoted twice concurrently: " + repr(promotions.calls)
    )
    assert REVIEWED_RUNS[0] in promotions.finished[0][1]
    assert first["report"]["reviews"]["accepted"] == [REVIEWED_RUNS[0]]


def test_the_report_names_the_promotions_it_handed_over(stubs) -> None:
    """The sweep's record says where the outcomes came from, not just that."""
    promotions, _dispatches = stubs

    report = _sweep(config={"promotion": {"width": 4, "wait_seconds": 60}})

    reviews = report["reviews"]
    assert reviews["in_flight"] == []
    assert promotions.peak >= 2, (
        "four promotions under a width of four never ran together: "
        f"peak {promotions.peak}"
    )
    assert reviews["accepted"] == REVIEWED_RUNS


def test_the_promotions_it_handed_over_reach_the_sweep_record(stubs) -> None:
    """The recovery log carries the promotions the sweep's report named."""
    _promotions, _dispatches = stubs

    report = _sweep(config={"promotion": {"width": 2, "wait_seconds": 60}})

    log = resumption.recovery_log_path(PROJECT)
    assert log.exists(), "the sweep wrote no recovery record"
    recorded = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert recorded[-1]["reviews"]["accepted"] == report["reviews"]["accepted"]
    assert recorded[-1]["reviews"]["accepted"] == REVIEWED_RUNS
