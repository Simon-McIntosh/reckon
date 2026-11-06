"""A successful spawn keeps its identity across competing refusal writes."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import queue_admission, routing, runs
from reckon.crew.node import TaskNode
from tests.helpers.git import git
from tests.test_a_queued_launch_keeps_the_run_record import (
    PROJECT,
    SESSION,
    _config_for,
    _prime_lane,
    _queued_request,
)
from tests.test_a_queued_launch_keeps_the_run_record import (
    admit_repo as _admit_repo,
)

admit_repo = _admit_repo

DISPATCH = importlib.import_module("reckon.crew.dispatch")
REFUSAL = "CrewError: worktree creation failed: refused earlier attempt"


def _launch(repository, tmp_path, *, path, during_spawn=None):
    config, resolve = _config_for()
    _prime_lane(tmp_path, config)
    config["roles"]["investigate"] = {}
    request = _queued_request(repository, node_id="sample", run_id="r-launch-record")
    request["node"]["role"] = "investigate"
    request["dispatch_options"]["allow_unreviewed_plan"] = True
    captured = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        captured["argv"] = list(plan.argv)
        captured["record"] = runs.read_pointer(request["run_id"])
        Path(log_path).write_text(
            json.dumps({"type": "thread.started", "thread_id": "fixture-session"})
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {}})
            + "\n"
        )
        if during_spawn:
            during_spawn(request)
        process = subprocess.Popen(["true"])
        process.wait(timeout=5)
        return process.pid

    if path == "admission":
        runs.queue_dispatch(request)
        result = queue_admission.admit_once(config_for=resolve, launcher=launcher)
        assert result["launched"] == [request["run_id"]], result
    else:
        DISPATCH.dispatch(
            node=TaskNode(**request["node"]),
            project=PROJECT,
            repo=repository,
            config=config,
            session=SESSION,
            run_id=request["run_id"],
            launcher=launcher,
            local=True,
            watch_override=True,
            unreviewed_plan_override=True,
        )
    return request, captured


def _assert_identity(request, captured):
    record = runs.read_pointer(request["run_id"])
    assert record.get("argv") == captured["argv"], record
    for key in ("worktree", "base_sha", "backend", "node", "prompt_path"):
        assert record.get(key) == captured["record"][key], (key, record)
    assert record.get("launch_recorded_at"), record
    assert "failure" not in record, record
    assert record.get("admission_history"), record
    assert record["admission_history"][-1]["failure"] == REFUSAL
    return record


def test_a_queued_request_does_not_hold_its_own_launch_path(
    admit_repo, tmp_path, monkeypatch
):
    request = _queued_request(admit_repo, node_id="sample", run_id="r-own-path")
    tree = routing._launch_worktree_path(admit_repo, SESSION, "sample")
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(admit_repo, "worktree", "add", "--detach", str(tree), "HEAD")
    request["worktree"] = str(tree)
    runs.queue_dispatch(request)
    config, resolve = _config_for()
    _prime_lane(tmp_path, config)
    launched = []
    create_worktree = DISPATCH._create_worktree

    def create(repo, session, worker, base):
        def name_path(pointer):
            pointer["worktree"] = str(tree)
            return pointer

        runs._mutate_pointer(request["run_id"], name_path)
        return create_worktree(repo, session, worker, base)

    monkeypatch.setattr(DISPATCH, "_create_worktree", create)

    def launcher(plan, **kwargs):
        launched.append(plan)
        return 99999999

    result = queue_admission.admit_once(config_for=resolve, launcher=launcher)
    assert result["launched"] == [request["run_id"]], result
    assert len(launched) == 1
    assert runs.read_pointer(request["run_id"])["worktree"] == str(tree)


@pytest.mark.parametrize("path", ["direct", "admission"])
def test_the_spawn_republishes_the_full_record(admit_repo, tmp_path, path):
    def replace_while_spawning(request):
        queue_admission._restore_queued(request, REFUSAL)

    request, captured = _launch(
        admit_repo, tmp_path, path=path, during_spawn=replace_while_spawning
    )
    _assert_identity(request, captured)


@pytest.mark.parametrize("path", ["direct", "admission"])
@pytest.mark.parametrize("writer", ["annotate", "restore"])
def test_a_late_refusal_only_appends_history(admit_repo, tmp_path, path, writer):
    request, captured = _launch(admit_repo, tmp_path, path=path)
    if writer == "annotate":
        assert queue_admission._record_queued_failure(request, REFUSAL) == (
            request["run_id"],
            None,
        )
    else:
        queue_admission._restore_queued(request, REFUSAL)
    _assert_identity(request, captured)


def test_publication_preserves_a_supervisors_newer_state(admit_repo, tmp_path):
    def advance(request):
        def change(record):
            record.update(
                phase="complete",
                session_id="observed-session",
                launched_at="2026-01-01T00:00:00Z",
            )
            return record

        runs._mutate_pointer(request["run_id"], change)

    request, captured = _launch(
        admit_repo, tmp_path, path="direct", during_spawn=advance
    )
    record = runs.read_pointer(request["run_id"])
    assert record["phase"] == "complete"
    assert record["session_id"] == "observed-session"
    assert record["launched_at"] == "2026-01-01T00:00:00Z"
    assert record["argv"] == captured["argv"]


def test_a_peer_still_holds_a_launch_path(admit_repo):
    tree = routing._launch_worktree_path(admit_repo, SESSION, "sample")
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(admit_repo, "worktree", "add", "--detach", str(tree), "HEAD")
    runs._write_json(
        runs.pointer_path("r-peer"),
        {
            "run_id": "r-peer",
            "phase": "queued",
            "worktree": str(tree),
        },
    )
    assert "still holds live run(s) r-peer" in routing._stale_launch_tree_refusal(
        admit_repo, tree, "HEAD"
    )
    assert tree.is_dir()


def test_complete_accepts_the_unrepaired_launch_record(admit_repo, tmp_path):
    request, captured = _launch(admit_repo, tmp_path, path="direct")
    queue_admission._restore_queued(request, REFUSAL)
    manifest = Path(captured["record"]["manifest_path"])
    manifest.write_text(
        "node: sample\nstatus: complete\ncommits: []\nchanged_paths: []\ntests: true\n"
    )
    result = crew.complete(
        request["run_id"],
        root=admit_repo,
        gate="not-run",
        outcome="The investigation delivered without changing repository files.",
    )
    assert result["record"]["run_id"] == request["run_id"]
    assert result["record"]["base_sha"] == captured["record"]["base_sha"]
    assert not runs.pointer_path(request["run_id"]).exists()


@pytest.mark.parametrize("path", ["direct", "admission"])
def test_a_refused_dispatch_cannot_restore_over_another_launch(
    admit_repo, tmp_path, monkeypatch, path
):
    request, captured = _launch(admit_repo, tmp_path, path=path)
    launched = runs.read_pointer(request["run_id"])
    runs._write_json(runs.pointer_path(request["run_id"]), request)
    config, resolve = _config_for()
    config["roles"]["investigate"] = {}
    _prime_lane(tmp_path, config)

    def refuse(repo, session, worker, base):
        # The other attempt publishes between this attempt's claim and refusal.
        runs._write_json(runs.pointer_path(request["run_id"]), launched)
        tree = Path(launched["worktree"])
        runs._write_json(
            runs.pointer_path("r-holder"),
            {
                "run_id": "r-holder",
                "phase": "working",
                "worktree": str(tree),
            },
        )
        refusal = routing._stale_launch_tree_refusal(repo, tree, base)
        assert "still holds live run(s) r-holder" in refusal
        raise crew.CrewError("worktree creation failed: " + refusal)

    monkeypatch.setattr(DISPATCH, "_create_worktree", refuse)
    with pytest.raises(crew.CrewError, match="still holds live run"):
        queue_admission._launch_queued(
            request,
            launcher=lambda *args, **kwargs: pytest.fail("refused attempt spawned"),
            config_for=resolve,
        )
    record = runs.read_pointer(request["run_id"])
    assert record["argv"] == captured["argv"]
    assert record["worktree"] == launched["worktree"]
    assert record["base_sha"] == launched["base_sha"]
    assert record["launch_recorded_at"] == launched["launch_recorded_at"]
    assert "failure" not in record
    assert "still holds live run" in record["admission_history"][-1]["failure"]
