"""Dispatch reaps idle roster members at most once per interval per project.

The reap loads and decodes the whole ledger. The admission service sends every
queued launch through dispatch, so reaping on each dispatch cost minutes per
launch while queued work waited with the lane idle.
"""

from __future__ import annotations

from reckon.crew.dispatching import preparation


def test_the_reap_runs_once_per_interval(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(preparation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(preparation, "_LAST_IDLE_MEMBER_REAP", {})

    assert preparation._idle_member_reap_due("p", "/repo") is True
    clock[0] += 5.0
    assert preparation._idle_member_reap_due("p", "/repo") is False
    assert preparation._idle_member_reap_due("other", "/repo") is True
    clock[0] += preparation.IDLE_MEMBER_REAP_INTERVAL_SECONDS
    assert preparation._idle_member_reap_due("p", "/repo") is True
