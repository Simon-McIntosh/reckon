"""The git shim refuses a mutating verb against a repository that is not the
run's worktree, decided at execution time rather than from the command text.

Every case drives the real shim through a shell, with the shim's directory
first on ``PATH``, so what is exercised is the file a worker actually runs: the
shell moves the working directory, resolves ``git`` through ``PATH``, and the
shim asks the real git which repository that invocation resolves to. The two
bypasses a text parser kept missing — a leading ``GIT_DIR`` assignment, and a
``cd`` or ``pushd`` earlier in the same command — are here because at execution
time they are no longer special: they only change the environment and the
working directory the shim reads.

Each refusal is paired with the same command succeeding once the guard does not
apply, so a test that passes because nothing ran is not mistaken for a working
control: without ``RECKON_RUN_ID`` the shim mutates the other checkout, and
inside the worktree the run's own HEAD moves and the assertion reads it moved.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.worker_git_shim import worker_shim_directory

SHIM = worker_shim_directory() / "git"
RUN_ID = "r-git-shim-resolved-repo"
REFUSAL_STATUS = 97


def _real_git() -> str:
    """The real git, with the shim's own directory dropped from the search."""
    shim_dir = os.path.realpath(str(SHIM.parent))
    kept = [
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and os.path.realpath(entry) != shim_dir
    ]
    found = shutil.which("git", path=os.pathsep.join(kept))
    assert found, "no real git on PATH outside the shim directory"
    return found


REAL_GIT = _real_git()


def _env(
    home: Path, *, run_id: str | None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """A minimal environment carrying only what the shim and git need."""
    real_dir = os.path.dirname(REAL_GIT)
    env = {
        "PATH": os.pathsep.join([str(SHIM.parent), real_dir, "/usr/bin", "/bin"]),
        "HOME": str(home),
        "RECKON_HOME": str(home),
        "RECKON_SHIM_PYTHON": sys.executable,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }
    if run_id is not None:
        env["RECKON_RUN_ID"] = run_id
    env.update(extra or {})
    return env


def _git_run(
    args: list[str], *, cwd: Path, home: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [REAL_GIT, *args],
        cwd=str(cwd),
        env=_env(home, run_id=None),
        capture_output=True,
        text=True,
        check=False,
    )


def _head(repo: Path, home: Path) -> str:
    result = _git_run(["rev-parse", "HEAD"], cwd=repo, home=home)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(path: Path, home: Path) -> str:
    """Create a repository with two commits; return the first commit's sha."""
    path.mkdir(parents=True, exist_ok=True)
    assert _git_run(["init", "-q"], cwd=path, home=home).returncode == 0
    for key, value in (("user.email", "shim@example.invalid"), ("user.name", "Shim")):
        assert _git_run(["config", key, value], cwd=path, home=home).returncode == 0
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    assert _git_run(["add", "a.txt"], cwd=path, home=home).returncode == 0
    assert (
        _git_run(["commit", "-q", "-am", "first"], cwd=path, home=home).returncode == 0
    )
    first = _head(path, home)
    (path / "a.txt").write_text("two\n", encoding="utf-8")
    assert (
        _git_run(["commit", "-q", "-am", "second"], cwd=path, home=home).returncode == 0
    )
    return first


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Any]:
    home = tmp_path / "home"
    home.mkdir()
    worktree = tmp_path / "worktree"
    other = tmp_path / "other"
    first_worktree = _repo(worktree, home)
    first_other = _repo(other, home)
    pointer = home / "crew" / "live" / f"{RUN_ID}.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"worktree": str(worktree)}), encoding="utf-8")
    return {
        "home": home,
        "worktree": worktree,
        "other": other,
        "first_worktree": first_worktree,
        "first_other": first_other,
    }


def _shell(
    script: str,
    *,
    cwd: Path,
    home: Path,
    run_id: str | None = RUN_ID,
    extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one shell command with the shim first on ``PATH``."""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=_env(home, run_id=run_id, extra=extra),
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_refused(
    result: subprocess.CompletedProcess[str], *, repo: Path, worktree: Path
) -> None:
    """A refusal exits with its own status and names run, worktree and target."""
    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "refusing" in result.stderr
    assert RUN_ID in result.stderr
    assert str(worktree) in result.stderr
    assert str(repo) in result.stderr


def test_a_leading_git_dir_against_another_checkout_is_refused(
    repos: dict[str, Any],
) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)
    assert before != repos["first_other"]

    result = _shell(
        f"env GIT_DIR={other}/.git git reset --hard {repos['first_other']}",
        cwd=other,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before


def test_a_cd_into_another_checkout_is_refused(repos: dict[str, Any]) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)

    result = _shell(
        f"cd {other} && git reset --hard {repos['first_other']}",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before


def test_a_pushd_into_another_checkout_is_refused(repos: dict[str, Any]) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)

    result = _shell(
        f"pushd {other} >/dev/null && git reset --hard {repos['first_other']}",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before


def test_a_plain_dash_c_against_another_checkout_is_refused(
    repos: dict[str, Any],
) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)

    result = _shell(
        f"git -C {other} reset --hard {repos['first_other']}",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before


def test_the_same_verb_inside_the_run_worktree_succeeds(
    repos: dict[str, Any],
) -> None:
    home, worktree = repos["home"], repos["worktree"]
    before = _head(worktree, home)
    assert before != repos["first_worktree"]

    result = _shell(
        f"git -C {worktree} reset --hard {repos['first_worktree']}",
        cwd=home,
        home=home,
    )

    assert result.returncode == 0, result.stderr
    assert _head(worktree, home) == repos["first_worktree"]


def test_read_only_verbs_pass_against_another_checkout(
    repos: dict[str, Any],
) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)

    logged = _shell(f"git -C {other} log --oneline", cwd=home, home=home)
    status = _shell(f"git -C {other} status --porcelain", cwd=home, home=home)

    assert logged.returncode == 0, logged.stderr
    assert status.returncode == 0, status.stderr
    assert _head(other, home) == before


def test_without_a_run_id_the_shim_is_transparent(repos: dict[str, Any]) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)
    assert before != repos["first_other"]

    result = _shell(
        f"git -C {other} reset --hard {repos['first_other']}",
        cwd=home,
        home=home,
        run_id=None,
    )

    assert result.returncode == 0, result.stderr
    assert _head(other, home) == repos["first_other"]


def test_a_run_id_with_no_pointer_refuses_rather_than_guessing(
    repos: dict[str, Any],
) -> None:
    home, other = repos["home"], repos["other"]
    before = _head(other, home)
    (home / "crew" / "live" / f"{RUN_ID}.json").unlink()

    result = _shell(
        f"git -C {other} reset --hard {repos['first_other']}", cwd=home, home=home
    )

    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "refusing" in result.stderr
    assert _head(other, home) == before
