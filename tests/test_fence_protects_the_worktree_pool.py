"""The fence seals the worktree pool, so a worker writes only its own tree.

Dispatch places every run's worktree under ``Code/.reckon-worktrees``, so the
pool holds a sibling run's tree beside the worker's own. When the pool is not
part of the fence's protected set, the whole pool is dev-bind-mounted writable
and a fenced worker can write another run's worktree — a boundary the fence
closes by naming the pool in the protected set.

The property is asserted two ways, because a shape assertion cannot tell a
fence that holds from one spelled correctly and doing nothing, and an executed
refusal cannot tell which overlay produced it.

* The outline is asserted on the *composed argv*: the read-only overlay of the
  pool is emitted, and the run's own worktree is bound writable **after** it.
  bubblewrap applies mounts in argv order, so the later ``--bind`` keeps the
  own tree writable while every sibling stays sealed under the same pool.
* The fence holds, proved by *executing* the argv ``launch_plan`` composes
  through the production path: a stub launched for worktree A writes into A
  (lands) and into sibling B (refused), and records both. The refusal is the
  read-only overlay, not a missing path: the shell says so.

The declared negative control drops the pool from the fence's declared
protected set — the function the fence composes from — so the pool's read-only
overlay leaves the argv; case 1's write into B then lands and the test fails.
Running this file directly prints that mutation verbatim on the first line and
the observed landing beneath it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence

NEGATIVE_CONTROL_MUTATION = (
    "strip Code/.reckon-worktrees from declared_protected_paths — the composed "
    "set the fence reads — so no read-only overlay of the pool is emitted; "
    "case 1's write into sibling worktree B then lands and the test fails"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

# Stands in for the harness: writes into its own worktree and into a sibling's,
# and records what happened. Both paths arrive through the environment, so the
# stub holds no assumption about where the fence put the boundary.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN/.own-probe" 2>/dev/null; then
  echo "own-write-ok $FENCE_OWN" >> "$FENCE_RESULTS"
else
  echo "own-write-refused $FENCE_OWN" >> "$FENCE_RESULTS"
fi
if err=$(touch "$FENCE_SIBLING/.sibling-probe" 2>&1); then
  echo "sibling-write-landed $FENCE_SIBLING" >> "$FENCE_RESULTS"
else
  echo "sibling-write-refused $FENCE_SIBLING :: $err" >> "$FENCE_RESULTS"
fi
"""


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


