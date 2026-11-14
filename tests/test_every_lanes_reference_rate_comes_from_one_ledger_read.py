"""Every lane's reference rate in a window comes from one read of the ledger.

A run that overran its budget is judged against the median committed rate of
its own backend and model. The rate was cached per lane and five-minute window,
so each lane paid for its own decode of the whole ledger, and the station
classifies runs of several lanes in several projects on every tick: a profile on
14 November put the station's main thread in that decode for half its samples.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reckon import ledger as ledger_module
from reckon.crew import recovery_stream

PROJECT = "rate-proj"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
BUCKET = int(NOW.timestamp() // 300)
LANES = {
    ("clive", "model-a"): 40.0,
    ("codex", "model-b"): 90.0,
    ("claude", "model-c"): 60.0,
}


def _rows() -> list[dict]:
    return [
        {
            "backend": backend,
            "agent": {"model": model},
            "completed_at": (NOW - timedelta(hours=index + 1)).isoformat(),
            "throughput": {"tokens_per_second": rate},
        }
        for (backend, model), rate in LANES.items()
        for index in range(12)
    ]


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def load(project, *args, **kwargs):
        calls.append(project)
        return {"runs": _rows()}, 0

    monkeypatch.setattr(ledger_module, "load", load)
    recovery_stream._historical_reference_rate.cache_clear()
    yield calls
    recovery_stream._historical_reference_rate.cache_clear()


def test_three_lanes_in_one_window_read_the_ledger_once(loads):
    for (backend, model), rate in LANES.items():
        reference, count = recovery_stream._historical_reference_rate(
            PROJECT, backend, model, BUCKET
        )
        assert (reference, count) == (rate, 12)
    assert loads == [PROJECT]


def test_clearing_the_cache_reads_again(loads):
    recovery_stream._historical_reference_rate(PROJECT, "clive", "model-a", BUCKET)
    recovery_stream._historical_reference_rate.cache_clear()
    recovery_stream._historical_reference_rate(PROJECT, "clive", "model-a", BUCKET)
    assert loads == [PROJECT, PROJECT]


def test_a_small_cohort_is_no_reference(loads):
    reference, count = recovery_stream._historical_reference_rate(
        PROJECT, "clive", "no-such-model", BUCKET
    )
    assert (reference, count) == (None, 0)
