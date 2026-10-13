"""A node is queued at most once while a run of it is queued, admitted or live.

The admission pass releases a queued pointer to launch it, so for a moment
nobody on disk names that node. A dispatch arriving then queued a second copy,
which cycled beside its working twin until somebody discarded it. The cases
below fix what a dispatch must do in each state a run of the node can be in.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_dispatch_commands
from reckon.crew import runs
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

LOCAL = "clive"
PROJECT = "proj"
SESSION = "session"
NODE = "held-node"
PLAN = "plan-a"
SECTION = "dispatch"


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _configure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, session_slots: float
) -> dict:
    """Point the local lane at a document granting the session a slot count."""
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": session_slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": session_slots}},
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _arguments(repo: Path, node: str, write_paths: tuple[str, ...]) -> list[str]:
    arguments = backend_tests._arguments(repo, node=node, dry_run=False)
    index = arguments.index("--write-path")
    arguments[index + 1 : index + 2] = list(write_paths)
    return arguments


def _dispatch(repo: Path, node: str = NODE, *extra: str, paths=("result.json",)):
    return CliRunner().invoke(
        cli.main,
        [*_arguments(repo, node, tuple(paths)), "--local", "--no-watch", *extra],
    )


def _node(node_id: str = NODE, write_paths=("result.json",)) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="queue the node once",
        plan=PLAN,
        section=SECTION,
        role="implement",
        spec_level="exact",
        done_when="pytest reports the node-is-queued-once cases passed",
        write_paths=list(write_paths),
        time_budget="25m",
    )


def _live(
    run_id: str,
    *,
    phase: str,
    node_id: str = NODE,
    write_paths=("result.json",),
    pid: int | None = None,
) -> dict[str, Any]:
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "node": {
            "id": node_id,
            "plan": PLAN,
            "section": SECTION,
            "write_paths": list(write_paths),
        },
    }
    if pid is not None:
        record["pid"] = pid
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _request(run_id: str, *, node_id: str = NODE) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "plan": PLAN,
        "section": SECTION,
        "node": {
            "id": node_id,
            "plan": PLAN,
            "section": SECTION,
            "write_paths": ["result.json"],
        },
        "phase": "queued",
        "queued_at": "2026-10-13T00:00:00+00:00",
        "created_at": "2026-10-13T00:00:00+00:00",
    }


def _answer(repo: Path, config: Mapping[str, Any], *, node_id: str = NODE):
    """The hold a dispatch answers with, read from the dispatch itself."""
    from reckon.crew.dispatch_admission import LaneHeld

    with pytest.raises(LaneHeld) as caught:
        dispatch_module.dispatch(
            node=_node(node_id),
            project=PROJECT,
            repo=str(repo),
            config=config,
            session=SESSION,
            local=True,
            backend_override=LOCAL,
        )
    return caught.value


# --- Case 1: the admission handover names the run being admitted -------------


def test_a_dispatch_in_the_handover_writes_no_second_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    # The pass has taken the request: no pointer survives, a handover does.
    runs.write_handover(_request("r-admitted"))
    assert runs.list_live(project=PROJECT) == []

    result = _dispatch(dispatch_repo)
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    assert payload["run_id"] == "r-admitted"
    assert payload["position"] is None
    assert runs.list_live(project=PROJECT) == []
    assert [h["run_id"] for h in runs.list_handovers(PROJECT)] == ["r-admitted"]


def test_the_handover_answer_reports_the_admitted_pointer_and_phase(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _configure(dispatch_repo, monkeypatch, session_slots=0)
    runs.write_handover(_request("r-admitted"))

    held = _answer(dispatch_repo, config)
    assert held.queued["run_id"] == "r-admitted"
    assert held.queued["phase"] == "admitted"
    assert held.queued["position"] is None


# --- Case 2: a working run is reported, never queued over --------------------


def test_a_dispatch_of_a_working_node_reports_it_and_writes_no_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _configure(dispatch_repo, monkeypatch, session_slots=0)
    _live("r-working", phase="working", pid=os.getpid())

    result = _dispatch(dispatch_repo)
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["run_id"] == "r-working"
    assert payload["position"] is None
    assert [p["run_id"] for p in runs.list_live(project=PROJECT)] == ["r-working"]

    held = _answer(dispatch_repo, config)
    assert held.queued["run_id"] == "r-working"
    assert held.queued["phase"] == "working"
    assert held.queued["position"] is None


# --- Case 3: a request put back after a failed launch is updated in place ----


def test_a_dispatch_after_a_put_back_updates_that_one_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    runs.queue_dispatch(_request("r-requeued"))

    result = _dispatch(dispatch_repo, NODE, "--comment", "a second attempt")
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload["run_id"] == "r-requeued"
    assert payload["position"] == 1
    pointers = runs.list_live(project=PROJECT)
    assert [pointer["run_id"] for pointer in pointers] == ["r-requeued"]
    assert pointers[0]["dispatch_options"]["comment"] == "a second attempt"


# --- Case 4: a queued node is re-dispatched in place, with no scope conflict --


def test_a_queued_node_is_redispatched_in_place_with_no_scope_conflict(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lane has room, so without the same-node match the re-dispatch would
    # reach the launch path and be refused against the queued pointer's own
    # claim on result.json.
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    runs.queue_dispatch(_request("r-queued"))

    result = _dispatch(dispatch_repo, NODE, "--comment", "add the waiver")
    payload = backend_tests._payload(result)

    assert result.exit_code == 0, result.output
    assert payload.get("error") != "scope-conflict"
    assert payload["queued"] is True
    assert payload["run_id"] == "r-queued"
    pointers = runs.list_live(project=PROJECT)
    assert [pointer["run_id"] for pointer in pointers] == ["r-queued"]
    assert pointers[0]["dispatch_options"]["comment"] == "add the waiver"


# --- Case 5: only the same node is deduplicated ------------------------------


def test_a_different_node_still_queues(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)

    first = _dispatch(dispatch_repo, "node-one", paths=("one.json",))
    second = _dispatch(dispatch_repo, "node-two", paths=("two.json",))

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert backend_tests._payload(first)["queued"] is True
    assert backend_tests._payload(second)["queued"] is True
    assert {pointer["node"]["id"] for pointer in runs.list_live(project=PROJECT)} == {
        "node-one",
        "node-two",
    }


def test_a_different_node_over_a_claimed_path_is_still_refused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, session_slots=0)
    queued = _dispatch(dispatch_repo, "node-one")
    assert queued.exit_code == 0, queued.output

    # The lane now has room, so the second node reaches the launch path, where
    # the queued request's claim on result.json is enforced.
    _configure(dispatch_repo, monkeypatch, session_slots=4)
    refused = _dispatch(dispatch_repo, "node-three")

    assert refused.exit_code == 7, refused.output
    assert backend_tests._payload(refused)["error"] == "scope-conflict"
    assert {pointer["node"]["id"] for pointer in runs.list_live(project=PROJECT)} == {
        "node-one"
    }
