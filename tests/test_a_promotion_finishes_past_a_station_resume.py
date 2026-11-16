"""A promotion finishes cleanup past a launch that took the pointer since it began.

A promotion writes its ledger row, commits, and only then retires the live
pointer. A station resume can start the same run again inside that window, so
the cleanup meets a pointer that names a newer attempt and cannot remove the
attempt it read.

When that newer attempt has recorded its own end, the run is over either way
and the promotion has no landing left to defer: it finishes the cleanup its own
attempt started and the pointer goes. When the newer attempt is still running,
the promotion must not delete the file a live process owns, and its refusal has
to name the attempt and the process that launched it so a coordinator can find
the work instead of stopping and discarding the run by hand.

A temporary repository with a bare origin stands in for the project, a temporary
crew home for the crew state, and the real pointer directory is asserted
untouched.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path, read_pointer, run_dir
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

RUN_ID = "r-20261112T175326514846-a-promotion-finishes-past-a-resume"


@pytest.fixture()
def promoted_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A finished run whose worktree holds one committed change, ready to promote."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The crew home the run uses is the temporary one, so no pointer this test
    # writes can reach the operator's own live directory.
    assert pointer_path(RUN_ID).is_relative_to(tmp_path), pointer_path(RUN_ID)
    _origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    worktree = tmp_path / "worktrees" / "resumed-during-promotion"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text(
        "the reviewed change survives the promotion\n", encoding="utf-8"
    )
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "attempt": 3,
            "phase": "delivered",
            "created_at": "2026-11-12T17:53:26Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s7",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "repository": repository,
        "worktree": worktree,
        "run_id": RUN_ID,
        "head": head,
        "gate_log": gate_log,
    }


def _end_attempt(run_id: str, attempt: int) -> None:
    """Write the attempt's own end record beside the run, as its launch does."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"attempt-{attempt}-exit.json").write_text(
        json.dumps({"run_id": run_id, "attempt": attempt, "exit_status": 0}),
        encoding="utf-8",
    )


def _reserve_attempt(
    run_id: str, attempt: int, *, supervisor_pid: int, launcher_pid: int
) -> None:
    """Write the launch reservation a live attempt leaves beside its run."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"attempt-{attempt}.reservation").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "attempt": attempt,
                "kind": "resume",
                "pid": supervisor_pid,
                "supervisor_pid": supervisor_pid,
                "launcher_pid": launcher_pid,
                "launcher_host": "test-host",
                "at": "2026-11-12T18:20:00Z",
            }
        ),
        encoding="utf-8",
    )


def _move_the_pointer_at_cleanup(
    run: dict[str, Any],
    record_the_launch: Callable[[int], None],
    monkeypatch: pytest.MonkeyPatch,
) -> int:
    """Record a new attempt on the pointer after the landing writes commit.

    The commit is the stage between the ledger row and the pointer cleanup, so a
    launch recorded here lands before the removal, exactly where the station's
    resume arrived. The return is the attempt the newer launch took.
    """
    real_commit = promotion._commit_landing_writes
    attempt = int(read_pointer(run["run_id"]).get("attempt") or 1) + 1

    def commit_then_launch(*args: Any, **kwargs: Any) -> Any:
        written = real_commit(*args, **kwargs)
        current = read_pointer(run["run_id"])
        current["attempt"] = attempt
        current["phase"] = "running"
        _write_json(pointer_path(run["run_id"]), current)
        record_the_launch(attempt)
        return written

    monkeypatch.setattr(promotion, "_commit_landing_writes", commit_then_launch)
    return attempt


def test_a_launch_that_ended_lets_the_promotion_finish_its_cleanup(
    promoted_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = promoted_run

    def ended(attempt: int) -> None:
        _end_attempt(run["run_id"], attempt)

    attempt = _move_the_pointer_at_cleanup(run, ended, monkeypatch)

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    assert not pointer_path(run["run_id"]).exists(), "the pointer stayed held"
    assert (run_dir(run["run_id"]) / f"attempt-{attempt}-exit.json").is_file(), (
        "the ended attempt's own record was taken with the pointer"
    )


def test_a_running_launch_refuses_naming_its_attempt_and_launcher(
    promoted_run: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = promoted_run
    supervisor = subprocess.Popen(["sleep", "120"])
    launcher = subprocess.Popen(["sleep", "120"])
    try:

        def running(attempt: int) -> None:
            _reserve_attempt(
                run["run_id"],
                attempt,
                supervisor_pid=supervisor.pid,
                launcher_pid=launcher.pid,
            )

        attempt = _move_the_pointer_at_cleanup(run, running, monkeypatch)

        result = _complete_land(run)

        assert result.exit_code != 0, result.output
        assert "changed while promotion ran" in result.output, result.output
        assert f"attempt {attempt}" in result.output, result.output
        assert f"pid {launcher.pid}" in result.output, result.output
        assert pointer_path(run["run_id"]).exists(), "a live launch lost its pointer"
    finally:
        for process in (supervisor, launcher):
            process.kill()
            process.wait()
