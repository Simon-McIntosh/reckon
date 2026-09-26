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
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from reckon.worker_git_shim import (
    _OUTPUT_OPTIONS,
    _READ_ACTIONS,
    _READ_ONLY_VERBS,
    MISSING_BINARY_STATUS,
    worker_shim_directory,
)

SHIM = worker_shim_directory() / "git"
RUN_ID = "r-git-shim-resolved-repo"
REFUSAL_STATUS = 97

# The shortest a written option name may be and still be read as a spelling of a
# banned one. Three characters is the floor the shim applies; the unit test
# comparing this to the shim's own constant is what keeps the two from drifting.
_MIN_PREFIX_LENGTH = 3

# Every prefix of every banned option, from the shortest a refusal treats as a
# spelling of it. git resolves each of these to the option it abbreviates, so a
# check that compared whole names would forward the short spellings of a write
# it refuses in full. Generated rather than listed, so a banned option added to
# the shim is covered without an edit here.
_BANNED_OPTION_PREFIXES: list[tuple[str, str]] = [
    (option, option[: length + 2])
    for option in _OUTPUT_OPTIONS
    for length in range(_MIN_PREFIX_LENGTH, len(option) - 1)
]


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
    # A tag, so a describe in these repositories has something to resolve: the
    # work-tree forms the read allowlist has to consider answer only when a tag
    # is reachable, and refreshing the index is part of answering.
    assert (
        _git_run(["tag", "-a", "v1", "-m", "tagged"], cwd=path, home=home).returncode
        == 0
    )
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


@pytest.mark.parametrize(
    ("option", "prefix"),
    _BANNED_OPTION_PREFIXES,
    ids=[f"{option}-{prefix}" for option, prefix in _BANNED_OPTION_PREFIXES],
)
def test_every_prefix_of_a_banned_option_is_refused(
    repos: dict[str, Any], option: str, prefix: str
) -> None:
    """A spelling git resolves to a banned option is refused as that option.

    git accepts any unambiguous abbreviation of a long option, so every prefix
    of ``--output`` reaches the output write the full spelling is refused for.
    The target here is another checkout, so a prefix that got through would land
    the file there; the case is generated from the banned names, so an option
    added to the list is covered here without an edit.
    """
    home, other = repos["home"], repos["other"]
    target = other / f"ESCAPE-{prefix.lstrip('-')}.txt"
    assert not target.exists()

    result = _shell(f"git -C {other} log {prefix}={target}", cwd=home, home=home)

    _assert_output_refused(result, option=prefix)
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


# --- the whole read allowlist, driven against another checkout --------------
#
# Both writes this guard kept missing are made by a verb the allowlist forwards,
# and neither is visible in the verb: an output option names a file, and a read
# form whose answer is defined in terms of a refreshed index records that
# refresh in the target's index. The cases below are built by reading the
# allowlist from the module rather than by listing verbs here, so a verb added
# to the shim joins the parametrisation without an edit to this file, and every
# verb is run against a checkout that is not the run's with its stat cache
# deliberately stale, so a refresh has something to record. The assertion is the
# invariant that matters: the target's index bytes are unchanged and no new file
# appears in the target.

# The read invocation for each allowlisted verb. A verb not named here is run
# bare, so a verb added to the allowlist is still exercised — every verb is
# covered structurally, and the table only chooses the argument that makes a
# verb answer rather than error.
_READ_INVOCATION: dict[str, list[str]] = {
    "blame": ["blame", "a.txt"],
    "branch": ["branch", "--list"],
    "cat-file": ["cat-file", "-p", "HEAD"],
    "check-attr": ["check-attr", "text", "a.txt"],
    "check-ignore": ["check-ignore", "a.txt"],
    "cherry": ["cherry", "HEAD"],
    "config": ["config", "--get", "user.name"],
    "count-objects": ["count-objects", "-v"],
    "describe": ["describe", "--tags"],
    "diff": ["diff", "--cached"],
    "diff-tree": ["diff-tree", "-r", "HEAD"],
    "for-each-ref": ["for-each-ref"],
    "grep": ["grep", "one"],
    "help": ["help", "status"],
    "log": ["log", "--oneline"],
    "ls-files": ["ls-files", "--stage"],
    "ls-remote": ["ls-remote", "."],
    "ls-tree": ["ls-tree", "HEAD"],
    "merge-base": ["merge-base", "HEAD", "HEAD"],
    "name-rev": ["name-rev", "HEAD"],
    "notes": ["notes", "list"],
    "reflog": ["reflog", "show"],
    "remote": ["remote", "-v"],
    "rev-list": ["rev-list", "--count", "HEAD"],
    "show": ["show", "--stat"],
    "show-ref": ["show-ref"],
    "stash": ["stash", "list"],
    "status": ["status", "--porcelain"],
    "tag": ["tag", "--list"],
    "var": ["var", "GIT_AUTHOR_IDENT"],
    "verify-commit": ["verify-commit", "HEAD"],
    "verify-tag": ["verify-tag", "HEAD"],
    "version": ["version"],
    "whatchanged": ["whatchanged"],
    "worktree": ["worktree", "list"],
}

