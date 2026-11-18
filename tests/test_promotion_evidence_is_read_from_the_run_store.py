"""Landing and station retirement read the promotion writer's durable store."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reckon import ledger, run_store
from reckon.crew import (
    obligations,
    open_run_index,
    pending_landing,
    recovery_repair_dispatch,
    runs,
)
from reckon.crew.station import SweepBeat
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout


@pytest.fixture()
def promoted_run(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    _origin, repo = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    head = git(repo, "rev-parse", "HEAD")
    tree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(tree), head)
    run_id = "r-store-promoted"
    manifest = runs.run_dir(run_id) / "manifest.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(f'status: complete\ncommits: ["{head}"]\nblockers: none\n')
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "base_sha": head,
        "phase": "complete",
        "session": "owner",
        "role": "implement",
        "launch": "in-harness",
        "manifest_path": str(manifest),
        "node": {"id": "delivered", "time_budget": "20m", "write_paths": ["seed.txt"]},
        "pending_landing_repair": True,
    }
    pointer["pending_landing_repair_attempt"] = pending_landing.pointer_attempt(pointer)
    runs._write_json(runs.pointer_path(run_id), pointer)
    row = {
        **pointer,
        "node": "delivered",
        "commits": [head],
        "promoted_revision": head,
        "gate": "passed",
    }
    return {"pointer": pointer, "row": row, "tree": tree, "repo": repo}


def _promote(run, storage):
    pointer = run["pointer"]
    run_id = pointer["run_id"]
    path = ledger.run_path(PROJECT, run_id, run["repo"])
    if storage == "store":
        aggregate = ledger.ledger_path(PROJECT, run["repo"])
        aggregate.write_text(
            json.dumps({"data": {"members": [], "runs": [], "holds": []}})
        )
        git(run["repo"], "add", str(aggregate))
        git(run["repo"], "commit", "-qm", "test: record empty aggregate")
        git(run["repo"], "rm", str(aggregate))
        git(run["repo"], "commit", "-qm", "test: retire aggregate projection")
        written = ledger.append_run(PROJECT, run["row"], root=run["repo"])
        assert written["commit_paths"] == []
        assert open_run_index.has_row(PROJECT, run_id, run["repo"])
        assert (
            run_store.stored_run_rows(PROJECT, run_ids=[run_id])[0]["run_id"] == run_id
        )
        assert not path.exists()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_run_file(PROJECT, run["row"], run["repo"])
        assert run_store.stored_run_rows(PROJECT) is None
        assert path.is_file()


@pytest.mark.parametrize("storage", ["store", "legacy"])
@pytest.mark.parametrize("route", ["landing", "station"])
def test_a_promoted_run_releases_its_pointer_and_worktree(promoted_run, storage, route):
    run = promoted_run
    pointer = run["pointer"]
    run_id = pointer["run_id"]
    _promote(run, storage)
    if route == "landing":
        result = pending_landing.finish_pending_landings(only_run=run_id)
        assert any(item["status"] == "already-landed" for item in result), result
    else:
        swept = []
        beat = SweepBeat(
            start=lambda callback: callback(), sweep=lambda *a, **k: swept.append(a)
        )
        assert beat.request(PROJECT, "owner", moment=datetime.now(UTC), changed=True)
        assert swept == [(PROJECT,)]
    assert not runs.pointer_path(run_id).exists()
    assert not run["tree"].exists()
    row = pending_landing._promoted_row(PROJECT, run_id)
    assert row["release"]["worktree_released"] is True
    assert (run["repo"] / "seed.txt").read_text() == "seed\n"
    with pytest.raises(runs.CrewError, match="is promoted"):
        pending_landing.mark_pending_landing_repair(run_id, pointer)
    assert not runs.pointer_path(run_id).exists()


@pytest.mark.parametrize("storage", ["store", "legacy"])
def test_a_promoted_pointer_cannot_be_written_back(promoted_run, storage):
    run = promoted_run
    _promote(run, storage)
    pointer = run["pointer"]
    with pytest.raises(runs.CrewError, match="is promoted"):
        pending_landing.mark_pending_landing_repair(pointer["run_id"], pointer)
    result = recovery_repair_dispatch.dispatch_merge_repair(
        pointer,
        head="unresolved-head",
        primary="unresolved-primary",
        paths=["seed.txt"],
        promoted=True,
    )
    assert result["status"] == "refused"
    assert "is promoted" in result["reason"]


@pytest.mark.parametrize("legacy", [False, True])
def test_an_unreadable_store_keeps_the_pointer_and_refuses_writeback(
    promoted_run, legacy
):
    run = promoted_run
    if legacy:
        _promote(run, "legacy")
    run_store.store_path().parent.mkdir(parents=True, exist_ok=True)
    run_store.store_path().write_text("unreadable database")
    pointer = run["pointer"]
    path = runs.pointer_path(pointer["run_id"])
    before = path.read_bytes()
    pending_landing.finish_pending_landings(only_run=pointer["run_id"])
    pending_landing.retire_promoted_pointers(project=PROJECT, session="owner")
    assert path.read_bytes() == before
    assert run["tree"].exists()
    assert not pending_landing._promoted_row_absent(PROJECT, pointer["run_id"])
    with pytest.raises(runs.CrewError, match="unreadable promotion evidence"):
        pending_landing.mark_pending_landing_repair(pointer["run_id"], pointer)


@pytest.mark.parametrize("storage", ["store", "legacy"])
def test_a_dirty_promoted_worktree_stays_as_an_obligation(promoted_run, storage):
    run = promoted_run
    _promote(run, storage)
    dirty = run["tree"] / "uncommitted.txt"
    dirty.write_text("retain this work\n")
    pending_landing.finish_pending_landings()
    pending_landing.retire_promoted_pointers(project=PROJECT, session="owner")
    assert dirty.read_text() == "retain this work\n"
    pointer = run["pointer"]
    assert runs.pointer_path(pointer["run_id"]).exists()
    items = obligations.obligations(PROJECT, "owner")["obligations"]
    retained = [item for item in items if item.get("run_id") == pointer["run_id"]]
    assert len(retained) == 1, retained
    assert "uncommitted.txt" in retained[0].get("reason", ""), retained


def test_absence_requires_both_readers_and_a_valid_legacy_row(promoted_run):
    run = promoted_run
    run_id = run["pointer"]["run_id"]
    assert pending_landing._promoted_row_absent(PROJECT, run_id)
    path = ledger.run_path(PROJECT, run_id, run["repo"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a row")
    assert not pending_landing._promoted_row_absent(PROJECT, run_id)
    assert (
        "unreadable promotion evidence"
        in pending_landing.promoted_pointer_write_refusal(run_id, run["pointer"])
    )


def test_a_stored_row_precedes_an_unreadable_legacy_projection(promoted_run):
    run = promoted_run
    _promote(run, "store")
    run_id = run["pointer"]["run_id"]
    path = ledger.run_path(PROJECT, run_id, run["repo"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a row")
    assert (
        pending_landing._promoted_row(PROJECT, run_id)["promoted_revision"]
        == run["row"]["promoted_revision"]
    )
