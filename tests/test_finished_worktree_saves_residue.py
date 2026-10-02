from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew.routing import _workspace_roots, garbage_collect


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "source.py").write_text("original\n")
    git(root, "add", "source.py")
    git(root, "commit", "-q", "-m", "test: seed")
    return root


def worktree(root: Path, name: str, revision: str = "HEAD") -> Path:
    tree = _workspace_roots(root)[0] / "session" / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "-q", "--detach", str(tree), revision)
    return tree


@pytest.mark.parametrize("case", ["subsumed", "superseded", "unique"])
def test_integrated_dirty_tree_saves_residue_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    tree = worktree(root, case)
    content = {"subsumed": "same\n", "superseded": "local\n", "unique": "unique\n"}[
        case
    ]
    (tree / "source.py").write_text(content)
    (tree / "notes.txt").write_text("untracked\n")
    if case == "subsumed":
        (root / "source.py").write_text(content)
    elif case == "superseded":
        (root / "source.py").write_text("other\n")
    if case != "unique":
        git(root, "add", "source.py")
        git(root, "commit", "-q", "-m", "test: integrate another edit")

    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))
    assert not tree.exists()
    patch = Path(row["residue_patch"])
    archive = Path(row["residue_tar"])
    assert patch.is_file() and archive.is_file()
    replay = worktree(root, f"replay-{case}", row["head"])
    git(replay, "apply", str(patch))
    assert (replay / "source.py").read_text() == content
    with tarfile.open(archive) as saved:
        assert saved.extractfile("notes.txt").read() == b"untracked\n"
    assert row["residue_classes"]["source.py"] == case
    assert row["residue_classes"]["notes.txt"] == "unique"
    assert str(tree) in report["removed_worktrees"]
    if case == "unique":
        assert any(item["worktree"] == str(tree) for item in report["residue_report"])


def test_live_claim_leaves_dirty_tree_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    tree = worktree(root, "live")
    (tree / "source.py").write_text("live\n")
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / "run-live.json").write_text(
        json.dumps({"run_id": "run-live", "worktree": str(tree), "phase": "working"})
    )
    report = garbage_collect(repo=root, apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))
    assert row["classification"] == "live-referenced"
    assert tree.exists()
    assert (tree / "source.py").read_text() == "live\n"


def test_equal_change_in_another_saved_residue_is_subsumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    first = worktree(root, "first")
    second = worktree(root, "second")
    for tree in (first, second):
        (tree / "source.py").write_text("same local edit\n")

    report = garbage_collect(repo=root, apply=True)
    rows = {Path(item["path"]).name: item for item in report["worktrees"]}
    assert rows["first"]["residue_classes"]["source.py"] == "unique"
    assert rows["second"]["residue_classes"]["source.py"] == "subsumed"
    assert not first.exists() and not second.exists()


def test_run_owned_residue_lands_in_its_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    root = repository(tmp_path)
    run_id = "run-saved"
    tree = worktree(root, "candidate")
    (tree / "source.py").write_text("staged\n")
    git(tree, "add", "source.py")
    ledger.append_run(
        "test",
        ledger.build_record(
            run_id=run_id,
            plan="sample",
            gate="passed",
            node="candidate",
            session="session",
        ),
        root=root,
    )

    report = garbage_collect(repo=root, project="test", apply=True)
    row = next(item for item in report["worktrees"] if item["path"] == str(tree))
    assert not tree.exists()
    assert Path(row["residue_patch"]).is_relative_to(home / "crew" / "runs" / run_id)
    replay = worktree(root, "replay-run", row["head"])
    git(replay, "apply", str(row["residue_patch"]))
    assert (replay / "source.py").read_text() == "staged\n"
