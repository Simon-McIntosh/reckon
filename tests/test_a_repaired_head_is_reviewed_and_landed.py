"""Promoted merge repairs keep a fresh head and owe its reviews before landing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import ledger
from reckon.crew import merge, pending_landing, queue_landing, runs
from reckon.crew import recovery_review_dispatch as dispatch
from reckon.crew import review as reviews
from tests.helpers.git import git
from tests.helpers.landing_repository import PROJECT, _write_origin_and_checkout


def _commit(tree, path, text):
    (tree / path).write_text(text)
    git(tree, "add", path)
    git(tree, "commit", "-q", "-m", "test: retain the delivered intent")
    return git(tree, "rev-parse", "HEAD")


def _review(pointer, head, *, blocks=False):
    review = reviews.parse_review(
        "\n".join(f"SCORE {key}: 20" for key in reviews.REVIEW_DIMENSIONS)
    )
    review.update(
        project=PROJECT,
        reviewed_run_id=pointer["run_id"],
        review_run_id="review-" + pointer["run_id"],
        reviewed_base_sha=pointer["base_sha"],
        reviewed_head_sha=head,
        added_failure_count=0,
        added_failure_ids=[],
    )
    if blocks:
        review["findings"] = [
            {
                "id": "incorrect-result",
                "severity": "blocking",
                "text": "The repaired result is incorrect.",
            }
        ]
    reviews.store_review(review)
    return review


@pytest.fixture()
def repaired_fleet(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repo = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    base = _commit(repo, "shared.py", "VALUE = 'base'\n")
    primary_tree = tmp_path / "primary"
    git(repo, "worktree", "add", "--detach", str(primary_tree), base)
    primary = _commit(primary_tree, "shared.py", "VALUE = 'primary'\n")
    git(repo, "merge", "--ff-only", primary)
    git(repo, "push", "-q", "origin", "main")
    members, requests, repairs = {}, [], []
    ledger_paths = []
    for name in ("clean", "blocked", "conflicting"):
        run_id = "r-repaired-" + name
        tree = tmp_path / name
        git(repo, "worktree", "add", "--detach", str(tree), base)
        original = _commit(tree, name + ".py", f"INTENT = '{name}'\n")
        git(tree, "merge", "--no-ff", "-m", "merge: retain the primary intent", primary)
        head = git(tree, "rev-parse", "HEAD")
        run_dir = runs.run_dir(run_id)
        run_dir.mkdir(parents=True)
        manifest, log = run_dir / "manifest.md", run_dir / "gate.log"
        log.write_text(f"# revision: {head}\n# command: true\nEXIT=0\n")
        scope = [name + ".py", "shared.py"]
        manifest.write_text(
            f"status: complete\ncommits: {json.dumps([original, head])}\n"
            f"tests: true\ntest_logs: {json.dumps([str(log)])}\n"
        )
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "session": "fixture-owner",
            "repo": str(repo),
            "worktree": str(tree),
            "base_sha": base,
            "manifest_path": str(manifest),
            "launch": "in-harness",
            "role": "implement",
            "process_alive": False,
            "created_at": "2026-01-01T00:00:00Z",
            "node": {"id": name, "role": "implement", "write_paths": scope},
            "merge_conflict": {
                "head": original,
                "main_revision": primary,
                "paths": ["shared.py"],
                "status": "resumed",
            },
        }
        pointer = pending_landing.mark_pending_landing_repair(run_id, pointer)
        pointer["attempt"] = 2
        runs._write_json(runs.pointer_path(run_id), pointer)
        pending_landing.write_pending(run_id, PROJECT, pointer=pointer, head=original)
        record_path = pending_landing._record_path(run_id)
        record = json.loads(record_path.read_text())
        record["merge_conflict"] = pointer["merge_conflict"]
        runs._write_json(record_path, record)
        row_path = ledger.run_path(PROJECT, run_id, repo)
        runs._write_json(
            row_path,
            {"run_id": run_id, "promoted_revision": original, "commits": [original]},
        )
        ledger_paths.append(str(row_path.relative_to(repo)))
        members[name] = {"pointer": pointer, "head": head, "original": original}
    git(repo, "add", *ledger_paths)
    git(repo, "commit", "-q", "-m", "test: record the promoted deliveries")
    _commit(repo, "conflicting.py", "INTENT = 'another primary intent'\n")
    git(repo, "push", "-q", "origin", "main")
    monkeypatch.setattr(
        "reckon.crew.recovery_repair_dispatch.dispatch_merge_repair",
        lambda pointer, **kwargs: (
            repairs.append((pointer["run_id"], kwargs))
            or {"advice": "Resolve conflicting.py"}
        ),
    )

    # Composition is the process boundary: record its exact subject and let a
    # later pass consume the stored result, without starting an external worker.
    def compose(pointer, fields, **kwargs):
        requests.append((pointer["run_id"], fields["head"]))
        return {
            "run_id": pointer["run_id"],
            "dispatched": True,
            "review_run_id": "review-" + pointer["run_id"],
        }

    monkeypatch.setattr(dispatch, "_dispatch_composed_review", compose)
    return SimpleNamespace(
        repo=repo, origin=origin, members=members, requests=requests, repairs=repairs
    )


def test_three_redeliveries_are_reviewed_then_land_or_name_their_hold(repaired_fleet):
    fleet = repaired_fleet
    first = queue_landing.land_once()
    clean, blocked, conflicting = (
        fleet.members[name] for name in ("clean", "blocked", "conflicting")
    )
    expected = {(item["pointer"]["run_id"], item["head"]) for item in (clean, blocked)}
    assert set(fleet.requests) == expected, first
    for item in fleet.members.values():
        record = pending_landing._read(
            pending_landing._record_path(item["pointer"]["run_id"])
        )
        assert record["head"] == item["head"], record
    assert not merge._is_ancestor(fleet.repo, clean["head"], "main"), (
        "an unreviewed repair landed"
    )
    _review(clean["pointer"], clean["head"])
    _review(blocked["pointer"], blocked["head"], blocks=True)

    result = queue_landing.land_once()

    assert merge._is_ancestor(fleet.repo, clean["head"], "main"), result
    assert git(fleet.origin, "show", "main:clean.py") == "INTENT = 'clean'"
    assert not pending_landing._record_path(clean["pointer"]["run_id"]).exists()
    assert not runs.pointer_path(clean["pointer"]["run_id"]).exists()
    assert any(
        row["status"] == "finished" and row["head"] == clean["head"]
        for row in result["interrupted_landings"]
    )
    obligations = result["obligations"]
    assert len(obligations) == 2, obligations
    by_run = {row["run_id"]: row["guard"] for row in obligations}
    assert "review" in by_run[blocked["pointer"]["run_id"]]
    assert "repaired result is incorrect" in by_run[blocked["pointer"]["run_id"]]
    assert "conflict" in by_run[conflicting["pointer"]["run_id"]]
    for item in (blocked, conflicting):
        landing = runs.read_pointer(item["pointer"]["run_id"])["admission_landing"]
        assert landing["status"] == "refused", landing
        assert landing["head"] == item["head"], landing
        assert landing["guard"], landing
    assert not merge._is_ancestor(fleet.repo, blocked["head"], "main")
    assert not merge._is_ancestor(fleet.repo, conflicting["head"], "main")
    assert len(fleet.requests) == 2, "a stored result caused a duplicate review"


def test_a_promoted_repair_can_dispatch_its_new_head_review(repaired_fleet):
    item = repaired_fleet.members["clean"]
    report = dispatch.dispatch_review_for_run(item["pointer"])
    assert report["dispatched"], report
    assert repaired_fleet.requests == [(item["pointer"]["run_id"], item["head"])]


def test_a_review_of_the_superseded_head_does_not_accept_the_repair(repaired_fleet):
    item = repaired_fleet.members["clean"]
    _review(item["pointer"], item["original"])
    result = pending_landing.finish_pending_landings(only_run=item["pointer"]["run_id"])
    assert result[0]["status"] == "awaiting-review", result
    assert repaired_fleet.requests == [(item["pointer"]["run_id"], item["head"])]
    assert not merge._is_ancestor(repaired_fleet.repo, item["head"], "main")


def test_a_repair_with_an_incomplete_gate_never_requests_review(repaired_fleet):
    item = repaired_fleet.members["clean"]
    pointer = item["pointer"]
    log = Path(pointer["manifest_path"]).with_name("gate.log")
    log.write_text(f"# revision: {item['head']}\n# command: true\n")
    result = pending_landing.finish_pending_landings(only_run=pointer["run_id"])
    assert result[0]["status"] == "repair-gate-refused", result
    assert not repaired_fleet.requests
    assert not merge._is_ancestor(repaired_fleet.repo, item["head"], "main")
