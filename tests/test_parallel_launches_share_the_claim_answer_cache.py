"""Parallel launches can fill the claim answer cache at once."""

from __future__ import annotations

import threading
from pathlib import Path

from reckon.crew import node


def test_concurrent_writers_at_the_cache_limit_raise_nothing(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(node, "_CLAIM_COMMIT_ANSWERS", {})
    monkeypatch.setattr(node, "_CLAIM_ANSWER_CACHE_LIMIT", 8)
    live = tmp_path / "live"
    live.mkdir()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def writer(index: int) -> None:
        try:
            start.wait()
            for step in range(300):
                # Half the keys name a directory that exists and half one that
                # does not, so every write past the limit runs the prune.
                root = live if step % 2 else tmp_path / f"gone-{index}-{step}"
                node._remember_claim_commit_answer(
                    (str(root), f"head-{index}-{step}", "main", "base"), "answer"
                )
        except BaseException as exc:  # noqa: BLE001 - the test reports any failure
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(node._CLAIM_COMMIT_ANSWERS) <= 8


def test_a_full_cache_drops_answers_for_worktrees_that_are_gone(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(node, "_CLAIM_COMMIT_ANSWERS", {})
    monkeypatch.setattr(node, "_CLAIM_ANSWER_CACHE_LIMIT", 2)
    kept = tmp_path / "kept"
    kept.mkdir()
    node._remember_claim_commit_answer((str(tmp_path / "gone"), "a", "main", "b"), "1")
    node._remember_claim_commit_answer((str(kept), "a", "main", "b"), "2")
    node._remember_claim_commit_answer((str(kept), "c", "main", "b"), "3")

    assert (str(tmp_path / "gone"), "a", "main", "b") not in node._CLAIM_COMMIT_ANSWERS
    assert node._CLAIM_COMMIT_ANSWERS[(str(kept), "c", "main", "b")] == "3"
