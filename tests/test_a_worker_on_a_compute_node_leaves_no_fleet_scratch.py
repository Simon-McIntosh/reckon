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

import os
import subprocess
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import dispatch_launch, process_signal

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


def test_a_placed_step_is_told_to_make_its_scratch_on_its_own_node(
    tmp_path: Path, scratch_root: Path
) -> None:
    """The launch's own composition names the scratch, and the wrapper makes it.

    Driven through ``step_launch_environment`` — the call a launch makes — so
    the wiring that passes the run's scratch to ``step_node_directories`` is
    what is under test, not a value the test hands to itself.
    """
    owned = dispatch_launch.worker_scratch_dir(RUN_ID)
    run_directory = tmp_path / "crew" / "runs" / RUN_ID
    run_directory.mkdir(parents=True)
    environment = dispatch_launch.step_launch_environment(
        {"TMPDIR": str(owned)},
        _step_argv(owned),
        run_directory=run_directory,
    )

    assert owned.parent == scratch_root
    assert not owned.exists()
    assert str(owned) in environment[dispatch_launch.STEP_NODE_DIRS_ENV].split(
        os.pathsep
    )
    assert environment["RECKON_STEP_RECORD"] == str(run_directory / "step-task.txt")

    # The wrapper the step runs is what creates it, so run the composed value
    # and read the directory it leaves behind: the effect a source-only
    # assertion cannot show.
    completed = subprocess.run(
        [
            "/bin/sh",
            "-c",
            dispatch_launch._STEP_TASK_WRAPPER,
            "sh",
            "/bin/sh",
            "-c",
            'test -d "$1" && echo "made"',
            "sh",
            str(owned),
        ],
        env={**os.environ, **environment},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "made" in completed.stdout, completed.stderr
    assert owned.is_dir()


def test_the_step_question_has_one_owner(tmp_path: Path) -> None:
    """Dispatch asks ``process_signal`` whether an argv is a step, and has
    no second copy to fall out of step with it.

    The signal path asks the same question of the argv a run recorded, so the
    predicate lives there. A copy beside the composition would answer by its own
    rule after the owner changed, and nothing else compares the two: this reads
    the object dispatch reaches and the wording it decides on.
    """
    assert dispatch_launch.argv_uses_step is process_signal.argv_uses_step

    run_directory = tmp_path / "crew" / "runs" / RUN_ID
    run_directory.mkdir(parents=True)
    environment = {"TMPDIR": str(dispatch_launch.worker_scratch_dir(RUN_ID))}

    assert (
        dispatch_launch.step_launch_environment(
            environment, ["srun", "--overlap"], run_directory=run_directory
        )
        == environment
    )
    staged = dispatch_launch.step_launch_environment(
        environment, ["srun", "--overlap", "--jobid=4242"], run_directory=run_directory
    )
    assert "RECKON_STEP_RECORD" in staged


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
