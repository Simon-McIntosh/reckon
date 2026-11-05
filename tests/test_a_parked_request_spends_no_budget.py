"""A request that never launched has spent none of its time budget.

A request's budget measures the time it spent working. A pointer that is still
queued, waiting on upstream work, blocked on it, or held has never run, so a
week spent parked is a week of waiting, not a week of budget. The wait is
reported under its own key so a reader can still see it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from reckon.crew import (
    query,
    recovery,
    recovery_classification,
    recovery_liveness,
    recovery_stream,
    recovery_watch,
)

_WEEK_SECONDS = 7 * 24 * 60 * 3_600


def _record(
    run_id: str,
    *,
    phase: str,
    created: datetime,
    budget: str = "3h",
    launched: datetime | None = None,
    attempt: datetime | None = None,
    process_alive: bool | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": run_id, "plan": "plan-a", "time_budget": budget},
        "backend": "clive",
        "phase": phase,
        "created_at": created.isoformat(),
    }
    if attempt is not None:
        record["attempt_started_at"] = attempt.isoformat()
    if launched is not None:
        record["launched_at"] = launched.isoformat()
    if process_alive is not None:
        assert isinstance(process_alive, bool)
        record["process_alive"] = process_alive
    return record


@pytest.mark.parametrize(
    "phase", ["queued", "awaiting-upstream", "blocked-upstream", "held"]
)
def test_a_pointer_that_never_launched_spends_no_budget(phase: str) -> None:
    """A parked request reports no elapsed budget time, only its wait.

    The measured defect: an ``awaiting-upstream`` pointer created a week ago
    reported an elapsed figure counted from creation — 539,530s against a
    10,800s budget — so every parked request raised the same overrun alarm.
    """
    created = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    record = _record("r-parked", phase=phase, created=created)
    now = (created + timedelta(seconds=_WEEK_SECONDS)).timestamp()

    timing = recovery._budget_timing(record, now_seconds=now)

    assert timing["budget_overrun"] is False
    assert timing["budget_overrun_seconds"] == 0
    assert timing["elapsed_seconds"] is None
    assert timing["budget_seconds"] == 3 * 3_600
    assert timing["waiting_seconds"] == _WEEK_SECONDS


def test_a_launched_run_past_its_budget_still_reports_the_overrun() -> None:
    """The launch stamp is what charges a run, and an overrun still shows."""
    launched = datetime(2026, 11, 3, 8, 0, 0, tzinfo=UTC)
    record = _record(
        "r-launched",
        phase="working",
        created=launched - timedelta(hours=1),
        launched=launched,
        process_alive=True,
    )
    now = (launched + timedelta(hours=4)).timestamp()

    timing = recovery._budget_timing(record, now_seconds=now)

    assert timing["budget_seconds"] == 3 * 3_600
    assert timing["elapsed_seconds"] == 4 * 3_600
    assert timing["budget_overrun"] is True
    assert timing["budget_overrun_seconds"] == 3_600
    assert "waiting_seconds" not in timing


def test_elapsed_is_counted_from_the_first_launch_not_from_creation() -> None:
    """Time spent queueing is not charged, even to a run that later launched."""
    launched = datetime(2026, 11, 3, 8, 0, 0, tzinfo=UTC)
    record = _record(
        "r-slow-queue",
        phase="working",
        created=launched - timedelta(hours=10),
        launched=launched,
        process_alive=True,
    )
    now = (launched + timedelta(hours=1)).timestamp()

    timing = recovery._budget_timing(record, now_seconds=now)

    assert timing["elapsed_seconds"] == 3_600
    assert timing["budget_overrun"] is False


def test_the_live_view_answers_waiting_seconds_for_a_parked_request(
    monkeypatch,
) -> None:
    """A caller can ask for the wait before any parked row is in flight.

    The name is declared with the rest of the live fields, so the request is
    accepted however empty the fleet is, and a parked pointer reports its wait
    under it.
    """
    created = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)
    now = (created + timedelta(seconds=_WEEK_SECONDS)).timestamp()
    for module in (
        recovery_stream,
        recovery_liveness,
        recovery_classification,
        recovery_watch,
    ):
        monkeypatch.setattr(module, "_utc_seconds", lambda: now)
    record = _record("r-parked-live", phase="awaiting-upstream", created=created)

    # The declared vocabulary answers the name with no row in hand.
    query._refuse_unknown_live_fields(["waiting_seconds"], [], with_mine=False)

    row = query.project_live_rows([record], fields=["waiting_seconds"])[0]

    assert row["waiting_seconds"] == _WEEK_SECONDS
