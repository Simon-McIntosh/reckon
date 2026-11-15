"""A pass shares its seats between reviews and implementations.

A delivered run lands only after its review, so a review queued behind a wave of
implementations holds finished work, and a wave of reviews holds back new work.
Each pass therefore shares the seats it fills between the two roles in proportion
to how many of each wait, with neither role taking fewer than one seat in three
while both wait, and it records what each role got and how old the oldest request
it left waiting is.

The seat mix is read from the order the pass launches in rather than from which
dispatch succeeded: a launch on a contended host can fail for reasons outside
this ordering, so a test that counted successful launches would measure the host.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon import flight
from reckon.crew import runs
from reckon.crew.admission import ordering
from tests import test_each_admission_pass_records_its_verdict as fixtures

SEATS = 3
IMPLEMENTATION_COUNT = 9
REVIEW_COUNT = 3


@pytest.fixture()
def admit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shared admission fixture: isolated crew home, launchable checkout."""
    monkeypatch.setattr(flight, "admission_pass_budget_seconds", lambda _config: 600.0)
    return fixtures.admit_repo.__wrapped__(tmp_path, monkeypatch)


def _pointer(
    project: str,
    role: str,
    index: int,
    *,
    start: datetime,
    seconds_ago: int,
) -> dict[str, Any]:
    """One queued request of the named role, ranked by when it was queued."""
    pointer: dict[str, Any] = {
        "run_id": f"{project}-{role}-{index}",
        "project": project,
        "session": "coordinator-a",
        "backend": "local",
        "phase": "queued",
        "queued_at": (start - timedelta(seconds=seconds_ago)).isoformat(),
    }
    if role == "review":
        pointer["role"] = "review"
        pointer["node"] = {"id": f"review-of-{project}-impl-{index}", "role": "review"}
    return pointer


def _queue(
    *, implementations: int, reviews: int, project: str = "alpha"
) -> list[dict[str, Any]]:
    """Nine implementations queued first, with the reviews behind them."""
    start = datetime(2026, 11, 15, tzinfo=UTC)
    queued = [
        _pointer(project, "impl", index, start=start, seconds_ago=10_000 - index)
        for index in range(implementations)
    ]
    queued += [
        _pointer(project, "review", index, start=start, seconds_ago=5_000 - index)
        for index in range(reviews)
    ]
    return queued


def _launch_order(queued: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order one admission pass launches ``queued`` in."""
    return ordering.fair_share_admission_order(
        queued,
        [],
        now=datetime(2026, 11, 15, 1, 0, tzinfo=UTC),
        config_for=lambda _pointer: {},
    )


def _roles(ordered: list[dict[str, Any]], seats: int | None = None) -> list[str]:
    rows = ordered if seats is None else ordered[:seats]
    return [
        "review" if str(row.get("role") or "") == "review" else "impl" for row in rows
    ]


def test_a_pass_gives_both_roles_a_seat_with_three_waiting() -> None:
    """Nine implementations ahead of three reviews share three seats."""
    ordered = _launch_order(
        _queue(implementations=IMPLEMENTATION_COUNT, reviews=REVIEW_COUNT)
    )

    seats = _roles(ordered, SEATS)

    assert "review" in seats, seats
    assert "impl" in seats, seats


def test_a_lone_waiting_role_fills_every_seat() -> None:
    """With only implementations waiting, every seat is an implementation."""
    ordered = _launch_order(_queue(implementations=SEATS, reviews=0))

    assert _roles(ordered, SEATS) == ["impl"] * SEATS


def test_a_pass_row_names_each_role_it_launched_and_left_waiting(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict row a pass writes carries both roles' figures."""
    queued = _queue(implementations=IMPLEMENTATION_COUNT, reviews=REVIEW_COUNT)
    for pointer in queued:
        base = copy.deepcopy(fixtures._queue(admit_repo))
        base.update({key: value for key, value in pointer.items() if key != "node"})
        base["session"] = fixtures.SESSION
        base["node"]["id"] = (
            f"review-of-{pointer['project']}-impl-{pointer['run_id'].rsplit('-', 1)[-1]}"
            if pointer.get("role") == "review"
            else pointer["run_id"]
        )
        base["node"]["role"] = str(pointer.get("role") or "implement")
        base["node"]["write_paths"] = [f"{pointer['run_id']}.txt"]
        runs.queue_dispatch(base)
    lane = tmp_path / "lane.json"
    config, resolve = fixtures._config_with_lane(lane)
    config["roles"]["review"] = {}

    fixtures._drive(
        monkeypatch,
        lane_document=lane,
        resolve=resolve,
        launcher=fixtures._launcher([]),
        lanes=[fixtures._session_lane(float(SEATS))],
    )

    rows = fixtures._read_verdict_lines(admit_repo)
    assert len(rows) == 1, rows
    roles = rows[0]["roles"]

    assert set(roles) == {"review", "implementation"}
    implement = roles["implementation"]
    review = roles["review"]
    assert implement["launched"] + implement["waiting"] == IMPLEMENTATION_COUNT
    assert review["launched"] + review["waiting"] == REVIEW_COUNT
    assert implement["waiting"] == 0 or implement["oldest_waiting_seconds"] > 0
    assert review["waiting"] == 0 or review["oldest_waiting_seconds"] > 0


def test_the_share_stays_inside_each_projects_place() -> None:
    """The role share reorders within a project, never across projects."""
    start = datetime(2026, 11, 15, tzinfo=UTC)
    rows = [
        _pointer("alpha", "impl", 0, start=start, seconds_ago=40),
        _pointer("alpha", "impl", 1, start=start, seconds_ago=30),
        _pointer("beta", "impl", 0, start=start, seconds_ago=20),
        _pointer("alpha", "impl", 2, start=start, seconds_ago=15),
        _pointer("alpha", "review", 0, start=start, seconds_ago=10),
    ]

    shared = ordering.role_share_order(rows)

    assert [pointer["project"] for pointer in shared] == [
        pointer["project"] for pointer in rows
    ]
    assert {pointer["run_id"] for pointer in shared} == {
        pointer["run_id"] for pointer in rows
    }
