"""Only an unlanded, undiscarded delivery may reopen its merge repair."""

from __future__ import annotations

import json

import pytest

from reckon import ledger
from reckon.crew import merge, pending_landing, promotion, resumption, runs
from reckon.crew.recovery_repair_dispatch import dispatch_merge_repair
from tests.helpers.git import git

PROJECT = "merge-repair-fixture"
RUN_ID = "r-promoted-conflict"


@pytest.fixture()
def delivery(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.invalid")
    source = repository / "result.txt"
    source.write_text("base\n")
    git(repository, "add", source.name)
    git(repository, "commit", "-qm", "test: seed shared result")
    worker = tmp_path / "worker"
    git(repository, "worktree", "add", "--detach", str(worker), "HEAD")
    (worker / source.name).write_text("worker\n")
    git(worker, "add", source.name)
    git(worker, "commit", "-qm", "test: deliver worker result")
    head = git(worker, "rev-parse", "HEAD")
    source.write_text("primary\n")
    git(repository, "add", source.name)
    git(repository, "commit", "-qm", "test: change primary result")
    primary = git(repository, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    row = {"run_id": RUN_ID, "project": PROJECT, "promoted_revision": head}
    runs._write_json(ledger.run_path(PROJECT, RUN_ID, repository), row)
    pointer = {
        **row,
        "repo": str(repository),
        "worktree": str(worker),
        "attempt": 1,
        "phase": "completed",
        "node": {"id": "conflicting-delivery", "role": "implement"},
    }
    pending_landing.write_pending(RUN_ID, PROJECT, pointer=pointer)
    pending = pending_landing._read(pending_landing._record_path(RUN_ID))
    pending["merge_conflict"] = {
        "main_revision": primary,
        "head": head,
        "paths": [source.name],
    }
    runs._write_json(pending_landing._record_path(RUN_ID), pending)
    resumed = []
    monkeypatch.setattr(
        resumption, "_resume", lambda *args, **kwargs: resumed.append((args, kwargs))
    )
    return repository, worker, pointer, head, primary, resumed


def _repair(delivery, route):
    _repository, _worker, pointer, head, primary, _resumed = delivery
    if route == "direct":
        return dispatch_merge_repair(
            pointer, head=head, primary=primary, paths=["result.txt"], promoted=True
        )
    return pending_landing.finish_pending_landings(live_run_ids=())


@pytest.mark.parametrize("route", ["direct", "pending-pass"])
@pytest.mark.parametrize("marker", ["run", "spool"])
def test_discarded_delivery_does_not_recreate_a_pointer(delivery, route, marker):
    path = (
        promotion.discard_record_path(RUN_ID)
        if marker == "run"
        else promotion.discard_spool_path(RUN_ID)
    )
    runs._write_json(path, {"run_id": RUN_ID, "reason": "withdrawn"})
    assert promotion.discard_recorded(RUN_ID)

    receipt = _repair(delivery, route)

    assert not runs.pointer_path(RUN_ID).exists(), receipt
    assert delivery[-1] == [], receipt
    assert "discarded" in str(receipt), receipt
    assert path.is_file()


@pytest.mark.parametrize("route", ["direct", "pending-pass"])
@pytest.mark.parametrize("at_tip", [False, True])
def test_landed_delivery_clears_pending_without_a_resume(delivery, route, at_tip):
    repository, worker, pointer, head, primary, resumed = delivery
    # The head reaches primary independently while its conflict record stays stale.
    git(
        repository,
        "merge",
        "--no-ff",
        "-s",
        "ours",
        head,
        "-m",
        "test: integrate delivery",
    )
    if at_tip:
        pointer["promoted_revision"] = git(repository, "rev-parse", "HEAD")
        delivery = (
            repository,
            worker,
            pointer,
            pointer["promoted_revision"],
            primary,
            resumed,
        )
        runs._write_json(ledger.run_path(PROJECT, RUN_ID, repository), pointer)
    assert merge._is_ancestor(repository, delivery[3], "main")

    receipt = _repair(delivery, route)

    assert not runs.pointer_path(RUN_ID).exists(), receipt
    assert not pending_landing._record_path(RUN_ID).exists(), receipt
    assert resumed == [], receipt
    assert "already-landed" in str(receipt), receipt


@pytest.mark.parametrize("route", ["direct", "pending-pass"])
def test_unlanded_conflict_still_marks_and_resumes(delivery, route):
    repository, _worker, _pointer, head, primary, resumed = delivery
    conflict = merge._run(repository, "merge-tree", "--write-tree", "main", head)
    assert conflict.returncode == 1
    assert "result.txt" in conflict.stdout
    assert not merge._is_ancestor(repository, head, "main")
    assert not promotion.discard_recorded(RUN_ID)

    _repair(delivery, route)

    live = runs.read_pointer(RUN_ID)
    assert live["pending_landing_repair"] is True
    assert live["merge_conflict"]["status"] == "resumed"
    assert len(resumed) == 1
    assert head in resumed[0][1]["advice"]
    assert primary in resumed[0][1]["advice"]
    assert pending_landing._record_path(RUN_ID).is_file()
