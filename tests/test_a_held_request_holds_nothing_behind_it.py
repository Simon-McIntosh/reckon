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
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode
from tests.test_a_claim_hold_reads_the_holder_again import _stopped_holder
from tests.test_a_queued_run_is_admitted import admit_repo  # noqa: F401 - fixture

PROJECT = "proj"
LANE = "clive"
GATED = "gated"
SESSION_A = "coordinator-a"
SESSION_B = "coordinator-b"
SHARED = "shared.txt"
CLAIMED = "claimed.txt"
START = datetime(2026, 11, 3, 11, 0, 0, tzinfo=UTC)
EARLIER = "r-earlier"
LATER = "r-later"


def _lane_document(tmp_path: Path) -> Path:
    lane = tmp_path / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 2,
                "admission": {
                    "worker_slots": 2,
                    "observed_seconds": 900,
                    "sessions": {
                        SESSION_A: {"worker_slots": 1},
                        SESSION_B: {"worker_slots": 1},
                    },
                    "verdict": "full",
                },
            }
        ),
        encoding="utf-8",
    )
    return lane


def _gate_document(tmp_path: Path) -> Path:
    gate = tmp_path / "gate.json"
    gate.write_text(
        json.dumps({"paused": True, "reason": "moving"}), encoding="utf-8"
    )
    return gate


def _config(tmp_path: Path, *, gated: bool = False) -> dict[str, Any]:
    backend: dict[str, Any] = {
        "launch": "cli",
        "command": "codex",
        "model": "local-model",
        "effort": "medium",
        "sandbox": "worktree-full",
        "time_budget": "25m",
        "lane_document": str(_lane_document(tmp_path)),
    }
    config: dict[str, Any] = {
        "default_backend": LANE,
        "local_backend": LANE,
        "backends": {LANE: dict(backend)},
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if gated:
        config["backends"][GATED] = {
            **backend,
            "gate_document": str(_gate_document(tmp_path)),
        }
    return config


def _request(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    session: str,
    write_paths: list[str],
    queued_at: datetime,
    backend: str = LANE,
) -> dict[str, Any]:
    node = TaskNode(
        id=node_id,
        goal="admit a queued node that overlaps an earlier request",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_held_request_holds_nothing_behind_it.py passes",
        write_paths=list(write_paths),
        time_budget="20m",
        spec_level="exact",
    )
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "repo": str(repository),
        "node": node.as_dict(),
        "backend": backend,
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": queued_at.isoformat(),
        "created_at": queued_at.isoformat(),
        "hold": {"held": True},
        "dispatch_options": {
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": session,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    }


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _verdicts(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("run_id") or ""): row for row in result["verdicts"]}


def _pair(
    repository: Path,
    *,
    earlier_paths: list[str] | None = None,
    earlier_backend: str = LANE,
) -> None:
    """Queue A and B, where B writes a path A also writes."""
    runs.queue_dispatch(
        _request(
            repository,
            node_id="earlier",
            run_id=EARLIER,
            session=SESSION_A,
            write_paths=earlier_paths or [SHARED],
            queued_at=START - timedelta(minutes=3),
            backend=earlier_backend,
        )
    )
    runs.queue_dispatch(
        _request(
            repository,
            node_id="later",
            run_id=LATER,
            session=SESSION_B,
            write_paths=[SHARED],
            queued_at=START - timedelta(minutes=2),
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
    config = _config(tmp_path)
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
    config = _config(tmp_path, gated=True)
    _pair(admit_repo, earlier_backend=GATED)
    recorded: list = []

    result = queue_admission.admit_once(
        now=START, config_for=lambda _pointer: config, launcher=_launcher(recorded)
    )

    assert _verdicts(result)[EARLIER]["verdict"] == "held-by-gate"
    _assert_later_launched(result, recorded)


def test_a_request_held_on_its_retry_backoff_holds_nothing_behind_it(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path)
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
    config = _config(tmp_path)
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
    config = _config(tmp_path)
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


def test_a_reprioritised_request_is_ranked_first_across_sessions_and_projects(
    admit_repo: Path, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    queued = [
        _request(
            admit_repo,
            node_id="older-one",
            run_id="r-older-one",
            session=SESSION_A,
            write_paths=["one.txt"],
            queued_at=START - timedelta(minutes=9),
        ),
        _request(
            admit_repo,
            node_id="older-two",
            run_id="r-older-two",
            session=SESSION_B,
            write_paths=["two.txt"],
            queued_at=START - timedelta(minutes=8),
        ),
        _request(
            admit_repo,
            node_id="raised",
            run_id="r-raised",
            session="coordinator-c",
            write_paths=["three.txt"],
            queued_at=START - timedelta(minutes=1),
        ),
    ]
    # The raised request is the freshest on its own project, and the two older
    # ones lead their projects, so the session and project ranking puts both
    # of them ahead of it unless the override is applied across the lane.
    queued[0]["project"] = "another-project"
    queued[1]["project"] = "third-project"
    queued[2]["priority_override"] = 100

    placements = queue_admission.queued_placements(
        queued, now=START, config_for=lambda _pointer: config
    )

    assert placements["r-raised"]["position"] == 1
    assert placements["r-raised"]["reason"] == "priority override 100"
    assert placements["r-older-one"]["position"] == 2

    order = queue_admission.fair_share_admission_order(
        queued, queued, now=START, config_for=lambda _pointer: config
    )
    assert [pointer["run_id"] for pointer in order][0] == "r-raised"