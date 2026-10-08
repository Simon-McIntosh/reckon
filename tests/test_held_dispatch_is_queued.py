"""A local lane hold leaves one durable, withdrawable queued dispatch."""

from __future__ import annotations

import copy
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import recovery, runs
from tests import test_dispatch_names_its_backend as backend_tests


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _config(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": 4,
                    "observed_seconds": 900,
                    "sessions": {"session": {"worker_slots": 0, "live_runs": 1}},
                    "verdict": "full",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    config = copy.deepcopy(backend_tests.CONFIG)
    config["local_backend"] = "clive"
    config["backends"]["clive"]["lane_document"] = str(lane)
    config["backends"]["beta"]["lane_document"] = str(lane)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )


def _dispatch(repo: Path, node: str = "held-node", *extra: str):
    return CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(repo, node=node, dry_run=False),
            "--local",
            "--no-watch",
            *extra,
        ],
    )


def test_held_local_dispatch_is_one_queued_pointer_and_can_be_discarded(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    assert runs.crew_home() == dispatch_repo.parent / "config" / "crew"
    worktrees_before = subprocess.check_output(
        ["git", "-C", str(dispatch_repo), "worktree", "list", "--porcelain"],
        text=True,
    )

    first = _dispatch(dispatch_repo)
    first_payload = backend_tests._payload(first)
    assert first.exit_code == 75
    assert first_payload["run_id"]
    assert first_payload["position"] == 1
    assert first_payload["error"] == "lane-paused"
    pointers = runs.list_live(project="proj")
    assert len(pointers) == 1
    queued = pointers[0]
    assert queued["run_id"] == first_payload["run_id"]
    assert queued["phase"] == "queued"
    assert queued["project"] == "proj"
    assert queued["plan"] == "plan-a"
    assert queued["section"] == "dispatch"
    assert queued["node"]["plan"] == "plan-a"
    assert queued["node"]["section"] == "dispatch"
    assert queued["node"]["id"] == "held-node"
    assert queued["session"] == "session"
    assert queued["dispatch_options"]["local"] is True
    assert queued["dispatch_options"]["no_watch"] is True
    assert queued["dispatch_options"]["project"] == "proj"
    assert queued["dispatch_options"]["plan_slug"] == "plan-a"
    assert queued["dispatch_options"]["session"] == "session"
    assert queued["hold"]["held"] is True
    assert queued["queued_at"]
    assert not queued.get("worktree")
    assert not queued.get("pid")
    assert not runs.run_dir(queued["run_id"]).exists()

    second = _dispatch(dispatch_repo, "held-node", "--comment", "updated request")
    second_payload = backend_tests._payload(second)
    assert second.exit_code == 75
    assert second_payload["run_id"] == queued["run_id"]
    assert second_payload["position"] == 1
    assert len(runs.list_live(project="proj")) == 1
    assert runs.read_pointer(queued["run_id"])["queued_at"] == queued["queued_at"]
    assert (
        runs.read_pointer(queued["run_id"])["dispatch_options"]["comment"]
        == "updated request"
    )

    listed = CliRunner().invoke(cli.main, ["crew", "list", "--project", "proj"])
    assert listed.exit_code == 0
    row = json.loads(listed.output)["runs"][0]
    assert row["classification"] == "queued"
    assert row["process_alive"] is not True
    assert row["next_action"] == "wait for a local lane slot"
    assert (
        subprocess.check_output(
            ["git", "-C", str(dispatch_repo), "worktree", "list", "--porcelain"],
            text=True,
        )
        == worktrees_before
    )

    discarded = CliRunner().invoke(
        cli.main, ["crew", "discard", "--run", queued["run_id"]]
    )
    assert discarded.exit_code == 0
    assert json.loads(discarded.output)["pointer_removed"] is True
    assert runs.list_live(project="proj") == []


def test_metered_hold_has_no_queued_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    result = CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(
                dispatch_repo, node="metered-hold", dry_run=False
            ),
            "--backend",
            "beta",
            "--no-watch",
        ],
    )
    assert result.exit_code == 75
    assert runs.list_live(project="proj") == []


def test_named_local_backend_queues_without_a_local_flag(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    result = CliRunner().invoke(
        cli.main,
        [
            *backend_tests._arguments(dispatch_repo, node="named-local", dry_run=False),
            "--backend",
            "clive",
            "--no-watch",
        ],
    )
    assert result.exit_code == 75
    assert backend_tests._payload(result)["position"] == 1
    assert runs.list_live(project="proj", phase="queued")[0]["local"] is True


def test_fleet_gate_hold_has_no_queued_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(dispatch_repo, monkeypatch)
    paused = CliRunner().invoke(
        cli.main, ["crew", "gate", "--pause", "allocation moving"]
    )
    assert paused.exit_code == 0
    result = _dispatch(dispatch_repo, "fleet-held")
    assert result.exit_code == 75
    assert backend_tests._payload(result)["lane_gate"]["gate"] == "fleet"
    assert runs.list_live(project="proj") == []


def _queued_request(section: str, run_id: str) -> dict:
    joined = datetime.now(UTC).isoformat()
    return {
        "run_id": run_id,
        "project": "proj",
        "session": "session",
        "node": {"id": "shared-node", "plan": "plan-a", "section": section},
        "phase": "queued",
        "backend": "clive",
        "queued_at": joined,
        "created_at": joined,
    }


def test_queued_identity_includes_the_section(dispatch_repo: Path) -> None:
    first, _ = runs.queue_dispatch(_queued_request("first", "r-first"))
    second, _ = runs.queue_dispatch(_queued_request("second", "r-second"))

    assert first["run_id"] == "r-first"
    assert second["run_id"] == "r-second"
    assert {row["node"]["section"] for row in runs.list_live(project="proj")} == {
        "first",
        "second",
    }


def test_queued_write_obeys_the_per_run_pointer_lock(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-locked"
    runs.queue_dispatch(_queued_request("first", run_id))
    original_lock = runs._pointer_lock
    original_write = runs._write_json
    attempting = Event()
    written = Event()

    def announced_lock(target: str):
        attempting.set()
        return original_lock(target)

    def marked_write(path: Path, payload: dict) -> None:
        if path == runs.pointer_path(run_id):
            written.set()
        original_write(path, payload)

    monkeypatch.setattr(runs, "_pointer_lock", announced_lock)
    monkeypatch.setattr(runs, "_write_json", marked_write)
    updated = _queued_request("first", "r-new-request")
    updated["reason"] = "router still full"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with original_lock(run_id):
            future = pool.submit(runs.queue_dispatch, updated)
            reached_lock = attempting.wait(3)
            wrote_while_locked = written.wait(0.1)
        assert reached_lock
        assert not wrote_while_locked
        queued, _ = future.result(timeout=3)

    assert queued["run_id"] == run_id
    assert runs.read_pointer(run_id)["reason"] == "router still full"


def test_queued_counter_is_waiting_and_never_working() -> None:
    assert recovery._fleet_counts({"run": {"state": "queued"}}) == {
        "working": 0,
        "blocked": 0,
        "unpromoted": 0,
        "waiting": 1,
    }