class Pool:
    """A synthetic home: one main checkout and two sibling worktrees.

    The main checkout sits under the home's ``Code`` root, where every git
    checkout is sealed. Both worktrees sit under the single pool root
    ``Code/.reckon-worktrees``, the shape dispatch produces.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.config = self.home / ".config" / "reckon"
        self.run = self.config / "crew" / "runs" / "r1"
        self.run.mkdir(parents=True)
        self.checkout = self.home / "Code" / "proj"
        self.checkout.mkdir(parents=True)
        _git("init", "-q", cwd=self.checkout)
        _git("config", "user.email", "t@example.com", cwd=self.checkout)
        _git("config", "user.name", "t", cwd=self.checkout)
        _git("config", "commit.gpgsign", "false", cwd=self.checkout)
        (self.checkout / "README").write_text("x")
        self.pool = self.home / "Code" / ".reckon-worktrees"
        self.pool.mkdir(parents=True)
        _git("add", "README", cwd=self.checkout)
        _git("commit", "-q", "-m", "init", cwd=self.checkout)
        # Two sibling runs, each with one worktree, exactly as dispatch lays
        # them out: <pool>/<session>/<node>.
        self.own = self.pool / "session-a" / "node-a"
        self.sibling = self.pool / "session-b" / "node-b"
        _git("worktree", "add", "--detach", str(self.own), cwd=self.checkout)
        _git("worktree", "add", "--detach", str(self.sibling), cwd=self.checkout)
        self.stub = tmp_path / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)
        self.manifest = self.run / "manifest.md"
        self.results = self.run / "results.txt"

    def argv(self) -> list[str]:
        """Compose the launch the way a dispatch does, through the production path."""
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": str(self.stub),
                "dialect": "claude",
                "sandbox": _backends.WORKTREE_FULL,
            },
            prompt="p",
            worktree=str(self.own),
            manifest_path=str(self.manifest),
            writable_directories=[str(self.run)],
            fence=True,
            fence_home=str(self.home),
        ).argv

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_OWN": str(self.own),
            "FENCE_SIBLING": str(self.sibling),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.own),
            capture_output=True,
            text=True,
            check=False,
        )

    def results_text(self) -> str:
        return self.results.read_text()


def _bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the writable ``--bind`` of ``destination``, or -1."""
    target = str(_backends.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the read-only overlay of ``destination``, or -1."""
    target = str(_backends.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--ro-bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _without_pool(home: Path, original: Callable[..., list[Path]]) -> Callable:
    """Return ``declared_protected_paths`` with the pool stripped — the mutation.

    The fence composes from the declared set rather than from
    :func:`_backends.protected_paths`, because a protected path absent for an
    instant while it is rewritten must still be sealed. A mutation that patched
    the narrowing instead would change nothing the fence reads, so the pool's
    read-only overlay would stay in the argv and the control could not produce
    its red result.
    """
    pool = _backends.resolved_destination(home / "Code" / ".reckon-worktrees")

    def patched(h=None, config=None):
        return [
            path
            for path in original(home if h is None else h, config)
            if _backends.resolved_destination(path) != pool
        ]

    return patched


def test_protected_paths_includes_the_worktree_pool(tmp_path: Path) -> None:
    """The pool is in the protected set, so an overlay of it is composed."""
    pool = Pool(tmp_path)
    protected = [
        _backends.resolved_destination(p) for p in _backends.protected_paths(pool.home)
    ]
    assert _backends.resolved_destination(pool.pool) in protected


def test_the_own_worktree_bind_follows_the_pool_overlay(tmp_path: Path) -> None:
    """The own-tree grant is emitted after the pool overlay, so it is not shadowed."""
    pool = Pool(tmp_path)
    argv = pool.argv()
    pool_overlay = _ro_bind_of(argv, pool.pool)
    own_bind = _bind_of(argv, pool.own)
    sibling_bind = _bind_of(argv, pool.sibling)
    # Positive control: the pool is actually overlaid, so an absent overlay
    # would mean the assertion below is testing nothing rather than passing.
    assert pool_overlay != -1, "the pool was not overlaid read-only"
    assert own_bind != -1, "the run's own worktree was not bound writable"
    assert own_bind > pool_overlay, (
        "the own-tree grant precedes the pool overlay, so the read-only bind "
        "would shadow it"
    )
    # The sibling worktree gets no writable grant of its own.
    assert sibling_bind == -1, "a sibling worktree was bound writable"


@requires_bwrap
def test_a_worker_writes_its_own_tree_and_not_the_sibling(tmp_path: Path) -> None:
    """Case 1: the own write lands, the sibling write is refused read-only."""
    pool = Pool(tmp_path)
    proc = pool.run_fence(pool.argv())
    assert proc.returncode == 0, proc.stderr
    results = pool.results_text()
    # Positive control: the stub only writes results from inside the fence, so
    # a non-empty record proves the run directory was writable and the stub ran.
    assert "own-write-ok" in results, results
    assert (pool.own / ".own-probe").exists()
    assert f"sibling-write-refused {pool.sibling}" in results
    assert "Read-only file system" in results
    assert not (pool.sibling / ".sibling-probe").exists()


def _negative_control_report(root: Path) -> list[str]:
    pool = Pool(root)
    original = _worker_fence.declared_protected_paths
    _worker_fence.declared_protected_paths = _without_pool(pool.home, original)
    try:
        proc = pool.run_fence(pool.argv())
    finally:
        _worker_fence.declared_protected_paths = original
    return [
        f"stub exit: {proc.returncode}",
        f"results: {pool.results_text().strip()!r}",
        f"sibling probe exists after the run: {(pool.sibling / '.sibling-probe').exists()}",
    ]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    # The fixture makes git repositories, and the worker's git shim refuses a
    # mutating verb outside its own worktree. The suite clears the dispatch
    # identity so the shim is transparent; this driver must do the same or the
    # fixture cannot build its repositories.
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        os.environ.pop(name, None)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
