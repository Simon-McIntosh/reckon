"""A fenced worker cannot remove another run's scratch.

Every run's scratch directory is a direct child of one node-local root
(``worker_scratch_root()``, normally ``/tmp/reckon-crew-scratch``). The fence
dev-binds ``/`` writable and overlays only the declared protected paths
read-only, and the scratch root was not among them, so a fenced worker could
write — and delete — every other run's scratch beneath it. One run's cleanup
did exactly that on 2026-10-16, removing 609 sibling directories including the review
whose ``TMPDIR`` lived there.

The fence closes this the way it closes the worktree pool: the shared scratch
root is sealed read-only among the protected paths, and the run's *own* scratch
directory is re-bound writable after it, using the existing protected-then-
granted bind order. A worker can still read a sibling's scratch — a read-only
bind withholds writes, not reads.

The property is asserted three ways, because a shape assertion cannot tell a
fence that holds from one spelled correctly and doing nothing, and an executed
refusal cannot tell which overlay produced it.

* The outline is asserted on the *composed argv*: the read-only overlay of the
  scratch root is emitted, and the run's own scratch directory is bound
  writable **after** it, with no writable grant for a sibling.
* The hand-off is asserted on ``_fence_write_roots``: the run's own scratch
  directory is among the roots the fence re-binds, so a resumed or lane-changed
  run keeps its ``TMPDIR`` writable under the seal.
* The fence holds, proved by *executing* the composed argv: a fenced command
  writes and deletes a file in its own scratch (lands) and ``rm -rf`` of a
  sibling's scratch exits non-zero with the sibling still present.

The declared negative control strips the scratch root from the fence's sealed
set — patching the function that decides it — so no read-only overlay of the
root is emitted; the sibling ``rm -rf`` then lands and the executed test fails.
It is applied by running with ``RECKON_TEST_SCRATCH_SEAL_NEGATIVE=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from reckon import _worker_fence
from reckon.crew import dispatch_admission

NEGATIVE_CONTROL_MUTATION = (
    "strip the worker scratch root from the fence's sealed set — patch "
    "worker_scratch_seal_root to return None — so no read-only overlay of the "
    "root is emitted; the fenced rm then deletes the sibling directory and the "
    "test fails"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_SCRATCH_SEAL_NEGATIVE") == "1"

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

RUN_ID = "r-20261016T120000000000-under-test"
SIBLING_ID = "r-20261016T120500000000-sibling-run"

# Stands in for the harness: writes and deletes a file in its own scratch, then
# removes a sibling's whole scratch as its final command, so the command's exit
# code is the removal's. Every path arrives through the environment, so the stub
# holds no assumption about where the fence put the boundary.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN/probe" 2>/dev/null && rm -f "$FENCE_OWN/probe" 2>/dev/null; then
  echo "own-write-delete-ok" >> "$FENCE_RESULTS"
else
  echo "own-write-delete-failed" >> "$FENCE_RESULTS"
fi
rm -rf "$FENCE_SIBLING"
"""


class Scratch:
    """A synthetic run: one scratch root holding the run's own and a sibling's."""

    def __init__(self, root: Path) -> None:
        self.base = root
        self.scratch_root = root / "reckon-crew-scratch"
        self.own = self.scratch_root / RUN_ID
        self.sibling = self.scratch_root / SIBLING_ID
        self.own.mkdir(parents=True)
        self.sibling.mkdir(parents=True)
        (self.sibling / "keep.txt").write_text("sibling\n")
        self.home = root / "home"
        self.run = self.home / ".config" / "reckon" / "crew" / "runs" / RUN_ID
        self.run.mkdir(parents=True)
        self.manifest = self.run / "manifest.md"
        self.results = self.run / "results.txt"
        self.stub = root / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)

    def point_the_scratch_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.scratch_root))

    def argv(self) -> list[str]:
        """Compose the fence the way a dispatch does, through the production path."""
        return _worker_fence.fence_argv(
            [str(self.stub)],
            writable_directories=[str(self.own)],
            manifest_path=str(self.manifest),
            home=str(self.home),
        )

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
            cwd=str(self.base),
            capture_output=True,
            text=True,
            check=False,
        )

    def results_text(self) -> str:
        return self.results.read_text()


def _bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the writable ``--bind`` of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    """Return the index of the read-only overlay of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == "--ro-bind" and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def test_the_scratch_root_is_sealed_before_the_own_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root is overlaid read-only, and the own scratch is granted after it."""
    scratch = Scratch(tmp_path)
    scratch.point_the_scratch_root(monkeypatch)
    argv = scratch.argv()
    root_overlay = _ro_bind_of(argv, scratch.scratch_root)
    own_bind = _bind_of(argv, scratch.own)
    sibling_bind = _bind_of(argv, scratch.sibling)
    # Positive control: the root is actually overlaid, so an absent overlay
    # would mean the assertions below are testing nothing rather than passing.
    assert root_overlay != -1, "the scratch root was not overlaid read-only"
    assert own_bind != -1, "the run's own scratch was not bound writable"
    assert own_bind > root_overlay, (
        "the own-scratch grant precedes the root overlay, so the read-only bind "
        "would shadow it"
    )
    # No writable grant reaches a sibling run's scratch.
    assert sibling_bind == -1, "a sibling's scratch was bound writable"


def test_the_run_scratch_reaches_the_fence_as_a_write_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own scratch is among the roots the fence re-binds writable."""
    scratch = Scratch(tmp_path)
    scratch.point_the_scratch_root(monkeypatch)
    roots = {
        Path(root).resolve()
        for root in dispatch_admission._fence_write_roots(
            backend={"launch": "cli", "command": "true", "sandbox": "worktree-full"},
            repository=str(tmp_path / "repo"),
            run_directory=str(scratch.run),
            manifest_path=str(scratch.manifest),
            worktree=None,
            declared_write_paths=(),
        )
    }
    assert scratch.own.resolve() in roots, (
        f"{scratch.own} is not among the fence write roots {sorted(map(str, roots))}"
    )


@pytest.fixture
def declared_seal_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative control when the env var asks for it.

    The mutation removes the scratch root from the sealed set exactly as the
    control string states: the seam that decides which scratch root is sealed
    returns None, so no read-only overlay of the root is composed.
    """
    if not NEGATIVE_CONTROL:
        return

    def no_seal(writable_roots: Iterable[str | Path]) -> Path | None:
        return None

    monkeypatch.setattr(_worker_fence, "worker_scratch_seal_root", no_seal)


@requires_bwrap
def test_a_fenced_run_cannot_remove_a_siblings_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_seal_mutation: None,
) -> None:
    """Case 1: the sibling removal is refused read-only; the own write lands."""
    scratch = Scratch(tmp_path)
    scratch.point_the_scratch_root(monkeypatch)
    proc = scratch.run_fence(scratch.argv())
    results = scratch.results_text()
    # Positive control: the stub only writes its record from inside the fence, so
    # a non-empty record proves the shim ran and the own scratch was writable.
    assert "own-write-delete-ok" in results, results
    assert not (scratch.own / "probe").exists()
    assert proc.returncode != 0, (
        f"the fenced rm of a sibling's scratch exited {proc.returncode}, not "
        f"non-zero; stderr: {proc.stderr!r}"
    )
    assert scratch.sibling.is_dir(), "the sibling scratch was removed"
    assert (scratch.sibling / "keep.txt").exists()
