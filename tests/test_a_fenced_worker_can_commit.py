"""A fenced worker can commit in its own worktree, proved by running the fence.

A linked worktree keeps its HEAD, index and reflog under the main checkout's
``.git/worktrees/<name>`` and writes its objects into that repository's shared
store, and both of those live below a path the fence seals. So a worker that
should be able to commit the work it was dispatched to do finds its own git
metadata read-only, and the fence that was meant to protect the operator's home
takes the worker's deliverable with it.

Nothing here is asserted by inspecting the argv alone. The fence composes an
argument vector and that vector is *executed* under bubblewrap against a
synthetic home: a real repository with a real linked worktree, and probes that
attempt the commit inside the fence and the three writes that must stay refused
— the main checkout's index, its ``refs/heads`` and a file in its working tree.

Two properties, and the second is what makes the first mean anything:

* inside the fence ``git commit --allow-empty`` succeeds and the new commit is
  reachable from the worktree HEAD, while every probe into the main checkout is
  refused;
* the declared negative control — leave the worktree git dir and object store
  out of the write roots, as before the change — refuses the commit with a
  read-only file system error, so the success above rests on the new grants and
  not on a fence that happened to allow the write anyway.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed refusal.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends

DECLARED_MUTATION = (
    "leave the worktree git dir and object store out of the write roots as "
    "today; the fenced commit must fail with a read-only file system error"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which(_backends.FENCE_BINARY) is None,
    reason="bubblewrap is not installed",
)

# The probe the fence runs. It commits in the worktree, prints the HEAD before
# and after, and then attempts three writes into the main checkout that must
# stay refused. Each probe reports itself so the outcome is read from the run
# rather than inferred from an exit code.
#
# The writes use ``touch`` rather than a redirection: a redirection error on a
# POSIX special builtin (``:``) makes the shell exit on the spot, which would
# abort the probe at the first refusal instead of recording it.
_PROBE = """
set -u
WT={worktree}
REPO={repo}
echo "before=$(git -C "$WT" rev-parse HEAD)"
if git -C "$WT" -c gc.auto=0 commit --allow-empty -m fenced-commit \
        >"$WT/commit.out" 2>&1; then
  echo "commit=ok"
else
  echo "commit=fail"
  sed 's/^/commit-err: /' "$WT/commit.out"
fi
echo "after=$(git -C "$WT" rev-parse HEAD)"
if touch "$WT/.fence-write-probe" 2>/dev/null; then
  echo "worktree-write=ok"
else
  echo "worktree-write=refused"
fi
if touch "$REPO/.git/index" 2>/dev/null; then
  echo "main-index=wrote"
else
  echo "main-index=refused"
fi
if touch "$REPO/.git/refs/heads/fence-probe" 2>/dev/null; then
  echo "main-refs=wrote"
else
  echo "main-refs=refused"
fi
if touch "$REPO/fence-probe.txt" 2>/dev/null; then
  echo "main-tree=wrote"
else
  echo "main-tree=refused"
