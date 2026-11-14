"""One run that cannot be classified never fails a project's obligations.

The station derives each project's obligations from every live pointer, and a
classification that raised -- a run's manifest removed while it was being read,
or a missing key -- took the whole derivation with it. On 14 November the
station logged "refresh could not complete" 110 times for reckon alone and its
snapshot sat six hours old, so every session's hooks served stale duties.
"""

from __future__ import annotations

import pytest

from reckon.crew import obligations, recovery, runs

PROJECT = "derive-proj"


def _pointer(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": "s-1",
        "phase": "working",
        "node": {"id": run_id},
    }


@pytest.fixture
def fleet(home, monkeypatch: pytest.MonkeyPatch):
    pointers = [_pointer("r-ok"), _pointer("r-broken"), _pointer("r-gone")]
    for pointer in pointers[:2]:
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)

    def classify(pointer, *args, **kwargs):
        if pointer["run_id"] == "r-ok":
            return {"run_id": "r-ok", "classification": "running"}
        raise FileNotFoundError(f"manifest of {pointer['run_id']}")

    monkeypatch.setattr(obligations, "_live_pointers", lambda project: pointers)
    monkeypatch.setattr(recovery, "classify_pointer", classify)
    return pointers


def test_a_raising_classification_does_not_fail_the_derivation(fleet):
    rows = {row["run_id"]: row for row in obligations._classified_rows(PROJECT)}
    assert set(rows) == {"r-ok", "r-broken"}
    assert rows["r-ok"]["classification"] == "running"


def test_a_present_run_that_cannot_be_classified_reads_unreadable(fleet):
    rows = {row["run_id"]: row for row in obligations._classified_rows(PROJECT)}
    broken = rows["r-broken"]
    assert broken["recovery_classification"] == "unreadable"
    assert "manifest of r-broken" in broken["detail"]
    assert broken["session"] == "s-1"
