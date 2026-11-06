"""A request the lane already holds parks nothing behind it.

On 3 November an admission pass held the repair for two tests red on main as
``held-behind-earlier-request``: an earlier request named the same write path
and was itself ``held-on-claim`` behind another run's worktree. Neither
launched, and the repair's priority override of 100 changed nothing. An
earlier request is only a reason to wait while it could launch in the same
pass; one the pass has already held on a claim, a gate, a back-off or a plan
review is waiting on something outside the queue and fences nothing.

The four holds below are exercised one at a time against the same pair of
overlapping requests. The plan-review case stands in the predicate that reads a
plan's review coverage -- a stored-review comparison whose fixture belongs to
the plan and review modules -- while the hold it writes and the overlap check
that follows it are the real ones.

The checkout, lane document, request builder and launcher are the ones the
overlapping-queue suite already builds, so a change to the shape of a queued
request is made in one place rather than two.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, queue_order, runs
from tests.test_a_claim_hold_reads_the_holder_again import _stopped_holder
from tests.test_a_queued_run_is_admitted import admit_repo  # noqa: F401 - fixture
from tests.test_overlapping_queued_requests_launch_in_turn import (  # noqa: F401
    LANE,
    PROJECT,
    SESSION_A,
    SESSION_B,
    SHARED,
    START,
    _config,
    _lane_document,
    _launcher,
    _queue,
)

GATED = "gated"
CLAIMED = "claimed.txt"
EARLIER = "r-earlier"
LATER = "r-later"


def _open_config(tmp_path: Path) -> dict[str, Any]:
    """The shared lane config with both sessions granted one slot each."""
    return _config(_lane_document(tmp_path, slots={SESSION_A: 1, SESSION_B: 1}))


def _gated_config(tmp_path: Path) -> dict[str, Any]:
    """The same lane config, with a second backend whose gate is paused."""
    config = _open_config(tmp_path)
    gate = tmp_path / "gate.json"
    gate.write_text('{"paused": true, "reason": "moving"}', encoding="utf-8")
    config["backends"][GATED] = {
        **config["backends"][LANE],
        "gate_document": str(gate),
    }
    return config


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("run_id") or ""): row for row in result["verdicts"]}


def _pair(repository: Path, *, earlier_paths: list[str] | None = None) -> None:
    """Queue A and B, where B writes a path A also writes."""
    runs.queue_dispatch(
        _queue(
            repository,
            node_id="earlier",
            run_id=EARLIER,
            session=SESSION_A,
            write_paths=earlier_paths or [SHARED],
            queued_at=(START - timedelta(minutes=3)).isoformat(),
        )
    )
    runs.queue_dispatch(
        _queue(
            repository,
            node_id="later",
            run_id=LATER,
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at=(START - timedelta(minutes=2)).isoformat(),
        )
    )


def _assert_later_launched(result: dict[str, Any], recorded: list) -> None:
    assert result["launched"] == [LATER], result["verdicts"]
    assert len(recorded) == 1
    assert runs.read_pointer(LATER)["phase"] != "queued"


# --- The four holds that fence nothing ----------------------------------------


def test_a_request_held_on_a_peers_claim_holds_nothing_behind_it(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _open_config(tmp_path)
    # The holder claims only the earlier request's own path, so the later
    # request overlaps the earlier one but nothing fences the later one.
    _stopped_holder(admit_repo, tmp_path)
    _pair(admit_repo, earlier_paths=[CLAIMED, SHARED])
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert _verdicts(result)[EARLIER]["verdict"] == "held-on-claim"
    _assert_later_launched(result, recorded)


def test_a_request_held_by_the_lane_gate_holds_nothing_behind_it(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _gated_config(tmp_path)
    _pair(admit_repo)
    earlier = runs.read_pointer(EARLIER)
    earlier["backend"] = GATED
    runs._write_json(runs.pointer_path(EARLIER), earlier)
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert _verdicts(result)[EARLIER]["verdict"] == "held-by-gate"
    _assert_later_launched(result, recorded)


def test_a_request_held_on_its_retry_backoff_holds_nothing_behind_it(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _open_config(tmp_path)
    _pair(admit_repo)
    failed = runs.read_pointer(EARLIER)
    failed["failure"] = "RuntimeError: the previous attempt did not start"
    failed["failure_at"] = (START - timedelta(minutes=1)).isoformat()
    failed["retry_at"] = (START + timedelta(minutes=5)).isoformat()
    runs._write_json(runs.pointer_path(EARLIER), failed)
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert _verdicts(result)[EARLIER]["verdict"] == "held-on-retry-backoff"
    _assert_later_launched(result, recorded)


def test_a_request_held_for_a_plan_review_holds_nothing_behind_it(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _open_config(tmp_path)
    _pair(admit_repo)

    def owed(pointer, *, config_for):
        if str(pointer.get("run_id") or "") == EARLIER:
            return "plan 'plan-a' changed after this request was queued"
        return None

    monkeypatch.setattr(queue_admission, "_plan_review_owed", owed)
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert _verdicts(result)[EARLIER]["verdict"] == "held-on-plan-review"
    _assert_later_launched(result, recorded)


# --- The earlier request can launch, so it still fences -----------------------


def test_an_earlier_request_that_could_launch_still_holds_the_later_one(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _open_config(tmp_path)
    _pair(admit_repo)
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert result["launched"] == [EARLIER]
    assert len(recorded) == 1
    assert _verdicts(result)[LATER]["verdict"] == "held-behind-earlier-request"
    assert runs.read_pointer(LATER)["phase"] == "queued"
    assert not runs.read_pointer(LATER).get("failure")


# --- An explicit override ranks ahead of every request across the lane --------


def _three_projects(repository: Path) -> list[dict[str, Any]]:
    queued = [
        _queue(
            repository,
            node_id="older-one",
            run_id="r-older-one",
            session=SESSION_A,
            write_paths=["one.txt"],
            queued_at=(START - timedelta(minutes=9)).isoformat(),
        ),
        _queue(
            repository,
            node_id="older-two",
            run_id="r-older-two",
            session=SESSION_B,
            write_paths=["two.txt"],
            queued_at=(START - timedelta(minutes=8)).isoformat(),
        ),
        _queue(
            repository,
            node_id="raised",
            run_id="r-raised",
            session="coordinator-c",
            write_paths=["three.txt"],
            queued_at=(START - timedelta(minutes=1)).isoformat(),
        ),
    ]
    # The raised request is the freshest on its own project, and the two older
    # ones lead their projects, so the session and project ranking puts both
    # of them ahead of it unless the override is applied across the lane.
    queued[0]["project"] = "another-project"
    queued[1]["project"] = "third-project"
    queued[2]["priority_override"] = 100
    return queued


def test_a_reprioritised_request_is_ranked_first_across_sessions_and_projects(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _open_config(tmp_path)
    queued = _three_projects(admit_repo)

    placements = queue_admission.queued_placements(
        queued, now=START, config_for=lambda _pointer: config
    )

    assert placements["r-raised"]["position"] == 1
    assert placements["r-raised"]["reason"] == "priority override 100"
    assert placements["r-older-one"]["position"] == 2

    order = queue_admission.fair_share_admission_order(
        queued, queued, now=START, config_for=lambda _pointer: config
    )
    assert order[0]["run_id"] == "r-raised"


def test_the_hoist_reads_the_override_the_queue_ordering_reads(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one reader of ``priority_override`` is the queue ordering's own.

    A second definition here would agree with it only until one of them moved,
    so the hoist is asked to follow a patched ordering and to stop when the
    ordering stops seeing an override.
    """
    config = _open_config(tmp_path)
    queued = _three_projects(admit_repo)
    raised_first = queue_admission.queued_placements(
        queued, now=START, config_for=lambda _pointer: config
    )
    assert raised_first["r-raised"]["position"] == 1

    monkeypatch.setattr(queue_order, "_override", lambda _pointer: None)
    unraised = queue_admission.queued_placements(
        queued, now=START, config_for=lambda _pointer: config
    )

    assert unraised["r-raised"]["position"] == 3
