"""A session's priority points are budgeted and they order the lane.

A priority is a whole number of points a session places on one request while it
waits. The flight configuration declares how many points one session may hold
at once, a submission (``crew dispatch``, a batch entry, ``crew review``) or a
later ``crew queue reprioritise`` spends them, and points return when the
request launches, is dropped or is discarded. Every request's points are read
from the request itself, so a launch needs no ledger to be kept in step with
the queue.

The lane's admission order reads them in one place: a delivered review first,
then a request past the ageing bound oldest-first, then the highest priority,
then the session with the fewest live runs, then the roadmap rank. Ageing sits
above priority on purpose, and the case that fixes the order is a fresh
priority-3 request against a priority-0 request that has waited longer than the
bound.

The queue is written through the real queue writer and the real batch path, so
the priority a test reads back is the one the request was stored with rather
than a value the test put there.
"""

# ruff: noqa: F811 - the fixtures are imported and also named as test parameters.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import queue_order, runs
from tests.test_a_batch_entry_fits_its_window_when_queued import (  # noqa: F401
    CONFIG,
    DONE_WHEN,
    PLAN,
    PROJECT,
    batch_repo,
)
from tests.test_a_jev_hold_queues_for_the_local_lane import (  # noqa: F401
    _configure,
    _invoke,
    _payload,
    _stub_hold,
    dispatch_repo,
)

LANE = "clive"
SESSION = "coordinator-a"
OTHER_SESSION = "coordinator-b"
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=UTC)
HOLDING = ("queued", "awaiting-upstream", "held")


def _config(*, budget: int | None) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["queue"] = {} if budget is None else {"priority_budget": budget}
    return config


def _entry(node: str, *, priority: int = 0, **overrides: object) -> dict:
    entry: dict = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s1",
        "node": node,
        "goal": "Store a request carrying priority points.",
        "time_budget": "20m",
        "spec_level": "exact",
        "done_when": DONE_WHEN,
        "write_paths": ["notes/report.md"],
        "negative_control": "none: this request writes no check",
    }
    if priority:
        entry["priority"] = priority
    return entry | overrides


def _batch(repository: Path, entries: list[dict], *, budget: int | None = None) -> dict:
    from reckon.crew import dispatch_batch

    config = _config(budget=budget)
    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=lambda _project: config,
        repo_root=repository,
    )


def _pointers() -> list[dict]:
    return [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if str(pointer.get("phase") or "") in HOLDING
    ]


def _pointer_for(node: str) -> dict:
    for pointer in _pointers():
        if str((pointer.get("node") or {}).get("id") or "") == node:
            return pointer
    raise AssertionError(f"no waiting pointer for node {node!r}")


# --- A submission carries its points onto the request it stores ---------------


