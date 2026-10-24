"""A dispatch yields its lane slot to its own session's older queued request.

Nothing ordered a session's direct dispatch behind that same session's older
queued request on the same lane, so its own new work kept taking the slots its
oldest waiting request needed. Now a dispatch that finds room spends it on the
oldest of its session's queued requests on that lane the admission pass could
admit, launching it through the pass's own path, and queues itself behind it.
A request the pass would hold is skipped, and another session's request neither
launches nor moves.

The launch is stubbed, so no worker starts; the dispatch path is the subject.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew_dispatch_commands
from reckon.crew import runs
from reckon.crew.dispatch import LaneHeld, dispatch
from reckon.crew.node import TaskNode
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    admit_repo,
)

LANE = "clive"
OLDER_NODE = "older-node"
NEW_NODE = "new-node"
OTHER_SESSION = "coordinator-b"


def _config_with_lane(lane_document: Path) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config["backends"][LANE]["lane_document"] = str(lane_document)
    return config


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(plan)
        return os.getpid()

    return launch


def _node(node_id: str) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal="stand in for a queued and a fresh dispatch",
        plan="plan-a",
        section="dispatch",
        done_when="pytest tests/test_a_dispatch_yields_to_its_sessions_older_request.py passes",
        write_paths=[f"{node_id}.txt"],
        time_budget="20m",
        spec_level="exact",
    )


def _queue(
    repository: Path,
    *,
    node_id: str,
    run_id: str,
    session: str = SESSION,
    queued_at: str | None = None,
    failure: str = "",
    retry_at: str = "",
) -> dict:
    joined = queued_at or datetime.now(UTC).isoformat()
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "repo": str(repository),
        "node": _node(node_id).as_dict(),
        "backend": LANE,
        "launch": "cli",
        "local": True,
        "phase": "queued",
        "queued_at": joined,
        "created_at": joined,
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
    if failure:
        record["failure"] = failure
        record["failure_at"] = joined
        record["failure_attempts"] = 1
        record["retry_at"] = retry_at
    return record


def _resolve_config(config: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )


def _dispatch(repository: Path, config: dict, launcher, node_id: str = NEW_NODE):
    return dispatch(
        node=_node(node_id),
        project=PROJECT,
        repo=str(repository),
        config=config,
        session=SESSION,
        launcher=launcher,
        local=True,
        backend_override=LANE,
        route="deterministic",
        check_budget=False,
        watch_required=False,
        watch_override=True,
        dispatch_options={
            "project": PROJECT,
            "plan_slug": "plan-a",
            "section": "dispatch",
            "session": SESSION,
            "local": True,
            "no_watch": True,
            "overrides": [],
        },
    )


def _live_by_node() -> dict[str, dict]:
    return {
        str(pointer["node"]["id"]): pointer
        for pointer in runs.list_live(project=PROJECT)
    }


# --- Case 1: the older request launches, the fresh dispatch queues behind it ---


def test_a_dispatch_launches_its_sessions_older_request_and_queues_behind_it(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_lane(_lane_document(tmp_path, worker_slots=1))
    _resolve_config(config, monkeypatch)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id=OLDER_NODE,
            run_id="r-older",
            queued_at="2026-10-14T11:00:00+00:00",
        )
    )
    recorded: list = []

    with pytest.raises(LaneHeld) as held:
        _dispatch(admit_repo, config, _launcher(recorded))

    live = _live_by_node()
    # The older request left the queue and launched under its own run id, so
    # its pointer now carries the launched phase rather than the queued one.
    assert runs.pointer_path("r-older").exists()
    assert live[OLDER_NODE]["run_id"] == "r-older"
    assert live[OLDER_NODE]["phase"] != "queued"
    assert recorded, "the older request's launch reached the launcher"
    # The fresh dispatch queued behind it.
    assert held.value.queued["phase"] == "queued"
    assert live[NEW_NODE]["phase"] == "queued"
    assert live[NEW_NODE]["run_id"] == held.value.queued["run_id"]


# --- Case 2: a request inside its back-off is skipped, the dispatch launches --


def test_a_dispatch_launches_itself_when_its_request_is_inside_its_back_off(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_lane(_lane_document(tmp_path, worker_slots=1))
    _resolve_config(config, monkeypatch)
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id=OLDER_NODE,
            run_id="r-older",
            queued_at="2026-10-14T11:00:00+00:00",
            failure="FileNotFoundError: the brief is missing",
            retry_at=future,
        )
    )
    recorded: list = []

    launched = _dispatch(admit_repo, config, _launcher(recorded))

    live = _live_by_node()
    assert launched["node"]["id"] == NEW_NODE
    assert live[NEW_NODE]["phase"] != "queued"
    assert live[OLDER_NODE]["phase"] == "queued"
    assert live[OLDER_NODE]["run_id"] == "r-older"


# --- Case 3: another session's queued request is neither launched nor moved ---


def test_a_dispatch_leaves_another_sessions_queued_request_alone(
    admit_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config_with_lane(_lane_document(tmp_path, worker_slots=1))
    _resolve_config(config, monkeypatch)
    runs.queue_dispatch(
        _queue(
            admit_repo,
            node_id=OLDER_NODE,
            run_id="r-other",
            session=OTHER_SESSION,
            queued_at="2026-10-14T11:00:00+00:00",
        )
    )
    recorded: list = []

    launched = _dispatch(admit_repo, config, _launcher(recorded))

    live = _live_by_node()
    assert launched["node"]["id"] == NEW_NODE
    assert live[NEW_NODE]["phase"] != "queued"
    other = runs.read_pointer("r-other")
    assert other["phase"] == "queued"
    assert other["session"] == OTHER_SESSION
