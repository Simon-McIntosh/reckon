"""A fenced worker runs the real git; only an unfenced worker carries the shim.

The git shim refuses a mutating verb aimed at a repository other than the run's
worktree, and it does so by starting an interpreter on every git call. Inside the
fence that refusal is already the kernel's: every checkout under the code root
and every other worktree in the pool is overlaid read-only, and only the run's
own worktree, its git directory and the shared object store are re-opened. So a
fenced launch leaves the shim off its path, and an unfenced one keeps it.

Two kinds of evidence, and both are needed. The path cases show which launches
carry the shim. The fence case runs the real git, with no shim anywhere on its
path, inside a composed fence against a synthetic home, and shows that each
spelling of a foreign write the shim exists to refuse is refused by the fence:
a commit, a branch and a ref update in the main checkout, a commit in a peer's
worktree, and ``--git-dir``/``--work-tree`` aimed back at the main checkout.
A commit in the run's own worktree still lands.

The declared negative control leaves the main checkout and the worktree pool
out of the protected set. The same foreign writes then land, so the refusals
above rest on the fence and not on git declining the commands for some other
reason.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon import _backends, _worker_fence
from reckon._backends import LaunchPlan
from reckon.host import HostFacts
from reckon.nested_launch import shim_directory
from reckon.worker_git_shim import worker_shim_directory

requires_bwrap = pytest.mark.skipif(
    shutil.which(_backends.FENCE_BINARY) is None,
    reason="bubblewrap is not installed",
)

GIT_SHIMS = str(worker_shim_directory())


def _facts(*, inside: bool) -> HostFacts:
    return HostFacts(
        in_allocation=inside,
        job_id="1277272" if inside else None,
        step_id="batch" if inside else None,
        node="98dci4-clu-2058" if inside else None,
        tmp_filesystem="xfs",
        home_filesystem="gpfs",
        tmp_is_node_local=True,
        reason="" if inside else "no-slurm-job-id-in-the-environment",
        sources={},
    )


def _entries(path: str) -> list[str]:
    return path.split(os.pathsep)


def test_an_unfenced_launch_puts_the_git_shim_first() -> None:
    searched = dispatch_launch_module.launch_search_path(
        {"PATH": "/usr/bin"}, facts=_facts(inside=False)
    )

    assert _entries(searched)[0] == GIT_SHIMS


def test_a_fenced_launch_carries_no_git_shim_and_keeps_the_scheduler_shims() -> None:
    searched = dispatch_launch_module.launch_search_path(
        {"PATH": "/usr/bin"}, facts=_facts(inside=True), fenced=True
    )

    assert GIT_SHIMS not in _entries(searched)
    assert _entries(searched)[0] == str(shim_directory())
    assert "/usr/bin" in _entries(searched)


def test_a_fenced_launch_drops_an_inherited_git_shim() -> None:
    inherited = os.pathsep.join([GIT_SHIMS, "/usr/bin"])

    searched = dispatch_launch_module.launch_search_path(
        {"PATH": inherited}, facts=_facts(inside=False), fenced=True
    )

    assert GIT_SHIMS not in _entries(searched)
    assert _entries(searched) == ["/usr/bin"]


@pytest.mark.parametrize(("fenced", "carries_shim"), [(True, False), (False, True)])
def test_the_supervisor_spec_carries_the_shim_only_when_unfenced(
    tmp_path: Path, fenced: bool, carries_shim: bool
) -> None:
    plan = LaunchPlan(
        backend="fixture",
        dialect="fixture",
        argv=["worker"],
        cwd=str(tmp_path),
        stdin_text="",
        environment={"PATH": "/usr/bin"},
        final_message_path=None,
        resumed_session=None,
    )

    spec = dispatch_launch_module._supervisor_spec(
        run_id="fenced-path",
        run_directory=tmp_path / "run",
        repo_root=tmp_path,
        worktree=tmp_path / "worktree",
        plan=plan,
        fenced=fenced,
        prompt_path=tmp_path / "prompt.txt",
        log_path=tmp_path / "stream.jsonl",
        stderr_path=tmp_path / "stderr.log",
        facts=_facts(inside=False),
    )

    path = spec["plan"]["environment"]["PATH"]
    assert (GIT_SHIMS in _entries(path)) is carries_shim


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _checkout(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "fence-test@example.invalid")
    _git(path, "config", "user.name", "Fence Test")
    (path / "tracked.txt").write_text("base\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "base")


def _home(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A main checkout, the run's worktree and a peer's worktree under one home."""
    home = tmp_path / "home"
    repo = home / "Code" / "zzrepo"
    _checkout(repo)
    pool = home / "Code" / ".reckon-worktrees" / "zzrepo"
    own = pool / "own"
    peer = pool / "peer"
    pool.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "--detach", str(own), "HEAD")
    _git(repo, "worktree", "add", "-q", "--detach", str(peer), "HEAD")
    return home, repo, own, peer


