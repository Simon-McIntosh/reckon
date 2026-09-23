"""A promotion records the revision it asserts landed, and a sweep tests it.

The record and the check are two halves of one question: a promotion writes a
durable "passed" whose truth depends on a merge that is a separate, later act,
so the row has to carry the revision the merge is supposed to bring across, and
a later sweep has to ask whether it actually did.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "landed-project"
MARKER = "LANDED_MARKER_LINE"
FILE = "region.txt"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _is_ancestor(repository: Path, revision: str, target: str = "HEAD") -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, target],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


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


def _pointer(repository: Path, run_tree: Path, run_id: str, base: str) -> None:
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
            "repository_tree_snapshot": _snapshot(repository),
            "node": {
                "id": "landed",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": [FILE],
            },
        },
    )


def _snapshot(repository: Path) -> dict[str, object]:
    status = _git(repository, "status", "--porcelain")
    return {"status": status, "head": _git(repository, "rev-parse", "HEAD")}


def _detached_tree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _promote(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    add_marker: bool,
) -> tuple[dict[str, object], str]:
    """Run a real promotion of one run that edits ``FILE``.

    ``add_marker`` commits the marker line into ``FILE``; otherwise it deletes
    the seeded marker line, which is the removal shape a presence-only check
    cannot see.
    """
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / f"{run_id}-tree")
    _pointer(repository, run_tree, run_id, base)
    target = run_tree / FILE
    text = target.read_text(encoding="utf-8")
    if add_marker:
        target.write_text(f"{MARKER}\n{text}", encoding="utf-8")
    else:
        target.write_text(
            text.replace(f"{MARKER}\n", "").replace(MARKER, ""), encoding="utf-8"
        )
    _git(run_tree, "add", FILE)
    _git(run_tree, "commit", "-q", "-m", "test: change")
    head = _git(run_tree, "rev-parse", "HEAD")
    stored = crew.complete(run_id, gate="passed", commits=[head], root=repository)
    return stored["record"], head


def test_complete_records_the_run_head_not_the_promote_commit(
    repository: Path, tmp_path: Path
) -> None:
    record, head = _promote(repository, tmp_path, "r-head-recorded", add_marker=True)

    promote_commit = _git(repository, "rev-parse", "HEAD")
    assert record["promoted_revision"] == head
    assert record["promoted_revision"] != promote_commit
    # The two revisions answer the ancestry question differently, which is the
    # whole reason the row must carry the run's own head: the promote commit is
    # an ancestor of the branch by construction, the run's own head is not until
    # a merge actually carries it.
    assert _is_ancestor(repository, promote_commit, "HEAD")
    assert not _is_ancestor(repository, str(record["promoted_revision"]), "HEAD")


def test_sweep_reports_a_promotion_whose_work_is_not_an_ancestor(
    repository: Path, tmp_path: Path
) -> None:
    record, head = _promote(repository, tmp_path, "r-never-merged", add_marker=True)

    report = promotion.sweep_promoted_revisions(PROJECT, root=repository)

    assert report["checked"] == 1
    assert [f["run_id"] for f in report["findings"]] == ["r-never-merged"]
    finding = report["findings"][0]
    assert finding["promoted_revision"] == head
    assert "not-an-ancestor" in finding["reasons"]
    assert record["promoted_revision"] == head


def test_sweep_reports_a_marker_absent_though_ancestry_passes(
    repository: Path, tmp_path: Path
) -> None:
    _record, head = _promote(repository, tmp_path, "r-dropped-content", add_marker=True)
    # The merge carries the run's commit across, so ancestry passes, and a later
    # commit drops the content the merge conclusion was meant to keep.
    _git(repository, "merge", "-q", "--no-ff", head, "-m", "Merge run")
    assert _is_ancestor(repository, head, "HEAD")
    (repository / FILE).write_text("seed\n", encoding="utf-8")
    _git(repository, "add", FILE)
    _git(repository, "commit", "-q", "-m", "test: drop the merged content")

    report = promotion.sweep_promoted_revisions(
        PROJECT,
        root=repository,
        markers=[{"run_id": "r-dropped-content", "marker": MARKER, "path": FILE}],
    )

    finding = report["findings"][0]
    assert finding["reasons"] == ["marker-absent"]
    assert finding["markers"][0]["present"] is False


def test_sweep_reports_a_removal_whose_marker_is_still_present(
    repository: Path, tmp_path: Path
) -> None:
    (repository / FILE).write_text(f"{MARKER}\nseed\n", encoding="utf-8")
    _git(repository, "add", FILE)
    _git(repository, "commit", "-q", "-m", "test: seed the marker to remove")
    _record, head = _promote(repository, tmp_path, "r-removed-marker", add_marker=False)
    # The merge applies the deletion, then a later commit restores what the
    # branch removed — the dropped deletion a presence-only check cannot see.
    _git(repository, "merge", "-q", "--no-ff", head, "-m", "Merge run")
    assert _is_ancestor(repository, head, "HEAD")
    (repository / FILE).write_text(f"{MARKER}\nseed\n", encoding="utf-8")
    _git(repository, "add", FILE)
    _git(repository, "commit", "-q", "-m", "test: restore the removed marker")

    report = promotion.sweep_promoted_revisions(
        PROJECT,
        root=repository,
        markers=[
            {
                "run_id": "r-removed-marker",
                "marker": MARKER,
                "path": FILE,
                "expect": "absent",
            }
        ],
    )

    finding = report["findings"][0]
    assert finding["reasons"] == ["removed-marker-still-present"]
    assert finding["markers"][0]["present"] is True


def test_sweep_reports_nothing_when_the_work_landed_intact(
    repository: Path, tmp_path: Path
) -> None:
    _record, head = _promote(repository, tmp_path, "r-landed-intact", add_marker=True)
    _git(repository, "merge", "-q", "--no-ff", head, "-m", "Merge run")
    assert _is_ancestor(repository, head, "HEAD")

    report = promotion.sweep_promoted_revisions(
        PROJECT,
        root=repository,
        markers=[{"run_id": "r-landed-intact", "marker": MARKER, "path": FILE}],
    )

    assert report["checked"] == 1
    assert report["findings"] == []
    assert report["unresolved_markers"] == []
