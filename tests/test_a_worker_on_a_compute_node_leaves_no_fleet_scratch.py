"""A worker on a compute node leaves no scratch on the node that dispatched it.

A dispatch composes the launch on the fleet node and places the worker as a
scheduler step, which runs it on the compute node the allocation is held on.
The run's own scratch directory is the one path the launch owns beneath a
node's ``/tmp``, and it belongs to the node that runs the worker: created on
the composing host it is a directory for a worker that never runs there, and
created nowhere it is a ``TMPDIR`` that does not exist when the fence binds it.
So the composition creates every handed root except that one, and a placed
step is told to make it on its own node before the fence is exec'd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_launch

RUN_ID = "r-20261115T201500000000-place-owner"

# The node's own tmp, the root the fence seals and mounts the run's scratch
# over. Written as a literal because the argv under test is the fence's.
NODE_TMP = "/tmp"  # noqa: S108 - the node's own tmp, never shared


@pytest.fixture
def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic host scratch root, so nothing here touches the host's ``/tmp``."""
    root = tmp_path / "host-scratch"
    monkeypatch.setenv(dispatch_launch.WORKER_SCRATCH_ROOT_ENV, str(root))
    return root


def _composed_launch(tmp_path: Path, manifest: Path) -> list[str]:
    """Compose a fenced launch the way a dispatch does, with the run as its own."""
    home = tmp_path / "home"
    sealed = home / ".config" / "reckon"
    reports = sealed / "reports"
    reviews = sealed / "reviews"
    reports.mkdir(parents=True)
    reviews.mkdir(parents=True)
    (home / "Code").mkdir(parents=True)
    worktree = home / "Code" / "tree"
    worktree.mkdir()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    roots = _backends.fenced_write_roots(
        {"launch": "cli", "sandbox": _backends.WORKTREE_FULL},
        repository=worktree,
        run_directory=manifest.parent,
        reports_directory=reports,
        review_store_directory=reviews,
        manifest_path=manifest,
        worktree=worktree,
    )
    return list(
        _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": "true",
                "dialect": "claude",
                "sandbox": _backends.WORKTREE_FULL,
            },
            prompt="p",
            worktree=str(worktree),
            manifest_path=str(manifest),
            writable_directories=(
                *roots,
                dispatch_launch.worker_scratch_dir(RUN_ID),
            ),
            fence=True,
            fence_home=str(home),
        ).argv
    )


def _step_argv(scratch: Path) -> list[str]:
    """A placed step's argv: the fence's node-tmp mount of the run's scratch."""
    return [
        "srun",
        "--overlap",
        "--jobid=4242",
        "/usr/bin/bwrap",
        "--dev-bind",
        "/",
        "/",
        "--ro-bind",
        NODE_TMP,
        NODE_TMP,
        "--bind",
        str(scratch),
        str(scratch),
        "--",
        "/bin/true",
    ]


def test_composing_a_launch_creates_no_scratch_for_a_worker_elsewhere(
    tmp_path: Path, scratch_root: Path
) -> None:
    """The run's scratch directory is not invented on the composing host."""
    manifest = tmp_path / "crew" / "runs" / RUN_ID / "manifest.md"
    argv = _composed_launch(tmp_path, manifest)

    owned = dispatch_launch.worker_scratch_dir(RUN_ID)
    assert owned.parent == scratch_root
    assert not owned.exists(), f"{owned} was created on the composing host"
    # Created nowhere would be just as wrong: the argv still grants it, and the
    # node that runs the worker is what makes it before the fence binds it.
    grants = [
        (argv[index + 1], argv[index + 2])
        for index, word in enumerate(argv)
        if word == "--bind"
    ]
    assert (str(owned), str(owned)) in grants


def test_a_placed_step_is_told_to_make_its_scratch_on_its_own_node() -> None:
    """The run's scratch reaches the step's wrapper even though it is absent here."""
    owned = dispatch_launch.worker_scratch_dir(RUN_ID)
    argv = _step_argv(owned)

    assert not owned.exists()
    named = dispatch_launch.step_node_directories(argv, expected=(owned,))
    assert str(owned) in named
    # Named by construction, not by observation: read only from this host's
    # disk the scratch directory is absent, so nothing would name it.
    assert dispatch_launch.step_node_directories(argv) == []


def test_a_worker_launched_as_the_launcher_child_still_gets_its_scratch(
    tmp_path: Path, scratch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is placement, not the run: an unplaced worker's dir is made here."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    environment = dispatch_launch._worker_runtime_environment(
        {},
        run_id=RUN_ID,
        manifest_path=str(tmp_path / "manifest.md"),
        attempt_started_at="2026-11-15T20:15:00Z",
        coordinator_session="s",
        claude_headers=False,
        placed_step=False,
    )
    owned = dispatch_launch.worker_scratch_dir(RUN_ID)
    assert environment["TMPDIR"] == str(owned)
    assert owned.is_dir()
