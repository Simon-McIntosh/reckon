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


def test_an_alias_in_the_target_repository_config_is_refused(
    repos: dict[str, Any],
) -> None:
    """A verb the target repository aliases to a writer is refused.

    The alias lives in the repository's own config, so the shim cannot see it in
    the invocation's argv. The rule is an allowlist for that reason: a name that
    is not a built-in reader is refused whether it expands to a reader or a
    writer, because a deny-list would have to know the name in advance.
    """
    home, other = repos["home"], repos["other"]
    configured = _git_run(["config", "alias.ci", "commit"], cwd=other, home=home)
    assert configured.returncode == 0, configured.stderr
    before = _head(other, home)
    assert before != repos["first_other"]

    result = _shell(
        f"git -C {other} ci --allow-empty -m bypass",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before


def test_a_run_id_that_is_not_one_path_component_is_refused(
    repos: dict[str, Any],
) -> None:
    """A run id carrying a separator cannot name a pointer outside the live dir.

    The record a traversal reaches is made to point at the other checkout, so a
    shim that trusts it forwards the mutation; one that requires a single safe
    component refuses before reading it.
    """
    home, other = repos["home"], repos["other"]
    escape = home / "crew" / "escape.json"
    escape.write_text(json.dumps({"worktree": str(other)}), encoding="utf-8")
    before = _head(other, home)
    assert before != repos["first_other"]

    result = _shell(
        f"git -C {other} reset --hard {repos['first_other']}",
        cwd=home,
        home=home,
        run_id="../escape",
    )

    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "refusing" in result.stderr
    assert _head(other, home) == before


def test_a_bare_stash_against_another_checkout_is_refused(
    repos: dict[str, Any],
) -> None:
    """A multi-purpose verb is mutating bare: `git stash` pushes.

    Only an explicit listing or reading form is read-only, so a bare verb is
    checked rather than forwarded. The other checkout is left dirty so the
    forwarding arm would have stashed and reverted it.
    """
    home, other = repos["home"], repos["other"]
    (other / "a.txt").write_text("dirty\n", encoding="utf-8")
    before = _head(other, home)

    result = _shell(f"git -C {other} stash", cwd=home, home=home)

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert _head(other, home) == before
    assert (other / "a.txt").read_text(encoding="utf-8") == "dirty\n"
    listed = _git_run(["stash", "list"], cwd=other, home=home)
    assert listed.stdout.strip() == ""


def test_a_stash_listing_against_another_checkout_passes(
    repos: dict[str, Any],
) -> None:
    """The explicit read form of the same verb still passes."""
    home, other = repos["home"], repos["other"]
    listed = _shell(f"git -C {other} stash list", cwd=home, home=home)
    assert listed.returncode == 0, listed.stderr


def test_a_work_tree_flag_override_is_refused(repos: dict[str, Any]) -> None:
    """The run's own git dir with a foreign work tree is refused.

    ``--git-dir`` naming the run's repository is not enough: ``--work-tree``
    sends the write to another directory, so both must be checked.
    """
    home, worktree, other = repos["home"], repos["worktree"], repos["other"]
    (other / "a.txt").write_text("keep me\n", encoding="utf-8")

    result = _shell(
        f"git --git-dir {worktree}/.git --work-tree {other} reset --hard HEAD",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=worktree)
    assert (other / "a.txt").read_text(encoding="utf-8") == "keep me\n"


def test_a_work_tree_env_override_is_refused(repos: dict[str, Any]) -> None:
    """The same escape through GIT_DIR + GIT_WORK_TREE is refused."""
    home, worktree, other = repos["home"], repos["worktree"], repos["other"]
    (other / "a.txt").write_text("keep me\n", encoding="utf-8")

    result = _shell(
        f"env GIT_DIR={worktree}/.git GIT_WORK_TREE={other} git reset --hard HEAD",
        cwd=home,
        home=home,
    )

    _assert_refused(result, repo=other, worktree=worktree)
    assert (other / "a.txt").read_text(encoding="utf-8") == "keep me\n"


def test_fsck_lost_found_against_another_checkout_is_refused(
    repos: dict[str, Any],
) -> None:
    """`fsck --lost-found` writes into the target's git dir, so it is refused."""
    home, other = repos["home"], repos["other"]

    result = _shell(f"git -C {other} fsck --lost-found", cwd=home, home=home)

    _assert_refused(result, repo=other, worktree=repos["worktree"])
    assert not (other / ".git" / "lost-found").exists()


def _assert_output_refused(
    result: subprocess.CompletedProcess[str], *, option: str
) -> None:
    """An output option is refused by naming the option and saying why.

    The refusal happens before the repository is resolved, so it names the run
    and the worktree only as the rule it applies, not as a target: the check is
    on the option, whatever the repository.
    """
    assert result.returncode == REFUSAL_STATUS, result.stderr
    assert "refusing" in result.stderr
    assert option in result.stderr
    assert "output file" in result.stderr


@pytest.mark.parametrize("verb", ["log", "show", "diff"])
def test_an_output_option_against_another_checkout_is_refused(
    repos: dict[str, Any], verb: str
) -> None:
    """A read verb carrying `--output=<path>` writes <path>, so it is refused.

    The verb is on the read-only allowlist, so the verb test alone forwards it;
    the file it names is written wherever the path points — another checkout
    here — and is the write the option check exists to stop.
    """
    home, other = repos["home"], repos["other"]
    target = other / f"ESCAPE-{verb}.txt"
    assert not target.exists()

    result = _shell(f"git -C {other} {verb} --output={target}", cwd=home, home=home)

    _assert_output_refused(result, option="--output")
    assert not target.exists()


def test_an_output_option_into_the_home_directory_is_refused(
    repos: dict[str, Any],
) -> None:
    """The refusal is on the option, not on the repository it names.

    A path outside any checkout — the home directory, which a worker must not
    write — is refused the same way, because the option is what is checked.
    """
    home, other = repos["home"], repos["other"]
    target = home / "x"
    assert not target.exists()

    result = _shell(f"git -C {other} log --output={target}", cwd=home, home=home)

    _assert_output_refused(result, option="--output")
    assert not target.exists()


def test_an_output_option_in_separate_argument_form_is_refused(
    repos: dict[str, Any],
) -> None:
    """The separated spelling `--output <path>` writes too, so it is refused.

    Only the option token has to be found: the path is the next token and is
    left in the tail, so a check that required the joined form would forward a
    command that writes the same file.
    """
    home, other = repos["home"], repos["other"]
    target = other / "ESCAPE-separate.txt"
    assert not target.exists()

    result = _shell(f"git -C {other} log --output {target}", cwd=home, home=home)

    _assert_output_refused(result, option="--output")
    assert not target.exists()


def test_the_output_option_check_ignores_options_that_write_nothing() -> None:
    """Only an option that names an output file is matched.

    ``--output-indent`` and the other ``--output-*`` modifiers write nothing, and
    the short ``-o`` is not an output on this allowlist at all: on ``ls-files``
    it is ``--others`` and on ``grep`` it is ``--only-matching``.
    """
    from reckon.worker_git_shim import writing_argument

    assert writing_argument(["--output=out.txt", "HEAD"]) == "--output"
    assert writing_argument(["--output", "out.txt"]) == "--output"
    assert writing_argument(["--output-directory", "dir"]) == "--output-directory"
    assert writing_argument(["--output-directory=dir"]) == "--output-directory"
    assert writing_argument(["--output-indent=2"]) is None
    assert writing_argument(["-o"]) is None
    assert writing_argument(["--oneline", "HEAD"]) is None
    assert writing_argument([]) is None


def test_a_reader_whose_short_option_is_not_an_output_passes(
    repos: dict[str, Any],
) -> None:
    """The reads whose short `-o` means something else still pass.

    ``git ls-files -o`` lists untracked files and ``git grep -o`` prints only
    the match: both read, so an output check that matched every short ``-o``
    would refuse reads it has no reason to.
    """
    home, other = repos["home"], repos["other"]

    untracked = _shell(
        f"git -C {other} ls-files -o --exclude-standard", cwd=home, home=home
    )
    matched = _shell(f"git -C {other} grep -o two", cwd=home, home=home)

    assert untracked.returncode == 0, untracked.stderr
    assert matched.returncode == 0, matched.stderr
    assert "two" in matched.stdout


def test_status_against_another_checkout_leaves_its_index_unchanged(
    repos: dict[str, Any],
) -> None:
    """A forwarded read takes no optional lock, so it cannot write the index.

    ``git status`` refreshes the stat cache and writes the index back when it
    can take the index lock, so a read aimed at another checkout edits that
    checkout's index — a write performed by a verb this guard forwards. The
    forwarded environment sets ``GIT_OPTIONAL_LOCKS=0``, git's documented switch
    that keeps a read from taking an optional lock, so the refresh still happens
    in memory and the bytes on disk do not move when only the stat cache is
    stale.
    """
    home, other = repos["home"], repos["other"]
    tracked = other / "a.txt"
    index = other / ".git" / "index"
    assert tracked.read_text(encoding="utf-8") == "two\n"
    before_bytes = index.read_bytes()
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime - 3600, stat.st_mtime - 3600))
    assert tracked.read_text(encoding="utf-8") == "two\n"
    assert index.read_bytes() == before_bytes

    result = _shell(f"git -C {other} status --porcelain", cwd=home, home=home)

    assert result.returncode == 0, result.stderr
    assert index.read_bytes() == before_bytes
