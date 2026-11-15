"""A run that merged the shared branch into its worktree can still be cited.

A merge repair tells a worker to merge the current head of the shared branch
into its worktree. The dry run then inferred the run's citations from
``base..HEAD``, which includes every commit that merge brought in from the
shared branch, and the citation guard in ``reckon.crew.promotion_evidence``
refused the run for citing a commit the run did not make. The repair advice
therefore led straight to a refusal, and the dry run went past it only when the
coordinator supplied the run's own commits itself.

The guard is right to refuse a commit from outside the run's own line, so the
defect is in what the inference cites, not in the guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon.crew import promotion, runs
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git

PROJECT = "proj"
PLAN = "plan-a"


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared checkout on its primary branch holding a plan to promote against."""
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "worker@example.invalid")
    git(root, "config", "user.name", "Worker")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(root, "add", "seed.txt", "docs")
    git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _set_plan(repository: Path, **fields: object) -> None:
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _manifest(tmp_path: Path, run_id: str) -> Path:
    """The run's manifest: a contracted gate receipt and no citation.

    The ``commits`` line is left out deliberately, because the citations under
    test are the ones the promotion infers from the run's own history. A
    manifest that declared them would hide whether that inference read the
    history correctly.
    """
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    log = manifest.parent / f"{run_id}.log"
    log.write_text("ran the gate\nEXIT=0\n", encoding="utf-8")
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
        f"test_logs: [{log}]\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    run_id: Path | str,
    *,
    manifest: Path,
    worktree: Path,
    base_sha: str,
) -> None:
    """Record the run the way a dispatch does: its own tree, its own base."""
    record = {
        "run_id": str(run_id),
        "repo": str(repository),
        "worktree": str(worktree),
        "base_sha": base_sha,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-11-15T06:00:00Z",
        "manifest_path": str(manifest),
        "project": PROJECT,
        "plan_impl_at_dispatch": 0.5,
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "s2",
            "time_budget": "25m",
        },
    }
    _write_json(pointer_path(str(run_id)), record)


def _commit_here(tree: Path, name: str, subject: str) -> str:
    """Write ``name`` into ``tree`` and commit it, returning the new commit."""
    (tree / name).write_text(f"{subject}\n", encoding="utf-8")
    git(tree, "add", name)
    git(tree, "commit", "-q", "-m", subject)
    return git(tree, "rev-parse", "HEAD")


def _peer_branch_commit(repository: Path, tmp_path: Path) -> str:
    """A commit on a peer's branch, forked from the shared head as it stands."""
    fork = git(repository, "rev-parse", "HEAD")
    git(repository, "branch", "peer-branch", fork)
    tree = tmp_path / "peer-tree"
    git(repository, "worktree", "add", "-q", str(tree), "peer-branch")
    return _commit_here(tree, "peer-work.txt", "a peer's own work")


def _another_branch_commit(repository: Path, tmp_path: Path) -> str:
    """A commit on a branch that is neither the run's nor the shared one."""
    fork = git(repository, "rev-parse", "HEAD")
    git(repository, "branch", "other-branch", fork)
    tree = tmp_path / "other-tree"
    git(repository, "worktree", "add", "-q", str(tree), "other-branch")
    return _commit_here(tree, "other-work.txt", "work on another branch")


def _merge(tree: Path, branch: str, subject: str) -> str:
    """Merge ``branch`` into the checkout at ``tree``, returning the merge."""
    git(tree, "merge", "--no-ff", "-m", subject, branch)
    return git(tree, "rev-parse", "HEAD")


def _run_worktree(repository: Path, tmp_path: Path, base: str) -> tuple[Path, str]:
    """A worktree off ``base`` holding one commit of the run's own work."""
    worktree = tmp_path / "run-tree"
    git(repository, "worktree", "add", "-q", "-b", "run-branch", str(worktree), base)
    return worktree, _commit_here(worktree, "run-work.txt", "the run's own work")


def _cited_commits(run_id: str) -> tuple[str, ...]:
    """What the dry run's fill infers, read from the run's own record."""
    record = runs.read_pointer(run_id)
    _check, commits = promotion._default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )
    return tuple(commits)


def _dry_run(repository: Path, run_id: str) -> dict:
    """The promotion's own guards, evaluated with no commit supplied."""
    return promotion.complete_dry_run(run_id, root=repository, gate="passed")


def test_a_run_that_merged_the_shared_branch_cites_only_its_own_work(
    repository: Path, tmp_path: Path
) -> None:
    """The run's own merge is cited; the shared branch's commits are not.

    A peer forked its branch before the run's base and landed it on the shared
    branch, so the shared branch now reaches commits that do not descend from
    the run's base. Merging that branch in — what a merge repair tells a worker
    to do — pulled them into the run's history, and the citation guard refused
    the run for a commit it never made.
    """
    run_id = "r-20261115T060000000000-node-a"
    peer_commit = _peer_branch_commit(repository, tmp_path)
    base = _commit_here(repository, "advance.txt", "advance the shared branch")
    _merge(repository, "peer-branch", "land the peer's branch")
    worktree, run_commit = _run_worktree(repository, tmp_path, base)
    merge_commit = _merge(worktree, "main", "merge the shared branch")
    _set_plan(repository, impl=1.0)
    _pointer(
        repository,
        run_id,
        manifest=_manifest(tmp_path, run_id),
        worktree=worktree,
        base_sha=base,
    )

    report = _dry_run(repository, run_id)
    assert report["would_promote"] is True, report["refusals"]
    cited = _cited_commits(run_id)
    assert peer_commit not in cited, cited
    assert set(cited) == {run_commit, merge_commit}, cited


def test_a_run_never_credits_a_commit_it_merged_from_another_branch(
    repository: Path, tmp_path: Path
) -> None:
    """What must not move: another branch's commit is not the run's work.

    The run merged an unrelated branch as well, so its history holds a commit
    from neither the shared branch nor its own line. That commit is left out of
    this run's citations, which are its own line and its own merges only.

    This case replaces the older pin that such a run was refused for citing
    that commit: the guard judges a citation reachable from the run's head as a
    valid import, so the refusal no longer arises, and what the row credits is
    decided here instead.
    """
    run_id = "r-20261115T060500000000-node-a"
    other_commit = _another_branch_commit(repository, tmp_path)
    base = _commit_here(repository, "advance.txt", "advance the shared branch")
    worktree = _run_worktree(repository, tmp_path, base)[0]
    _merge(worktree, "main", "merge the shared branch")
    merge_commit = _merge(worktree, "other-branch", "merge an unrelated branch")
    run_commit = git(worktree, "rev-parse", "HEAD^")
    _set_plan(repository, impl=1.0)
    _pointer(
        repository,
        run_id,
        manifest=_manifest(tmp_path, run_id),
        worktree=worktree,
        base_sha=base,
    )

    cited = _cited_commits(run_id)

    assert other_commit not in cited, cited
    assert set(cited) == {run_commit, merge_commit}, cited
    report = _dry_run(repository, run_id)
    assert report["would_promote"] is True, report["refusals"]