def test_a_dispatch_carries_its_priority_onto_the_stored_request(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    _stub_hold(monkeypatch, offered_local=True)

    result = _invoke(dispatch_repo, node="held-local", extra=["--priority", "6"])

    payload = _payload(result)
    assert result.exit_code == 0, result.output
    assert payload["queued"] is True, payload
    pointer = runs.list_live(project=PROJECT)[0]
    assert pointer["priority"] == 6
    assert pointer["dispatch_options"]["priority"] == 6


def test_a_batch_entry_stores_the_priority_it_was_given(batch_repo: Path) -> None:
    result = _batch(batch_repo, [_entry("budgeted-six", priority=6)])

    assert result["queued"] == 1, result["results"][0].get("reason")
    pointer = _pointer_for("budgeted-six")
    assert pointer["priority"] == 6
    # The points are on the request, not only in the answer the caller reads,
    # so the admission pass replays them when the request launches.
    assert pointer["dispatch_options"]["priority"] == 6


def test_a_request_naming_no_priority_carries_none(batch_repo: Path) -> None:
    result = _batch(batch_repo, [_entry("unraised")])

    assert result["queued"] == 1, result
    assert _pointer_for("unraised")["priority"] == 0


# --- Points are budgeted per session -----------------------------------------


def test_a_submission_past_the_budget_is_refused_naming_the_points_in_use(
    batch_repo: Path,
) -> None:
    first = _batch(batch_repo, [_entry("holds-six", priority=6)], budget=10)
    assert first["queued"] == 1, first

    second = _batch(batch_repo, [_entry("would-hold-five", priority=5)], budget=10)

    assert second["queued"] == 0, second
    assert second["refused"] == 1, second
    reason = str(second["results"][0]["reason"])
    assert "6 of 10" in reason, reason
    assert "holds-six" in reason, reason
    assert _pointer_for("holds-six")["priority"] == 6

    # The points return when the first request launches, and the same submission
    # is then accepted unchanged.
    launched = runs.read_pointer(str(_pointer_for("holds-six")["run_id"]))
    launched["phase"] = "running"
    runs._write_json(runs.pointer_path(str(launched["run_id"])), launched)

    third = _batch(batch_repo, [_entry("would-hold-five", priority=5)], budget=10)

    assert third["queued"] == 1, third
    assert _pointer_for("would-hold-five")["priority"] == 5


def test_a_dropped_request_returns_its_points(batch_repo: Path) -> None:
    first = _batch(batch_repo, [_entry("holds-six", priority=6)], budget=10)
    assert first["queued"] == 1, first
    second = _batch(batch_repo, [_entry("holds-four", priority=4)], budget=10)
    assert second["queued"] == 1, second
    assert runs.session_priority_in_use(SESSION)[0] == 10

    from reckon.crew import queue_edit

    queue_edit.drop(str(_pointer_for("holds-six")["run_id"]), project=PROJECT)

    remaining, holders = runs.session_priority_in_use(SESSION)
    assert remaining == 4, holders
    # A dropped request and a discarded one leave by the same path, so this also
    # reads the discard's return.
    third = _batch(batch_repo, [_entry("takes-the-six", priority=6)], budget=10)
    assert third["queued"] == 1, third


def test_an_undeclared_budget_bounds_nothing(batch_repo: Path) -> None:
    result = _batch(
        batch_repo,
        [_entry("big-one", priority=60), _entry("big-two", priority=60)],
        budget=None,
    )

    assert result["queued"] == 2, result


# --- Points a session's waiting requests hold ---------------------------------


def _queued_pointer(
    run_id: str,
    session: str,
    *,
    priority: int = 0,
    queued_at: str,
    project: str = PROJECT,
) -> dict:
    return {
        "run_id": run_id,
        "project": project,
        "session": session,
        "backend": LANE,
        "phase": "queued",
        "queued_at": queued_at,
        "node": {"id": run_id, "plan": "", "section": ""},
        "priority": priority,
    }


def _order(queued: list[dict], live: list[dict]) -> list[str]:
    return [
        str(pointer["run_id"])
        for pointer, _reason in queue_order.admission_order_details(
            queued, live, now=NOW
        )
    ]


def test_the_priority_orders_two_sessions_before_their_live_run_count() -> None:
    busy = _queued_pointer(
        "busy",
        SESSION,
        priority=3,
        queued_at=(NOW - timedelta(minutes=5)).isoformat(),
    )
    # The two sessions work in different projects, which is where the priority
    # must outrank fair share: within one project the priority already sorted
    # ahead of the roadmap key, so only a cross-project pair measures it.
    idle = _queued_pointer(
        "idle",
        OTHER_SESSION,
        queued_at=(NOW - timedelta(minutes=4)).isoformat(),
        project=f"{PROJECT}-other",
    )
    # The busy session already runs three workers on the lane; the idle one
    # runs none, so fair share alone would put the idle session first.
    live = [
        {
            "run_id": f"live-{index}",
            "project": PROJECT,
            "session": SESSION,
            "backend": LANE,
            "phase": "running",
        }
        for index in range(3)
    ]

    assert _order([busy, idle], live) == ["busy", "idle"]


def test_an_aged_request_launches_before_a_fresh_priority_request() -> None:
    aged = _queued_pointer(
        "aged",
        SESSION,
        queued_at=(
            NOW - timedelta(seconds=queue_order.STARVATION_AGE_SECONDS + 60)
        ).isoformat(),
    )
    fresh = _queued_pointer(
        "fresh",
        OTHER_SESSION,
        priority=3,
        queued_at=(NOW - timedelta(minutes=1)).isoformat(),
    )

    assert _order([fresh, aged], []) == ["aged", "fresh"]


def test_a_delivered_review_launches_before_a_prioritised_request() -> None:
    review = _queued_pointer(
        "review",
        OTHER_SESSION,
        queued_at=(NOW - timedelta(minutes=2)).isoformat(),
    )
    review["role"] = "review"
    raised = _queued_pointer(
        "raised",
        SESSION,
        priority=9,
        queued_at=(NOW - timedelta(minutes=3)).isoformat(),
    )

    assert _order([raised, review], []) == ["review", "raised"]
