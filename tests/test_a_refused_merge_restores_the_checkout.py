"""A refused ref update cannot leave an unlanded tree serving as source."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import merge
from tests.helpers.git import git


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("RECKON_CREW_RUN_ID", raising=False)
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    for path in ("source.py", "removed.py", "peer.txt"):
        (repo / path).write_text("base\n")
    git(repo, "add", "source.py", "removed.py", "peer.txt")
    git(repo, "commit", "-qm", "test: seed the checkout")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


def delivered(repo, tmp_path, name, content):
    worker = tmp_path / name
    git(repo, "worktree", "add", "--detach", str(worker), "HEAD")
    (worker / "source.py").write_text(content)
    (worker / "added.py").write_bytes(b"\x00binary\xff\n")
    (worker / "removed.py").unlink()
    (worker / "link.py").symlink_to("source.py")
    (worker / "executable.py").write_text("executable\n")
    (worker / "executable.py").chmod(0o755)
    git(
        worker, "add", "source.py", "added.py", "removed.py", "link.py", "executable.py"
    )
    git(worker, "commit", "-qm", "test: deliver source")
    return git(worker, "rev-parse", "HEAD")


def digest(repo):
    paths = [
        "source.py",
        "added.py",
        "removed.py",
        "link.py",
        "executable.py",
        "peer.txt",
    ]
    index = (
        git(repo, "ls-files", "--stage", "--", *paths),
        git(repo, "ls-files", "-v", "--", *paths),
    )
    worktree = []
    for name in paths:
        path = repo / name
        if path.is_symlink():
            worktree.append((name, "link", os.readlink(path)))
        elif path.exists():
            worktree.append(
                (
                    name,
                    path.stat().st_mode,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            )
        else:
            worktree.append((name, None))
    return index, worktree


def land(repo, head, **kwargs):
    return merge.land_merge(
        repo,
        head=head,
        message="fix: land reviewed source",
        body="Carry reviewed source onto the primary branch.",
        release=False,
        attempts=1,
        **kwargs,
    )


@pytest.mark.parametrize("flag", [None, "--assume-unchanged", "--skip-worktree"])
def test_a_refused_ref_update_restores_the_index_and_worktree(checkout, tmp_path, flag):
    repo = checkout
    head = delivered(repo, tmp_path, "first", "first delivery\n")
    other = delivered(repo, tmp_path, "second", "second delivery\n")
    (repo / "peer.txt").write_text("peer staged\n")
    git(repo, "add", "peer.txt")
    (repo / "peer.txt").write_text("peer unstaged\n")
    if flag:
        git(repo, "update-index", flag, "source.py")
    before = digest(repo)
    base = git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        '#!/bin/sh\nif [ "$1" = prepared ] && grep -q " refs/heads/main$"; then echo "ref update refused" >&2; exit 1; fi\n'
    )
    hook.chmod(0o755)

    with pytest.raises(merge.MergeError) as refusal:
        land(repo, head)

    hook.unlink()
    after = digest(repo)
    assert git(repo, "rev-parse", "HEAD") == base
    # Attempt the following delivery even when residue is present, so the red
    # control records both the stale index and its practical consequence.
    second_error = None
    try:
        report = land(repo, other)
    except merge.MergeError as exc:
        second_error = str(exc)
    assert after == before, (
        f"refusal: {refusal.value}; second merge refused: {second_error}"
    )
    assert second_error is None
    assert "restored" in str(refusal.value)
    assert report["pushed"] is True
    assert (repo / "source.py").read_text() == "second delivery\n"
    assert (repo / "peer.txt").read_text() == "peer unstaged\n"
    assert git(repo, "show", ":peer.txt") == "peer staged"


def test_an_exception_after_unpacking_also_restores_the_checkout(
    checkout, tmp_path, monkeypatch
):
    repo = checkout
    head = delivered(repo, tmp_path, "worker", "delivered source\n")
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        '#!/bin/sh\nif [ "$1" = prepared ] && grep -q " refs/heads/main$"; then exit 1; fi\n'
    )
    hook.chmod(0o755)
    before = digest(repo)
    original = merge._run

    def interrupted(repository, *args, **kwargs):
        result = original(repository, *args, **kwargs)
        if args[:2] == ("merge", "--ff-only"):
            assert result.returncode != 0
            raise OSError("transport of git result interrupted")
        return result

    monkeypatch.setattr(merge, "_run", interrupted)
    with pytest.raises(merge.MergeError, match="restored"):
        land(repo, head)
    assert digest(repo) == before


@pytest.mark.parametrize("directory_first", [True, False])
def test_a_refusal_restores_file_directory_replacements(
    checkout, tmp_path, directory_first
):
    repo = checkout
    if directory_first:
        (repo / "source.py").unlink()
        (repo / "source.py").mkdir()
        (repo / "source.py" / "child.py").write_text("child\n")
        git(repo, "add", "source.py")
        git(repo, "commit", "-qm", "test: seed a directory")
    worker = tmp_path / "worker"
    git(repo, "worktree", "add", "--detach", str(worker), "HEAD")
    if directory_first:
        (worker / "source.py" / "child.py").unlink()
        (worker / "source.py").rmdir()
        (worker / "source.py").write_text("file\n")
    else:
        (worker / "source.py").unlink()
        (worker / "source.py").mkdir()
        (worker / "source.py" / "child.py").write_text("child\n")
    git(worker, "add", "source.py")
    git(worker, "commit", "-qm", "test: replace a checkout entry")
    head = git(worker, "rev-parse", "HEAD")
    paths = merge.landed_paths(repo, "HEAD", head)
    files = {name: merge._checkout_path(repo / name) for name in paths}
    index = merge._checkout_index(repo, paths)
    hook = repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(
        '#!/bin/sh\nif [ "$1" = prepared ] && grep -q " refs/heads/main$"; then exit 1; fi\n'
    )
    hook.chmod(0o755)
    with pytest.raises(merge.MergeError, match="restored"):
        land(repo, head)
    assert {name: merge._checkout_path(repo / name) for name in paths} == files
    assert merge._checkout_index(repo, paths) == index


@pytest.mark.parametrize(
    "point", ["verification", "conflict", "candidate", "index-lock"]
)
def test_checks_refuse_before_writing_and_leave_the_next_delivery_free(
    checkout, tmp_path, point
):
    repo = checkout
    head = delivered(repo, tmp_path, "first", "first delivery\n")
    options = {}
    lock = None
    if point == "verification":

        def refuse(*args):
            raise merge.MergeError("verification refused")

        options["verify"] = refuse
    elif point == "candidate":
        options["candidate"] = head
    elif point == "conflict":
        (repo / "source.py").write_text("primary delivery\n")
        git(repo, "add", "source.py")
        git(repo, "commit", "-qm", "test: advance primary")
    elif point == "index-lock":
        lock = repo / ".git" / "index.lock"
        lock.write_text("live writer\n")
    other = delivered(repo, tmp_path, "second", "second delivery\n")
    (repo / "peer.txt").write_text("peer edit\n")
    before = digest(repo)
    with pytest.raises(merge.MergeError):
        land(repo, head, **options)
    assert digest(repo) == before
    if lock is not None:
        lock.unlink()
    assert land(repo, other)["pushed"] is True
    assert (repo / "peer.txt").read_text() == "peer edit\n"


def test_a_successful_landing_keeps_its_files_and_the_push(checkout, tmp_path):
    repo = checkout
    head = delivered(repo, tmp_path, "first", "delivered source\n")
    base = git(repo, "rev-parse", "HEAD")
    report = land(repo, head)
    assert report["base"] == base
    assert git(repo, "rev-parse", "HEAD^@").split() == [base, head]
    assert git(repo, "rev-parse", "origin/main") == report["commit"]
    assert (repo / "source.py").read_text() == "delivered source\n"
    assert (repo / "added.py").read_bytes() == b"\x00binary\xff\n"
    assert (repo / "link.py").is_symlink()
    assert (repo / "executable.py").stat().st_mode & 0o111
    assert not (repo / "removed.py").exists()


def test_a_head_with_no_file_delta_uses_the_same_landing(checkout):
    repo = checkout
    base = git(repo, "rev-parse", "HEAD")
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    head = git(
        repo, "commit-tree", tree, "-p", base, "-m", "test: record an empty delivery"
    )
    before = digest(repo)
    report = land(repo, head)
    assert report["pushed"] is True
    assert git(repo, "rev-parse", "HEAD^@").split() == [base, head]
    assert digest(repo) == before


@pytest.mark.parametrize("staged", [True, False])
def test_source_residue_names_the_run_and_its_older_head(
    checkout, tmp_path, monkeypatch, staged
):
    obligations = importlib.import_module("reckon.crew.obligations")
    repo = checkout
    older = delivered(repo, tmp_path, "worker", "older source\n")
    worker = tmp_path / "worker"
    (worker / "source.py").write_text("newer source\n")
    git(worker, "add", "source.py")
    git(worker, "commit", "-qm", "test: revise delivered source")
    newer = git(worker, "rev-parse", "HEAD")
    (repo / "docs").mkdir()
    home = Path(os.environ["RECKON_HOME"])
    (home / "mounts.json").write_text(json.dumps({"project": str(repo / "docs")}))
    pointer = {
        "run_id": "run-source",
        "project": "project",
        "session": "owner",
        "worktree": str(worker),
        "head": newer,
    }
    monkeypatch.setattr(obligations, "_live_pointers", lambda project: [pointer])
    monkeypatch.setattr(obligations, "_run_file_rows", lambda *args: [])
    assert obligations._unlanded_source_items("project") == []
    with monkeypatch.context() as unchanged:

        def unexpected_git(*args, **kwargs):
            raise AssertionError("unchanged source spawned another git read")

        unchanged.setattr(obligations, "run_git", unexpected_git)
        assert obligations._unlanded_source_items("project") == []
    (repo / "source.py").write_text(git(repo, "show", f"{older}:source.py") + "\n")
    if staged:
        git(repo, "add", "source.py")
        # Index and serving source are independently checked.
        (repo / "source.py").write_text("base\n")
    before = digest(repo)
    notices = obligations._unlanded_source_items("project")
    assert len(notices) == 1, notices
    notice = notices[0]
    assert notice["kind"] == "unlanded-source"
    assert notice["run_id"] == "run-source"
    assert notice["paths"] == ["source.py"]
    assert notice["head"] == older
    assert older != newer
    assert "run-source" in notice["reason"]
    assert "source.py" in notice["next_command"]
    inspection = subprocess.run(
        ["sh", "-c", notice["next_command"]],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "older source" in inspection.stdout
    assert digest(repo) == before

    # The published sweep and a direct read must carry the same source duty.
    from reckon.crew import obligation_snapshot

    monkeypatch.setattr(obligations, "_classified_rows", lambda project: [])
    monkeypatch.setattr(obligations, "_held_worktrees", lambda *args, **kwargs: [])
    monkeypatch.setattr(obligations, "_sub_floor_items", lambda *args, **kwargs: [])
    direct = obligations.obligations("project", "owner")
    projected = obligation_snapshot.payload_for(
        obligation_snapshot.FleetState(
            project="project",
            now=datetime.now(UTC),
            config={},
            grace=0,
            floors={},
            rows=[],
            pointers={},
            acknowledged={},
            reviews_in_flight={},
            sub_floor={},
            held_worktrees={},
            unreconciled={},
            findings=[],
            unlanded_source=notices,
        ),
        "owner",
    )
    assert [
        row for row in direct["obligations"] if row["kind"] == "unlanded-source"
    ] == [row for row in projected["obligations"] if row["kind"] == "unlanded-source"]


def test_a_primary_history_copy_and_unattributed_edit_raise_no_residue_duty(
    checkout, tmp_path, monkeypatch
):
    obligations = importlib.import_module("reckon.crew.obligations")
    repo = checkout
    old_content = (repo / "source.py").read_text()
    head = delivered(repo, tmp_path, "worker", "delivered source\n")
    land(repo, head)
    (repo / "docs").mkdir()
    home = Path(os.environ["RECKON_HOME"])
    (home / "mounts.json").write_text(json.dumps({"project": str(repo / "docs")}))
    monkeypatch.setattr(
        obligations,
        "_run_file_rows",
        lambda *args: [
            {"run_id": "run-source", "commits": [head]},
        ],
    )
    monkeypatch.setattr(obligations, "_live_pointers", lambda project: [])
    (repo / "source.py").write_text(old_content)
    git(repo, "add", "source.py")
    assert obligations._unlanded_source_items("project") == []
    (repo / "source.py").write_text("independent peer edit\n")
    assert obligations._unlanded_source_items("project") == []
