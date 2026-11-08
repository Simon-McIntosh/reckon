"""A landing records the release it made after its merge, not the one before.

``crew complete --land`` promotes and records a release before it merges, so
that release sees the reviewed head off the branch and withholds the tree as
unintegrated. The release made after the merge frees the tree, but the recorded
one used to win, so every landing reported "the worktree was withheld" with
its head already on the primary branch. A post-merge release that freed a tree
the recorded one kept now replaces it, keeping the earlier scratch receipt.
"""

from __future__ import annotations

from pathlib import Path

from reckon import ledger
from reckon.crew.promotion_release import _record_release_on_ledger

WITHHELD = {
    "worktree_released": False,
    "worktree_withheld": "its HEAD is not reachable from the integration revision",
    "scratch_removed": True,
    "scratch_path": "/scratch/run-0",
}
RELEASED_AFTER_MERGE = {
    "worktree_released": True,
    "scratch_removed": False,
    "scratch_withheld": "scratch is already gone",
}


def _run_with_release(tmp_path: Path, monkeypatch, release: dict) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repository"
    ledger.ledger_path("sample", repo).parent.mkdir(parents=True)
    ledger.write("sample", {"members": [], "runs": [], "holds": []}, 0, root=repo)
    target = ledger.run_path("sample", "run-0", repo)
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        ledger.serialize_run({"run_id": "run-0", "worktree": "/w", "release": release})
    )
    return repo


def test_the_post_merge_release_replaces_a_pre_merge_withheld_tree(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _run_with_release(tmp_path, monkeypatch, WITHHELD)

    recorded, _row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="run-0",
        release=RELEASED_AFTER_MERGE,
        checkout=None,
        after_merge=True,
    )

    assert recorded["worktree_released"] is True
    assert "worktree_withheld" not in recorded
    assert recorded["scratch_removed"] is True, "the earlier scratch receipt stands"
    assert recorded["scratch_path"] == "/scratch/run-0"


def test_a_later_pass_still_keeps_the_recorded_release(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _run_with_release(tmp_path, monkeypatch, WITHHELD)

    recorded, _row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="run-0",
        release=RELEASED_AFTER_MERGE,
        checkout=None,
    )

    assert recorded == WITHHELD


def test_a_post_merge_release_that_also_withholds_keeps_the_record(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _run_with_release(tmp_path, monkeypatch, WITHHELD)

    recorded, _row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="run-0",
        release={"worktree_released": False, "worktree_withheld": "dirty"},
        checkout=None,
        after_merge=True,
    )

    assert recorded == WITHHELD
