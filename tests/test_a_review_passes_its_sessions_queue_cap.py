"""A review is exempt from its session's queue cap and launches ahead of one.

The per-session cap bounds queued implementation, but a review of a delivered
run is what lets that run land, so it is the last dispatch the cap may refuse.
Among one session's queued runs a review launches before an implementation
without displacing the session's place in the cross-session order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_order, runs


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private crew home, so no queued pointer is written anywhere shared."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _request(
    run_id: str,
    *,
    queued_at: datetime,
    role: str = "implement",
    session: str = "coordinator",
) -> dict[str, Any]:
    moment = queued_at.isoformat()
    return {
        "run_id": run_id,
        "project": "proj",
        "plan": "fixture",
        "section": "s1",
        "repo": "/repo",
        "session": session,
        "backend": "local",
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": moment,
        "node": {"id": run_id, "plan": "fixture", "section": "s1", "role": role},
    }


def _at(minutes_ago: float, now: datetime) -> datetime:
    return now - timedelta(minutes=minutes_ago)


# ── the cap exempts a review, and still bounds implementation


def test_a_review_is_queued_when_the_session_holds_its_cap(crew_home: Path) -> None:
    cap = 2
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    for index in range(cap):
        runs.queue_dispatch(
            _request(f"impl-{index}", queued_at=_at(index, now)), cap=cap
        )

    review, position = runs.queue_dispatch(
        _request("review-of-impl-0", role="review", queued_at=_at(9, now)), cap=cap
    )

    assert review["phase"] == "queued"
    assert position is not None
    assert [row["run_id"] for row in runs.list_live(project="proj")] == [
        "impl-0",
        "impl-1",
        "review-of-impl-0",
    ]


def test_a_further_implementation_is_still_refused_queue_full(crew_home: Path) -> None:
    cap = 2
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    for index in range(cap):
        runs.queue_dispatch(
            _request(f"impl-{index}", queued_at=_at(index, now)), cap=cap
        )
    runs.queue_dispatch(
        _request("review-of-impl-0", role="review", queued_at=_at(9, now)), cap=cap
    )

    with pytest.raises(runs.QueueFull) as refused:
        runs.queue_dispatch(_request("impl-three", queued_at=now), cap=cap)

    assert refused.value.cap == cap
    assert refused.value.held == cap


def test_a_queued_review_does_not_shrink_the_implementation_allowance(
    crew_home: Path,
) -> None:
    cap = 2
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    runs.queue_dispatch(
        _request("review-of-earlier", role="review", queued_at=_at(9, now)), cap=cap
    )

    one, _ = runs.queue_dispatch(_request("impl-one", queued_at=_at(3, now)), cap=cap)
    two, _ = runs.queue_dispatch(_request("impl-two", queued_at=_at(2, now)), cap=cap)

    assert one["phase"] == two["phase"] == "queued"
    with pytest.raises(runs.QueueFull):
        runs.queue_dispatch(_request("impl-three", queued_at=now), cap=cap)


# ── admission launches one session's review before its earlier implementation


def test_a_review_launches_before_an_implementation_queued_earlier(
    crew_home: Path,
) -> None:
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    queued = [
        _request("impl-one", queued_at=_at(30, now)),
        _request("impl-two", queued_at=_at(10, now)),
        _request("review-of-impl-one", role="review", queued_at=_at(1, now)),
    ]

    order = queue_order.admission_order(queued, [], now=now)

    assert [row["run_id"] for row in order] == [
        "review-of-impl-one",
        "impl-one",
        "impl-two",
    ]


def test_a_review_keeps_its_sessions_oldest_wait_in_the_order(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review is not counted as a fresh wait, so it cannot mask starvation."""
    monkeypatch.setattr(queue_order, "STARVATION_AGE_SECONDS", 1800)
    now = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)
    queued = [
        _request("stale-impl", queued_at=_at(40, now), session="A"),
        _request("stale-review", role="review", queued_at=_at(1, now), session="A"),
        _request("fresh-impl", queued_at=_at(2, now), session="B"),
    ]

    order = queue_order.admission_order(queued, [], now=now)

    assert [row["run_id"] for row in order] == [
        "stale-review",
        "stale-impl",
        "fresh-impl",
    ]
