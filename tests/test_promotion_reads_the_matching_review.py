"""A promotion reads the review of the revision it promotes.

A stored review is evidence about one diff, and a repair moves the diff. A
review of an earlier revision therefore cannot answer for the revision a
promotion asserts, and taking the newest record on disk as the run's review
makes landing a repair depend on which file the store happens to hold newest.
The review a promotion reads is selected by the revision it recorded reading,
and a store holding only a review of an earlier head refuses the promotion
rather than promoting on evidence about code that no longer exists.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "matching-project"
FILE = "region.txt"
BASE = "1" * 40


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / FILE).write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", FILE),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _snapshot(repository: Path) -> dict[str, object]:
    return {
        "status": _git(repository, "status", "--porcelain"),
        "head": _git(repository, "rev-parse", "HEAD"),
    }


def _pointer(
    repository: Path, run_tree: Path, run_id: str, base: str, manifest: Path
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(run_tree),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-23T12:00:00Z",
            "manifest_path": str(manifest),
            "repository_tree_snapshot": _snapshot(repository),
            "node": {
                "id": "matching",
                "plan": "fixture",
                "section": "review-head",
                "time_budget": "25m",
                "write_paths": [FILE],
            },
        },
    )


def _detached_tree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _store_complete_review(run_id: str, *, base: str, head: str, score: int) -> Path:
    """Store a parsed review that records reading ``base``..``head``."""
    emitted = "\n".join(
        f"SCORE {dimension}: {score}" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _age(path: Path, *, seconds: int) -> None:
    """Set a file's mtime so store ordering is explicit rather than incidental."""
    moment = 1_700_000_000 + seconds
    os.utime(path, ns=(moment * 10**9, moment * 10**9))


def _promoted_head(
    repository: Path, tmp_path: Path, run_id: str
) -> tuple[Path, str, str]:
    """Seed a real run tree with one commit and return (tree, base, head)."""
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / f"{run_id}-tree")
    target = run_tree / FILE
    target.write_text(f"repair\n{target.read_text(encoding='utf-8')}", encoding="utf-8")
    _git(run_tree, "add", FILE)
    _git(run_tree, "commit", "-q", "-m", "test: repair")
    head = _git(run_tree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: matching\n"
        "status: complete\n"
        f"commits: [{head}]\n"
        f"changed_paths: [{FILE}]\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    _pointer(repository, run_tree, run_id, base, manifest)
    return run_tree, base, head


def test_promotion_reads_the_review_of_the_revision_it_promotes(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-matching-head"
    _run_tree, base, head = _promoted_head(repository, tmp_path, run_id)
    # Two complete reviews of one run at different revisions. The one of an
    # earlier revision is stored last and stamped newest, so a reader that takes
    # the newest record on disk picks precisely the review that says nothing
    # about the revision being promoted.
    matching = _store_complete_review(run_id, base=BASE, head=head, score=20)
    stale = _store_complete_review(run_id, base=BASE, head=base, score=5)
    _age(matching, seconds=100)
    _age(stale, seconds=200)
    assert matching != stale

    stored = crew.complete(run_id, gate="passed", commits=[head], root=repository)

    row = stored["record"]
    assert row["promoted_revision"] == head
    assert row["review"]["total"] == 100  # five dimensions at 20
    assert row["review"]["scores"] == dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20)


def test_only_a_review_of_an_earlier_head_refuses_the_promotion(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-stale-head"
    _run_tree, base, head = _promoted_head(repository, tmp_path, run_id)
    _store_complete_review(run_id, base=BASE, head=base, score=20)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[head], root=repository)

    message = str(refusal.value)
    assert run_id in message
    assert base[:12] in message
    assert head[:12] in message
    # The refusal must not read as an absent review: the record is on disk and
    # the operator needs the two revisions that disagree, not a search.
    assert "no complete independent review is stored" not in message
    assert pointer_path(run_id).exists()


def test_a_waiver_still_lands_a_run_whose_review_is_of_an_earlier_head(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-stale-head-waived"
    _run_tree, base, head = _promoted_head(repository, tmp_path, run_id)
    _store_complete_review(run_id, base=BASE, head=base, score=20)
    reason = "the review lane is unavailable and this repair is urgent"

    stored = crew.complete(
        run_id,
        gate="passed",
        commits=[head],
        review_waiver=reason,
        root=repository,
    )

    # The waiver lands the run, and the row still records that no review of the
    # promoted revision was stored: the earlier record is evidence about a diff
    # this promotion does not assert, so it is not this run's review.
    assert stored["record"]["review"] is None
    assert stored["record"]["review_waiver"]["reason"] == reason


def test_two_partial_records_of_one_run_are_both_kept_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    run_id = "r-partial-run"
    shared_reviewer = "r-shared-review"
    first = review_module.parse_review("SCORE goal_fidelity: 11")
    second = review_module.parse_review("SCORE goal_fidelity: 7")
    first.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": shared_reviewer,
            "reviewed_head_sha": "a" * 40,
        }
    )
    second.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": shared_reviewer,
            "reviewed_head_sha": "b" * 40,
        }
    )

    first_path = review_module.store_review(first)
    second_path = review_module.store_review(second)

    assert first_path != second_path
    assert first_path.is_file()
    assert second_path.is_file()
    assert json.loads(first_path.read_text(encoding="utf-8"))["scores"] == {
        "goal_fidelity": 11
    }
    assert json.loads(second_path.read_text(encoding="utf-8"))["scores"] == {
        "goal_fidelity": 7
    }
    # The same partial rewritten targets the path it already occupies, so a
    # re-store is an update rather than a second record beside the first.
    assert review_module.store_review(first) == first_path
