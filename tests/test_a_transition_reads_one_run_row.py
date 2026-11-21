"""A transition's recorded spend reads one run's row, not the project's history.

The station publishes one transition per settled run per sweep, and each one
wants that run's spend as promotion committed it. When the run has no document
of its own the row lives in the run store, so the read selects it by key: the
cost of a transition is its own row however many runs the project has recorded.
The answer is the one the history walk gave — the same spend fields for a run
found, and no answer for one that is not.
"""

from __future__ import annotations

import math

import pytest

from reckon import ledger, run_store
from reckon.crew.recovery_watch import _recorded_transition_spend

PROJECT = "example"
ROW_COUNT = 2000
SAMPLED = 50


def _record(position: int) -> dict:
    """One promoted row whose every spend figure names its own position."""
    return {
        "run_id": f"r-{position:04d}",
        "project": PROJECT,
        "node": f"node-{position}",
        "plan": "a-plan",
        "section": "work",
        "role": "implement",
        "gate": "passed",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T01:00:00Z",
        "throughput": {
            "cumulative_input_tokens": 1000 + position,
            "generated_tokens": 200 + position,
            "elapsed_seconds": 60.0 + position,
            "generation_seconds": 30.0 + position,
            "machine_seconds": 90.0 + position,
            "tokens_per_second": 4.0 + position / 1000,
        },
        "budget": {
            "cost_usd_cumulative": round(position / 100, 4),
            "runner_usd": round(position / 200, 4),
        },
        "outcome": f"landed {position}",
    }


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    """A crew home holding 2,000 promoted rows and no per-run document."""
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    with run_store.RunStore() as opened:
        for position in range(ROW_COUNT):
            opened.append(PROJECT, _record(position))
    assert not ledger.run_path(PROJECT, "r-0000").is_file()
    assert not ledger.run_path(PROJECT, f"r-{ROW_COUNT - 1:04d}").is_file()
    return [
        f"r-{position:04d}" for position in range(0, ROW_COUNT, ROW_COUNT // SAMPLED)
    ]


def _expected(row: dict) -> dict:
    """The spend answer the history walk gave for the row it selected."""

    def measured(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value if math.isfinite(value) else None

    throughput = row.get("throughput")
    throughput = throughput if isinstance(throughput, dict) else {}
    budget = row.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    input_tokens = measured(throughput.get("cumulative_input_tokens"))
    output_tokens = measured(throughput.get("generated_tokens"))
    return {
        "spend_folded_run_count": 1,
        "spend_measured_stream_count": None,
        "spend_unmeasured_stream_count": None,
        "spend_wall_seconds": measured(throughput.get("elapsed_seconds")),
        "spend_model_seconds": measured(throughput.get("generation_seconds")),
        "spend_machine_seconds": measured(throughput.get("machine_seconds")),
        "spend_charged_tokens": (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        "spend_generation_rate": measured(throughput.get("tokens_per_second")),
        "spend_notional_cost_usd": measured(budget.get("cost_usd_cumulative")),
    }


def test_a_transition_reads_one_run_row(sampled, monkeypatch):
    # The answers the base revision gave: every stored row of the project,
    # selected by identity afterwards, through the spend mapping the function
    # documents.
    history = {row["run_id"]: row for row in run_store.stored_run_rows(PROJECT)}
    assert len(history) == ROW_COUNT
    expected = {run_id: _expected(history[run_id]) for run_id in sampled}

    original = run_store.stored_run_rows
    decoded: list[str] = []

    def counted(*args, **kwargs):
        rows = original(*args, **kwargs)
        decoded.extend(row["run_id"] for row in (rows or ()))
        return rows

    monkeypatch.setattr(run_store, "stored_run_rows", counted)

    answers = {
        run_id: _recorded_transition_spend(PROJECT, run_id) for run_id in sampled
    }
    absent = _recorded_transition_spend(PROJECT, "r-absent")

    assert len(decoded) <= SAMPLED + 1, f"{len(decoded)} rows decoded"
    assert answers == expected
    assert absent is None