# The forms that refresh the index and record the refresh. Measured against
# another checkout with a stale stat cache, every one of them moved that
# checkout's index when the shim forwarded it, and the abbreviated spellings
# moved it too: git resolves any unambiguous long-option prefix, so a check that
# compared whole option names never saw the spelling that reached the form.
_INDEX_FORM_CASES: list[tuple[str, list[str]]] = [
    ("describe --dirty", ["describe", "--dirty"]),
    ("describe --dirty=<suffix>", ["describe", "--dirty=-dirty"]),
    ("describe --broken", ["describe", "--broken"]),
    ("diff", ["diff"]),
    ("diff --stat", ["diff", "--stat"]),
    ("diff HEAD", ["diff", "HEAD"]),
    ("diff --quiet", ["diff", "--quiet"]),
    ("describe --di --long", ["describe", "--di", "--long"]),
    ("describe --dirt", ["describe", "--dirt"]),
    ("describe --di=<suffix>", ["describe", "--di=-dirty"]),
]

# A ``diff`` answered from the object store is a read the guard must not refuse:
# it is here so a fix that refuses every ``diff`` fails the suite rather than
# passing it.
_READ_ONLY_FORM_CASES: list[tuple[str, list[str]]] = [
    ("diff --cached", ["diff", "--cached"]),
    ("diff --staged", ["diff", "--staged"]),
    ("diff --no-index", ["diff", "--no-index", "--", "/dev/null", "/dev/null"]),
    ("describe --tags", ["describe", "--tags"]),
]

# The read verbs carrying an option that names an output file.
_OUTPUT_FORM_CASES: list[tuple[str, list[str]]] = [
    ("log --output", ["log", "--output=ESCAPE-allowlist.txt"]),
    ("show --output", ["show", "--output=ESCAPE-allowlist.txt"]),
    ("diff --output", ["diff", "--output=ESCAPE-allowlist.txt"]),
]


def _read_cases() -> list[tuple[str, list[str]]]:
    """A case per allowlisted verb, plus the forms that write.

    Reading the two allowlisted sets is what makes the coverage structural: a
    verb added to either set is exercised by the case built from it, whatever
    its arguments, because a verb the table does not name is run bare.
    """
    cases = [
        (verb, _READ_INVOCATION.get(verb, [verb]))
        for verb in sorted(set(_READ_ONLY_VERBS) | set(_READ_ACTIONS))
    ]
    cases.extend(_INDEX_FORM_CASES)
    cases.extend(_READ_ONLY_FORM_CASES)
    cases.extend(_OUTPUT_FORM_CASES)
    return cases


_READ_CASES = _read_cases()


# The tracked file the fixture writes and commits, and the one whose mtime is
# moved to make the stat cache stale without changing what it holds.
_TRACKED = "a.txt"


def _target_files(repo: Path) -> list[str]:
    """Every file under a repository, as its own relative path."""
    return sorted(
        entry.relative_to(repo).as_posix()
        for entry in repo.rglob("*")
        if entry.is_file()
    )


@pytest.mark.parametrize("staleness", ["older", "newer"])
@pytest.mark.parametrize(
    ("label", "argv"), _READ_CASES, ids=[case[0] for case in _READ_CASES]
)
def test_no_read_form_writes_another_checkouts_index_or_files(
    repos: dict[str, Any], label: str, argv: list[str], staleness: str
) -> None:
    """No forwarded read form writes the target's index or a new file in it.

    The target is not the run's worktree, every tracked file is touched so its
    mtime differs from the recorded stat cache — older in one direction, newer
    in the other, because git re-checks in both — and the content is left
    alone. A form that refreshes the index and records the refresh, or that
    names a file to write, then moves one of those two bytes or files and the
    case fails.
    """
    home, other = repos["home"], repos["other"]
    index = other / ".git" / "index"
    before_index = index.read_bytes()
    before_files = _target_files(other)
    before_contents = (other / _TRACKED).read_text()
    delta = -3600 if staleness == "older" else 3600
    tracked = other / _TRACKED
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime + delta, stat.st_mtime + delta))
    # The staleness is the setup, not the measurement: touching a file with its
    # content unchanged must not have moved the index or anything else by itself.
    assert index.read_bytes() == before_index
    assert _target_files(other) == before_files

    result = _shell(f"git -C {other} {shlex.join(argv)}", cwd=home, home=home)

    assert result.returncode != MISSING_BINARY_STATUS, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    if result.returncode == REFUSAL_STATUS:
        assert "refusing" in result.stderr, result.stderr
    assert index.read_bytes() == before_index, (
        f"`git {label}` rewrote another checkout's index\n{result.stderr}"
    )
    assert _target_files(other) == before_files, (
        f"`git {label}` left a new file in another checkout\n{result.stderr}"
    )
    assert (other / _TRACKED).read_text() == before_contents


