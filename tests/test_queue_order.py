"""Queue admission follows lane use, join order, and the starvation age."""

from datetime import UTC, datetime, timedelta

from reckon.crew import queue_order


def _queued(session: str, index: int, at: datetime) -> dict[str, str]:
    return {
        "run_id": f"{session}-{index}",
        "session": session,
        "backend": "local",
        "phase": "queued",
        "queued_at": at.isoformat(),
    }


def test_lighter_session_catches_up_then_admissions_alternate() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    queued = [
        *(
            _queued("A", index, now - timedelta(minutes=2, seconds=-index))
            for index in range(8)
        ),
        *(
            _queued("B", index, now - timedelta(minutes=1, seconds=-index))
            for index in range(8)
        ),
    ]
    in_flight = [
        {"session": "A", "backend": "local", "phase": "working"} for _ in range(4)
    ]

    order = queue_order.admission_order(queued, in_flight, now=now)

    assert [row["session"] for row in order[:8]] == [
        "B",
        "B",
        "B",
        "B",
        "A",
        "B",
        "A",
        "B",
    ]
    assert [row["run_id"] for row in order if row["session"] == "A"] == [
        f"A-{index}" for index in range(8)
    ]
    assert [row["run_id"] for row in order if row["session"] == "B"] == [
        f"B-{index}" for index in range(8)
    ]


def test_join_order_within_session_uses_queued_at() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    queued = [
        _queued("A", 2, now - timedelta(seconds=10)),
        _queued("A", 1, now - timedelta(seconds=20)),
    ]

    assert [
        row["run_id"] for row in queue_order.admission_order(queued, [], now=now)
    ] == ["A-1", "A-2"]


def test_starved_entry_precedes_a_lighter_sessions_new_entry(monkeypatch) -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    monkeypatch.setattr(queue_order, "STARVATION_AGE_SECONDS", 3)
    queued = [
        _queued("A", 0, now - timedelta(seconds=4)),
        _queued("B", 0, now - timedelta(seconds=1)),
    ]
    in_flight = [
        {"session": "A", "backend": "local", "phase": "working"} for _ in range(4)
    ]

    assert queue_order.admission_order(queued, in_flight, now=now)[0]["run_id"] == "A-0"


def test_wait_breaks_equal_counts_and_queued_runs_hold_no_slot() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    queued = [
        _queued("A", 0, now - timedelta(seconds=10)),
        _queued("B", 0, now - timedelta(seconds=20)),
    ]
    live = [
        queued[0],
        {"session": "B", "backend": "other", "phase": "working"},
        {"session": "B", "backend": "local", "phase": "complete"},
    ]

    assert queue_order.admission_order(queued, live, now=now)[0]["run_id"] == "B-0"
