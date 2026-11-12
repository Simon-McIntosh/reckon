"""Only an unlanded, undiscarded delivery may reopen its merge repair."""

from __future__ import annotations

import json

import pytest

from reckon import ledger
from reckon.crew import (
    merge,
    pending_landing,
    promotion,
    queue_landing,
    recovery,
    resumption,
    runs,
)
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
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare", "-b", "main")
    git(repository, "remote", "add", "origin", str(origin))
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
    git(repository, "push", "-q", "origin", "main")
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
    git(repository, "push", "-q", "origin", "main")

    receipt = _repair(delivery, route)

    assert not runs.pointer_path(RUN_ID).exists(), receipt
    assert not pending_landing._record_path(RUN_ID).exists(), receipt
    assert resumed == [], receipt
    assert "already-landed" in str(receipt), receipt


def test_local_ancestry_keeps_a_pending_push(delivery):
    repository, _worker, _pointer, head, _primary, resumed = delivery
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
    assert merge._is_ancestor(repository, head, "main")
    assert merge.head_on_remote_branch(repository, head)["on_branch"] is False

    assert pending_landing._settled_landing(RUN_ID, head, repository=repository) == ""

    assert pending_landing._record_path(RUN_ID).exists()
    assert resumed == []


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


def test_discarded_conflict_is_refused_through_the_landing_queue(delivery, monkeypatch):
    repository, worker, pointer, head, _primary, resumed = delivery
    pointer["role"] = "implement"
    runs._write_json(runs.pointer_path(RUN_ID), pointer)
    runs._write_json(promotion.discard_record_path(RUN_ID), {"reason": "withdrawn"})
    monkeypatch.setattr(
        recovery,
        "classify_pointer",
        lambda record: {"manifest_status": "complete", "manifest_commits": [head]},
    )
    monkeypatch.setattr(
        recovery, "_review_head_and_tree", lambda record: (head, worker)
    )
    monkeypatch.setattr(queue_landing, "_owed_review_tiers", lambda record: [])
    assert (
        merge._run(repository, "merge-tree", "--write-tree", "main", head).returncode
        == 1
    )

    result = queue_landing.land_once()

    assert result["landed"] == []
    assert len(result["obligations"]) == 1
    assert "merge-conflict" in result["obligations"][0]["guard"]
    attempt = runs.read_pointer(RUN_ID)["admission_landing"]
    assert attempt["status"] == "refused"
    assert "discarded" in attempt["remedy"]
    receipt = json.loads((runs.run_dir(RUN_ID) / "merge-repair.json").read_text())
    assert {
        "outcome",
        "paths",
        "main_revision",
        "head",
        "round_id",
        "status",
        "advice",
    } <= receipt.keys()
    assert receipt["status"] == "refused"
    assert receipt["advice"] == attempt["remedy"]
    assert resumed == []


@pytest.mark.parametrize("newer_attempt", [False, True])
@pytest.mark.parametrize("route", ["direct", "pending-pass"])
def test_settled_landing_releases_only_the_marked_attempt(
    delivery, monkeypatch, newer_attempt, route
):
    repository, _worker, pointer, head, _primary, resumed = delivery
    marked = pending_landing.mark_pending_landing_repair(RUN_ID, pointer)
    if newer_attempt:
        marked["attempt"] += 1
        runs._write_json(runs.pointer_path(RUN_ID), marked)
    before = runs.pointer_path(RUN_ID).read_bytes()
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
    git(repository, "push", "-q", "origin", "main")
    primary_head = git(repository, "rev-parse", "HEAD")
    released = []

    def release(repo, integration, **kwargs):
        assert not runs.pointer_path(RUN_ID).exists()
        released.append((repo, integration, kwargs))

    monkeypatch.setattr(merge, "release_landed_worktrees", release)

    _repair(delivery, route)

    assert not pending_landing._record_path(RUN_ID).exists()
    assert resumed == []
    if newer_attempt:
        assert runs.pointer_path(RUN_ID).read_bytes() == before
        assert released == []
    else:
        assert not runs.pointer_path(RUN_ID).exists()
        assert released == [
            (repository, primary_head, {"head": head, "projects": [PROJECT]})
        ]
