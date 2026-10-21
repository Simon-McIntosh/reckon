"""One call lands a run: promote it, merge its head, push, release the worktree.

A coordinator that lands a run by hand runs four commands in sequence, and a
scratch script that does it for them ran about forty times in one session. The
landing step is the one that reads back: a merge built out of objects puts the
reviewed head on the primary branch without touching the shared index, the
merged tree is refused unless it carries a marker the change introduced and
lacks any marker it removed, and only then is the branch pushed. The worktree is
released after the head is reachable from the branch, which is what makes the
release a removal rather than a withholding.

The declared mutation resolves the merge so the change is dropped —
``RECKON_LAND_DROP_CHANGE`` applies it under pytest by resolving the merge tree
to the base's own tree — and the marker check then refuses with nothing pushed,
which is the red log the manifest names.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from reckon.crew import merge
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the merge\n"


@pytest.fixture()
def landed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A finished run whose worktree holds one committed change."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    run_id = "r-20261019T140000000000-one-call-lands-a-run"
    worktree = tmp_path / "worktrees" / "one-call"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    # The change the run is reviewed at, committed in its own worktree.
    changed = worktree / "reviewed.txt"
    changed.write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-19T14:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§4",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
        "head": head,
        "gate_log": gate_log,
    }


def _drop_the_change_if_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation: resolve the merge to the base's own tree."""
    if not os.environ.get("RECKON_LAND_DROP_CHANGE"):
        return

    def _merge_tree_without_the_change(repo: Path, base: str, head: str):
        tree = merge._run(repo, "rev-parse", f"{base}^{{tree}}").stdout.strip()
        return tree, []

    monkeypatch.setattr(merge, "_merge_tree", _merge_tree_without_the_change)


def test_one_call_lands_the_change_pushes_it_and_removes_the_worktree(
    landed_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary branch carries the marker, the remote matches, the tree is gone."""
    _drop_the_change_if_declared(monkeypatch)
    run = landed_run
    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True, report
    assert report["steps"]["promote"]["ok"] is True
    assert report["steps"]["land"]["ok"] is True
    assert report["steps"]["land"]["pushed"] is True
    landed = _git(run["repository"], "rev-parse", "main")
    # The marker the change introduced is on the primary branch.
    assert MARKER.strip() in _git(run["repository"], "show", "main:reviewed.txt")
    # The remote ref equals the local one, and the worktree has been released.
    assert _git(run["origin"], "rev-parse", "main") == landed
    assert report["steps"]["land"]["remote_head"] == landed
    assert not run["worktree"].exists()
    assert report["steps"]["release"]["worktree_released"] is True


def test_a_merge_that_drops_the_change_is_refused_and_nothing_is_pushed(
    landed_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: with the merge resolved to drop the change.

    The marker check refuses, the remote keeps the revision it had, and the
    worktree is left in place for recovery. The remote is the revision that
    testifies here rather than the local branch: promotion commits its own
    ledger to the local branch before the merge is attempted, so the local
    branch moves whether or not the landing lands.
    """
    run = landed_run
    origin_before = _git(run["origin"], "rev-parse", "main")

    def _merge_tree_without_the_change(repo: Path, base: str, head: str):
        tree = merge._run(repo, "rev-parse", f"{base}^{{tree}}").stdout.strip()
        return tree, []

    monkeypatch.setattr(merge, "_merge_tree", _merge_tree_without_the_change)
    result = _complete_land(run)

    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is False, report
    assert report["steps"]["land"]["ok"] is False
    assert "dropped the reviewed change" in report["steps"]["land"]["error"]
    assert report["steps"]["land"]["resume"].startswith("reckon crew merge")
    assert _git(run["origin"], "rev-parse", "main") == origin_before
    assert run["worktree"].is_dir()


def test_the_marker_check_reads_what_the_change_introduced_and_removed(
    tmp_path: Path,
) -> None:
    """A change's markers are the lines it wrote, and a dropped one is refused."""
    _origin, repository = _write_origin_and_checkout(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    (repository / "reviewed.txt").write_text(
        "kept line one\n" + MARKER, encoding="utf-8"
    )
    _git(repository, "add", "reviewed.txt")
    _git(repository, "commit", "-q", "-m", "add the reviewed change")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", base)
    _git(repository, "branch", "-f", "main", base)

    markers = merge.change_markers(repository, base, head)

    assert markers["reviewed.txt"]["introduced"] == [
        "kept line one",
        MARKER.strip(),
    ]
    assert markers["reviewed.txt"]["removed"] == []
    # A merge that keeps the change is accepted; one resolved to the base's own
    # tree drops it and is refused, which is the whole decision the landing
    # rests on.
    kept = merge.build_merge_commit(
        repository, base, head, "land", "the reviewed change is on the branch"
    )
    assert merge.surviving_change_refusals(repository, kept, markers) == []
    base_tree = merge._run(repository, "rev-parse", f"{base}^{{tree}}").stdout.strip()
    dropped = merge._commit_tree(
        repository, base_tree, [base, head], "land\n\nthe change is dropped\n"
    )
    refusals = merge.surviving_change_refusals(repository, dropped, markers)
    assert refusals, "a merge that dropped the change was not refused"
    assert "the merge dropped it" in refusals[0]


def test_the_landing_reuses_the_shared_merge_loop() -> None:
    """The marker check rides the one landing loop rather than a copy of it.

    Two copies of the pinned-HEAD build, the index.lock retry, the --ff-only
    move and the push would have to be fixed twice, and the copy would drift
    the first time one of them was. The count is asserted against the source
    because that is what the defect is: a second loop, not a wrong result.
    """
    source = Path(merge.__file__).read_text(encoding="utf-8")
    assert source.count('"--ff-only"') == 1
    assert len(inspect.getsource(merge.land).splitlines()) < len(
        inspect.getsource(merge.land_merge).splitlines()
    )


def test_the_marker_check_is_the_seam_the_shared_loop_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``land`` hands its marker check to the shared loop, which runs it."""
    _origin, repository = _write_origin_and_checkout(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "-b", "reviewed")
    (repository / "reviewed.txt").write_text(
        "kept line one\n" + MARKER, encoding="utf-8"
    )
    _git(repository, "add", "reviewed.txt")
    _git(repository, "commit", "-q", "-m", "add the reviewed change")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-q", "main")

    seen: dict[str, object] = {}
    real = merge.land_merge

    def _recording_land_merge(repo, **kwargs):
        seen.update(kwargs)
        return real(repo, **kwargs)

    monkeypatch.setattr(merge, "land_merge", _recording_land_merge)
    report = merge.land(repository, head=head, message="land", body="the change lands")

    assert callable(seen.get("verify")), "the shared loop was given no verify seam"
    assert report["survived"]["checked"] is True
    assert report["survived"]["paths"] == ["reviewed.txt"]
    assert report["pushed"] is True
    merged = _git(repository, "rev-parse", "HEAD")
    assert merged != base
    assert "the reviewed change survives" in _git(
        repository, "show", f"{merged}:reviewed.txt"
    )
