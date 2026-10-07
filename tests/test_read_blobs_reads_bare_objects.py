"""``read_blobs answers a bare object name, not only a ``(revision, path)`` spec.

The reviewed-bytes reader looked up a single blob by sha, and used to restate
the batched ``cat-file --batch`` protocol to reach it. ``read_blobs`` now takes
a bare object name as a spec alongside a ``(revision, path)`` pair, so one
reader owns the protocol. These tests pin that entry: the bytes a bare sha
names, and ``None`` for a sha git reports missing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from reckon import velocity

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")
MISSING_SHA = "0" * 40


def _git_env() -> dict[str, str]:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside the worker's own worktree, so drop the inherited identity
    # before pointing the subprocess at the synthesised repository.
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _DISPATCH_IDENTITY
    }


def _git(repo: Path, *arguments: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        input=input,
        env=_git_env(),
    )
    return result.stdout


def _write_blob(repo: Path, text: str) -> str:
    """Write ``text`` as a loose object and return its bare sha."""
    return (
        _git(repo, "hash-object", "-w", "--stdin", input=text.encode()).decode().strip()
    )


def _empty_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    return repo


def test_read_blobs_answers_a_bare_object_name(tmp_path: Path):
    repo = _empty_repo(tmp_path)
    sha = _write_blob(repo, "reviewed plan bytes\n")

    blobs = velocity.read_blobs(repo, [sha])

    assert blobs == {sha: b"reviewed plan bytes\n"}


def test_read_blobs_reports_a_missing_bare_object_as_none(tmp_path: Path):
    repo = _empty_repo(tmp_path)
    present = _write_blob(repo, "present\n")

    # One call carries the present sha and a sha git has never seen, so the
    # present-bytes branch and the missing-object branch are exercised together.
    blobs = velocity.read_blobs(repo, [present, MISSING_SHA])

    assert blobs[present] == b"present\n"
    assert blobs[MISSING_SHA] is None


def test_a_bare_object_name_reads_alongside_a_revision_path_pair(tmp_path: Path):
    repo = _empty_repo(tmp_path)

    target = repo / "reckon" / "example.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def example():\n    return 1\n")
    _git(repo, "add", "reckon/example.py")
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-q",
        "-m",
        "feat: seed",
    )
    revision = _git(repo, "rev-parse", "HEAD").decode().strip()
    bare = _write_blob(repo, "bare\n")

    blobs = velocity.read_blobs(repo, [bare, (revision, "reckon/example.py")])

    assert blobs[bare] == b"bare\n"
    assert blobs[(revision, "reckon/example.py")] == target.read_bytes()
