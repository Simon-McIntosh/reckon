"""A delivery the primary branch already carries is recorded against its own head.

The landing pass writes a ledger row for a run whose head the branch already
holds, typically because the lead merged it by hand. The row cited the branch
tip at the time of the pass, which is whatever landed last: on 23 November a
hand-merged run was recorded against an unrelated plan-review promotion commit,
so its review no longer matched its row. The row now names the run's own head
and records the commit that carried it onto the branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import pending_landing, promotion_scope, recovery_review_dispatch


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _commit(repo, "base.txt")
    return repo


def _record(monkeypatch, repo: Path, head: str):
    monkeypatch.setattr(
        recovery_review_dispatch, "_review_head_and_tree", lambda _p: (head, "")
    )
    monkeypatch.setattr(
        promotion_scope, "_commit_landing_writes", lambda **_kwargs: None
    )
    pointer = {
        "run_id": "r-carried-delivery",
        "project": "carried-project",
        "repo": str(repo),
        "role": "implement",
        "node": {"id": "carried-node", "plan": "a-plan", "section": "s1"},
    }
    written = {}

    def append_run(project, row, root=None):
        written["row"] = dict(row)
        return {"path": str(repo / "ledger.json")}

    from reckon import ledger

    monkeypatch.setattr(ledger, "append_run", append_run)
    monkeypatch.setattr(
        pending_landing, "_promoted_row", lambda *_a, **_k: written["row"]
    )
    row, refusal = pending_landing.record_a_carried_delivery(
        pointer, "r-carried-delivery"
    )
    assert refusal == "", refusal
    return row


def test_a_no_ff_merged_head_is_recorded_with_its_merge(
    monkeypatch, repository
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "work")
    head = _commit(repository, "work.txt")
    _git(repository, "checkout", "-q", "main")
    _commit(repository, "peer.txt")
    _git(repository, "merge", "-q", "--no-ff", "work", "-m", "merge the work")
    merge = _git(repository, "rev-parse", "HEAD")
    tip = _commit(repository, "later.txt")
    assert base and tip != merge

    row = _record(monkeypatch, repository, head)

    assert row["commits"] == [head]
    assert row["promoted_revision"] == head
    assert row["suite_delta"]["recorded_merge"] == merge
    assert tip[:12] not in row["outcome"]


def test_a_fast_forwarded_head_carries_itself(monkeypatch, repository) -> None:
    head = _commit(repository, "work.txt")
    _commit(repository, "later.txt")

    row = _record(monkeypatch, repository, head)

    assert row["commits"] == [head]
    assert row["suite_delta"]["recorded_merge"] == head