fi
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _rev(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def _git_directory(worktree: Path) -> Path:
    return Path(_git(worktree, "rev-parse", "--absolute-git-dir").stdout.strip())


def _common_objects(worktree: Path) -> Path:
    common = Path(_git(worktree, "rev-parse", "--git-common-dir").stdout.strip())
    return (common / "objects").resolve()


def _make_repo(home: Path) -> tuple[Path, Path]:
    """Build a main checkout and a detached linked worktree under ``home``.

    The worktree sits under ``Code/.reckon-worktrees`` — not a protected path —
    so the test exercises the real shape: the tree itself is writable and only
    the git metadata below the main checkout is sealed.
    """
    repo = home / "Code" / "zzrepo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fence-test@example.invalid")
    _git(repo, "config", "user.name", "Fence Test")
    (repo / "tracked.txt").write_text("base\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    worktree = home / "Code" / ".reckon-worktrees" / "zzrepo" / "wt"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return repo, worktree


def _argv(worktree: Path, repo: Path, home: Path) -> list[str]:
    return _backends.fence_argv(
        ["sh", "-c", _PROBE.format(worktree=worktree, repo=repo)],
        worktree=worktree,
        home=home,
    )


def _argv_without_git_roots(worktree: Path, repo: Path, home: Path) -> list[str]:
    """Compose the fence with the git write roots removed, as before the change.

    The declared mutation, applied at the one place the new roots enter the
    argv. Restoring the original in a ``finally`` keeps the removal confined to
    this call.
    """
    original = _backends.worktree_git_write_roots
    _backends.worktree_git_write_roots = lambda *_args, **_kwargs: []
    try:
        return _argv(worktree, repo, home)
    finally:
        _backends.worktree_git_write_roots = original


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@requires_bwrap
def test_a_fenced_worker_commits_in_its_own_worktree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo, worktree = _make_repo(home)
    before = _rev(worktree)

    argv = _argv(worktree, repo, home)
    assert argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    # The new roots are exactly the worktree's own git directory and the shared
    # object store — never the common directory that carries refs/heads and the
    # main index.
    assert str(_git_directory(worktree)) in argv
    assert str(_common_objects(worktree)) in argv
    assert str((repo / ".git").resolve()) not in argv

    proc = _run(argv)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "commit=ok" in out, out

    after = _rev(worktree)
    assert after != before, out
    assert f"before={before}" in out, out
    assert f"after={after}" in out, out
    # Reachable from the worktree HEAD and present in the shared object store,
    # so the commit is real rather than a written ref with no object behind it.
    assert _git(worktree, "cat-file", "-e", after).returncode == 0
    assert _git(worktree, "merge-base", "--is-ancestor", after, "HEAD").returncode == 0

    # The worktree itself was writable through the fence: the success above
    # rests on the tree, not on the grants.
    assert "worktree-write=ok" in out, out
    # Everything else in the main checkout is still sealed.
    for probe in ("main-index", "main-refs", "main-tree"):
        assert f"{probe}=refused" in out, out
    assert not (repo / "fence-probe.txt").exists()
    assert not (repo / ".git" / "refs" / "heads" / "fence-probe").exists()


@requires_bwrap
def test_the_negative_control_refuses_the_commit_without_the_git_roots(
    tmp_path: Path,
) -> None:
    """The declared mutation, applied: the fenced commit must be refused.

    With the worktree git dir and object store left out of the write roots, the
    commit cannot write its index, HEAD and object, so it fails on a read-only
    file system. If the mutation did not apply the commit would succeed and this
    test would fail, so the refusal is attributable to the removed roots.
    """
    home = tmp_path / "home"
    repo, worktree = _make_repo(home)
    before = _rev(worktree)

    argv = _argv_without_git_roots(worktree, repo, home)
    # The mutation applied: the new roots are gone and the main checkout's own
    # git directory was never among them.
    assert str(_git_directory(worktree)) not in argv
    assert str(_common_objects(worktree)) not in argv
    assert str((repo / ".git").resolve()) not in argv

    proc = _run(argv)
    out = proc.stdout + proc.stderr
    assert "commit=fail" in out, out
    assert "Read-only file system" in out, out
    assert _rev(worktree) == before, out


def _negative_control_report(home: Path) -> list[str]:
    repo, worktree = _make_repo(home)
    before = _rev(worktree)
    argv = _argv_without_git_roots(worktree, repo, home)
    proc = _run(argv)
    lines = [f"worktree-git-dir-in-write-roots={str(_git_directory(worktree)) in argv}"]
    lines += proc.stdout.splitlines()
    lines += proc.stderr.splitlines()
    lines.append(f"head-unchanged={_rev(worktree) == before}")
    return lines


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory) / "home"):
            print(line)
    if shutil.which(_backends.FENCE_BINARY) is None:
        print("bubblewrap is not installed", file=sys.stderr)
        raise SystemExit(2)
