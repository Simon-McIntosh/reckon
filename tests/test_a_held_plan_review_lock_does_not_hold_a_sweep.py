"""A sweep that finds a plan's review being composed moves on instead of waiting.

Each coordinator session's station sweep may reach the same owed plan review.
The dispatch takes a per-plan lock around the in-flight check and the launch.
Taken with a wait, every other session's sweep sat behind the holder's whole
dispatch, and a session whose sweep is held never reaches its own code reviews:
a profile of the live station found three quarters of its sweep threads'
samples waiting on that lock. These cases hold the lock and time the call.
"""

from __future__ import annotations

import fcntl
import threading
import time

import pytest

from reckon.crew import plan_review
from reckon.crew import recovery_review_dispatch as dispatch

RECORD = {
    "subject": "plan",
    "project": "lock-proj",
    "plan_slug": "lock-plan",
    "run_id": "r-20261114T000000000000-plan-review-of-lock-plan",
}


@pytest.fixture
def composed(home, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stand in for the composition so a case measures only the lock."""
    calls: list[dict] = []

    def compose(record, fields, **_kwargs):
        calls.append(dict(record))
        return {"dispatched": True, "run_id": record["run_id"]}

    monkeypatch.setattr(dispatch, "_standing_plan_review", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_review_dispatch_fields", lambda *_a, **_k: {})
    monkeypatch.setattr(dispatch, "_dispatch_composed_review", compose)
    return calls


def _lock_path():
    directory = plan_review.review_report_directory(
        RECORD["project"], RECORD["plan_slug"], RECORD["run_id"]
    ).parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "dispatch.lock"


def test_a_held_lock_returns_at_once_without_composing(composed):
    held = threading.Event()
    release = threading.Event()

    def holder():
        with _lock_path().open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        report = dispatch._dispatch_text_review_for_run(dict(RECORD))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(10)
    assert elapsed < 2.0, elapsed
    assert report["dispatched"] is False
    assert "composing this plan's review" in report["reason"]
    assert composed == []


def test_a_free_lock_still_dispatches(composed):
    report = dispatch._dispatch_text_review_for_run(dict(RECORD))
    assert report["dispatched"] is True
    assert len(composed) == 1