# Each probe runs the real git found on a path that holds no reckon shim, and
# reports its own outcome, so a refusal is read from the run rather than inferred
# from an exit code.
_PROBE = """
set -u
export PATH=/usr/bin:/bin
G="git -c gc.auto=0 -c user.email=f@example.invalid -c user.name=F"
report() {{ if "$@" >/dev/null 2>&1; then echo ok; else echo refused; fi; }}
echo "own-commit=$(report $G -C {own} commit -q --allow-empty -m own)"
echo "main-commit=$(report $G -C {repo} commit -q --allow-empty -m foreign)"
echo "main-branch=$(report $G -C {repo} branch fence-probe)"
echo "main-update-ref=$(report $G -C {repo} update-ref refs/heads/fence-ref HEAD)"
echo "peer-commit=$(report $G -C {peer} commit -q --allow-empty -m peer)"
echo "git-dir-work-tree=$(report $G --git-dir={repo}/.git --work-tree={repo} \
    commit -q --allow-empty -m spelled)"
"""

_FOREIGN = (
    "main-commit",
    "main-branch",
    "main-update-ref",
    "peer-commit",
    "git-dir-work-tree",
)


@pytest.fixture
def unsealed_node_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the fence's node-tmp seal at an empty directory beside the home.

    pytest's temporary home lies under ``/tmp``, which the fence seals read-only
    in its own right. Left in place, that seal would refuse every foreign write
    below and the cases would prove the node-tmp seal rather than the checkout
    seal they are about.
    """
    node_tmp = tmp_path / "node-tmp"
    node_tmp.mkdir()
    monkeypatch.setattr(_worker_fence, "NODE_TMP_ROOT", node_tmp)


def _fenced_probe(home: Path, repo: Path, own: Path, peer: Path) -> dict[str, str]:
    argv = _backends.fence_argv(
        ["sh", "-c", _PROBE.format(own=own, repo=repo, peer=peer)],
        worktree=own,
        home=home,
    )
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in proc.stdout.split())


@requires_bwrap
@pytest.mark.usefixtures("unsealed_node_tmp")
def test_the_fence_refuses_every_foreign_git_write_the_shim_refused(
    tmp_path: Path,
) -> None:
    home, repo, own, peer = _home(tmp_path)
    main_head = _git(repo, "rev-parse", "HEAD")
    peer_head = _git(peer, "rev-parse", "HEAD")

    outcome = _fenced_probe(home, repo, own, peer)

    assert outcome["own-commit"] == "ok", outcome
    for probe in _FOREIGN:
        assert outcome[probe] == "refused", (probe, outcome)
    assert _git(repo, "rev-parse", "HEAD") == main_head
    assert _git(peer, "rev-parse", "HEAD") == peer_head
    assert "fence-probe" not in _git(repo, "branch", "--list")


@requires_bwrap
@pytest.mark.usefixtures("unsealed_node_tmp")
def test_the_negative_control_lands_the_foreign_writes_without_the_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, own, peer = _home(tmp_path)
    main_head = _git(repo, "rev-parse", "HEAD")
    # The declared mutation: the main checkout and the worktree pool leave the
    # protected set, which is what an unfenced worker without the shim would
    # face.
    sealed = _worker_fence._named_protected_paths

    def without_checkouts(home_dir: str | Path | None = None) -> list[Path]:
        return [
            path
            for path in sealed(home_dir)
            if path not in {repo, home / "Code" / ".reckon-worktrees"}
        ]

    monkeypatch.setattr(_worker_fence, "_named_protected_paths", without_checkouts)

    outcome = _fenced_probe(home, repo, own, peer)

    assert outcome["main-commit"] == "ok", outcome
    assert outcome["peer-commit"] == "ok", outcome
    assert _git(repo, "rev-parse", "HEAD") != main_head
