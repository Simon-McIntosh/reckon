"""A crew verb that writes a tracked state record commits it.

A tracked record under ``docs/state/<project>`` is shared with every clone only
once a commit carries it, so a verb that writes one and leaves it as a modified
file has produced a record no other reader can see and that the next unrelated
commit sweeps up. Three writes are held here: a disposition recorded on a
committed review record, the release pass's ledger write, and a second release
pass over an already-released run.

The third is the failure this file exists for. A release pass is not always the
first — promotion over an already-promoted run, a second ``crew release`` and a
recovery sweep all reach the ledger write after a truthful release was recorded
— and a fresh reading at that point can only say the worktree and the scratch
copy were already gone. The record must keep the release it holds.

Every store, repository and git checkout here resolves inside ``tmp_path``;
nothing reads or writes the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion_release, review_dispositions
from reckon.crew import review as review_module

PROJECT = "sample"
REVIEWED = "r-20261022T050000000000-reviewed-subject"
REVIEW_RUN = "r-20261022T051911816200-review-run"
FOLD_NODE = "repair-the-disposed-finding"

RELEASE = {
    "worktree_released": True,
    "scratch_removed": True,
    "process_signalled": True,
}


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _porcelain_at(tree: Path, relative: str) -> str:
    """The change at one path, and nothing about any other path in the tree."""
    return _git(tree, "status", "--porcelain", "--", relative)


def _seed_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fixture repository"),
    ):
        _git(repository, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _review_record() -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15),
        "findings": [
            {
                "file": "reckon/crew/first.py",
                "line": "10",
                "text": "the first defect",
                "severity": "blocking",
            }
        ],
        "timestamp": "2026-10-22T05:20:00+00:00",
    }


def _committed_review(repository: Path) -> tuple[Path, str]:
    committed_root = review_module.committed_review_root(PROJECT)
    assert committed_root is not None, "premise: the project's reviews tree mounts"
    path = review_module.review_path(
        PROJECT,
        REVIEWED,
        committed_root=committed_root,
        review_run_id=REVIEW_RUN,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_review_record()), encoding="utf-8")
    _git(repository, "add", "--", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "test: commit the review record")
    return path, _git(repository, "rev-parse", "HEAD")


def _ledger_with_run(repository: Path) -> Path:
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=repository)
    target = ledger.run_path(PROJECT, REVIEW_RUN, repository)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        ledger.serialize_run(
            {
                "run_id": REVIEW_RUN,
                "project": PROJECT,
                "repo": str(repository),
                "scratch": str(repository / "no-such-scratch"),
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "add", "--", str(target.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "test: commit the run record")
    return target


def test_dispose_leaves_its_review_record_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disposition written to a committed record travels with the tree."""
    repository = _seed_repository(tmp_path, monkeypatch)
    path, before = _committed_review(repository)

    written = review_dispositions.record_finding_disposition(
        PROJECT, REVIEWED, 0, kind="folded", node=FOLD_NODE
    )

    assert Path(written) == path
    assert _porcelain_at(repository, str(path.relative_to(repository))) == "", (
        "the disposition left the record modified"
    )
    after = _git(repository, "rev-parse", "HEAD")
    assert after != before, "the disposition was never committed"
    touched = _git(repository, "show", "--name-only", "--format=", after)
    assert str(path.relative_to(repository)) in touched


def test_the_release_pass_commits_what_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release outcome lands in a commit rather than in the working tree."""
    repository = _seed_repository(tmp_path, monkeypatch)
    target = _ledger_with_run(repository)
    before = _git(repository, "rev-parse", "HEAD")

    promotion_release._record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=REVIEW_RUN,
        release=RELEASE,
        checkout=repository,
    )

    relative = str(target.relative_to(repository))
    assert _porcelain_at(repository, relative) == "", (
        "the release left the record modified"
    )
    after = _git(repository, "rev-parse", "HEAD")
    assert after != before, "the release write was never committed"
    assert json.loads(target.read_text(encoding="utf-8"))["release"] == RELEASE
    touched = _git(repository, "show", "--name-only", "--format=", after)
    assert relative in touched


def test_a_second_release_leaves_the_record_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later pass reads the release already recorded instead of rewriting it.

    The second pass is handed a degraded reading, which is what a release step
    produces once the worktree and the scratch copy are already gone. The
    recorded release names what the host actually removed, so the record must
    keep it — byte for byte, at one head, with no new commit.
    """
    repository = _seed_repository(tmp_path, monkeypatch)
    target = _ledger_with_run(repository)
    promotion_release._record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=REVIEW_RUN,
        release=RELEASE,
        checkout=repository,
    )
    first_bytes = target.read_bytes()
    first_head = _git(repository, "rev-parse", "HEAD")

    degraded = {
        "worktree_released": False,
        "scratch_removed": False,
        "process_signalled": False,
        "worktree_withheld": "tree is no longer available",
    }
    recorded, _row = promotion_release._record_release_on_ledger(
        project=PROJECT,
        root=repository,
        run_id=REVIEW_RUN,
        release=degraded,
        checkout=repository,
    )

    assert target.read_bytes() == first_bytes, "the second release rewrote the record"
    assert _git(repository, "rev-parse", "HEAD") == first_head
    assert recorded["worktree_released"] is True, "the recorded release was lost"
    assert _porcelain_at(repository, str(target.relative_to(repository))) == ""