@pytest.mark.parametrize("staleness", ["older", "newer"])
@pytest.mark.parametrize(
    ("label", "argv"), _INDEX_FORM_CASES, ids=[case[0] for case in _INDEX_FORM_CASES]
)
def test_a_refreshing_read_leaves_another_checkouts_index_identical(
    repos: dict[str, Any], label: str, argv: list[str], staleness: str
) -> None:
    """A read form that refreshes the index is forwarded against a copy of it.

    The verb is on the read allowlist and the form carries no output option, so
    the form reaches the real git and the answer comes back. What it would write
    is the index refresh, and for a repository that is not this run's worktree
    the shim runs git against a private copy of that repository's index, so the
    target's bytes do not move whichever spelling reached the form — including
    the abbreviated spellings of an option a whole-name check never matched.
    """
    home, other = repos["home"], repos["other"]
    index = other / ".git" / "index"
    before_index = index.read_bytes()
    before_files = _target_files(other)
    delta = -3600 if staleness == "older" else 3600
    tracked = other / _TRACKED
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime + delta, stat.st_mtime + delta))
    assert index.read_bytes() == before_index

    result = _shell(f"git -C {other} {shlex.join(argv)}", cwd=home, home=home)

    assert result.returncode != MISSING_BINARY_STATUS, result.stderr
    assert result.returncode != REFUSAL_STATUS, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert index.read_bytes() == before_index, (
        f"`git {label}` rewrote another checkout's index\n{result.stderr}"
    )
    assert _target_files(other) == before_files


def test_a_forwarded_read_still_answers_from_the_targets_own_index(
    repos: dict[str, Any],
) -> None:
    """The copy carries the target's index, so the answer is the real one.

    An index the read could not see would be an empty one, and an empty index
    makes every committed file look untracked: ``status --porcelain`` would list
    it. The forwarded read answers an empty status for a checkout whose work
    tree matches its index, so what git read was that repository's own index,
    copied rather than invented.
    """
    home, other = repos["home"], repos["other"]
    tracked = other / _TRACKED
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime - 3600, stat.st_mtime - 3600))

    result = _shell(f"git -C {other} status --porcelain", cwd=home, home=home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout


def test_a_read_against_a_bare_repository_is_forwarded_without_an_index(
    repos: dict[str, Any], tmp_path: Path
) -> None:
    """A repository with no index has nothing to isolate, so it is forwarded.

    ``GIT_INDEX_FILE`` naming a file that does not exist would make git answer
    from an empty index — a different answer, not a safer one — so a bare
    repository, which has no index at all, is forwarded with nothing set and the
    read still answers.
    """
    home = repos["home"]
    bare = tmp_path / "bare.git"
    assert (
        _git_run(
            ["init", "-q", "--bare", str(bare)], cwd=tmp_path, home=home
        ).returncode
        == 0
    )
    assert not (bare / "index").exists()

    result = _shell(
        f"git -C {bare} rev-parse --is-bare-repository", cwd=home, home=home
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true", result.stdout
    assert not (bare / "index").exists()


def test_the_same_index_writing_form_inside_the_run_worktree_passes(
    repos: dict[str, Any],
) -> None:
    """The index written by these forms is a worker's own when it is at home.

    A worker legitimately asks whether its own work tree is dirty, and that
    refresh records into its own worktree's index, so the form is forwarded
    with the same arguments inside the run's worktree.
    """
    home, worktree = repos["home"], repos["worktree"]
    tracked = worktree / _TRACKED
    tracked.write_text(tracked.read_text() + "changed\n", encoding="utf-8")

    result = _shell(f"git -C {worktree} describe --dirty", cwd=home, home=home)

    # The forward is what this case is for, and the suffix proves it answered a
    # question about the run's own work tree rather than being refused.
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("-dirty")


def test_a_banned_option_is_matched_in_every_prefix_git_resolves_to_it() -> None:
    """The option check matches a name that abbreviates a banned option.

    git resolves any unambiguous long-option prefix, so ``--outp=<path>``
    reaches ``--output`` and writes exactly the file the full spelling writes. A
    check comparing whole names forwards the short spelling of the write it
    refuses in full, so the written name is matched against the banned names it
    is a prefix of, and the refusal names both.

    ``diff-index`` and ``diff-files`` share a prefix with allowlisted verbs but
    are not on the read allowlist at all, so every one of their forms is refused
    by the verb test rather than by this one.
    """
    from reckon.worker_git_shim import (
        _MIN_OPTION_PREFIX_LENGTH,
        _abbreviates,
        mutating_verb,
        output_refusal,
    )

    assert _MIN_OPTION_PREFIX_LENGTH == _MIN_PREFIX_LENGTH
    assert _abbreviates("output", _OUTPUT_OPTIONS) == "--output"
    assert _abbreviates("outp", _OUTPUT_OPTIONS) == "--output"
    assert _abbreviates("output-dir", _OUTPUT_OPTIONS) == "--output-directory"
    assert _abbreviates("output-indent", _OUTPUT_OPTIONS) is None
    assert _abbreviates("o", _OUTPUT_OPTIONS) is None
    assert _abbreviates("", _OUTPUT_OPTIONS) is None
    refusal = output_refusal("log", "--outp")
    assert "`--outp`" in refusal
    assert "(abbreviating `--output`)" in refusal
    assert mutating_verb("diff-index", []) == "diff-index"
    assert mutating_verb("diff-files", []) == "diff-files"
